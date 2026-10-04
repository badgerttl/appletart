"""Validate deployment intent and generate commands without touching the host."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


class DeploymentError(Exception):
    """A deployment could not be validated or completed."""


SIZES = {
    "small": {"cpu": 2, "memory_mb": 4096, "disk_gb": 40},
    "medium": {"cpu": 4, "memory_mb": 8192, "disk_gb": 80},
    "large": {"cpu": 8, "memory_mb": 16384, "disk_gb": 160},
}


def vm_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise DeploymentError("VM names must be 1–64 letters, digits, dots, underscores or hyphens, starting with a letter or digit.")
    return value


def integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise DeploymentError(f"{field} must be an integer between {minimum} and {maximum}.")
    return value


@dataclass(frozen=True)
class VM:
    name: str
    os: str
    image: str
    cpu: int
    memory_mb: int
    disk_gb: int | None
    ssh_public_keys: tuple[Path, ...] = ()
    ssh_user: str = "admin"
    network: str = "nat"
    bridge: str = ""
    bridges: tuple[str, ...] = ()
    directory_shares: tuple = ()

    @classmethod
    def from_dict(cls, data: dict, base_dir: Path | None = None) -> "VM":
        allowed = {"name", "os", "image", "size", "cpu", "memory_mb", "disk_gb", "ssh_public_keys", "ssh_user", "network", "bridge", "bridges", "directory_shares"}
        unknown = set(data) - allowed
        if unknown:
            raise DeploymentError(f"Unknown VM fields: {', '.join(sorted(unknown))}")
        name = vm_name(data.get("name"))
        guest = data.get("os")
        if guest == "windows":
            raise DeploymentError("Windows guests are not supported by Tart. A separate backend is required.")
        if not isinstance(guest, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", guest):
            raise DeploymentError(f"{name}: os must be a platform identifier.")
        from .image_catalog import defaults as image_defaults
        definition = image_defaults(guest)
        image = data.get("image", definition.get("source") if definition["source_kind"] == "tart" else "")
        known_template = image == definition.get("source") or any(v["source"] == image for v in definition.get("versions", []))
        default_user = definition.get("ssh_user", "") if known_template else ""
        if not isinstance(image, str) or not image or image.startswith("-") or any(c.isspace() or not c.isprintable() for c in image):
            raise DeploymentError(f"{name}: image must be a local Tart template or a Tart OCI image. Provide an explicit image for a custom platform.")
        if image == name:
            raise DeploymentError(f"{name}: image and destination must be different.")
        size = data.get("size", "small")
        if not isinstance(size, str) or size not in SIZES:
            raise DeploymentError("size must be small, medium or large.")
        defaults = SIZES[size]
        cpu = integer(data.get("cpu", defaults["cpu"]), "cpu", 1, 65535)
        memory = integer(data.get("memory_mb", defaults["memory_mb"]), "memory_mb", 512, 1048576)
        disk_value = data.get("disk_gb", defaults["disk_gb"] if "size" in data else None)
        disk = integer(disk_value, "disk_gb", 1, 65535) if disk_value is not None else None
        keys = data.get("ssh_public_keys", [])
        if not isinstance(keys, list) or not all(isinstance(key, str) and key for key in keys):
            raise DeploymentError(f"{name}: ssh_public_keys must be a list of public key file paths.")
        paths = []
        for key in keys:
            path = Path(key).expanduser()
            paths.append(path if path.is_absolute() else (base_dir or Path.cwd()) / path)
        if keys and "ssh_user" not in data and not default_user:
            raise DeploymentError(f"{name}: specify the existing template username.")
        user = data.get("ssh_user", default_user or "vmadmin")
        if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", user):
            raise DeploymentError(f"{name}: ssh_user must be a valid guest username.")
        network = data.get("network", "nat")
        bridge = data.get("bridge", "")
        if network not in ("nat", "bridged"):
            raise DeploymentError("network must be nat or bridged.")
        if not isinstance(bridge, str) or (bridge and not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", bridge)):
            raise DeploymentError("bridge must be a host interface name such as en0.")
        bridges = data.get("bridges", [bridge] if bridge else [])
        if not isinstance(bridges, list) or len(bridges) > 8 or not all(isinstance(b, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", b) for b in bridges):
            raise DeploymentError("bridges must list up to eight host interface names.")
        if len(set(bridges)) != len(bridges):
            raise DeploymentError("Choose each host bridge interface only once.")
        if bridge and (not bridges or bridge != bridges[0]):
            raise DeploymentError("bridge must match the first entry in bridges.")
        bridge = bridges[0] if bridges else ""
        if network == "bridged" and not bridge:
            raise DeploymentError("Bridged networking requires a host interface.")
        if network == "nat" and bridge:
            raise DeploymentError("NAT networking must not specify a bridge.")
        from .connectivity import directory_shares
        shares = directory_shares(data.get("directory_shares", []))
        return cls(name, guest, image, cpu, memory, disk, tuple(paths), user, network, bridge, tuple(bridges), shares)

    def commands(self) -> list[list[str]]:
        configure = ["set", self.name, "--cpu", str(self.cpu), "--memory", str(self.memory_mb), "--random-mac"]
        if self.disk_gb is not None:
            configure += ["--disk-size", str(self.disk_gb)]
        return [["clone", self.image, self.name], configure]

    def run_args(self, *, headless: bool = False) -> list[str]:
        args = ["run", self.name]
        if headless:
            args.append("--no-graphics")
        if self.network == "bridged":
            for bridge in self.bridges or (self.bridge,):
                args += ["--net-bridged", bridge]
        for index, share in enumerate(self.directory_shares):
            args += ["--dir", share.tart_argument(index)]
        return args

    def ip_args(self, wait: int = 0) -> list[str]:
        return ["ip", self.name, "--wait", str(wait), "--resolver", "arp" if self.network == "bridged" else "dhcp"]


def load_manifest(path: Path) -> list[VM]:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DeploymentError(f"Cannot read {path}: {error}") from error
    if set(data) != {"version", "vms"} or type(data["version"]) is not int or data["version"] != 1:
        raise DeploymentError("Manifest must contain version = 1 and [[vms]] entries only.")
    if not isinstance(data["vms"], list) or not data["vms"] or not all(isinstance(item, dict) for item in data["vms"]):
        raise DeploymentError("Manifest requires at least one [[vms]] entry.")
    vms = [VM.from_dict(item, path.resolve().parent) for item in data["vms"]]
    names = [vm.name for vm in vms]
    if len(names) != len(set(names)):
        raise DeploymentError("Each destination VM name must be unique.")
    if any(vm.image in names for vm in vms):
        raise DeploymentError("Templates must exist before deployment; they cannot be destinations in this manifest.")
    return vms
