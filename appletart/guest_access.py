"""Agent-first guest management, bounded SSH fallback and health checks."""

import json
import re
from pathlib import Path
import shlex
import socket
import subprocess
import time

from . import forwarder, settings, guest_agent
from .deployment import DeploymentError
from .ip_resolver import address, resolve
from .operations import JobCancelled, run
from .ssh import host_key_file, ssh_options


def ssh_args(root, machine, ip, *, batch=False):
    ip = address(ip)
    args = ssh_options(machine.vm, known_hosts=host_key_file(root, machine.vm.name, migrate_legacy=True),
                       batch=batch, accept_new=not batch)
    return [*args, f"{machine.vm.ssh_user}@{ip}"]


def connection(root, machine, ip):
    return {"ip": ip, "user": machine.vm.ssh_user,
            "command": shlex.join(ssh_args(root, machine, ip))}


def management_command(root, machine, record, backend, command, *, stdin=False, report=print):
    """Choose a transport before executing an operation; never retry a mutation."""
    if guest_agent.root_available(backend, machine.vm.name):
        report("Using privileged guest-agent management over VSOCK. No guest IP or SSH login is required.")
        return guest_agent.exec_args(backend, machine.vm.name, command, stdin=stdin), "agent"
    report("Privileged guest-agent execution is unavailable; using the saved SSH identity. Upgrade the guest agent to manage this VM without SSH.")
    ip = resolve(backend, machine.vm, wait=2, agent_installed=bool(record.get("guest_agent_version")))
    return [*ssh_args(root, machine, ip, batch=True), shlex.join(["sudo", "-n", *command])], "ssh"


def open_terminal(root, machine, ip):
    identifier = settings.load(root)["terminal"]
    app = settings.application(identifier)
    label = settings.TERMINALS[identifier][0]
    # The selected app opens a private command file using macOS Launch Services.
    directory = root / "connections"
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / f"{machine.vm.name}.command"
    path.write_text("#!/bin/sh\nexec " + shlex.join(ssh_args(root, machine, ip)) + "\n")
    path.chmod(0o700)
    result = run(["open", "-a", str(app), str(path)], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise DeploymentError(f"Could not open {label}: " + result.stderr[-500:])
    return label


def macos_details(interfaces, mounts):
    """Parse the native tools available in macOS templates, including older releases."""
    result = []
    current = None
    for line in interfaces.splitlines():
        match = re.match(r"^([\w.:-]+): flags=.*<([^>]+)>", line)
        if match:
            current = {"name": match[1], "state": "UP" if "UP" in match[2].split(",") else "DOWN", "addresses": []}
            result.append(current)
        elif current:
            match = re.match(r"\s+inet ([0-9.]+)\s", line)
            if match:
                current["addresses"].append(match[1])
    mounted = {}
    for line in mounts.splitlines():
        match = re.match(r".* on (.+) \(([^)]+)\)$", line)
        if match and "virtiofs" in match[2].split(", "):
            mounted[match[1]] = {"options": match[2]}
    return result, mounted


def health(root, machine, record, backend, *, details=False):
    result = {"status": "booting", "checked_at": time.time(), "ip": "", "ssh_ready": False, "ssh_available": False,
              "agent": "unavailable", "agent_version": record.get("guest_agent_version", ""),
              "interfaces": [], "shares": [], "issues": [], "management_transport": "unavailable", "agent_privileged": False,
              "forwarding": "active" if forwarder.status(root, machine.vm.name) else "inactive"}
    try:
        agent_ip = address(backend.run(["ip", machine.vm.name, "--wait", "1", "--resolver", "agent"], capture=True, timeout=4))
        result.update(ip=agent_ip, agent="responding")
    except JobCancelled:
        raise
    except DeploymentError:
        try:
            result["ip"] = resolve(backend, machine.vm, wait=1, agent_installed=bool(result["agent_version"]))
        except DeploymentError as error:
            result["issues"].append(str(error))
    root_agent = guest_agent.root_available(backend, machine.vm.name) if details or record.get("guest_agent_privileged") else False
    if root_agent:
        result.update(agent="responding", agent_privileged=True, management_transport="agent", status="agent-ready")
    if result["ip"]:
        if not root_agent:
            result["status"] = "running"
        try:
            with socket.create_connection((result["ip"], 22), timeout=1) as sock:
                sock.settimeout(1)
                result["ssh_available"] = sock.recv(256).startswith(b"SSH-")
        except OSError:
            pass
        if not result["ssh_available"]:
            result["issues"].append("SSH has not responded yet. Wait for boot, or check the VM log and SSH service.")
    if result["agent"] != "responding":
        result["issues"].append("Guest agent is unavailable. If this persists, use Install/Repair guest agent.")
    if machine.port_forwards and result["forwarding"] != "active":
        result["issues"].append("Port forwarding is inactive. Check the Mac listener addresses and forwarding log.")
    for share in machine.vm.directory_shares:
        exists = Path(share.host_path).is_dir()
        result["shares"].append({**share.config(), "host_available": exists, "mounted": None})
        if not exists:
            result["issues"].append(f"Shared Mac directory is missing: {share.host_path}")
    if result["ssh_available"] and (machine.vm.ssh_public_keys or not machine.cloud_setup):
        try:
            probe = run([*ssh_args(root, machine, result["ip"], batch=True), "true"], capture_output=True, text=True, timeout=10)
            if probe.returncode:
                raise DeploymentError("SSH is responding but key authentication is not ready: " + probe.stderr[-500:])
            result.update(ssh_ready=True, status="ssh-ready")
            if not root_agent:
                result["management_transport"] = "ssh"
        except JobCancelled:
            raise
        except (DeploymentError, OSError, subprocess.TimeoutExpired) as error:
            result["issues"].append(str(error))
    if details and (root_agent or result["ssh_ready"]):
        if root_agent and machine.guest_family == "linux":
            from .guest_os import observe
            result["guest_os"] = observe(backend, machine.vm.name)
        script = "ip -j -4 address\nprintf '\\nAPPLETART_MOUNTS\\n'\nfindmnt -J -t virtiofs -o TARGET,FSTYPE,OPTIONS\n"
        if machine.guest_family == "macos":
            script = "/sbin/ifconfig -a\nprintf '\\nAPPLETART_MOUNTS\\n'\n/sbin/mount\n"
        command = guest_agent.exec_args(backend, machine.vm.name, ["/bin/sh", "-s"], stdin=True) if root_agent else [*ssh_args(root, machine, result["ip"], batch=True), "sh -s"]
        try:
            probe = run(command, input=script, capture_output=True, text=True, timeout=10)
            if probe.returncode not in (0, 1) or "APPLETART_MOUNTS" not in probe.stdout:
                raise DeploymentError("Guest details could not be checked: " + probe.stderr[-500:])
            interfaces, mounts = probe.stdout.split("\nAPPLETART_MOUNTS\n", 1)
            if machine.guest_family == "macos":
                result["interfaces"], mounted = macos_details(interfaces, mounts)
            else:
                result["interfaces"] = [{"name": item["ifname"], "state": item.get("operstate", "UNKNOWN"),
                    "addresses": [a["local"] for a in item.get("addr_info", []) if a.get("family") == "inet"]}
                    for item in json.loads(interfaces)]
                mounted = {item["target"]: item for item in json.loads(mounts or "{}").get("filesystems", [])}
            for share in result["shares"]:
                mount = mounted.get(share["guest_path"])
                share["mounted"] = bool(mount)
                share["read_only_verified"] = bool(set(mount.get("options", "").replace(" ", "").split(",")) & {"ro", "read-only"}) if mount else False
                if not mount:
                    result["issues"].append(f"Guest share is not mounted: {share['guest_path']}")
                elif share["read_only"] and not share["read_only_verified"]:
                    result["issues"].append(f"Guest mount is writable although read-only was requested: {share['guest_path']}")
        except JobCancelled:
            raise
        except (DeploymentError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            result["issues"].append(str(error))
    return result
