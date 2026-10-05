"""Import cloud disks and provision a guest using a local NoCloud seed."""

import hashlib
import ipaddress
from dataclasses import replace
from contextlib import contextmanager
import json
import lzma
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import tty

from .deployment import DeploymentError
from .downloads import digest
from .ssh import known_hosts_option, read_public_keys, supported_public_key_types, wait_for_ssh
from . import capabilities
from .operations import checkpoint, pause, resource_lock, run, stop_build, uncancellable
from .guest_agent import install as install_agent, verify_management
from . import diagnostics, guest_agent, guest_os

CLOUD_SSH_TIMEOUT = 30 * 60
CLOUD_INIT_TIMEOUT = 30 * 60
CLOUD_CONFIG_VERSION = 11
CLOUD_AGENT_TIMEOUT = 90

SSH_SERVICE_SETUP = """set -eu
if systemctl cat ssh.service >/dev/null 2>&1; then
    systemctl enable --now ssh.service
elif systemctl cat sshd.service >/dev/null 2>&1; then
    systemctl enable --now sshd.service
else
    echo 'The cloud image must include an OpenSSH server (ssh.service or sshd.service).' >&2
    exit 1
fi
"""

# Netplan 1.2 renders backend configuration from netplan-configure.service
# instead of its systemd generator. A distribution preset can install that unit
# disabled during the initial upgrade (Kali does), leaving every later boot
# without network configuration. Enable it wherever the guest provides it.
NETWORK_BACKEND_SETUP = """set -eu
if systemctl cat netplan-configure.service >/dev/null 2>&1; then
    systemctl enable netplan-configure.service
fi
"""

# Older cloud-init releases unlink machine-id while cleaning an image. Debian
# starts with /etc read-only, so systemd cannot create the missing file and
# networkd fails to initialize its DHCP client. Recover already-saved images
# during cloud-init bootcmd, after the root filesystem is writable.
LEGACY_GOLDEN_IDENTITY_REPAIR = """set -eu
if ! grep -Eq '^[[:xdigit:]]{32}$' /etc/machine-id 2>/dev/null; then
    echo 'Repairing the machine ID from an older golden image and retrying networking.'
    if [ -d /var/lib/dbus ]; then
        ln -sf /etc/machine-id /var/lib/dbus/machine-id
    fi
    systemd-machine-id-setup
    if command -v netplan >/dev/null 2>&1; then
        netplan apply
    elif systemctl is-active --quiet systemd-networkd; then
        systemctl restart systemd-networkd
    elif systemctl is-active --quiet NetworkManager; then
        systemctl restart NetworkManager
    else
        systemctl restart networking
    fi
fi
"""


@contextmanager
def serial_console(log):
    """Retain guest console output alongside Tart's messages during setup."""
    master, slave = pty.openpty()
    tty.setraw(slave)
    done = threading.Event()
    def capture():
        while not done.is_set():
            try:
                if not select.select([master], [], [], 0.2)[0]:
                    continue
                data = os.read(master, 65536)
                if not data:
                    break
                log.write(data.decode(errors="replace"))
                log.flush()
            except OSError:
                break
    reader = threading.Thread(target=capture, daemon=True)
    reader.start()
    def close():
        if done.is_set():
            return
        done.set()
        reader.join(timeout=1)
        os.close(master)
        os.close(slave)
    try:
        yield os.ttyname(slave), close
    finally:
        close()


def check_cloud_tools():
    for tool in ("qemu-img", "hdiutil", "ssh", "ssh-keygen"):
        if shutil.which(tool) is None:
            raise DeploymentError(f"Cloud setup requires {tool}. Install the disk converter with: brew install qemu")


def run_tool(args, *, timeout=600):
    try:
        result = run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError(f"{args[0]} could not complete: {error}") from error
    if result.returncode:
        raise DeploymentError(f"{args[0]} failed: {(result.stderr or result.stdout)[-2000:]}")
    return result.stdout


def copy_bounded(source, destination: Path, limit: int):
    total = 0
    zeros = bytes(1024 * 1024)
    with destination.open("wb") as output:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checkpoint()
            total += len(block)
            if total > limit:
                raise DeploymentError("Expanded cloud image exceeds the requested VM disk capacity.")
            if block == zeros or (len(block) < len(zeros) and block == zeros[:len(block)]):
                output.seek(len(block), os.SEEK_CUR)
            else:
                output.write(block)
        output.truncate(total)


def disk_format(path: Path) -> str:
    """Inspect disk/container magic rather than trusting its filename."""
    try:
        with path.open("rb") as source:
            header = source.read(6)
            if header.startswith(b"QFI\xfb"):
                return "qcow2"
            return "xz" if header == b"\xfd7zXZ\x00" else "raw"
    except OSError as error:
        raise DeploymentError(f"Cannot inspect the cloud disk format: {error}") from error


def convert_disk(artifact: Path, cache: Path, disk_gb: int, report=print) -> Path:
    """Extract one regular disk file; never extract archive paths or backing files."""
    cache.mkdir(parents=True, exist_ok=True)
    # A new namespace prevents reuse of conversions made before dependency
    # validation, which may already contain data from an external host file.
    checksum = hashlib.sha256(b"appletart-conversion-v2\0" + bytes.fromhex(digest(artifact))).hexdigest()
    with resource_lock(cache / f"{checksum}.convert.lock", wait=True, report=report):
        return _convert_disk(artifact, cache, disk_gb, checksum, report)


def _convert_disk(artifact, cache, disk_gb, checksum, report):
    raw = cache / f"{checksum}.disk.img"
    stamp = raw.with_suffix(".sha256")
    if raw.exists() and stamp.exists() and disk_format(raw) == "raw" and digest(raw) == stamp.read_text().strip():
        if raw.stat().st_size > disk_gb * 1000**3:
            raise DeploymentError("The cloud disk is larger than the selected VM capacity.")
        report("Using verified converted cloud disk.")
        return raw
    report("Preparing the cloud disk for Tart…")
    limit = disk_gb * 1000**3
    partial = raw.with_suffix(".partial")
    try:
        with tempfile.TemporaryDirectory(dir=cache, prefix="convert-") as temp:
            source = artifact
            if artifact.name.lower().endswith(".tar.xz"):
                with tarfile.open(artifact, "r:xz") as archive:
                    members = [m for m in archive.getmembers() if m.isfile() and m.name.lower().endswith((".qcow2", ".raw", ".img"))]
                    if len(members) != 1 or members[0].size > limit:
                        raise DeploymentError("Cloud archive must contain one regular disk image within the selected disk capacity.")
                    source = Path(temp) / ("disk.qcow2" if members[0].name.lower().endswith(".qcow2") else "disk.raw")
                    with archive.extractfile(members[0]) as stream:
                        copy_bounded(stream, source, limit)
            elif artifact.name.lower().endswith(".qcow2.xz"):
                source = Path(temp) / "disk.qcow2"
                with lzma.open(artifact, "rb") as stream:
                    copy_bounded(stream, source, limit)
            fmt = disk_format(source)
            if fmt == "qcow2":
                validate_qcow2_dependencies(source)
            info = json.loads(run_tool(["qemu-img", "info", "-f", fmt, "--output=json", str(source)]))
            details = info.get("format-specific", {}).get("data", {})
            if info.get("backing-filename") or info.get("data-file") or details.get("data-file"):
                raise DeploymentError("Cloud disks with external backing or data files are unsupported.")
            if not 0 < info.get("virtual-size", 0) <= limit:
                raise DeploymentError("The cloud disk is larger than the selected VM capacity, or empty.")
            run_tool(["qemu-img", "convert", "-f", fmt, "-O", "raw", str(source), str(partial)])
        partial.replace(raw)
        stamp.write_text(digest(raw) + "\n")
        report("Cloud disk converted to Tart's raw format.")
        return raw
    except (OSError, ValueError, tarfile.TarError, lzma.LZMAError) as error:
        raise DeploymentError(f"Cannot prepare the cloud disk: {error}") from error
    finally:
        partial.unlink(missing_ok=True)


def validate_qcow2_dependencies(path):
    """Reject external file references before QEMU can open those files."""
    with path.open("rb") as image:
        header = image.read(104)
    version = int.from_bytes(header[4:8], "big")
    if len(header) < 72 or version not in (2, 3) or (version == 3 and len(header) < 104):
        raise DeploymentError("The QCOW2 header is invalid or unsupported.")
    backing_offset = int.from_bytes(header[8:16], "big")
    backing_length = int.from_bytes(header[16:20], "big")
    incompatible = int.from_bytes(header[72:80], "big") if version == 3 else 0
    # QCOW2 v3 incompatible feature bit 2 enables an external data file.
    if backing_offset or backing_length or incompatible & (1 << 2):
        raise DeploymentError("Cloud disks with external backing or data files are unsupported.")


def share_commands(machine) -> list[list[str]]:
    commands = []
    for index, share in enumerate(machine.vm.directory_shares):
        commands.extend([["mkdir", "-p", share.guest_path],
                         ["sh", "-c", f"mountpoint -q {shlex.quote(share.guest_path)} || timeout 30 " + shlex.join(share.mount_command(index))]])
    return commands


def cloud_config(machine, keys: list[str], mac: str = "", *, boot_only: bool = False) -> dict:
    if boot_only:
        # A fresh NoCloud instance reads changed mounts. Restrict its modules
        # so this edit cannot recreate accounts, rotate host keys or reinstall
        # software. Keep disk expansion working on subsequent resource edits.
        commands = [["sh", "-c", capabilities.cloud_network_setup(mac)]] if mac else []
        # A new instance ID makes init-local set the hostname from image
        # defaults (Kali ships "hostname: kali") before user-data is read.
        # set_hostname then reapplies this VM's name from the user-data.
        return {"hostname": machine.hostname, "fqdn": machine.hostname,
                "cloud_init_modules": ["bootcmd", "set_hostname", "growpart", "resizefs"],
                "cloud_config_modules": [], "cloud_final_modules": [],
                "ssh_deletekeys": False, "package_update": False, "package_upgrade": False,
                "growpart": {"mode": "auto", "devices": ["/"], "ignore_growroot_disabled": False},
                "resize_rootfs": True, "bootcmd": commands + share_commands(machine)}
    packages = list(machine.effective_packages)
    # Refresh the initial image once. Golden preparation and clones reuse the
    # installed software, contacting repositories only for additional packages.
    initial_build = machine.source_kind == "cloud"
    config = {
        "hostname": machine.hostname, "manage_etc_hosts": True,
        "users": [{"name": machine.vm.ssh_user, "shell": "/bin/bash",
                   "sudo": "ALL=(ALL) NOPASSWD:ALL", "lock_passwd": True,
                   "ssh_authorized_keys": keys}],
        "disable_root": True, "ssh_pwauth": False, "ssh_deletekeys": True,
        "growpart": {"mode": "auto", "devices": ["/"], "ignore_growroot_disabled": False},
        "resize_rootfs": True, "package_update": initial_build or bool(packages),
        "package_upgrade": initial_build, "package_reboot_if_required": False,
        "runcmd": [["sh", "-c", SSH_SERVICE_SETUP], ["sh", "-c", NETWORK_BACKEND_SETUP],
                   ["systemctl", "set-default", "multi-user.target"]],
    }
    if packages:
        config["packages"] = packages
    if machine.source_kind == "golden":
        config["bootcmd"] = [["sh", "-c", guest_agent.reuse_script()], ["sh", "-c", LEGACY_GOLDEN_IDENTITY_REPAIR]]
        # runcmd is per-instance; bootcmd would erase explicitly provisioned
        # passwords on every subsequent boot. Also sanitize older goldens.
        config["runcmd"].insert(0, ["sh", "-c", capabilities.GOLDEN_PASSWORD_CLEANUP])
    config.setdefault("bootcmd", []).append(["sh", "-c", capabilities.cloud_preflight(machine.vm.ssh_user)])
    if mac:
        config["bootcmd"].append(["sh", "-c", capabilities.cloud_network_setup(mac)])
    if machine.vm.directory_shares:
        # bootcmd runs on every boot; runcmd also verifies the initial mounts.
        mounts = share_commands(machine)
        config.setdefault("bootcmd", []).extend(mounts)
        config["runcmd"].extend(mounts)
    return config


def write_seed(machine, keys: list[str], directory: Path, instance_id: str, mac: str, *, boot_only: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    inputs = directory / "seed-data"
    inputs.mkdir(exist_ok=True, mode=0o700)
    # JSON is a YAML subset. It safely encodes user fields without a YAML dependency.
    (inputs / "user-data").write_text("#cloud-config\n" + json.dumps(cloud_config(machine, keys, mac, boot_only=boot_only), indent=2) + "\n")
    (inputs / "meta-data").write_text(json.dumps({"instance-id": instance_id, "local-hostname": machine.hostname}) + "\n")
    # Ubuntu's initramfs can bring up DHCP before cloud-init runs. Renaming
    # that live NIC fails and leaves network-online waiting on the old name.
    nic = {"match": {"macaddress": mac}, "dhcp4": True, "dhcp-identifier": "mac"}
    if len(machine.vm.bridges) > 1:
        # Tart gives every bridged adapter the same MAC. Configure all Ethernet
        # devices and keep their existing names.
        nic = {"match": {"name": "e*"}, "dhcp4": True, "dhcp-identifier": "mac"}
    (inputs / "network-config").write_text(json.dumps({"version": 2, "ethernets": {"appletart": nic}}) + "\n")
    partial = directory / "seed.partial.iso"
    seed = directory / "seed.iso"
    try:
        run_tool(["hdiutil", "makehybrid", "-iso", "-joliet", "-default-volume-name", "CIDATA",
                  "-o", str(partial), str(inputs), "-ov"])
        partial.replace(seed)
        seed.chmod(0o600)
        return seed
    finally:
        partial.unlink(missing_ok=True)


def bootstrap_key(directory: Path) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = directory / "bootstrap"
    if not key.exists():
        run_tool(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "appletart-build", "-f", str(key)])
    key.chmod(0o600)
    return key, read_public_keys((key.with_suffix(".pub"),))[0]


GUEST_DIAGNOSTICS = """export LC_ALL=C
guest_admin() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n "$@"; fi; }
printf '\\n=== Guest system ===\\n'
uname -a; cat /etc/os-release; uptime; df -h; free -m
printf '\\n=== Guest network ===\\n'
ip address; ip route
printf '\\n=== Failed services ===\\n'
systemctl --failed --no-pager
for f in /var/log/cloud-init.log /var/log/cloud-init-output.log /var/lib/cloud/data/status.json /var/lib/cloud/data/result.json /var/log/apt/term.log /var/log/apt/history.log /var/log/dpkg.log /var/log/dnf.log /var/log/dnf.rpm.log; do
    printf '\\n=== %s (up to last 4 MiB) ===\\n' "$f"
    if guest_admin test -f "$f"; then guest_admin tail -c 4194304 "$f"; else printf '(not present)\\n'; fi
done
printf '\\n=== Boot journal: setup, SSH, networking, guest agent (up to 2000 entries) ===\\n'
guest_admin journalctl -b --no-pager -o short-iso -n 2000 -u cloud-init-local -u cloud-init -u cloud-init-main -u cloud-init-network -u cloud-config -u cloud-final -u ssh -u sshd -u networking -u systemd-networkd -u systemd-networkd-wait-online -u systemd-user-sessions -u NetworkManager -u NetworkManager-wait-online -u appletart-guest-agent
"""


def capture_guest(args, report, *, command=None):
    """Snapshot setup evidence before cleaning cloud-init or revoking its key."""
    if not (journal := diagnostics.current()):
        return False
    journal.write("diagnostics", "Capturing guest cloud-init, package-manager and service logs before cleanup.")
    try:
        # Even a cancelled build gets a short opportunity to preserve its error.
        with uncancellable():
            result = run(command or [*args, "sh -s"], input=GUEST_DIAGNOSTICS, capture_output=True, text=True, timeout=12)
        journal.write("diagnostics", f"Guest snapshot finished with exit code {result.returncode}.")
        report("Guest setup diagnostics saved to this operation's detailed log.")
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as error:
        journal.write("diagnostics", f"Guest snapshot unavailable: {error}. Tart / serial console output is preserved.")
        return False


def completed_cloud_status(result) -> bool:
    """Exit 2 is usable only when structured status confirms completed setup."""
    if result.returncode not in (0, 2):
        return False
    try:
        status = json.loads(result.stdout)
    except (ValueError, TypeError):
        return False
    if isinstance(status, dict) and isinstance(status.get("v1"), dict):
        status = status["v1"]
        stages = [status.get(name) for name in ("init-local", "init", "modules-config", "modules-final")]
        return all(isinstance(stage, dict) and stage.get("finished") and not stage.get("errors") for stage in stages)
    if not isinstance(status, dict) or status.get("status") != "done" or status.get("errors") != []:
        return False
    for name in ("init-local", "init", "modules-config", "modules-final"):
        stage = status.get(name)
        if isinstance(stage, dict):
            if stage.get("errors") or ("finished" in stage and stage["finished"] is None):
                return False
    return True


GUEST_BUILD_HEALTH = """set -eu
printf '\\n=== Cloud build health ===\\n'
fail() { echo "APPLETART_HEALTH_ERROR: $*" >&2; exit 1; }
if ! ip -o -4 address show scope global | awk -v expected="$expected" '
    { split($4, address, "/"); if (expected == "" || address[1] == expected) found=1 }
    END { exit !found }'; then
    fail "No usable guest IPv4 address is configured${expected:+ (expected $expected)}."
fi
ip -4 route show default | grep -q '^default ' || fail 'The guest has no IPv4 default route.'
if [ "$(id -u)" != 0 ]; then sudo -n true || fail 'The management account lacks passwordless sudo.'; fi
if systemctl cat ssh.service >/dev/null 2>&1; then
    systemctl is-active --quiet ssh.service || fail 'The guest SSH service (ssh.service) is not active.'
else
    systemctl is-active --quiet sshd.service || fail 'The guest SSH service (sshd.service) is not active.'
fi
printf 'Guest account, sudo, SSH service, IPv4 address and default route are ready.\\n'
"""


def wait_for_build_agent(backend, machine, process, report):
    """Select the inherited agent before doing any guest setup or SSH login."""
    report(f"Booting the image. Waiting up to {CLOUD_AGENT_TIMEOUT} seconds for privileged guest-agent readiness…")
    deadline = time.monotonic() + CLOUD_AGENT_TIMEOUT
    while time.monotonic() < deadline:
        checkpoint()
        if process.poll() is not None:
            raise DeploymentError(f"{machine.vm.name}: Tart exited before the guest agent became ready. Check the boot log.")
        if guest_agent.root_available(backend, machine.vm.name):
            report("Using the inherited privileged guest agent over VSOCK. SSH bootstrap is not required.")
            return True
        pause(min(2, max(0, deadline - time.monotonic())))
    report("The inherited privileged agent did not respond. Falling back to SSH bootstrap for this image.")
    return False


class BuildConnection:
    """Run the same build verification through root VSOCK or bootstrap SSH."""
    def __init__(self, backend, machine, ssh_args=None, address=""):
        self.backend, self.machine = backend, machine
        self.agent = ssh_args is None
        self.args, self.address = ssh_args or [], address

    def command(self, command, *, stdin=False, as_user=False, root=False):
        if not self.agent:
            if root:
                command = "sudo -n -- " + command
            return [*self.args, command]
        argv = shlex.split(command)
        if as_user:
            argv = ["/usr/bin/sudo", "-n", "-H", "-u", self.machine.vm.ssh_user, "--", *argv]
        return guest_agent.exec_args(self.backend, self.machine.vm.name, argv, stdin=stdin)

    def run(self, command, *, input=None, timeout=30, as_user=False, root=False):
        return run(self.command(command, stdin=input is not None, as_user=as_user, root=root),
                   **({"input": input} if input is not None else {}), capture_output=True, text=True, timeout=timeout)

    def capture(self, report):
        return capture_guest(self.args, report, command=self.command("sh -s", stdin=True) if self.agent else None)

    def save_host_keys(self, directory, report):
        """Trust the clone's public SSH host keys through its local VSOCK channel."""
        result = self.run("sh -s", input="set -eu\nfor key_file in /etc/ssh/ssh_host_*_key.pub; do\n    if [ -f \"$key_file\" ]; then cat \"$key_file\"; fi\ndone\n")
        lines = result.stdout.splitlines()
        if result.returncode or not lines or len(lines) > 16 or len(result.stdout.encode()) > 32768:
            raise DeploymentError("Could not read the clone's public SSH host keys through its agent.")
        supported = supported_public_key_types()
        with tempfile.TemporaryDirectory(dir=directory, prefix="host-keys-") as work:
            paths = []
            for index, line in enumerate(lines):
                algorithm = line.split()[0] if line.split() else ""
                if algorithm not in supported and algorithm.startswith(("ssh-", "ecdsa-", "sk-")):
                    report(f"Skipping guest SSH host key {algorithm}: this Mac's OpenSSH does not support it.")
                    continue
                path = Path(work) / f"host-{index}.pub"
                path.write_text(line + "\n")
                path.chmod(0o600)
                paths.append(path)
            if not paths:
                raise DeploymentError("The guest has no SSH host keys supported by this Mac's OpenSSH.")
            keys = read_public_keys(tuple(paths))
        pending = directory / "known_hosts.agent.tmp"
        try:
            pending.write_text("".join(f"appletart-{self.machine.vm.name} {key}\n" for key in keys))
            pending.chmod(0o600)
            pending.replace(directory / "known_hosts")
        finally:
            pending.unlink(missing_ok=True)
        report("Public SSH host keys saved through the agent for future verified terminal connections.")


def prepare_linux_golden(backend, machine, log_path, report=print):
    """Boot only the disposable copy to check and enable NoCloud provisioning."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics.console(log_path)
    with log_path.open("a") as log, serial_console(log) as (serial_path, close_serial):
        log_path.chmod(0o600)
        command = [backend.binary, *machine.vm.run_args(headless=True), "--serial-path", serial_path]
        diagnostics.command_start(command)
        process = subprocess.Popen(command, stdout=log, stderr=log,
            env={**os.environ, "TART_NO_AUTO_PRUNE": "1"}, start_new_session=True)
        try:
            report("Checking Linux capabilities and resetting cached cloud-init state on the golden-image copy…")
            if not wait_for_build_agent(backend, machine, process, report):
                raise DeploymentError("This Linux image needs a responding privileged guest agent before it can become a golden image. Install/repair the agent on the source VM first.")
            connection = BuildConnection(backend, machine)
            result = connection.run("sh -s", input=capabilities.linux_golden_preparation(machine.vm.ssh_user), root=True, timeout=60)
            if result.stdout.strip():
                report(result.stdout.strip())
            if result.returncode:
                connection.capture(report)
                raise DeploymentError("The Linux image cannot safely provision golden-image clones: " + (result.stdout + result.stderr)[-3000:])
        finally:
            close_serial()
            stop_build(process, machine.vm.name, backend, report)


def provision_cloud(backend, machine, seed: Path, key: Path, public_key: str, log_path: Path, report=print, *, prepare_image=False, users=(), prefer_agent=False):
    """Verify cloud-init through an inherited agent or a temporary SSH key."""
    setup_vm = machine.vm
    if setup_vm.network == "bridged":
        setup_vm = replace(setup_vm, network="nat", bridge="", bridges=())
        report("Using NAT temporarily for cloud setup and guest verification. Deployment will use your selected bridged interfaces.")
    log_path.parent.mkdir(exist_ok=True)
    diagnostics.console(log_path)
    authenticated, captured = False, False
    boot_log_start = log_path.stat().st_size if log_path.exists() else 0
    with log_path.open("a") as log, serial_console(log) as (serial_path, close_serial):
        log_path.chmod(0o600)
        def boot():
            try:
                command = [backend.binary, *setup_vm.run_args(headless=True), "--disk", str(seed) + ":ro", "--serial-path", serial_path]
                diagnostics.command_start(command)
                return subprocess.Popen(command,
                                        stdout=log, stderr=log, env={**os.environ, "TART_NO_AUTO_PRUNE": "1"}, start_new_session=True)
            except OSError as error:
                raise DeploymentError(f"Cannot boot cloud image: {error}") from error
        process = boot()
        try:
            if machine.source_kind == "cloud":
                from .software import LINUX_BUILD_NOTICE
                report("Refreshing package indexes and upgrading installed packages before selected software…")
                report(LINUX_BUILD_NOTICE)
            elif machine.effective_packages:
                report("Installing selected software can take 5–15 minutes or longer. Cloud setup allows 30 minutes for completion.")
            if (machine.source_kind == "golden" or prepare_image or prefer_agent) and wait_for_build_agent(backend, machine, process, report):
                connection = BuildConnection(backend, machine)
                address = ""
                authenticated = True
                report("Guest agent is ready. Waiting up to 30 minutes for cloud-init to finish package and account setup…")
            else:
                report("Booting the cloud image. Waiting up to 30 minutes for SSH readiness…")
                ssh_deadline = time.monotonic() + CLOUD_SSH_TIMEOUT
                try:
                    address = wait_for_ssh(backend, setup_vm, process, timeout=CLOUD_SSH_TIMEOUT, report=report, boot_failure=lambda: capabilities.boot_failure(log_path, boot_log_start))
                except DeploymentError as error:
                    if "SSH was not ready within" in str(error):
                        raise DeploymentError(f"{machine.vm.name}: SSH was not ready within 30 minutes. "
                                              "Check the VM log, network and guest boot before choosing Resume build.") from error
                    if "Tart exited before SSH became ready" in str(error):
                        log.flush()
                        with log_path.open("rb") as output:
                            output.seek(max(0, log_path.stat().st_size - 2000))
                            detail = output.read().decode(errors="replace").strip()
                        if detail:
                            raise DeploymentError(f"{error}\nGuest boot output:\n{detail}") from error
                    raise
                ipaddress.ip_address(address)
                args = ["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                        "-o", "StrictHostKeyChecking=accept-new",
                        "-o", known_hosts_option(key.parent / "known_hosts"),
                        "-o", f"HostKeyAlias=appletart-{machine.vm.name}", f"{machine.vm.ssh_user}@{address}"]
                # A listening sshd can precede account setup and PAM lifting nologin.
                # Use the same readiness deadline rather than a separate 30-second cap.
                next_auth_report = time.monotonic()
                auth_delay = 2
                result = None
                while time.monotonic() < ssh_deadline:
                    if process.poll() is not None:
                        raise DeploymentError(f"{machine.vm.name}: Tart exited before the SSH account became ready.")
                    result = run([*args, "true"], capture_output=True, text=True, timeout=20)
                    if result.returncode == 0:
                        break
                    if time.monotonic() >= next_auth_report:
                        report("Waiting for the SSH build account: " + result.stderr.strip()[-300:])
                        next_auth_report = time.monotonic() + 15
                    # Avoid continually triggering SSH penalties while PAM denies
                    # early logins; preserve the readiness deadline and cancellation.
                    pause(min(auth_delay, max(0, ssh_deadline - time.monotonic())))
                    auth_delay = min(auth_delay * 2, 15)
                else:
                    raise DeploymentError("Cannot authenticate the cloud build account within 30 minutes: " +
                                          (result.stderr[-1000:] if result is not None else "SSH readiness timed out."))
                report("SSH is ready. Waiting up to 30 minutes for cloud-init to finish package and account setup…")
                authenticated = True
                connection = BuildConnection(backend, machine, args, address)
            result = connection.run("cloud-init status --wait --long", timeout=CLOUD_INIT_TIMEOUT, root=True)
            degraded = result.returncode == 2
            if degraded or (connection.agent and result.returncode == 0):
                if degraded:
                    report(result.stdout.strip())
                confirmed = connection.run("cloud-init status --format json", root=True)
                if confirmed.returncode and any(word in confirmed.stderr.lower() for word in ("unrecognized", "unknown option", "invalid choice")):
                    report("This cloud-init lacks JSON status output; checking its structured status file.")
                    confirmed = connection.run(capabilities.STATUS_FALLBACK, root=True)
                if not completed_cloud_status(confirmed):
                    connection.capture(report)
                    captured = True
                    raise DeploymentError("Cloud-init completion could not be verified. "
                                          "Its first-boot state and build key were preserved. Check the detailed log before Resume build.\n" +
                                          (confirmed.stdout + confirmed.stderr)[-4000:])
                if degraded:
                    report("Cloud-init completed with recoverable warnings. Preserving its setup state and verifying guest health…")
                    health = connection.run("sh -s", input="expected=" + shlex.quote(address) + "\n" + GUEST_BUILD_HEALTH)
                    if health.returncode:
                        connection.capture(report)
                        captured = True
                        raise DeploymentError("Cloud-init completed with warnings, but guest health checks failed. "
                                              "Its first-boot state and build key were preserved.\n" +
                                              (health.stdout + health.stderr)[-2000:])
                    report(health.stdout.strip())
            elif result.returncode:
                connection.capture(report)
                captured = True
                detail = (result.stdout + result.stderr)[-4000:]
                setup_output = ""
                # Capture the package manager's actual error before first-boot
                # cleanup; cloud-init status often reports only its exit code.
                try:
                    output = connection.run("tail -n 60 /var/log/cloud-init-output.log", root=True)
                    if output.returncode == 0 and output.stdout.strip():
                        setup_output = output.stdout
                        report("Guest setup output (last 60 lines):")
                        for line in output.stdout[-6000:].splitlines():
                            report(line[:500])
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if any(message in (detail + setup_output).lower() for message in
                       ("no enabled repositories", "no repositories available", "no enabled repos")):
                    hint = ("Enable this image's package repositories before retrying. Use the image publisher's registration process or an approved repository mirror. "
                            "Initial builds require repositories for system upgrades even when no additional applications are selected.")
                    report(hint)
                    detail += "\n" + hint
                cleaned = connection.run("cloud-init clean", root=True)
                if cleaned.returncode == 0:
                    # The next boot recreates host keys after cleaning first-boot state.
                    (key.parent / "known_hosts").unlink(missing_ok=True)
                    report("Cloud-init failed. Its first-boot state was reset so Resume build can retry setup.")
                raise DeploymentError("Cloud-init reported a failure: " + detail)
            if not degraded:
                report(result.stdout.strip())
            if connection.agent:
                # Validate the configured login account as well as root's access.
                check = connection.run("sh -s", input="expected=''\n" + GUEST_BUILD_HEALTH +
                    shlex.join(["sudo", "-n", "-H", "-u", machine.vm.ssh_user, "--", "sudo", "-n", "true"]) + "\n")
                if check.returncode:
                    raise DeploymentError("Cloud-init finished, but the clone's network, SSH service or management account is not ready: " + (check.stdout + check.stderr)[-2000:])
                report("Guest network, SSH service and configured sudo account verified through the agent.")
                connection.save_host_keys(key.parent, report)
            else:
                install_agent(args, key.parent.parent.parent / "tools", report)
            # Fail the build before revoking its credential if VSOCK IP reporting is unavailable.
            ipaddress.IPv4Address(backend.run(["ip", machine.vm.name, "--wait", "5", "--resolver", "agent"], capture=True, timeout=8).strip())
            verify_management(backend, machine.vm.name)
            if users:
                from .users import install
                from .guest_agent import exec_args
                install(exec_args(backend, machine.vm.name, ["/bin/sh", "-s"], stdin=True), users, report)
            observed_os = {}
            try:
                release = connection.run("cat /etc/os-release", root=True)
                if release.returncode == 0:
                    observed_os = guest_os.parse_release(release.stdout)
            except (OSError, subprocess.TimeoutExpired):
                report("Guest release metadata was unavailable; provisioning verification continues.")
            if observed_os:
                report("Detected guest OS: " + observed_os.get("pretty_name", observed_os.get("name", "Linux")) +
                       " · VERSION_ID=" + observed_os.get("version_id", "not supplied"))
            connection.capture(report)
            captured = True
            # Revoke the build credential only after management and any
            # selected personal keys are confirmed.
            user_keys = read_public_keys(machine.vm.ssh_public_keys)
            script = "set -eu\n"
            for public in user_keys:
                script += f"awk -v k={shlex.quote(public)} '$1 \" \" $2 == k {{found=1}} END {{exit !found}}' ~/.ssh/authorized_keys\n"
            if not prepare_image:
                script += f"awk -v k={shlex.quote(public_key)} '$1 \" \" $2 != k' ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.appletart\n"
                script += "chmod 600 ~/.ssh/authorized_keys.appletart\nmv ~/.ssh/authorized_keys.appletart ~/.ssh/authorized_keys\n"
            script += "sync\n"
            # Credential revocation and the following saved seed update finish together.
            checkpoint()
            with uncancellable():
                result = connection.run("sh -s", input=script, as_user=True)
                if result.returncode == 0 and prepare_image:
                    result = connection.run("sh -s", input="set -eu\n" + capabilities.GOLDEN_CLEANUP + "sync\n", root=True)
            if result.returncode:
                raise DeploymentError("Could not verify user keys and revoke the cloud build key: " + result.stderr[-1000:])
            report("Golden image cleaned: instance state, machine ID and SSH keys will be regenerated by each new VM." if prepare_image else
                   "Cloud-init completed. " + ("Your keys are installed; " if user_keys else "Guest-agent management verified; no personal SSH keys installed. ") + "The temporary build key was revoked.")
            return observed_os
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeploymentError(f"Cloud setup did not finish: {error}. Resume the build after checking its log.") from error
        finally:
            if authenticated and not captured:
                connection.capture(report)
            close_serial()
            stop_build(process, machine.vm.name, backend, report)
