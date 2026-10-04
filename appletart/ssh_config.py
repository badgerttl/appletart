"""Add or update one VM's SSH entry while preserving user configuration."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shlex
import stat
import uuid

from .deployment import DeploymentError, vm_name
from .ip_resolver import address
from .operations import resource_lock
from .ssh import host_key_file, private_identity_paths


def _path_value(path):
    value = str(path)
    if any(character in value for character in ("\n", "\r", "\0")):
        raise DeploymentError("SSH configuration paths cannot contain newlines.")
    # OpenSSH expands percent tokens in IdentityFile and UserKnownHostsFile.
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def entry(root, machine, ip):
    name = vm_name(machine.vm.name)
    ip = address(ip)
    known = host_key_file(root, name, migrate_legacy=True)
    lines = [f"# BEGIN APPLETART SSH CONFIG: {name}", f"Host {name}",
             f"    HostName {ip}", "    Port 22", f"    User {machine.vm.ssh_user}"]
    for identity in private_identity_paths(machine.vm):
        lines.append("    IdentityFile " + _path_value(identity))
    lines.extend([f"    HostKeyAlias appletart-{name}", "    UserKnownHostsFile " + _path_value(known),
                  "    StrictHostKeyChecking accept-new", "    ConnectTimeout 3", "    ConnectionAttempts 1",
                  "    ServerAliveInterval 5", "    ServerAliveCountMax 2",
                  # Restore global scope before the user's original directives.
                  "Host *", f"# END APPLETART SSH CONFIG: {name}", "", ""])
    return "\n".join(lines)


def _replace_entry(content, name, block):
    begin = list(re.finditer(r"(?m)^# BEGIN APPLETART SSH CONFIG: " + re.escape(name) + r"\r?$", content))
    end = list(re.finditer(r"(?m)^# END APPLETART SSH CONFIG: " + re.escape(name) + r"(?:\r?\n|$)(?:\r?\n)?", content))
    if begin or end:
        if len(begin) != 1 or len(end) != 1 or end[0].start() <= begin[0].start():
            raise DeploymentError(f"The AppleTart SSH block for {name} has damaged or duplicate markers. Repair its markers before updating it.")
        before, after = content[:begin[0].start()], content[end[0].end():]
    else:
        before, after = "", content
    for line in (before + after).splitlines():
        host = re.match(r"\s*Host(?:\s+|\s*=\s*)(.*)$", line, flags=re.IGNORECASE)
        if host:
            try:
                aliases = shlex.split(host[1], comments=True)
            except ValueError as error:
                raise DeploymentError("Cannot parse an existing SSH Host directive. Check ~/.ssh/config before updating it.") from error
            if name.lower() in (alias.lower() for alias in aliases):
                raise DeploymentError(f"An SSH Host entry named {name} already exists outside AppleTart's managed block. The existing configuration was preserved.")
    return before + block + after


def _file_state(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise DeploymentError("The SSH configuration must be a regular file.")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _private_write(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def save(root, machine, ip):
    """The supplied machine and IP come from the saved, verified VM record."""
    name = vm_name(machine.vm.name)
    block = entry(root, machine, ip)
    config = Path.home() / ".ssh" / "config"
    temporary = None
    try:
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with resource_lock(config.parent / ".appletart-config.lock"):
            # Preserve a user's dotfile symlink by atomically updating its target.
            target = config.resolve()
            state = _file_state(target)
            original = target.read_bytes() if state is not None else b""
            updated = _replace_entry(original.decode("utf-8"), name, block).encode("utf-8")
            result = {"host": name, "ip": address(ip), "command": "ssh " + name,
                      "path": str(config), "changed": updated != original, "backup": ""}
            if updated == original:
                return result
            temporary = target.with_name(".appletart-config-" + uuid.uuid4().hex + ".tmp")
            _private_write(temporary, updated)
            if state is not None:
                backups = config.parent / "appletart-backups"
                backups.mkdir(exist_ok=True, mode=0o700)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                backup = backups / ("config-" + stamp + "-" + uuid.uuid4().hex)
                _private_write(backup, original)
                result["backup"] = str(backup)
            if config.resolve() != target or _file_state(target) != state:
                raise DeploymentError("SSH configuration changed while saving. Retry to preserve the latest changes.")
            temporary.replace(target)
            return result
    except (OSError, UnicodeError, RuntimeError) as error:
        raise DeploymentError(f"Cannot update ~/.ssh/config: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
