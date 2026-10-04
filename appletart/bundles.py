"""Named package selections with compatibility checks and frozen VM snapshots."""

import json
from pathlib import Path
import re
import uuid

from .deployment import DeploymentError, vm_name
from . import image_catalog


def packages(value):
    if not isinstance(value, list) or len(value) > 500 or not all(isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.@:/=-]{0,127}", p) for p in value):
        raise DeploymentError("Provide up to 500 package names, without whitespace, options or shell commands.")
    return list(dict.fromkeys(value))


def validate(value):
    if not isinstance(value, dict) or set(value) - {"id", "name", "platforms", "packages"}:
        raise DeploymentError("A bundle contains a name, platforms and package names.")
    name = value.get("name", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or "\0" in name:
        raise DeploymentError("Give the software bundle a name of up to 100 characters.")
    platforms = value.get("platforms", [])
    if not isinstance(platforms, list) or not platforms or not all(isinstance(p, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", p) for p in platforms):
        raise DeploymentError("Select compatible platforms, linux, macos, or all.")
    return {"id": vm_name(value.get("id") or "bundle-" + uuid.uuid4().hex[:12]), "name": name.strip(),
            "platforms": list(dict.fromkeys(platforms)), "packages": packages(value.get("packages", []))}


def listing(root):
    path = Path(root) / "software-bundles.json"
    try:
        values = json.loads(path.read_text()) if path.exists() else json.loads((image_catalog.DATA / "bundles.json").read_text())
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("invalid bundle list")
        return [validate(value) for value in values]
    except (OSError, ValueError) as error:
        raise DeploymentError(f"Cannot read software bundles: {error}") from error


def _write(root, values):
    path = Path(root) / "software-bundles.json"
    temporary = path.with_name(f".bundles-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(values, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save(root, value):
    value = validate(value)
    values = listing(root)
    if any(v["id"] != value["id"] and v["name"].casefold() == value["name"].casefold() for v in values):
        raise DeploymentError("A software bundle already has that name.")
    values = [v for v in values if v["id"] != value["id"]] + [value]
    if len(values) > 100:
        raise DeploymentError("Keep at most 100 software bundles.")
    _write(root, values)
    return value


def remove(root, identifier):
    values = listing(root)
    if not any(v["id"] == identifier for v in values):
        raise DeploymentError("This software bundle no longer exists.")
    _write(root, [v for v in values if v["id"] != identifier])


def resolve(root, identifiers, platform, family):
    if not isinstance(identifiers, list) or len(identifiers) > 100 or not all(isinstance(i, str) for i in identifiers):
        raise DeploymentError("Software bundles must be a list of bundle identifiers.")
    available = {v["id"]: v for v in listing(root)}
    result = []
    for identifier in dict.fromkeys(identifiers):
        value = available.get(identifier)
        if not value:
            raise DeploymentError(f"Software bundle {identifier} no longer exists. Update the profile's selections.")
        if not set(value["platforms"]).intersection({platform, family, "all"}):
            raise DeploymentError(f"Software bundle {value['name']} is not compatible with {platform}.")
        result.extend(value["packages"])
    return packages(list(dict.fromkeys(result)))


def legacy_packages():
    values = json.loads((image_catalog.DATA / "bundles.json").read_text())
    return tuple(next(v["packages"] for v in values if v["id"] == "kali-essentials"))
