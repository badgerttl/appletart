"""Local preferences for known terminal applications, without custom shell commands."""

import json
from pathlib import Path
import uuid

from .deployment import DeploymentError


TERMINALS = {
    "terminal": ("Terminal", "Terminal.app"),
    "iterm2": ("iTerm2", "iTerm.app"),
    "warp": ("Warp", "Warp.app"),
}


def application(identifier):
    if identifier not in TERMINALS:
        raise DeploymentError("Choose a supported SSH terminal.")
    filename = TERMINALS[identifier][1]
    for directory in (Path("/Applications"), Path.home() / "Applications", Path("/System/Applications/Utilities")):
        path = directory / filename
        if path.is_dir():
            return path
    raise DeploymentError(f"{TERMINALS[identifier][0]} is not installed. Choose another terminal in Settings.")


def load(root):
    path = root / "settings.json"
    try:
        value = json.loads(path.read_text()) if path.exists() else {"terminal": "terminal"}
        if not isinstance(value, dict) or set(value) - {"terminal", "default_public_key", "install_ssh_key_by_default"} or value.get("terminal") not in TERMINALS:
            raise ValueError("invalid preferences")
        if "install_ssh_key_by_default" in value and type(value["install_ssh_key_by_default"]) is not bool:
            raise ValueError("invalid key installation preference")
        return value
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise DeploymentError("Cannot read SSH terminal settings. Choose and save a terminal in Settings.") from error


def listing(root):
    # The settings page remains usable if a saved application was uninstalled.
    try:
        preferences = load(root)
        selected = preferences["terminal"]
    except DeploymentError:
        selected = "terminal"
        preferences = {}
    options = []
    for identifier, (label, _) in TERMINALS.items():
        try:
            application(identifier)
            installed = True
        except DeploymentError:
            installed = False
        options.append({"id": identifier, "label": label, "installed": installed})
    return {"terminal": selected, "terminals": options, "default_public_key": preferences.get("default_public_key", "~/.ssh/id_ed25519.pub"),
            "install_ssh_key_by_default": preferences.get("install_ssh_key_by_default", True)}


def save(root, value):
    if not isinstance(value, dict) or set(value) - {"terminal", "default_public_key", "install_ssh_key_by_default"} or not isinstance(value.get("terminal"), str):
        raise DeploymentError("Settings contain a terminal choice and optional default public key.")
    try:
        preferences = load(root)
    except DeploymentError:
        preferences = {}
    public = value.get("default_public_key", preferences.get("default_public_key", "~/.ssh/id_ed25519.pub"))
    if not isinstance(public, str) or len(public) > 4096 or "\0" in public or (public and not public.endswith(".pub")):
        raise DeploymentError("Select a .pub file for the default SSH public key.")
    install = value.get("install_ssh_key_by_default", preferences.get("install_ssh_key_by_default", True))
    if type(install) is not bool:
        raise DeploymentError("The default SSH key installation option must be true or false.")
    value = {**value, "default_public_key": public, "install_ssh_key_by_default": install}
    application(value["terminal"])
    temporary = root / f".settings-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x") as file:
            temporary.chmod(0o600)
            json.dump(value, file, indent=2)
            file.write("\n")
        temporary.replace(root / "settings.json")
    finally:
        temporary.unlink(missing_ok=True)
    return listing(root)
