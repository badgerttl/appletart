"""Editable image definitions; provisioning never dispatches on distro names."""

from copy import deepcopy
import json
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit

from .deployment import DeploymentError

DATA = Path(__file__).parent / "data"


ICONS = {"ubuntu", "kali", "rhel", "fedora", "debian", "rocky", "macos", "other"}


def _source(value, kind):
    if not isinstance(value, str) or len(value) > 4096 or "\0" in value:
        raise DeploymentError("Catalog image sources must be text of up to 4096 characters.")
    if not value:
        return  # Custom profiles request their source during deployment.
    if kind == "tart":
        if value.startswith("-") or any(c.isspace() or not c.isprintable() for c in value) or "://" in value:
            raise DeploymentError("Tart sources must be OCI references or local template names.")
    elif "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise DeploymentError("Cloud and ISO downloads require HTTPS without credentials.")


def _hashes(image):
    for field, count in (("sha256", 64), ("sha512", 128)):
        value = image.get(field, "")
        if not isinstance(value, str) or (value and not re.fullmatch(r"[a-fA-F0-9]{%d}" % count, value)):
            raise DeploymentError(f"Catalog {field} must contain {count} hexadecimal characters.")
    if image.get("sha256") and image.get("sha512"):
        raise DeploymentError("Use either SHA256 or SHA512 in an image definition.")


def _minimum_disk(image):
    value = image.get("minimum_disk_gb", 1)
    if type(value) is not int or not 1 <= value <= 65535:
        raise DeploymentError("Minimum image disk size must be an integer from 1 to 65535 GB.")


def validate(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 100:
        raise DeploymentError("An image catalog needs 1–100 platform definitions.")
    value = deepcopy(value)
    for identifier, image in value.items():
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier) or identifier == "windows":
            raise DeploymentError("Platform identifiers must be lowercase names; Windows is unsupported.")
        if not isinstance(image, dict) or set(image) - {"label", "family", "source_kind", "source", "sha256", "sha512", "ssh_user", "bootstrap_password", "description", "versions", "icon", "minimum_disk_gb"}:
            raise DeploymentError(f"Invalid image definition: {identifier}.")
        image.setdefault("source", "")
        if image.get("family") not in ("linux", "macos"):
            raise DeploymentError("Image family must be linux or macos.")
        if image.get("source_kind") not in ("tart", "cloud", "iso") or (image["family"] == "macos" and image["source_kind"] != "tart"):
            raise DeploymentError("macOS uses Tart templates; Linux supports Tart, cloud and ISO sources.")
        for key in ("label", "source", "ssh_user", "description", "bootstrap_password", "icon", "sha256", "sha512"):
            if key in image and (not isinstance(image[key], str) or len(image[key]) > 4096 or "\0" in image[key]):
                raise DeploymentError(f"Invalid catalog field: {key}.")
        if not image.get("label"):
            raise DeploymentError("Every platform needs a label.")
        _source(image.get("source", ""), image["source_kind"])
        _hashes(image)
        if image.get("icon", "other") not in ICONS:
            raise DeploymentError("Choose a shipped platform icon: " + ", ".join(sorted(ICONS)))
        if image.get("ssh_user") and not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", image["ssh_user"]):
            raise DeploymentError("Invalid template login username.")
        _minimum_disk(image)
        versions = image.get("versions", [])
        if not isinstance(versions, list) or len(versions) > 2000:
            raise DeploymentError("Image versions must be a list with at most 2000 entries.")
        for version in versions:
            if not isinstance(version, dict) or set(version) - {"label", "source", "source_kind", "sha256", "sha512", "minimum_disk_gb"} or not all(isinstance(version.get(k), str) and version[k] for k in ("label", "source")):
                raise DeploymentError("Every image version needs a label and source.")
            kind = version.get("source_kind", image["source_kind"])
            if kind not in ("tart", "cloud", "iso") or (image["family"] == "macos" and kind != "tart"):
                raise DeploymentError("Invalid version source type.")
            if len(version["label"]) > 200 or "\0" in version["label"]:
                raise DeploymentError("Version labels must be at most 200 characters.")
            _source(version["source"], kind)
            _hashes(version)
            _minimum_disk(version)
    return value


def load(root=None):
    path = Path(root) / "image-catalog.json" if root else DATA / "images.json"
    if root and not path.exists():
        path = DATA / "images.json"
    try:
        return validate(json.loads(path.read_text()))
    except (OSError, ValueError) as error:
        raise DeploymentError(f"Cannot read the image catalog: {error}") from error


def save(root, value):
    value = validate(value)
    path = Path(root) / "image-catalog.json"
    temporary = path.with_name(f".catalog-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def defaults(identifier, root=None):
    return load(root).get(identifier, {"label": identifier, "family": "linux", "source_kind": "cloud", "source": "", "ssh_user": "", "sha256": ""})
