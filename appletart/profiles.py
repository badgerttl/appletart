"""Portable deployment recipes containing configuration, never credentials."""

from datetime import datetime, timezone

from .catalog import Machine
from .deployment import DeploymentError, vm_name


def text(value, limit=4000):
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise DeploymentError(f"Text must be at most {limit} characters and contain no null bytes.")
    return value.strip()


def save(api, name, config, notes="", *, create_only=False):
    name = vm_name(name)
    config = dict(config)
    config.pop("bundle_packages", None)
    notes = text(notes)
    # Bundle validation and reference publication share the deletion lock.
    with api.operation("settings", "profile-" + name):
        machine = api.machine(config)
        old = api.profiles.get(name)
        if old and create_only:
            raise DeploymentError("A profile already uses this name. Rename it in the imported JSON first.")
        now = datetime.now(timezone.utc).isoformat()
        template = {**machine.config(), "name": name}
        if machine.cloud_setup:
            template["hostname"] = ""
        record = {"format": "appletart-profile", "schema_version": 1, "config": template,
                  "notes": notes, "created_at": old["created_at"] if old else now, "updated_at": now}
        api.profiles.put(record)
        return record


def import_profile(api, value):
    if not isinstance(value, dict) or set(value) - {"format", "schema_version", "name", "config", "notes", "created_at", "updated_at"}:
        raise DeploymentError("Expected an AppleTart profile containing only configuration and notes.")
    if value.get("format") != "appletart-profile" or value.get("schema_version") != 1:
        raise DeploymentError("This profile format/version is unsupported.")
    config = value.get("config")
    if not isinstance(config, dict):
        raise DeploymentError("Profile configuration must be an object.")
    name = vm_name(value.get("name") or config.get("name"))
    return save(api, name, config, value.get("notes", ""), create_only=True)


def remove(api, name, confirmation):
    name = vm_name(name)
    if confirmation != name:
        raise DeploymentError("Type the profile name to confirm deletion.")
    with api.operation("profile-" + name):
        if not api.profiles.get(name):
            raise DeploymentError("This profile no longer exists.")
        api.profiles.remove(name)


def deployment(api, profile_name, name):
    """Resolve a saved recipe for a new VM, with a fresh guest hostname."""
    name = vm_name(name)
    profile = api.profiles.get(vm_name(profile_name))
    if not profile:
        raise DeploymentError("This deployment profile no longer exists.")
    if api.store.get(name):
        raise DeploymentError("That VM name is already in use. Choose a new name.")
    config = {**profile["config"], "name": name}
    if config.get("source_kind") in {"cloud", "golden"}:
        config["hostname"] = ""
    config.pop("bundle_packages", None)
    return api.machine(config)
