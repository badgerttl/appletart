"""Persistent lifecycle operations behind the browser wizard."""

from datetime import datetime, timezone
from contextlib import contextmanager, ExitStack
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import replace

from .catalog import Machine
from .deployment import DeploymentError, vm_name
from .downloads import fetch_iso, fetch_image
from .cloud import CLOUD_CONFIG_VERSION, check_cloud_tools, convert_disk, disk_format, bootstrap_key, write_seed, provision_cloud, prepare_linux_golden, capture_guest
from . import bundles, image_catalog
from .ssh import check_ssh_tools, host_key_file, provision_keys, read_public_keys, ssh_options
from .tart import Tart
from . import forwarder
from . import guest_agent
from . import guest_access, profiles, storage, settings, users, ssh_config
from . import diagnostics
from .recovery import Recovery
from .ip_resolver import address as guest_address, resolve as resolve_ip
from .operations import JobCancelled, checkpoint, current, pause, resource_lock, run, stop_build, stream, uncancellable


class Store:
    def __init__(self, root: Path, folder="machines"):
        self.root = root.expanduser().resolve()
        self.records = self.root / folder
        self.records.mkdir(parents=True, exist_ok=True, mode=0o700)

    def get(self, name: str) -> dict | None:
        path = self.records / f"{vm_name(name)}.json"
        try:
            record = json.loads(path.read_text())
            if record:
                for field in ("seed", "artifact"):
                    value = record.get(field, "")
                    if value and not Path(value).is_absolute() and Path(value).parts[0] == self.root.name:
                        record[field] = str((self.root.parent / value).resolve())
            if record and record["config"].get("source_kind") in ("cloud", "golden"):
                # Records saved before the application preset preserve their
                # original package selection when reopened or resumed.
                legacy = record["config"].pop("install_default_packages", False)
                if legacy:
                    record["config"]["software_bundles"] = list(dict.fromkeys(["kali-essentials", *record["config"].get("software_bundles", [])]))
                    record["config"]["bundle_packages"] = list(dict.fromkeys([*bundles.legacy_packages(), *record["config"].get("bundle_packages", [])]))
                record["config"]["desktop"] = "none"
            return record
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise DeploymentError(f"Cannot read saved configuration for {name}.") from error

    def put(self, record: dict) -> None:
        name = vm_name(record["config"]["name"])
        temp = self.records / f".{uuid.uuid4()}.tmp"
        try:
            temp.write_text(json.dumps(record, indent=2))
            temp.chmod(0o600)
            temp.replace(self.records / f"{name}.json")
        finally:
            temp.unlink(missing_ok=True)

    def all(self) -> list[dict]:
        return [record for path in sorted(self.records.glob("*.json"))
                if (record := self.get(path.stem)) is not None]

    def remove(self, name: str) -> None:
        (self.records / f"{vm_name(name)}.json").unlink(missing_ok=True)


class Backend(Tart):
    def __init__(self, report=print):
        super().__init__()
        self.report = report

    def identity(self, name: str) -> dict:
        home = Path(os.environ.get("TART_HOME", str(Path.home() / ".tart"))).expanduser().resolve()
        try:
            stat = (home / "vms" / vm_name(name)).stat()
        except OSError as error:
            raise DeploymentError("Cannot verify the VM directory identity.") from error
        return {"home": str(home), "device": stat.st_dev, "inode": stat.st_ino,
                "birth": getattr(stat, "st_birthtime", None)}

    def mac_address(self, name: str) -> str:
        import re
        path = Path(self.identity(name)["home"]) / "vms" / name / "config.json"
        try:
            mac = json.loads(path.read_text())["macAddress"]
            if not isinstance(mac, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
                raise ValueError("invalid MAC")
            return mac
        except (OSError, ValueError, KeyError) as error:
            raise DeploymentError("Cannot read the Tart network MAC for cloud-init.") from error

    def disk_capacity_gb(self, name: str) -> int:
        path = Path(self.identity(name)["home"]) / "vms" / vm_name(name) / "disk.img"
        try:
            size = path.stat().st_size
            return (size + 1000**3 - 1) // 1000**3
        except OSError as error:
            raise DeploymentError("Cannot read the cloned template's disk capacity.") from error

    def disk_format(self, name: str) -> str:
        return disk_format(Path(self.identity(name)["home"]) / "vms" / vm_name(name) / "disk.img")

    def run(self, args: list[str], *, capture: bool = False, timeout=None) -> str:
        if args[0] in {"clone", "create", "set", "delete"} and current() is not None:
            with uncancellable():
                return self.run(args, capture=capture, timeout=timeout)
        if capture:
            return super().run(args, capture=True, timeout=timeout)
        self.report(f"Tart {args[0]}…")
        diagnostics.command_start([self.binary, *args])
        try:
            if current() is not None:
                return stream([self.binary, *args], self.report, env={**os.environ, "TART_NO_AUTO_PRUNE": "1"})
            with subprocess.Popen([self.binary, *args], text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  env={**os.environ, "TART_NO_AUTO_PRUNE": "1"}) as process:
                tail = []
                for line in process.stdout:
                    text = line.rstrip()
                    if text:
                        self.report(text)
                        tail = (tail + [text])[-8:]
                code = process.wait()
                if journal := diagnostics.current():
                    journal.write("exit", f"Tart {args[0]}: code={code}")
                if code != 0:
                    raise DeploymentError(f"Tart {args[0]} failed: " + "\n".join(tail))
        except OSError as error:
            raise DeploymentError(f"Cannot run Tart: {error}") from error
        return ""


def exclusive(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        subject = args[0] if args else kwargs.get("machine", kwargs.get("name"))
        name = subject.vm.name if isinstance(subject, Machine) else vm_name(subject)
        resources = ["vm-" + name]
        if method.__name__ == "create_golden":
            resources.append("image-" + vm_name(args[1] if len(args) > 1 else kwargs["image_name"]))
        with self.operation(*resources):
            checkpoint()
            return method(self, *args, **kwargs)
    return wrapped


class Lifecycle:
    def machine(self, config):
        if not isinstance(config, dict):
            raise DeploymentError("VM configuration must be an object.")
        config = dict(config)
        images = image_catalog.load(self.store.root)
        definition = images.get(config.get("os", "ubuntu"), {})
        config.setdefault("guest_family", definition.get("family", "linux"))
        selected = config.get("software_bundles", [])
        if selected and "bundle_packages" not in config:
            config["bundle_packages"] = bundles.resolve(self.store.root, selected, config.get("os", "ubuntu"), config["guest_family"])
        return Machine.from_dict(config, images=images)

    def save_bundle(self, value):
        with self.operation("settings"):
            return bundles.save(self.store.root, value)

    def delete_bundle(self, identifier):
        with self.operation("settings"):
            references = sorted(profile["config"]["name"] for profile in self.profiles.all()
                                if identifier in profile["config"].get("software_bundles", []))
            if references:
                raise DeploymentError("Update or delete these deployment profiles before removing the software bundle: " + ", ".join(references))
            bundles.remove(self.store.root, identifier)

    def save_catalog(self, value):
        with self.operation("settings"):
            return image_catalog.save(self.store.root, value)

    def __init__(self, root: Path, backend_factory=Backend):
        self.store = Store(root)
        self.images = Store(root, "images")
        self.profiles = Store(root, "profiles")
        self.recoveries = Store(root, "checkpoints")
        self.backend_factory = backend_factory
        self.processes = {}
        self.local = threading.local()

    @contextmanager
    def operation(self, *resources):
        held = getattr(self.local, "resources", set())
        with ExitStack() as stack:
            if not held:
                stack.enter_context(storage.guard(self.store.root))
            for resource in sorted(set(resources) - held):
                stack.enter_context(resource_lock(self.store.root / "locks" / f"{resource}.lock"))
            self.local.resources = held | set(resources)
            try:
                yield
            finally:
                self.local.resources = held

    def _verify_owned(self, backend, record):
        if not record.get("identity") or backend.identity(record["config"]["name"]) != record["identity"]:
            raise DeploymentError("The Tart VM directory changed outside AppleTart. Its identity cannot be verified; no VM changes were made.")

    def _preflight(self, machine: Machine, *, keys=True):
        vm = machine.vm
        if vm.cpu > (os.cpu_count() or 1):
            raise DeploymentError("The requested CPU count exceeds this Mac's CPUs.")
        if vm.network == "bridged":
            available = {name for _, name in socket.if_nameindex()}
            missing = set(vm.bridges or (vm.bridge,)) - available
            if missing:
                raise DeploymentError("Network interfaces are not available on this Mac: " + ", ".join(sorted(missing)))
        for share in vm.directory_shares:
            if not Path(share.host_path).is_dir():
                raise DeploymentError(f"Shared directory does not exist: {share.host_path}")
        if keys and vm.ssh_public_keys:
            check_ssh_tools()
        return read_public_keys(vm.ssh_public_keys) if keys else []

    def _record(self, machine: Machine) -> dict:
        record = self.store.get(machine.vm.name)
        if record and Machine.from_dict(record["config"]).config() != machine.config():
            raise DeploymentError("That name has a different saved configuration. Edit the existing VM or choose a new name.")
        return record or {"config": machine.config(), "phase": "new", "artifact": "", "owned": False,
                          "created_at": datetime.now(timezone.utc).isoformat()}

    def _managed(self, name: str):
        record = self.store.get(name)
        if not record:
            raise DeploymentError("This VM is not managed by AppleTart.")
        return record, Machine.from_dict(record["config"])

    def listing(self) -> dict:
        errors = []
        try:
            live = {item["Name"]: item for item in self.backend_factory().inventory()}
        except DeploymentError as error:
            live = {}
            errors.append(str(error))
        items = []
        for image in self.images.all():
            live.pop(image["tart_name"], None)
        for recovery in self.recoveries.all():
            live.pop(recovery["tart_name"], None)
        for record in self.store.all():
            name = record["config"]["name"]
            actual = live.pop(name, None)
            items.append({**record, "managed": True, "exists": actual is not None,
                          "running": actual["Running"] if actual else False,
                          "state": actual.get("State", "Running" if actual["Running"] else "Stopped") if actual else "Not created",
                          "disk": actual.get("Disk") if actual else None,
                          "forwarding_active": forwarder.status(self.store.root, name) if record["config"].get("port_forwards") else False})
        for name, actual in live.items():
            items.append({"config": {"name": name}, "managed": False, "exists": True,
                          "running": actual["Running"], "state": actual.get("State", "Unknown"), "phase": "external"})
        return {"machines": items, "errors": errors, "images": self.image_listing(), "profiles": self.profiles.all()}

    def _golden(self, name, backend):
        image = self.images.get(name)
        if not image or image.get("phase") != "ready":
            raise DeploymentError("That golden image is not ready. Choose an available image from the library.")
        actual = next((item for item in backend.inventory() if item["Name"] == image["tart_name"]), None)
        if not actual or actual["Running"] or backend.identity(image["tart_name"]) != image["identity"]:
            raise DeploymentError("The golden image must exist, be stopped and retain its saved directory identity.")
        return image

    def image_listing(self):
        return [{"name": image["config"]["name"], "os": image["config"]["os"], "phase": image["phase"],
                 "ssh_user": image["config"].get("ssh_user", ""),
                 "disk_gb": image["config"]["disk_gb"], "desktop": image["config"].get("desktop", "none"),
                 "source_vm": image["source_vm"], "created_at": image["created_at"],
                 "version": image.get("version", "1"), "notes": image.get("notes", ""),
                 "agent_version": image.get("guest_agent_version", "unknown"),
                 "requested_image": image.get("requested_image", {}), "guest_os": image.get("guest_os", {}),
                 "packages": image.get("packages", []), "source": image["config"].get("source", ""),
                 "sha256": image["config"].get("sha256", ""), "sha512": image["config"].get("sha512", ""),
                 "references": storage.golden_references(self, image["config"]["name"])} for image in self.images.all()]

    def _requested_image(self, machine):
        definition = image_catalog.load(self.store.root).get(machine.vm.os, {})
        version = next((item["label"] for item in definition.get("versions", []) if item["source"] == machine.source), "")
        return {"source": machine.source, "version": version}

    @exclusive
    def create_golden(self, name, image_name, report=print, *, version="1", notes=""):
        image_name = vm_name(image_name)
        version, notes = profiles.text(version, 64), profiles.text(notes)
        if not version:
            raise DeploymentError("Give the golden image a version.")
        source, machine = self._managed(name)
        if machine.guest_family != "linux" or machine.source_kind not in {"cloud", "golden", "tart"} or source["phase"] != "ready":
            raise DeploymentError("Save a completed Linux cloud or Tart VM as a golden image.")
        if self.images.get(image_name):
            raise DeploymentError("A golden image already uses that name. Choose a new name.")
        backend = self.backend_factory(report)
        actual = next((item for item in backend.inventory() if item["Name"] == name), None)
        if not actual or actual["Running"]:
            raise DeploymentError("Stop the source VM before saving its golden image.")
        self._verify_owned(backend, source)
        keys = read_public_keys(machine.vm.ssh_public_keys)
        tart_name = "appletart-golden-" + uuid.uuid4().hex[:16]
        image = {"config": {**machine.config(), "name": image_name}, "source_vm": name,
                 "tart_name": tart_name, "phase": "preparing", "created_at": datetime.now(timezone.utc).isoformat(),
                 "version": version, "notes": notes, "packages": list(machine.effective_packages)}
        image["requested_image"] = source.get("requested_image") or self._requested_image(machine)
        backend.run(["clone", name, tart_name])
        image["identity"] = backend.identity(tart_name)
        self.images.put(image)
        from dataclasses import replace
        prepared = replace(machine, source_kind="golden", vm=replace(machine.vm, name=tart_name, network="nat", bridge="", bridges=(), directory_shares=()),
                           hostname="golden-image", packages=(), software_bundles=(), bundle_packages=(), desktop="none", port_forwards=())
        directory = self.store.root / "cloud-init" / tart_name
        try:
            backend.run(prepared.vm.commands()[1])
            # Installed packages can change cloud-init's renderer, modules and
            # cached datasource behavior. Prepare every Linux copy before its
            # verification boot, regardless of the original image source.
            prepare_linux_golden(backend, prepared, self.store.root / "logs" / f"{tart_name}.log", report)
            key, public = bootstrap_key(directory)
            seed = write_seed(prepared, [*keys, public], directory, "appletart-golden-" + str(uuid.uuid4()), backend.mac_address(tart_name))
            observed_os = provision_cloud(backend, prepared, seed, key, public, self.store.root / "logs" / f"{tart_name}.log", report, prepare_image=True)
            if isinstance(observed_os, dict) and observed_os:
                image["guest_os"] = observed_os
        except Exception:
            report("The source VM is unchanged. The incomplete golden image was removed; try Save as golden image again after checking its log.")
            with uncancellable():
                if backend.identity(tart_name) == image["identity"]:
                    backend.run(["delete", tart_name])
                self.images.remove(image_name)
                import shutil
                shutil.rmtree(directory, ignore_errors=True)
            raise
        import shutil
        shutil.rmtree(directory, ignore_errors=True)
        image["phase"] = "ready"
        image["guest_agent_version"] = guest_agent.VERSION
        image["guest_agent_privileged"] = True
        self.images.put(image)
        report(f"Golden image {image_name} is ready. Future VMs can clone its installed disk.")
        return image

    @exclusive
    def download(self, machine: Machine, report=print) -> dict:
        self._preflight(machine)
        record = self._record(machine)
        if record["owned"]:
            return record
        backend = self.backend_factory(report)
        if any(item["Name"] == machine.vm.name for item in backend.inventory()):
            raise DeploymentError("A Tart VM with that name already exists. Choose another name.")
        if machine.source_kind == "golden":
            image = self._golden(machine.source, backend)
            if image["config"]["os"] != machine.vm.os or machine.vm.disk_gb < image["config"]["disk_gb"]:
                raise DeploymentError("Match the golden image's OS and choose a disk at least as large as its original disk.")
            record["artifact"] = image["tart_name"]
            record["requested_image"] = image.get("requested_image", {})
            record["guest_os"] = image.get("guest_os", {})
        elif machine.source_kind == "tart":
            if "/" in machine.source:
                source_key = hashlib.sha256(machine.source.encode()).hexdigest()
                with resource_lock(self.store.root / "locks" / f"tart-{source_key}.lock", wait=True, report=report):
                    backend.run(["pull", machine.source])
            else:
                template = next((item for item in backend.inventory() if item["Name"] == machine.source), None)
                if not template or template["Running"] or template.get("State", "Stopped").lower() != "stopped":
                    raise DeploymentError("The local Tart template must exist and be stopped.")
            record["artifact"] = machine.source
        else:
            fetch = fetch_image if machine.cloud_setup else fetch_iso
            record["artifact"] = str(fetch(machine.source, machine.checksum, self.store.root / "downloads", report, **({"refresh": True} if machine.refresh_source else {})))
        record.setdefault("requested_image", self._requested_image(machine))
        record["phase"] = "downloaded"
        self.store.put(record)
        report("Image available. Ready to build.")
        return record

    @exclusive
    def build(self, machine: Machine, report=print, password: str = "", *, guest_password="", user_passwords=None) -> dict:
        accounts = users.provisioning(machine, guest_password, user_passwords) if machine.source_kind != "iso" else []
        keys = self._preflight(machine)
        if machine.cloud_setup:
            check_cloud_tools()
        record = self._record(machine)
        if record["phase"] == "new":
            record = self.download(machine, report)
        backend = self.backend_factory(report)
        live = {item["Name"]: item for item in backend.inventory()}
        name = machine.vm.name
        if record["owned"]:
            if name not in live:
                raise DeploymentError("The managed VM was removed outside AppleTart. Destroy its saved record before rebuilding.")
            self._verify_owned(backend, record)
            if record["phase"] in {"ready", "installing", "awaiting-installation"}:
                report("VM is already built.")
                return record
            if live[name]["Running"]:
                raise DeploymentError("Stop the VM before resuming its build.")
        else:
            if name in live:
                raise DeploymentError("A Tart VM with that name already exists; it will not be overwritten.")
            if machine.source_kind in ("tart", "golden"):
                # Recheck a local source at build time; it may have started since download.
                if machine.source_kind == "golden":
                    image = self._golden(machine.source, backend)
                    if image["tart_name"] != record["artifact"]:
                        raise DeploymentError("The golden image changed after download; create a new VM configuration.")
                elif "/" not in machine.source:
                    source = live.get(machine.source)
                    if not source or source["Running"] or source.get("State", "Stopped").lower() != "stopped":
                        raise DeploymentError("The local source template must be stopped.")
                if machine.source_kind == "tart" and "/" not in machine.source:
                    with self.operation("vm-" + vm_name(machine.source)):
                        template = next((item for item in backend.inventory() if item["Name"] == machine.source), None)
                        if not template or template["Running"]:
                            raise DeploymentError("The local source template must be stopped while cloning.")
                        backend.run(["clone", record["artifact"], name])
                else:
                    backend.run(["clone", record["artifact"], name])
            else:
                fetch = fetch_image if machine.cloud_setup else fetch_iso
                fetch(record["artifact"], machine.checksum, self.store.root / "downloads", report)
                backend.run(["create", "--linux", "--disk-size", str(machine.vm.disk_gb), name])
            record.update(owned=True, phase="created")
            record["identity"] = backend.identity(name)
            if machine.source_kind == "golden":
                record["cloud_imported"] = True
            self.store.put(record)
        if machine.source_kind == "cloud" and record.get("cloud_imported") and backend.disk_format(name) in {"qcow2", "xz"}:
            report("Repairing an older cloud import: the VM disk contains container data and needs conversion to a raw disk.")
            record["cloud_imported"] = False
            self.store.put(record)
        if machine.cloud_setup and not record.get("cloud_imported"):
            artifact = fetch_image(record["artifact"], machine.checksum, self.store.root / "downloads", report)
            raw = convert_disk(artifact, self.store.root / "converted", machine.vm.disk_gb, report)
            backend.run(["set", name, "--disk", str(raw)])
            record["cloud_imported"] = True
            self.store.put(record)
        if machine.source_kind == "tart":
            capacity = backend.disk_capacity_gb(name)
            if capacity > machine.vm.disk_gb:
                report(f"The source disk is {capacity} GB. Preserving that capacity because Tart cannot shrink an image disk.")
                machine = replace(machine, vm=replace(machine.vm, disk_gb=capacity))
                record["config"] = machine.config()
                self.store.put(record)
        if not record.get("hardware_configured", record.get("cloud_configured", False)):
            backend.run(machine.vm.commands()[1])
            record["hardware_configured"] = True
            if machine.cloud_setup:
                record["cloud_configured"] = True
            self.store.put(record)
        if machine.source_kind == "tart":
            check_ssh_tools()
            report("Booting to provision SSH access, the guest agent and selected software…")
            from .software import install_script, LINUX_BUILD_NOTICE
            if machine.guest_family == "linux":
                report(LINUX_BUILD_NOTICE)
            elif machine.effective_packages:
                report("Installing software can take 5–15 minutes or longer. Allowing up to 30 minutes for guest provisioning.")
            if not password:
                definition = image_catalog.load(self.store.root).get(machine.vm.os, {})
                known = machine.source == definition.get("source") or any(v["source"] == machine.source for v in definition.get("versions", []))
                if known and machine.vm.ssh_user == definition.get("ssh_user"):
                    password = definition.get("bootstrap_password", "")
            binary = guest_agent.prepare(self.store.root / "tools", report, **({"family": machine.guest_family} if machine.guest_family != "linux" else {}))
            observed_os = provision_keys(backend, machine.vm, keys, password, batch=True, agent_binary=binary,
                           family=machine.guest_family,
                           known_hosts=host_key_file(self.store.root, name),
                           extra_script=install_script(machine.effective_packages, machine.guest_family, upgrade=True),
                           **({"users": accounts} if accounts else {}),
                           report=report, log_path=self.store.root / "logs" / f"{name}.log")
            if isinstance(observed_os, dict) and observed_os:
                record["guest_os"] = observed_os
            record["guest_agent_version"] = guest_agent.VERSION
            record["guest_agent_privileged"] = True
            record["phase"] = "ready"
        elif machine.cloud_setup:
            directory = self.store.root / "cloud-init" / name
            # An earlier attempt may have revoked bootstrap SSH before the
            # final seed or ready record was saved. Recover through its agent.
            prefer_agent = bool(record.get("cloud_instance_id"))
            key, public = bootstrap_key(directory)
            if machine.source_kind == "golden" and not record.get("golden_identity_recovery"):
                if record.get("cloud_instance_id"):
                    # NoCloud can retain an incomplete clone's cached user-data.
                    # A fresh instance ID makes it run the new identity repair;
                    # keep the existing disk, MAC and temporary build key.
                    record["cloud_instance_id"] = "appletart-" + str(uuid.uuid4())
                    known = directory / "known_hosts"
                    if known.exists():
                        known.rename(directory / "known_hosts.before-identity-repair")
                    report("Refreshing cloud-init for an older golden-image build. The existing VM disk and MAC are preserved.")
                record["golden_identity_recovery"] = True
            if record.get("cloud_config_version", 0) < CLOUD_CONFIG_VERSION:
                if record.get("cloud_instance_id"):
                    record["cloud_instance_id"] = "appletart-" + str(uuid.uuid4())
                    known = directory / "known_hosts"
                    if known.exists():
                        known.rename(directory / "known_hosts.before-cloud-update")
                    report("Refreshing cached cloud-init configuration for this older build. The VM disk and MAC are preserved.")
                record["cloud_config_version"] = CLOUD_CONFIG_VERSION
            record.setdefault("cloud_instance_id", "appletart-" + str(uuid.uuid4()))
            mac = backend.mac_address(name)
            seed = write_seed(machine, [*keys, public], directory, record["cloud_instance_id"], mac)
            record["seed"] = str(seed)
            self.store.put(record)
            observed_os = provision_cloud(backend, machine, seed, key, public, self.store.root / "logs" / f"{name}.log", report,
                                          **({"prefer_agent": True} if prefer_agent else {}),
                                          **({"users": accounts} if accounts else {}))
            if isinstance(observed_os, dict) and observed_os:
                record["guest_os"] = observed_os
            # Keep a stable seed on subsequent boots, containing only the user's keys.
            with uncancellable():
                write_seed(machine, keys, directory, record["cloud_instance_id"], mac)
                key.unlink(missing_ok=True)
                key.with_suffix(".pub").unlink(missing_ok=True)
                record["phase"] = "ready"
                record["guest_agent_version"] = guest_agent.VERSION
                record["guest_agent_privileged"] = True
                self.store.put(record)
        else:
            record["phase"] = "awaiting-installation"
        self.store.put(record)
        report("Build ready." if record["phase"] == "ready" else "VM hardware built. Start the installer, then finish setup after installing the OS.")
        return record

    @exclusive
    def start(self, name: str, report=print, *, headless: bool | None = None) -> None:
        record, machine = self._managed(name)
        if headless is None:
            headless = record.get("headless", machine.guest_family != "macos")
        if type(headless) is not bool:
            raise DeploymentError("headless must be true or false.")
        self._preflight(machine, keys=False)
        if not record["owned"]:
            raise DeploymentError("Build the VM before deploying it.")
        backend = self.backend_factory(report)
        live = next((item for item in backend.inventory() if item["Name"] == name), None)
        if not live:
            raise DeploymentError("The VM disk no longer exists in Tart.")
        self._verify_owned(backend, record)
        if live["Running"]:
            raise DeploymentError("This VM is already running.")
        if record["phase"] in {"created", "restore-failed"}:
            raise DeploymentError("VM setup or restore is incomplete. Resume build or restore a recovery point before deploying.")
        installer = record["phase"] in {"awaiting-installation", "installing"}
        if not installer and machine.port_forwards:
            forwarder.stop(self.store.root, name)
            forwarder.check_listeners(machine.port_forwards)
        args = machine.vm.run_args(headless=headless and not installer)
        if installer:
            args += ["--disk", record["artifact"] + ":ro"]
        elif machine.cloud_setup:
            if not Path(record.get("seed", "")).is_file():
                raise DeploymentError("The cloud-init seed is missing. Restore it before starting this VM.")
            args += ["--disk", record["seed"] + ":ro"]
        log_dir = self.store.root / "logs"
        log_dir.mkdir(exist_ok=True)
        diagnostics.console(log_dir / f"{name}.log")
        diagnostics.command_start([backend.binary, *args])
        with (log_dir / f"{name}.log").open("a") as log:
            os.chmod(log_dir / f"{name}.log", 0o600)
            try:
                process = subprocess.Popen([backend.binary, *args], stdout=log, stderr=log,
                                           env={**os.environ, "TART_NO_AUTO_PRUNE": "1"}, start_new_session=True)
            except OSError as error:
                raise DeploymentError(f"Cannot start VM: {error}") from error
        self.processes[name] = process
        try:
            for _ in range(20):
                checkpoint()
                if process.poll() is not None:
                    raise DeploymentError("Tart exited during startup. Open the VM log for details.")
                if any(item["Name"] == name and item["Running"] for item in backend.inventory()):
                    if installer:
                        record["phase"] = "installing"
                        self.store.put(record)
                    elif machine.port_forwards:
                        try:
                            forwarder.start(self.store.root, machine, backend, record["identity"], report)
                        except (DeploymentError, OSError):
                            with uncancellable():
                                forwarder.stop(self.store.root, name)
                                backend.run(["stop", name])
                            raise
                    record["headless"] = headless
                    self.store.put(record)
                    report("Installer opened in Tart. Complete the OS setup and shut down the guest." if installer else "VM is running.")
                    return
                pause(0.25)
        except JobCancelled:
            forwarder.stop(self.store.root, name)
            stop_build(process, name, backend, report)
            raise
        raise DeploymentError("Startup has not been confirmed yet. Check the dashboard and VM log before retrying.")

    @exclusive
    def stop(self, name: str, report=print) -> None:
        record, _ = self._managed(name)
        if not record["owned"]:
            raise DeploymentError("There is no built VM to stop.")
        backend = self.backend_factory(report)
        self._verify_owned(backend, record)
        forwarder.stop(self.store.root, name)
        backend.run(["stop", name])
        if any(item["Name"] == name and item["Running"] for item in backend.inventory()):
            raise DeploymentError("The VM is still running; inspect Tart before continuing.")
        report("VM stopped.")

    @exclusive
    def shutdown(self, name, report=print, *, timeout=60):
        record, machine = self._managed(name)
        backend = self.backend_factory(report)
        self._verify_owned(backend, record)
        if not any(i["Name"] == name and i["Running"] for i in backend.inventory()):
            raise DeploymentError("This VM is already stopped.")
        command, transport = guest_access.management_command(self.store.root, machine, record, backend,
                                                             ["/bin/sh", "-c", "sync; shutdown -h now"], report=report)
        report("Asking the guest to flush its disk and shut down cleanly…")
        try:
            result = run(command, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeploymentError("Could not request guest shutdown. Check guest management access or use Force stop: " + str(error)) from error
        if not any(i["Name"] == name and i["Running"] for i in backend.inventory()):
            forwarder.stop(self.store.root, name)
            report("Guest shut down cleanly.")
            return
        disconnected = any(word in result.stderr.lower() for word in ("closed", "reset", "broken pipe", "unavailable", "eof"))
        # Tart exec can exit silently when the guest closes VSOCK during
        # poweroff, just before inventory changes from running to stopped.
        silent_agent_exit = transport == "agent" and result.returncode and not (result.stdout.strip() or result.stderr.strip())
        if silent_agent_exit:
            report("The guest agent disconnected without an error message. Waiting for Tart to confirm power-off…")
            disconnected = True
        if result.returncode and not (transport == "agent" and disconnected) and result.returncode != 255:
            hint = "Passwordless sudo is required for SSH fallback: " if transport == "ssh" else "Guest agent returned an error: "
            raise DeploymentError("Guest shutdown failed. " + hint + result.stderr[-500:])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(i["Name"] == name and i["Running"] for i in backend.inventory()):
                forwarder.stop(self.store.root, name)
                report("Guest shut down cleanly.")
                return
            if result.returncode == 255 and not disconnected:
                raise DeploymentError("Guest management could not request shutdown: " + result.stderr[-500:])
            pause(0.5)
        raise DeploymentError(f"The guest did not shut down within {timeout} seconds. Its disk was not force-stopped; check guest management or choose Force stop.")

    @exclusive
    def restart(self, name, report=print):
        self.shutdown(name, report)
        self.start(name, report)

    @exclusive
    def force_stop(self, name, report=print):
        record, _ = self._managed(name)
        backend = self.backend_factory(report)
        self._verify_owned(backend, record)
        forwarder.stop(self.store.root, name)
        backend.run(["stop", name, "--timeout", "0"], capture=True, timeout=10)
        if any(i["Name"] == name and i["Running"] for i in backend.inventory()):
            raise DeploymentError("The VM is still running. Check its runtime log.")
        report("VM force-stopped.")

    def health(self, name, *, details=False):
        if type(details) is not bool:
            raise DeploymentError("details must be true or false.")
        record, machine = self._managed(name)
        backend = self.backend_factory()
        self._verify_owned(backend, record)
        running = any(i["Name"] == name and i["Running"] for i in backend.inventory())
        if not running:
            return {"status": "stopped", "ip": "", "ssh_ready": False, "agent": "stopped",
                    "agent_version": record.get("guest_agent_version", ""), "interfaces": [], "shares": [],
                    "issues": [], "forwarding": "inactive", "checked_at": time.time()}
        return guest_access.health(self.store.root, machine, record, backend, details=details)

    def connection(self, name, *, terminal=False):
        _, machine = self._managed(name)
        ip = self.ip(name)
        result = guest_access.connection(self.store.root, machine, ip)
        if terminal:
            result["terminal"] = guest_access.open_terminal(self.store.root, machine, ip)
        return result

    @exclusive
    def save_ssh_config(self, name):
        record, machine = self._managed(name)
        if not record.get("owned") or record["phase"] != "ready":
            raise DeploymentError("Complete the VM build before adding its SSH configuration.")
        return ssh_config.save(self.store.root, machine, self.ip(name))

    @exclusive
    def add_users(self, name, entries, report=print):
        entries = users.validate(entries)
        record, machine = self._managed(name)
        if not record.get("owned") or record["phase"] != "ready":
            raise DeploymentError("Complete the VM build before adding users.")
        backend = self.backend_factory(report)
        self._verify_owned(backend, record)
        if not any(item["Name"] == name and item["Running"] for item in backend.inventory()):
            raise DeploymentError("Start the VM before adding users. A privileged guest agent or SSH with passwordless sudo is required.")
        command, transport = guest_access.management_command(self.store.root, machine, record, backend,
                                                             ["/bin/sh", "-s"], stdin=True, report=report)
        users.install(command, entries, report, **({"family": machine.guest_family} if machine.guest_family != "linux" else {}))
        record["last_user_setup"] = {"usernames": [entry["username"] for entry in entries],
                                     "transport": transport,
                                     "completed_at": datetime.now(timezone.utc).isoformat()}
        self.store.put(record)

    def settings(self):
        return settings.listing(self.store.root)

    def save_settings(self, value):
        with self.operation("settings"):
            return settings.save(self.store.root, value)

    def checkpoints(self, name):
        self._managed(name)
        return Recovery(self).listing(name)

    def create_checkpoint(self, name, label, notes="", report=print):
        return Recovery(self).create(name, label, notes, report)

    def restore_checkpoint(self, name, identifier, confirmation, report=print):
        return Recovery(self).restore(name, identifier, confirmation, report)

    def delete_checkpoint(self, name, identifier, confirmation, report=print):
        return Recovery(self).remove(name, identifier, confirmation, report)

    def storage_listing(self):
        return storage.listing(self)

    def cleanup_storage(self, identifiers, confirmation, report=print, *, cache_only=False):
        storage.cleanup(self, identifiers, confirmation, report, cache_only=cache_only)

    def delete_image(self, name, confirmation, report=print):
        name = vm_name(name)
        if confirmation != name:
            raise DeploymentError("Type the golden image name to confirm deletion.")
        storage.cleanup(self, ["golden/" + name], "DELETE", report)

    def save_profile(self, name, config, notes=""):
        return profiles.save(self, name, config, notes)

    def import_profile(self, value):
        return profiles.import_profile(self, value)

    def delete_profile(self, name, confirmation):
        return profiles.remove(self, name, confirmation)

    def profile_deployment(self, profile_name, name):
        return profiles.deployment(self, profile_name, name)

    def update_image(self, name, version, notes):
        name = vm_name(name)
        version, notes = profiles.text(version, 64), profiles.text(notes)
        if not version:
            raise DeploymentError("Give the image a version.")
        with self.operation("image-" + name):
            image = self.images.get(name)
            if not image:
                raise DeploymentError("This golden image no longer exists.")
            image.update(version=version, notes=notes)
            self.images.put(image)

    @exclusive
    def finish_installation(self, name: str, report=print, password: str = "", *, guest_password="", user_passwords=None) -> None:
        record, machine = self._managed(name)
        accounts = users.provisioning(machine, guest_password, user_passwords)
        if not record["owned"] or record["phase"] not in {"installing", "awaiting-installation"}:
            raise DeploymentError("This VM does not have a pending installer step.")
        keys = self._preflight(machine)
        backend = self.backend_factory(report)
        live = next((item for item in backend.inventory() if item["Name"] == name), None)
        if not live or live["Running"]:
            raise DeploymentError("Finish the OS installation and shut down the VM first.")
        self._verify_owned(backend, record)
        if keys or machine.guest_family == "linux":
            check_ssh_tools()
            from .software import install_script, LINUX_BUILD_NOTICE
            report("Finishing guest setup. SSH must be enabled in the installed guest.")
            if machine.guest_family == "linux":
                report(LINUX_BUILD_NOTICE)
            binary = guest_agent.prepare(self.store.root / "tools", report)
            provision_keys(backend, machine.vm, keys, password, batch=True, agent_binary=binary,
                           known_hosts=host_key_file(self.store.root, name),
                           extra_script=install_script(machine.effective_packages, machine.guest_family, upgrade=True),
                           **({"users": accounts} if accounts else {}),
                           report=report, log_path=self.store.root / "logs" / f"{name}.log")
            record["guest_agent_version"] = guest_agent.VERSION
            record["guest_agent_privileged"] = True
        record["phase"] = "ready"
        self.store.put(record)
        report("Installation confirmed. Future starts will boot without the installer.")

    @exclusive
    def configure(self, name: str, config: dict, report=print) -> None:
        record, old = self._managed(name)
        machine = self.machine(config)
        if machine.vm.name != name or machine.vm.os != old.vm.os or (machine.source_kind, machine.source) != (old.source_kind, old.source):
            raise DeploymentError("Name, guest OS and image source cannot change after creation.")
        if (machine.vm.ssh_user, machine.vm.ssh_public_keys, machine.password_login, machine.users) != (old.vm.ssh_user, old.vm.ssh_public_keys, old.password_login, old.users):
            raise DeploymentError("SSH identity changes require a new build.")
        if (machine.hostname, machine.effective_packages, machine.desktop, machine.software_bundles) != (old.hostname, old.effective_packages, old.desktop, old.software_bundles):
            raise DeploymentError("Cloud-init account, hostname and package changes require a new build.")
        shares_changed = machine.vm.directory_shares != old.vm.directory_shares
        if record["owned"] and machine.cloud_setup and shares_changed and record["phase"] != "ready":
            raise DeploymentError("Complete the cloud build before changing directory shares.")
        self._preflight(machine)
        if record["owned"]:
            backend = self.backend_factory(report)
            live = next((item for item in backend.inventory() if item["Name"] == name), None)
            if not live or live["Running"]:
                raise DeploymentError("The VM must exist and be stopped before editing.")
            self._verify_owned(backend, record)
            if machine.vm.disk_gb < old.vm.disk_gb:
                raise DeploymentError("Disk size can only grow.")
            if machine.cloud_setup and shares_changed:
                instance = "appletart-shares-" + str(uuid.uuid4())
                directory = self.store.root / "cloud-init" / name / instance
                report("Preparing updated directory mounts for the next boot…")
                # Publish a separate seed revision only after Tart accepts the
                # edit. Failures leave the original boot seed and record intact.
                seed = write_seed(machine, [], directory, instance, backend.mac_address(name), boot_only=True)
            configure = machine.vm.commands()[1]
            configure.remove("--random-mac")
            backend.run(configure)
            if machine.cloud_setup and shares_changed:
                record.update(seed=str(seed), cloud_instance_id=instance)
        record["config"] = machine.config()
        self.store.put(record)
        report("VM configuration saved.")

    @exclusive
    def destroy(self, name: str, confirmation: str, report=print) -> None:
        if confirmation != name:
            raise DeploymentError("Type the exact VM name to confirm destruction.")
        record, _ = self._managed(name)
        if self.checkpoints(name):
            raise DeploymentError("Delete this VM's checkpoints from Details before destroying it.")
        backend = self.backend_factory(report)
        live = next((item for item in backend.inventory() if item["Name"] == name), None)
        if live and not record["owned"]:
            raise DeploymentError("An external VM now uses this name. It will not be deleted.")
        if live and live["Running"]:
            raise DeploymentError("Stop the VM before destroying its disk.")
        if live:
            self._verify_owned(backend, record)
            backend.run(["delete", name])
        forwarder.stop(self.store.root, name)
        self.store.remove(name)
        import shutil
        shutil.rmtree(self.store.root / "cloud-init" / vm_name(name), ignore_errors=True)
        report("VM destroyed. Downloaded source images were retained for reuse.")

    def ip(self, name: str) -> str:
        record, machine = self._managed(name)
        backend = self.backend_factory()
        self._verify_owned(backend, record)
        if not any(item["Name"] == name and item["Running"] for item in backend.inventory()):
            raise DeploymentError("The VM is stopped. Start it to discover its current IP address.")
        return resolve_ip(backend, machine.vm, agent_installed=bool(record.get("guest_agent_version")))

    @exclusive
    def install_guest_agent(self, name: str, report=print, *, password="") -> None:
        """Upgrade in place; boot stopped guests briefly without rebuilding disks."""
        from .ssh import wait_for_ssh
        record, machine = self._managed(name)
        if not record["owned"] or record["phase"] != "ready":
            raise DeploymentError("Complete the VM build before installing its guest agent.")
        backend = self.backend_factory(report)
        self._verify_owned(backend, record)
        if any(item["Name"] == name and item["Running"] for item in backend.inventory()):
            if guest_agent.root_available(backend, name):
                guest_agent.install_via_agent(backend, name, self.store.root / "tools", report, family=machine.guest_family)
            else:
                ip = resolve_ip(backend, machine.vm, wait=2, agent_installed=bool(record.get("guest_agent_version")))
                report("Privileged management is unavailable. Installing through the selected SSH login.")
                guest_agent.install(guest_access.ssh_args(self.store.root, machine, ip, batch=not bool(password)), self.store.root / "tools", report, family=machine.guest_family, **({"password": password} if password else {}))
                guest_agent.verify_management(backend, name)
            record.update(guest_agent_version=guest_agent.VERSION, guest_agent_privileged=True)
            self.store.put(record)
            report("Privileged guest-agent management verified. The VM remains running.")
            return
        self._preflight(machine)
        binary = guest_agent.prepare(self.store.root / "tools", report, **({"family": machine.guest_family} if machine.guest_family != "linux" else {}))
        vm = replace(machine.vm, network="nat", bridge="", bridges=())
        args = vm.run_args(headless=True)
        if machine.cloud_setup:
            seed = Path(record.get("seed", ""))
            if not seed.is_file():
                raise DeploymentError("Restore the VM's cloud-init seed before installing its guest agent.")
            args += ["--disk", str(seed) + ":ro"]
        logs = self.store.root / "logs"
        logs.mkdir(exist_ok=True)
        diagnostics.console(logs / f"{name}.log")
        diagnostics.command_start([backend.binary, *args])
        with (logs / f"{name}.log").open("a") as log:
            os.chmod(logs / f"{name}.log", 0o600)
            process = subprocess.Popen([backend.binary, *args], stdout=log, stderr=log,
                                       env={**os.environ, "TART_NO_AUTO_PRUNE": "1"}, start_new_session=True)
        try:
            report("Booting briefly on NAT. Checking privileged management before SSH fallback…")
            if guest_agent.wait_for_root(backend, name, process, timeout=45 if record.get("guest_agent_privileged") else 10):
                guest_agent.install_via_agent(backend, name, self.store.root / "tools", report, binary=binary, family=machine.guest_family)
            else:
                ip = wait_for_ssh(backend, vm, process, timeout=180, report=report)
                ssh_args = ssh_options(vm, known_hosts=host_key_file(self.store.root, name, migrate_legacy=True),
                                       batch=not bool(password), accept_new=True, connect_timeout=10)
                ssh_args.append(f"{vm.ssh_user}@{ip}")
                guest_agent.install(ssh_args, self.store.root / "tools", report, binary=binary, family=machine.guest_family, **({"password": password} if password else {}))
                guest_agent.verify_management(backend, name)
            record["guest_agent_version"] = guest_agent.VERSION
            record["guest_agent_privileged"] = True
            self.store.put(record)
            report("Privileged guest management verified. Start the VM to use your saved network settings.")
        finally:
            stop_build(process, name, backend, report)

    def log(self, name: str) -> str:
        return self.logs(name)["log"]

    def logs(self, name, source=None, before=None, after=None):
        return diagnostics.read(self.store.root, name, source, before, after)

    def log_path(self, name, source):
        return diagnostics.source_path(self.store.root, name, source)

    @exclusive
    def collect_logs(self, name, report=print):
        record, machine = self._managed(name)
        backend = self.backend_factory(report)
        self._verify_owned(backend, record)
        if not any(item["Name"] == name and item["Running"] for item in backend.inventory()):
            raise DeploymentError("Start the VM to collect current guest logs. Saved logs are available while stopped.")
        command, _ = guest_access.management_command(self.store.root, machine, record, backend, ["/bin/sh", "-s"], stdin=True, report=report)
        if not capture_guest([], report, command=command):
            raise DeploymentError("Guest logs could not be collected through the management connection. Inspect this attempt's detailed output.")
