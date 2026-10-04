"""Install public keys using an existing guest login, without exporting secrets."""

import ipaddress
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from contextlib import contextmanager

from .deployment import DeploymentError, VM, vm_name
from . import diagnostics
from .operations import JobCancelled, checkpoint, pause, resource_lock, run, stop_build, stream


def read_public_keys(paths: tuple[Path, ...]) -> list[str]:
    keys = []
    for path in paths:
        if path.suffix != ".pub":
            raise DeploymentError(f"SSH key {path}: select a .pub public key file.")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise DeploymentError(f"Cannot read public key {path}.") from error
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if len(lines) != 1 or not lines[0].startswith(("ssh-", "ecdsa-", "sk-")) or "PRIVATE KEY" in content:
            raise DeploymentError(f"SSH key {path}: expected one OpenSSH public key.")
        try:
            subprocess.run(["ssh-keygen", "-l", "-f", str(path)], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise DeploymentError(f"SSH key {path}: invalid public key or ssh-keygen unavailable.") from error
        # Normalize to key type + blob; comments are not part of the key identity.
        fields = lines[0].split()
        key = " ".join(fields[:2])
        if key not in keys:
            keys.append(key)
    return keys


def supported_public_key_types() -> set[str]:
    """Ask the host's OpenSSH which host-key formats it can verify."""
    try:
        result = subprocess.run(["ssh", "-Q", "key"], check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise DeploymentError("Cannot query supported SSH host-key algorithms on this Mac.") from error
    algorithms = set(result.stdout.splitlines())
    if not algorithms:
        raise DeploymentError("OpenSSH did not report any supported host-key algorithms.")
    return algorithms


def install_script(keys: list[str]) -> str:
    script = 'set -eu\numask 077\nmkdir -p "$HOME/.ssh"\nchmod 700 "$HOME/.ssh"\ntouch "$HOME/.ssh/authorized_keys"\nchmod 600 "$HOME/.ssh/authorized_keys"\n'
    for key in keys:
        # Match type + blob, including entries that already have comments.
        script += f"key={shlex.quote(key)}\n"
        script += 'if ! awk -v key="$key" \'$1 " " $2 == key { found=1 } END { exit !found }\' "$HOME/.ssh/authorized_keys"; then\n'
        # A preceding newline handles existing files lacking a final newline.
        script += '  printf \'\\n%s\\n\' "$key" >> "$HOME/.ssh/authorized_keys"\nfi\n'
    return script


def wait_for_ssh(tart, vm: VM, process, timeout: float = 120, *, report=None, boot_failure=None) -> str:
    deadline = time.monotonic() + timeout
    next_report = time.monotonic()
    while time.monotonic() < deadline:
        checkpoint()
        if boot_failure and (failure := boot_failure()):
            raise DeploymentError(f"{vm.name}: guest capability check failed: {failure}")
        if process.poll() is not None:
            raise DeploymentError(f"{vm.name}: Tart exited before SSH became ready.")
        address = None
        try:
            address = tart.run(vm.ip_args(2), capture=True).strip()
            ipaddress.ip_address(address)
            with socket.create_connection((address, 22), timeout=2) as connection:
                connection.settimeout(2)
                # Check for the SSH banner, rather than only an open port.
                if connection.recv(256).startswith(b"SSH-"):
                    if report:
                        report(f"SSH is responding at {address}.")
                    return address
            detail = f"Guest IP {address} is available; waiting for its SSH banner."
        except JobCancelled:
            raise
        except (DeploymentError, ValueError, OSError) as error:
            detail = f"Guest IP {address}; SSH is not ready: {error}" if address else str(error)
        if report and time.monotonic() >= next_report:
            resolver = "bridged ARP" if vm.network == "bridged" else "NAT DHCP"
            report(f"Waiting for SSH ({resolver}): {detail[:300]}")
            if vm.network == "bridged" and not address:
                report("No bridged guest IP has been discovered. Check the guest console log and LAN DHCP; you can cancel this build and retry on NAT.")
            next_report = time.monotonic() + 15
        pause(1)
    raise DeploymentError(f"{vm.name}: SSH was not ready within {timeout:g} seconds. Enable SSH in the template.")


def _prepare_host_key_file(path, legacy_alias=None):
    """Create private app-owned trust; optionally preserve an existing VM's trust."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with resource_lock(path.with_name(path.name + ".lock"), wait=True):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise DeploymentError("The VM's SSH host-key file must be a regular file.")
            if path.exists():
                path.chmod(0o600)
                return path
            trusted = ""
            legacy = Path.home() / ".ssh" / "known_hosts"
            if legacy_alias and legacy.is_file():
                result = run(["ssh-keygen", "-F", legacy_alias, "-f", str(legacy)],
                             capture_output=True, text=True, timeout=10)
                if result.returncode not in (0, 1):
                    raise DeploymentError("Cannot migrate this VM's existing SSH host-key trust.")
                trusted = "".join(line + "\n" for line in result.stdout.splitlines()
                                  if line and not line.startswith("#"))
            temporary = path.with_name(".known-hosts-" + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("x") as file:
                    temporary.chmod(0o600)
                    file.write(trusted)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return path
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError("Cannot prepare this VM's SSH host-key file.") from error


def host_key_file(root, name, *, migrate_legacy=False):
    if root is None:
        root = Path(os.environ.get("APPLETART_HOME", str(Path.home() / "Library" / "Application Support" / "AppleTart"))).expanduser()
    name = vm_name(name)
    return _prepare_host_key_file(Path(root) / "cloud-init" / name / "known_hosts",
                                  "appletart-" + name if migrate_legacy else None)


def known_hosts_option(path):
    value = str(path)
    if any(character.isspace() or character in '\\"' for character in value):
        value = json.dumps(value, ensure_ascii=False)
    return "UserKnownHostsFile=" + value


def private_identity_paths(vm):
    """Select local counterparts without reading private-key contents."""
    return tuple(identity for identity in dict.fromkeys(public.with_suffix("") for public in vm.ssh_public_keys)
                 if identity.is_file())


def ssh_options(vm, *, known_hosts, batch=False, accept_new=False, connect_timeout=3):
    """Use the same saved identities and trust policy for setup and management."""
    known_hosts = _prepare_host_key_file(known_hosts)
    args = ["ssh", "-o", f"ConnectTimeout={connect_timeout}", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=" + ("accept-new" if accept_new else "yes"),
            "-o", f"HostKeyAlias=appletart-{vm.name}", "-o", known_hosts_option(known_hosts)]
    if batch:
        args += ["-o", "BatchMode=yes"]
    for identity in private_identity_paths(vm):
        # OpenSSH reads the private identity; AppleTart only passes its path.
        args += ["-i", str(identity)]
    return args


@contextmanager
def password_environment(password):
    with tempfile.TemporaryDirectory(prefix="appletart-askpass-") as directory:
        env = dict(os.environ)
        if password:
            askpass = Path(directory) / "askpass"
            askpass.write_text('#!/bin/sh\nprintf \'%s\\n\' "$APPLETART_SSH_PASSWORD"\n')
            askpass.chmod(0o700)
            env.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force",
                       DISPLAY=env.get("DISPLAY", "appletart"), APPLETART_SSH_PASSWORD=password)
        yield env


def ssh_install(address: str, vm: VM, keys: list[str], password: str = "", *, batch: bool = False, extra_script: str = "", timeout: float = 120, report=None, known_hosts=None) -> None:
    ipaddress.ip_address(address)
    known_hosts = known_hosts if known_hosts is not None else host_key_file(None, vm.name)
    args = ssh_options(vm, known_hosts=known_hosts, batch=batch and not password,
                       accept_new=True, connect_timeout=10)
    if password:
        args += ["-o", "NumberOfPasswordPrompts=1"]
    with password_environment(password) as env:
        command = [*args, f"{vm.ssh_user}@{address}", "sh -s"]
        script = install_script(keys) + extra_script
        if report:
            stream(command, report, env=env, input=script, timeout=timeout, label="Guest provisioning")
        else:
            run(command, input=script, text=True, check=True, capture_output=True, env=env, timeout=timeout)


def provision_keys(tart, vm: VM, keys: list[str], password: str = "", *, batch: bool = False, agent_binary=None, extra_script="", report=None, log_path=None, family="linux", known_hosts=None, users=()):
    print(f"Booting {vm.name} to install {len(keys)} public key(s) for {vm.ssh_user}. SSH may ask for the existing guest password.")
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostics.console(log_path)
    with (log_path.open("a+", errors="replace") if log_path else tempfile.TemporaryFile(mode="w+")) as log:
        if log_path:
            log_path.chmod(0o600)
        try:
            setup_vm = replace(vm, network="nat", bridge="", bridges=()) if vm.network == "bridged" else vm
            process = subprocess.Popen([tart.binary, *setup_vm.run_args(headless=True)], stdout=log, stderr=log,
                                       env={**os.environ, "TART_NO_AUTO_PRUNE": "1"}, start_new_session=True)
        except OSError as error:
            raise DeploymentError(f"Cannot boot {vm.name}: {error}") from error
        try:
            address = wait_for_ssh(tart, setup_vm, process, **({"report": report} if report else {}))
            from .guest_agent import install_script as agent_script, root_available, exec_args, install_via_agent
            if agent_binary is not None and root_available(tart, vm.name):
                # Resume remains usable after a previous attempt retired the
                # bootstrap password but failed during later user setup.
                install_via_agent(tart, vm.name, None, report or print, binary=agent_binary, family=family)
                command = exec_args(tart, vm.name, ["/usr/bin/sudo", "-n", "-H", "-u", vm.ssh_user, "--", "/bin/sh", "-s"], stdin=True)
                stream(command, report or print, env={**os.environ, "TART_NO_AUTO_PRUNE": "1"},
                       input=install_script(keys) + extra_script, timeout=1800, label="Guest provisioning")
            else:
                ssh_install(address, vm, keys, password, batch=batch, known_hosts=known_hosts, extra_script=(agent_script(agent_binary) if agent_binary is not None else "") + extra_script, **({"timeout": 1800} if extra_script else {}), **({"report": report} if report else {}))
            if agent_binary is not None:
                from .ip_resolver import address as guest_address
                from .guest_agent import verify_management
                guest_address(tart.run(["ip", vm.name, "--wait", "5", "--resolver", "agent"], capture=True, timeout=8))
                verify_management(tart, vm.name)
                from .users import retire_template_password
                retire_template_password(exec_args(tart, vm.name, ["/bin/sh", "-s"], stdin=True), vm.ssh_user,
                                         report or print, **({"family": family} if family != "linux" else {}))
                if users:
                    from .users import install
                    from .guest_agent import exec_args
                    install(exec_args(tart, vm.name, ["/bin/sh", "-s"], stdin=True), users, report or print, family=family)
                if family == "linux":
                    from .guest_os import observe
                    return observe(tart, vm.name)
        except subprocess.CalledProcessError as error:
            raise DeploymentError(f"{vm.name}: Guest provisioning failed. Check the template login, guest agent and software installation output.") from error
        except OSError as error:
            raise DeploymentError(f"{vm.name}: cannot run SSH: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise DeploymentError(f"{vm.name}: Guest provisioning timed out. Check guest setup and package installation output.") from error
        finally:
            # Stop only the process this build started; never leave a build VM running.
            stop_build(process, vm.name)


def check_ssh_tools() -> None:
    if shutil.which("ssh") is None or shutil.which("ssh-keygen") is None:
        raise DeploymentError("SSH provisioning requires ssh and ssh-keygen on PATH.")
