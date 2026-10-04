"""Stopped Linux disk checkpoints; restore never replaces a VM's identity."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import shutil
import uuid

from .catalog import Machine
from .deployment import DeploymentError, vm_name
from .operations import uncancellable
from .profiles import text
from .storage import vm_directory


class Recovery:
    def __init__(self, api):
        self.api = api
        self.store = api.recoveries

    def listing(self, name=None):
        if name is not None:
            vm_name(name)
        return [{"id": item["config"]["name"], "name": item["source_vm"], "label": item["label"],
                 "notes": item["notes"], "created_at": item["created_at"], "disk_gb": item["record"]["config"]["disk_gb"]}
                for item in self.store.all() if name is None or item["source_vm"] == name]

    def stopped(self, name, backend):
        record, machine = self.api._managed(name)
        actual = next((i for i in backend.inventory() if i["Name"] == name), None)
        if not record.get("owned") or not actual or actual["Running"] or actual.get("State", "Stopped").lower() != "stopped":
            raise DeploymentError("Checkpoints require an existing, stopped AppleTart VM.")
        self.api._verify_owned(backend, record)
        directory = vm_directory(backend, name, record["identity"])
        if (directory / "state.vzvmsave").exists() or (directory / "state.vzvmsave").is_symlink():
            raise DeploymentError("This VM has saved runtime state. Resume it and shut it down fully before using checkpoints.")
        return record, machine

    def create(self, name, label, notes="", report=print):
        vm_name(name)
        label = text(label, 100)
        notes = text(notes)
        if not label:
            raise DeploymentError("Give this checkpoint a name.")
        with self.api.operation("vm-" + name):
            backend = self.api.backend_factory(report)
            record, _ = self.stopped(name, backend)
            if record["phase"] != "ready":
                raise DeploymentError("Complete the build before creating a checkpoint.")
            if any(i["label"] == label for i in self.listing(name)):
                raise DeploymentError("This VM already has a checkpoint with that name.")
            return self._capture(name, label, notes, record, backend, report)

    def _capture(self, name, label, notes, record, backend, report):
        identifier = "cp-" + uuid.uuid4().hex
        tart_name = "appletart-" + identifier
        directory = self.api.store.root / "recovery" / identifier
        directory.mkdir(parents=True, mode=0o700)
        item = {"config": {"name": identifier}, "label": label, "notes": notes, "source_vm": name,
                "tart_name": tart_name, "record": deepcopy(record), "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            seed = record.get("seed")
            if seed:
                shutil.copyfile(seed, directory / "seed.iso")
                (directory / "seed.iso").chmod(0o600)
            known = self.api.store.root / "cloud-init" / name / "known_hosts"
            if known.is_file():
                shutil.copyfile(known, directory / "known_hosts")
                (directory / "known_hosts").chmod(0o600)
            with uncancellable():
                backend.run(["clone", name, tart_name])
                item["identity"] = backend.identity(tart_name)
                self.store.put(item)
        except BaseException:
            # Never delete an unverified clone if ownership could not be saved.
            if item.get("identity"):
                with uncancellable():
                    if backend.identity(tart_name) == item["identity"]:
                        backend.run(["delete", tart_name])
            shutil.rmtree(directory, ignore_errors=True)
            raise
        report(f"Checkpoint '{label}' saved. VM identity and shared Mac directories are unchanged.")
        return identifier

    def restore(self, name, identifier, confirmation, report=print):
        name, identifier = vm_name(name), vm_name(identifier)
        if confirmation != name:
            raise DeploymentError("Type the VM name to confirm checkpoint restore.")
        with self.api.operation("vm-" + name, "checkpoint-" + identifier):
            backend = self.api.backend_factory(report)
            record, _ = self.stopped(name, backend)
            item = self.store.get(identifier)
            if not item or item["source_vm"] != name:
                raise DeploymentError("This checkpoint does not belong to this VM.")
            directory = vm_directory(backend, item["tart_name"], item["identity"])
            actual = next((i for i in backend.inventory() if i["Name"] == item["tart_name"]), None)
            if not actual or actual["Running"] or actual.get("State", "Stopped").lower() != "stopped" or (directory / "state.vzvmsave").exists() or (directory / "state.vzvmsave").is_symlink():
                raise DeploymentError("The checkpoint clone must be fully stopped with no saved runtime state before restoring it.")
            disk = directory / "disk.img"
            if disk.is_symlink() or not disk.is_file():
                raise DeploymentError("The checkpoint disk is missing or symlinked.")
            target = deepcopy(item["record"])
            machine = Machine.from_dict(target["config"])
            saved = self.api.store.root / "recovery" / identifier
            if target.get("seed") and not (saved / "seed.iso").is_file():
                raise DeploymentError("The checkpoint's cloud-init seed is missing.")
            if record["phase"] == "ready":
                backup = self._capture(name, "Before restore " + uuid.uuid4().hex[:8],
                                       "Automatic recovery point created before restoring " + item["label"], record, backend, report)
                report("Current disk preserved in recovery point " + backup)
            elif record["phase"] != "restore-failed":
                raise DeploymentError("Complete the build before restoring a checkpoint.")
            with uncancellable():
                pending = {**record, "phase": "restore-failed"}
                self.api.store.put(pending)
                backend.run(["set", name, "--disk", str(disk), "--cpu", str(machine.vm.cpu), "--memory", str(machine.vm.memory_mb)])
                if target.get("seed"):
                    seed = self.api.store.root / "cloud-init" / name / "seed.iso"
                    seed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    self._copy(saved / "seed.iso", seed)
                    target["seed"] = str(seed)
                if (saved / "known_hosts").is_file():
                    self._copy(saved / "known_hosts", self.api.store.root / "cloud-init" / name / "known_hosts")
                target.update(identity=record["identity"], phase="ready", last_restored_at=datetime.now(timezone.utc).isoformat())
                self.api.store.put(target)
            report(f"Restored '{item['label']}'. Start the VM when ready. MAC and Tart directory identity were preserved.")

    @staticmethod
    def _copy(source, destination):
        temporary = destination.with_name("." + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copyfile(source, temporary)
            temporary.chmod(0o600)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self, name, identifier, confirmation, report=print):
        name, identifier = vm_name(name), vm_name(identifier)
        with self.api.operation("vm-" + name, "checkpoint-" + identifier):
            item = self.store.get(identifier)
            if not item or item["source_vm"] != name or confirmation != item["label"]:
                raise DeploymentError("Type this checkpoint's exact name to confirm deletion.")
            backend = self.api.backend_factory(report)
            vm_directory(backend, item["tart_name"], item["identity"])
            if any(i["Name"] == item["tart_name"] and i["Running"] for i in backend.inventory()):
                raise DeploymentError("The checkpoint clone is running in Tart; stop it before deleting.")
            with uncancellable():
                backend.run(["delete", item["tart_name"]])
                self.store.remove(identifier)
                shutil.rmtree(self.api.store.root / "recovery" / identifier, ignore_errors=True)
            report("Checkpoint deleted.")
