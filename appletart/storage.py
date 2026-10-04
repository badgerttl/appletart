"""Storage accounting and conservative cleanup of AppleTart-owned resources."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat

from .deployment import DeploymentError, vm_name
from .operations import resource_lock


@contextmanager
def guard(root, *, exclusive=False):
    path = root / "locks" / "storage.guard"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentError("Storage cleanup and VM/image operations cannot run together. Wait for the active operation to finish.") from error
        yield


def usage(path):
    logical = allocated = 0
    if not path.exists() or path.is_symlink():
        return {"logical_bytes": 0, "allocated_bytes": 0}
    paths = [path] if path.is_file() else (Path(base) / name for base, dirs, files in os.walk(path, followlinks=False) for name in files)
    for item in paths:
        try:
            info = item.lstat()
            if stat.S_ISREG(info.st_mode):
                logical += info.st_size
                allocated += info.st_blocks * 512
        except OSError:
            continue
    return {"logical_bytes": logical, "allocated_bytes": allocated}


def vm_directory(backend, name, identity):
    if backend.identity(name) != identity:
        raise DeploymentError("The Tart directory identity changed. Storage actions are blocked.")
    path = Path(identity["home"]) / "vms" / vm_name(name)
    if path.is_symlink():
        raise DeploymentError("Symlinked Tart directories are not managed by storage cleanup.")
    return path


def listing(api):
    references = {str(Path(record["artifact"]).resolve()): record["config"]["name"] for record in api.store.all()
                  if record.get("artifact") and Path(record["artifact"]).is_absolute()}
    incomplete_cloud = any(record.get("config", {}).get("source_kind") in ("cloud", "golden") and record.get("phase") != "ready"
                           for record in api.store.all())
    items = []
    for folder in ("downloads", "converted", "tools"):
        directory = api.store.root / folder
        if directory.is_symlink():
            continue
        for path in sorted(directory.glob("*")):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"(?:[0-9a-f]{64}|[0-9a-f]{128}|source-[0-9a-f]{64})\.(?:tar\.xz|tar\.gz|qcow2\.xz|qcow2|raw|img|iso|disk\.img)", path.name):
                continue
            reason = "Used by VM " + references[str(path.resolve())] if str(path.resolve()) in references else ""
            if folder == "converted" and incomplete_cloud:
                reason = "An incomplete cloud build may need this converted disk"
            items.append({"id": f"{folder}/{path.name}", "kind": folder, "name": path.name,
                          **usage(path), "protected": bool(reason), "reason": reason})
    backend = api.backend_factory()
    for record in api.store.all():
        if not record.get("owned"):
            continue
        name = record["config"]["name"]
        try:
            size = usage(vm_directory(backend, name, record["identity"]))
            items.append({"id": "vm/" + name, "kind": "vm", "name": name, **size,
                          "protected": True, "reason": "Manage this disk from its VM card"})
        except (DeploymentError, OSError, KeyError):
            pass
    for store, kind in ((api.images, "golden"), (api.recoveries, "checkpoint")):
        for record in store.all():
            name = record["config"]["name"]
            try:
                size = usage(vm_directory(backend, record["tart_name"], record["identity"]))
            except (DeploymentError, OSError, KeyError):
                size = {"logical_bytes": 0, "allocated_bytes": 0}
            reasons = golden_references(api, name) if kind == "golden" else ["Manage recovery points from the VM details"]
            items.append({"id": kind + "/" + name, "kind": kind, "name": record.get("label", name),
                          **size, "protected": bool(reasons), "reason": "; ".join(reasons)})
    disk = shutil.disk_usage(api.store.root)
    return {"items": items, "free_bytes": disk.free, "total_bytes": disk.total,
            "allocated_bytes": sum(item["allocated_bytes"] for item in items),
            "note": "Allocated sizes may count APFS shared blocks more than once. Cleanup may reclaim less than the listed size. Tart registry caches and external VMs are excluded."}


def golden_references(api, name):
    reasons = []
    image = api.images.get(name)
    tart_name = image.get("tart_name") if image else None
    records = [*api.store.all(), *api.profiles.all()]
    records += [item["record"] for item in api.recoveries.all()]
    for record in records:
        config = record["config"]
        if (config.get("source_kind") == "golden" and config.get("source") == name) or (tart_name and (config.get("source") == tart_name or record.get("artifact") == tart_name)):
            reasons.append("Referenced by " + config["name"])
    return reasons


def cleanup(api, identifiers, confirmation, report=print, *, cache_only=False):
    if type(cache_only) is not bool:
        raise DeploymentError("cache_only must be true or false.")
    if confirmation != "DELETE" or not isinstance(identifiers, list) or not identifiers or (not cache_only and len(identifiers) > 100) or not all(isinstance(i, str) for i in identifiers):
        raise DeploymentError("Select unused cache files and confirm deletion." if cache_only else "Select up to 100 unused storage items and type DELETE to confirm.")
    with guard(api.store.root, exclusive=True):
        available = {item["id"]: item for item in listing(api)["items"]}
        selected = []
        kinds = {"downloads", "converted", "tools"} if cache_only else {"downloads", "converted", "tools", "golden"}
        for identifier in dict.fromkeys(identifiers):
            item = available.get(identifier)
            if not item or item["protected"] or item["kind"] not in kinds:
                raise DeploymentError("This storage item is protected or unavailable: " + identifier)
            selected.append(item)
        # All selections are checked before deleting any item.
        from contextlib import ExitStack
        with ExitStack() as locks:
            for item in selected:
                if item["kind"] == "golden":
                    backend = api.backend_factory(report)
                    record = api.images.get(item["name"])
                    vm_directory(backend, record["tart_name"], record["identity"])
                    if any(i["Name"] == record["tart_name"] and i["Running"] for i in backend.inventory()):
                        raise DeploymentError("A golden image is running outside AppleTart; it cannot be removed.")
                else:
                    folder, filename = item["id"].split("/")
                    checksum = filename.split(".", 1)[0]
                    lock = api.store.root / folder / (checksum + (".convert.lock" if folder == "converted" else ".download.lock"))
                    locks.enter_context(resource_lock(lock))
                    path = api.store.root / folder / filename
                    if path.is_symlink() or not path.is_file():
                        raise DeploymentError("The cached file changed. Refresh Storage.")
                    selected_stamp = path.with_suffix(".sha256")
                    if (folder == "converted" or filename.startswith("source-")) and selected_stamp.is_symlink():
                        raise DeploymentError("The cached image checksum is a symlink; cleanup is blocked.")
            for item in selected:
                if item["kind"] == "golden":
                    record = api.images.get(item["name"])
                    backend.run(["delete", record["tart_name"]])
                    api.images.remove(item["name"])
                else:
                    path = api.store.root / item["id"]
                    path.unlink()
                    if item["kind"] == "converted" or path.name.startswith("source-"):
                        path.with_suffix(".sha256").unlink(missing_ok=True)
                report("Removed " + item["id"])
