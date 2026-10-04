"""Image choices and validation shared by the wizard and lifecycle service."""

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from urllib.parse import urlsplit

from .deployment import DeploymentError, VM, SIZES, vm_name
from .connectivity import port_forwards


from . import image_catalog, bundles, settings


@dataclass(frozen=True)
class Machine:
    vm: VM
    source_kind: str
    source: str
    sha256: str = ""
    hostname: str = ""
    packages: tuple[str, ...] = ()
    desktop: str = "none"
    software_bundles: tuple[str, ...] = ()
    bundle_packages: tuple[str, ...] = ()
    guest_family: str = "linux"
    refresh_source: bool = False
    port_forwards: tuple = ()
    sha512: str = ""
    password_login: bool = False
    users: tuple = ()

    @property
    def checksum(self):
        return self.sha256 or self.sha512

    @property
    def cloud_setup(self):
        return self.source_kind in ("cloud", "golden")

    @property
    def effective_packages(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.packages, *self.bundle_packages)))

    @classmethod
    def from_dict(cls, data: dict, *, images=None) -> "Machine":
        if not isinstance(data, dict):
            raise DeploymentError("VM configuration must be an object.")
        allowed = {"name", "os", "size", "cpu", "memory_mb", "disk_gb", "network", "bridge",
                   "source_kind", "source", "sha256", "sha512", "ssh_user", "ssh_public_keys", "hostname", "packages", "desktop", "install_default_packages", "bridges", "port_forwards", "directory_shares", "software_bundles", "bundle_packages", "guest_family", "refresh_source", "password_login", "users"}
        if set(data) - allowed:
            raise DeploymentError("Unknown configuration fields: " + ", ".join(sorted(set(data) - allowed)))
        guest = data.get("os", "ubuntu")
        if not isinstance(guest, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", guest) or guest == "windows":
            raise DeploymentError("Choose a Linux or macOS platform. Windows is excluded.")
        defaults = (images if images is not None else image_catalog.load()).get(guest, {"family": "linux", "source_kind": "cloud", "source": "", "sha256": "", "ssh_user": ""})
        family = data.get("guest_family", defaults["family"])
        if family not in ("linux", "macos"):
            raise DeploymentError("Guest family must be linux or macos.")
        source = data.get("source", defaults.get("source", ""))
        version = next((v for v in defaults.get("versions", []) if isinstance(source, str) and v["source"] == source.strip()), {})
        kind = data.get("source_kind", version.get("source_kind", defaults["source_kind"]))
        hash_defaults = version if version.get("sha256") or version.get("sha512") else defaults if isinstance(source, str) and source.strip() == defaults.get("source", "") else {}
        sha512 = data.get("sha512", hash_defaults.get("sha512", "") if "sha256" not in data else "")
        checksum = data.get("sha256", hash_defaults.get("sha256", "") if not sha512 else "")
        if kind not in ("tart", "iso", "cloud", "golden") or not isinstance(source, str) or not source.strip():
            raise DeploymentError("Choose a Tart image, ARM64 cloud disk or installer ISO.")
        source = source.strip()
        if family == "macos" and kind not in ("tart",):
            raise DeploymentError("macOS provisioning uses a ready-made Tart template.")
        if kind == "golden":
            vm_name(source)
            checksum = ""
            sha512 = ""
        if not isinstance(checksum, str) or (checksum and not re.fullmatch(r"[a-fA-F0-9]{64}", checksum)):
            raise DeploymentError("SHA256 must contain exactly 64 hexadecimal characters.")
        if not isinstance(sha512, str) or (sha512 and not re.fullmatch(r"[a-fA-F0-9]{128}", sha512)):
            raise DeploymentError("SHA512 must contain exactly 128 hexadecimal characters.")
        if checksum and sha512:
            raise DeploymentError("Supply either SHA256 or SHA512, rather than both.")
        if kind in ("iso", "cloud"):
            if "://" in source:
                parsed = urlsplit(source)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise DeploymentError("Remote image downloads require an HTTPS URL without credentials.")
            else:
                path = Path(source).expanduser().resolve()
                suffixes = (".iso",) if kind == "iso" else (".tar.xz", ".qcow2", ".qcow2.xz", ".raw", ".img")
                if not str(path).lower().endswith(suffixes) or ":" in str(path):
                    raise DeploymentError("Select an .iso installer or a .tar.xz, .qcow2, .qcow2.xz, .raw or .img cloud disk with no colon in its path.")
                source = str(path)
            if "amd64" in source.lower() or "x86_64" in source.lower():
                raise DeploymentError("Choose an ARM64 image for Apple Silicon.")
        fields = {key: value for key, value in data.items() if key not in {"source_kind", "source", "sha256", "sha512", "hostname", "packages", "desktop", "install_default_packages", "port_forwards", "software_bundles", "bundle_packages", "guest_family", "refresh_source", "password_login", "users"}}
        fields.setdefault("os", guest)
        fields.setdefault("size", "small")
        fields.setdefault("ssh_user", defaults.get("ssh_user") or "vmadmin")
        minimum = version.get("minimum_disk_gb", defaults.get("minimum_disk_gb", 1)) if source == defaults.get("source") or version else 1
        if isinstance(fields["size"], str) and fields["size"] in SIZES:
            disk = fields.get("disk_gb", SIZES[fields["size"]]["disk_gb"])
            if type(disk) is int:
                fields["disk_gb"] = max(disk, minimum)
        fields["image"] = source if kind == "tart" else "installer-template"
        vm = VM.from_dict(fields)
        if vm.disk_gb is None:
            raise DeploymentError("A lifecycle VM must specify a disk size or size preset.")
        hostname = data.get("hostname", "")
        packages = data.get("packages", [])
        desktop = data.get("desktop", "none")
        selected = data.get("software_bundles", [])
        if not isinstance(selected, list) or len(selected) > 100 or not all(isinstance(v, str) for v in selected):
            raise DeploymentError("Select software bundles by their identifiers.")
        frozen = bundles.packages(data.get("bundle_packages", []))
        legacy = data.get("install_default_packages", False)
        if type(legacy) is not bool:
            raise DeploymentError("The legacy application option must be true or false.")
        if legacy:
            # Preserve saved recipes; new builds have no implicit package selection.
            frozen = list(dict.fromkeys((*bundles.legacy_packages(), *frozen)))
            selected = list(dict.fromkeys(("kali-essentials", *selected)))
        packages = bundles.packages(packages)
        if type(data.get("refresh_source", False)) is not bool:
            raise DeploymentError("Refresh source must be true or false.")
        if kind in ("cloud", "golden"):
            hostname = hostname or re.sub(r"[^a-z0-9-]", "-", vm.name.lower())[:63].strip("-")
            if not isinstance(hostname, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
                raise DeploymentError("Cloud hostname must be a lowercase DNS label of up to 63 characters.")
            if vm.ssh_user == "root":
                raise DeploymentError("Cloud setup requires a non-root guest username.")
            if desktop != "none":
                raise DeploymentError("Linux cloud builds are headless. desktop must be none.")
        elif hostname or desktop != "none" or (kind == "iso" and (packages or frozen or selected)):
            raise DeploymentError("Hostname and desktop setup require cloud-init; software provisioning requires a cloud or Tart image.")
        forwards = port_forwards(data.get("port_forwards", []), vm.network)
        from . import users
        extra = data.get("users", [])
        if not isinstance(extra, list):
            raise DeploymentError("Additional users must be a list.")
        extra = users.validate(extra, recipe=True) if extra else []
        if any(user["username"] == vm.ssh_user for user in extra):
            raise DeploymentError("The primary guest account cannot also appear in Additional Users.")
        login = data.get("password_login", False)
        if type(login) is not bool or (login and vm.ssh_user == "root"):
            raise DeploymentError("Password login requires a non-root guest account.")
        return cls(vm=vm, source_kind=kind, source=source, sha256=checksum.lower(), hostname=hostname, packages=tuple(packages), desktop=desktop, software_bundles=tuple(dict.fromkeys(selected)), bundle_packages=tuple(frozen), guest_family=family, refresh_source=data.get("refresh_source", False), port_forwards=forwards, sha512=sha512.lower(), password_login=login, users=tuple(extra))

    def config(self) -> dict:
        data = asdict(self.vm)
        data.pop("image")
        data["ssh_public_keys"] = [str(path) for path in self.vm.ssh_public_keys]
        data["bridges"] = list(self.vm.bridges)
        data["directory_shares"] = [share.config() for share in self.vm.directory_shares]
        data["port_forwards"] = [rule.config() for rule in self.port_forwards]
        data.update(source_kind=self.source_kind, source=self.source, sha256=self.sha256)
        if self.sha512:
            data["sha512"] = self.sha512
        data.update(guest_family=self.guest_family, software_bundles=list(self.software_bundles), bundle_packages=list(self.bundle_packages), packages=list(self.packages), refresh_source=self.refresh_source)
        if self.cloud_setup:
            data.update(hostname=self.hostname, desktop=self.desktop)
        if self.password_login:
            data["password_login"] = True
        if self.users:
            data["users"] = list(self.users)
        return data


def choices(root=None) -> dict:
    from .software import LINUX_BUILD_NOTICE
    preferences = settings.load(root) if root else {}
    return {"images": image_catalog.load(root), "sizes": SIZES,
            "default_public_key": preferences.get("default_public_key", "~/.ssh/id_ed25519.pub"),
            "install_ssh_key_by_default": preferences.get("install_ssh_key_by_default", True),
            "bundles": bundles.listing(root) if root else [],
            "linux_build_notice": LINUX_BUILD_NOTICE,
            "software_notice": "Installing software can take 5–15 minutes or longer. Cloud setup allows 30 minutes for completion."}
