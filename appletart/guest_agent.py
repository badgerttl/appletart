"""Install and use the pinned privileged Linux/macOS Tart agent over VSOCK."""

import base64
import hashlib
import os
import shlex
import subprocess
import tarfile
import time
import uuid

from .deployment import DeploymentError
from .downloads import fetch_image
from .operations import pause, run

VERSION = "0.15.0"
URL = f"https://github.com/openai/tart-guest-agent/releases/download/v{VERSION}/tart-guest-agent-linux-arm64.tar.gz"
SHA256 = "ec9d6111137f8a1e3c51b0df91fa7aaab9bb1af5fa155a370a8fc17f03c624e3"

SERVICE = """[Unit]
Description=AppleTart guest management
After=network.target

[Service]
User=root
ExecStart=/usr/local/bin/appletart-guest-agent --run-rpc
Restart=on-failure
RestartSec=3
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


VENDOR_RPC_HANDOFF = """agent_admin() {
    if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n "$@"; fi
}
if agent_admin systemctl cat tart-guest-agent.service >/dev/null 2>&1; then
    echo 'Handing VSOCK RPC from the template agent to AppleTart root management.'
    agent_admin systemctl stop tart-guest-agent.service
    agent_admin systemctl disable tart-guest-agent.service
fi
"""


def reuse_script():
    """Upgrade a clone's inherited service before waiting for account setup."""
    return f"""set -eu
if [ ! -x /usr/local/bin/appletart-guest-agent ]; then
    echo 'No inherited AppleTart agent; SSH bootstrap will be required.'
    exit 0
fi
{VENDOR_RPC_HANDOFF}agent_work=$(mktemp -d)
trap 'rm -rf "$agent_work"' EXIT
cat > "$agent_work/appletart-guest-agent.service" <<'APPLETART_AGENT_SERVICE'
{SERVICE}APPLETART_AGENT_SERVICE
if cmp -s "$agent_work/appletart-guest-agent.service" /etc/systemd/system/appletart-guest-agent.service; then
    systemctl enable appletart-guest-agent.service
    systemctl --no-block start appletart-guest-agent.service
    exit 0
fi
echo 'Enabling privileged management in this golden-image clone.'
install -m 644 "$agent_work/appletart-guest-agent.service" /etc/systemd/system/appletart-guest-agent.service
if command -v restorecon >/dev/null 2>&1; then
    restorecon /usr/local/bin/appletart-guest-agent /etc/systemd/system/appletart-guest-agent.service
fi
systemctl daemon-reload
systemctl enable appletart-guest-agent.service
systemctl --no-block restart appletart-guest-agent.service
"""


def prepare(cache, report=print, *, family="linux"):
    if family not in ("linux", "macos"):
        raise DeploymentError("The guest agent supports Linux and macOS.")
    report(f"Preparing Tart guest agent {VERSION} for privileged guest management…")
    url, checksum = (URL, SHA256) if family == "linux" else (
        f"https://github.com/openai/tart-guest-agent/releases/download/v{VERSION}/tart-guest-agent-darwin-all.tar.gz",
        "eb47f402f18e742a8ea96344115ca776179f9c4c67494bc0c46a10a68db280b7")
    archive = fetch_image(url, checksum, cache, report, suffix=".tar.gz")
    try:
        with tarfile.open(archive, "r:gz") as source:
            entries = [entry for entry in source.getmembers() if entry.name == "tart-guest-agent"]
            if len(entries) != 1 or not entries[0].isfile() or not 0 < entries[0].size <= 32 * 1024 * 1024:
                raise DeploymentError("The verified guest-agent archive has no valid binary.")
            binary = source.extractfile(entries[0]).read()
        # ELF64, little endian, AArch64. Never execute the guest binary on macOS.
        if family == "linux" and (binary[:6] != b"\x7fELF\x02\x01" or binary[18:20] != b"\xb7\x00"):
            raise DeploymentError("The guest agent must be a Linux ARM64 executable.")
        if family == "macos" and binary[:4] not in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf", b"\xcf\xfa\xed\xfe"):
            raise DeploymentError("The guest agent must be a macOS executable.")
        return binary
    except (OSError, tarfile.TarError) as error:
        raise DeploymentError(f"Cannot read the verified guest agent: {error}") from error


def install_script(binary, *, activate=True):
    if binary[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf", b"\xcf\xfa\xed\xfe"):
        return darwin_install_script(binary, activate=activate)
    encoded = base64.b64encode(binary).decode("ascii")
    checksum = hashlib.sha256(binary).hexdigest()
    activation = """sudo -n systemctl restart appletart-guest-agent.service
sleep 1
if ! sudo -n systemctl is-active --quiet appletart-guest-agent.service; then
    sudo -n journalctl -u appletart-guest-agent.service --no-pager -n 15
    exit 1
fi
""" if activate else ""
    return f"""set -eu
umask 077
agent_work=$(mktemp -d)
trap 'rm -rf "$agent_work"' EXIT
base64 -d > "$agent_work/tart-guest-agent" <<'APPLETART_AGENT_BINARY'
{encoded}
APPLETART_AGENT_BINARY
printf '%s  %s\\n' {shlex.quote(checksum)} "$agent_work/tart-guest-agent" | sha256sum -c -
sudo -n install -d -m 755 /usr/local/bin
sudo -n install -m 755 "$agent_work/tart-guest-agent" /usr/local/bin/appletart-guest-agent.new
sudo -n mv /usr/local/bin/appletart-guest-agent.new /usr/local/bin/appletart-guest-agent
cat > "$agent_work/appletart-guest-agent.service" <<'APPLETART_AGENT_SERVICE'
{SERVICE}APPLETART_AGENT_SERVICE
sudo -n install -m 644 "$agent_work/appletart-guest-agent.service" /etc/systemd/system/appletart-guest-agent.service
if command -v restorecon >/dev/null 2>&1; then
    sudo -n restorecon /usr/local/bin/appletart-guest-agent /etc/systemd/system/appletart-guest-agent.service
elif [ -x /usr/sbin/restorecon ]; then
    sudo -n /usr/sbin/restorecon /usr/local/bin/appletart-guest-agent /etc/systemd/system/appletart-guest-agent.service
fi
{VENDOR_RPC_HANDOFF if activate else ""}sudo -n systemctl daemon-reload
sudo -n systemctl enable appletart-guest-agent.service
{activation}sync
"""


def install(ssh_args, cache, report=print, *, binary=None, family="linux", password=""):
    binary = binary if binary is not None else prepare(cache, report, **({"family": family} if family != "linux" else {}))
    report("Installing the privileged guest agent for IP discovery and VM management…")
    from .ssh import password_environment
    args = [*ssh_args, *(["-o", "NumberOfPasswordPrompts=1"] if password else []), "sh -s"]
    with password_environment(password) as env:
        result = run(args, input=install_script(binary), capture_output=True, text=True, timeout=120,
                     **({"env": env} if password else {}))
    if result.returncode:
        raise DeploymentError("Guest-agent installation failed: " + (result.stderr + result.stdout)[-2000:])
    report("Guest agent installed and enabled for future boots.")


def darwin_install_script(binary, *, activate=True):
    """Keep the vendor GUI service for clipboard sharing; put RPC in launchd."""
    encoded = base64.b64encode(binary).decode("ascii")
    checksum = hashlib.sha256(binary).hexdigest()
    gui_activation = """sudo -n launchctl bootout "gui/$(id -u)/org.cirruslabs.tart-guest-agent" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || true""" if activate else ""
    activation = """sudo -n launchctl bootout system/org.appletart.guest-management 2>/dev/null || true
sudo -n launchctl bootstrap system /Library/LaunchDaemons/org.appletart.guest-management.plist
sudo -n launchctl print system/org.appletart.guest-management >/dev/null
""" if activate else ""
    return f"""set -eu
umask 077
agent_work=$(mktemp -d)
trap 'rm -rf "$agent_work"' EXIT
base64 -D > "$agent_work/agent" <<'APPLETART_AGENT_BINARY'
{encoded}
APPLETART_AGENT_BINARY
test "$(shasum -a 256 "$agent_work/agent" | awk '{{print $1}}')" = {shlex.quote(checksum)}
sudo -n mkdir -p /usr/local/bin
sudo -n install -m 755 "$agent_work/agent" /usr/local/bin/appletart-guest-agent.new
sudo -n mv /usr/local/bin/appletart-guest-agent.new /usr/local/bin/appletart-guest-agent
for plist in /Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist "$HOME/Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist"; do
    if test -f "$plist"; then
        sudo -n /usr/libexec/PlistBuddy -c 'Set :ProgramArguments:0 /usr/local/bin/appletart-guest-agent' "$plist"
        sudo -n /usr/libexec/PlistBuddy -c 'Set :ProgramArguments:1 --run-vdagent' "$plist"
        {gui_activation}
    fi
done
cat > "$agent_work/service.plist" <<'APPLETART_AGENT_SERVICE'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Label</key><string>org.appletart.guest-management</string>
<key>ProgramArguments</key><array><string>/usr/local/bin/appletart-guest-agent</string><string>--run-rpc</string></array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>/var/log/appletart-agent.log</string>
<key>StandardErrorPath</key><string>/var/log/appletart-agent.log</string>
</dict></plist>
APPLETART_AGENT_SERVICE
sudo -n install -m 644 "$agent_work/service.plist" /Library/LaunchDaemons/org.appletart.guest-management.plist
sudo -n chown root:wheel /Library/LaunchDaemons/org.appletart.guest-management.plist
sudo -n plutil -lint /Library/LaunchDaemons/org.appletart.guest-management.plist
{activation}sync
"""


def wait_for_root(backend, name, process, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DeploymentError(f"{name}: Tart exited before guest management became ready.")
        if root_available(backend, name):
            return True
        pause(min(1, max(0, deadline - time.monotonic())))
    return False


def install_via_agent(backend, name, cache, report=print, *, binary=None, family="linux"):
    """Stage synchronously, restart outside the agent's process group, verify."""
    import plistlib
    binary = binary if binary is not None else prepare(cache, report, family=family)
    identifier = uuid.uuid4().hex
    marker = f"/var/run/appletart-agent-upgrade-{identifier}.complete"
    if family == "linux":
        restart = ("set -eu\n" + VENDOR_RPC_HANDOFF +
                   "systemctl restart appletart-guest-agent.service\n" +
                   f"printf '%s\\n' {identifier} > {marker}\n")
        # A systemd timer survives the RPC process being stopped and ensures
        # the staging response returns before the management connection closes.
        preflight = "command -v systemd-run >/dev/null\n"
        schedule = shlex.join(["systemd-run", "--quiet", "--collect", "--unit=appletart-agent-upgrade-" + identifier,
                               "--on-active=1s", "/bin/sh", "-c", restart]) + "\n"
        verify_hash = f"printf '%s  %s\\n' {hashlib.sha256(binary).hexdigest()} /usr/local/bin/appletart-guest-agent | sha256sum -c - >/dev/null\n"
    elif family == "macos":
        label = "org.appletart.guest-management-upgrade." + identifier
        path = "/Library/LaunchDaemons/" + label + ".plist"
        restart = ("sleep 1\nlaunchctl bootout system/org.appletart.guest-management 2>/dev/null || true\n"
                   "launchctl bootstrap system /Library/LaunchDaemons/org.appletart.guest-management.plist && " +
                   f"printf '%s\\n' {identifier} > {marker}\nrm -f {path}\nlaunchctl bootout system/{label}\n")
        plist = plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/sh", "-c", restart], "RunAtLoad": True}).decode()
        preflight = "command -v launchctl >/dev/null\n"
        schedule = (f"cat > {path} <<'APPLETART_RESTART_PLIST'\n{plist}APPLETART_RESTART_PLIST\n"
                    f"chmod 600 {path}\nchown root:wheel {path}\nplutil -lint {path}\nlaunchctl bootstrap system {path}\n")
        verify_hash = f'test "$(shasum -a 256 /usr/local/bin/appletart-guest-agent | awk \'{{print $1}}\')" = {hashlib.sha256(binary).hexdigest()}\n'
    else:
        raise DeploymentError("The guest agent supports Linux and macOS.")
    report("Installing through privileged guest-agent management. No SSH login is required.")
    stage = "set -eu\ntest \"$(id -u)\" = 0\n" + preflight + install_script(binary, activate=False) + schedule
    result = run(exec_args(backend, name, ["/bin/sh", "-s"], stdin=True), input=stage,
                 capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise DeploymentError("Agent upgrade could not be staged: " + (result.stderr + result.stdout)[-1500:])
    report("Agent restart scheduled. Waiting for privileged management and the installed binary to be verified…")
    verification = (f'set -eu\ntest "$(id -u)" = 0\ntest "$(cat {marker} 2>/dev/null)" = {identifier}\n' +
                    verify_hash + f"rm -f {marker}\necho APPLETART_AGENT_VERIFIED\n")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            result = run(exec_args(backend, name, ["/bin/sh", "-s"], stdin=True), input=verification,
                         capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip() == "APPLETART_AGENT_VERIFIED":
                report("Privileged guest agent restarted and its binary checksum verified.")
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        pause(min(1, max(0, deadline - time.monotonic())))
    raise DeploymentError("Agent restart could not be verified within 45 seconds. Check its service log before retrying; no SSH mutation was attempted.")


def exec_args(backend, name, command, *, stdin=False):
    return [backend.binary, "exec", *(["-i"] if stdin else []), name, *command]


def root_available(backend, name):
    """Probe without modifying the guest; choose SSH only before setup starts."""
    try:
        result = run(exec_args(backend, name, ["/usr/bin/id", "-u"]),
                     capture_output=True, text=True, timeout=8,
                     env={**os.environ, "TART_NO_AUTO_PRUNE": "1"})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "0"


def verify_management(backend, name):
    if not root_available(backend, name):
        raise DeploymentError("The guest agent was installed but privileged command execution did not respond. Check its service log and use Repair guest agent before trying again.")
