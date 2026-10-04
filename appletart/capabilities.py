"""Guest feature checks and compatibility handling, independent of release IDs."""

import shlex
import re


def cloud_network_setup(mac):
    """Resolve our opaque v2 ID when cloud-init selects the ENI renderer.

    Netplan understands MAC matches without renaming a live interface. ENI
    treats the same ID as a device name; repair only its generated stanza.
    """
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
        raise ValueError("A valid VM MAC address is required for cloud networking")
    return """set -eu
fail() { echo "APPLETART_NETWORK_ERROR: $*" >&2; exit 1; }
file=/etc/network/interfaces.d/50-cloud-init
test -f "$file" || exit 0
grep -Eq '^iface[[:blank:]]+appletart[[:blank:]]+inet[[:blank:]]+dhcp[[:blank:]]*$' "$file" || exit 0
grep -q '^# This file is generated from information provided by the datasource' "$file" || exit 0
command -v python3 >/dev/null || fail 'Python 3 is required to resolve the cloud-init network adapter.'
python3 - """ + shlex.quote(mac.lower()) + """ <<'APPLETART_ENI_ADAPTER'
from pathlib import Path
import os
import re
import stat
import sys
import tempfile

def fail(message):
    sys.exit(f'APPLETART_NETWORK_ERROR: {message}')

mac = sys.argv[1]
path = Path('/etc/network/interfaces.d/50-cloud-init')
original = path.read_text()
stanza = re.compile(r'(?m)^auto[ \\t]+appletart[ \\t]*\\niface[ \\t]+appletart[ \\t]+inet[ \\t]+dhcp[ \\t]*\\n((?:[ \\t]+[^\\n]*\\n)*)')
if not stanza.search(original):
    fail('The generated ENI adapter stanza could not be resolved safely.')
interfaces = sorted(address.parent.name for address in Path('/sys/class/net').glob('*/address')
                    if address.read_text().strip().lower() == mac and address.parent.name != 'lo')
if not interfaces or any(not re.fullmatch(r'[A-Za-z0-9_.:-]+', name) for name in interfaces):
    fail(f'No usable guest network interface matches the requested MAC {mac}.')

def atomic_write(target, value):
    info = target.stat()
    with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(value)
            output.flush()
            os.chmod(temporary, stat.S_IMODE(info.st_mode))
            os.chown(temporary, info.st_uid, info.st_gid)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

updated = stanza.sub(lambda match: ''.join(f'auto {name}\\niface {name} inet dhcp\\n{match[1]}'
                                           for name in interfaces), original)
rules = Path('/etc/udev/rules.d/70-persistent-net.rules')
if rules.is_file():
    previous = rules.read_text()
    retained = ''.join(line for line in previous.splitlines(keepends=True)
                       if not (re.search(r'NAME\\s*=\\s*"appletart"', line)
                               and re.search(r'ATTR\\{address\\}\\s*==\\s*"' + re.escape(mac) + r'"', line, re.I)))
    if retained != previous:
        atomic_write(rules, retained)
atomic_write(path, updated)
print('Resolved cloud-init ENI adapter by MAC to: ' + ', '.join(interfaces))
APPLETART_ENI_ADAPTER
timeout 45 systemctl restart networking.service || fail 'Could not activate the resolved ENI network configuration.'
# Some DHCP clients return after IPv6 becomes ready, before the IPv4 lease.
attempt=0
until ip -o -4 address show scope global | grep -q ' inet ' && ip -4 route show default | grep -q '^default '; do
    test "$attempt" -lt 30 || fail 'The guest IPv4 address and default route were not ready within 30 seconds after ENI activation.'
    attempt=$((attempt + 1))
    sleep 1
done
echo 'Resolved ENI network has an IPv4 address and default route.'
"""


def cloud_preflight(username):
    return f"""set -eu
fail() {{ echo "APPLETART_PREFLIGHT_ERROR: $*" >&2; exit 1; }}
test "$(uname -s)" = Linux || fail 'Cloud provisioning requires a Linux guest.'
case "$(uname -m)" in aarch64|arm64) ;; *) fail 'This image is not ARM64.';; esac
for tool in cloud-init systemctl ip sudo getent useradd awk grep; do
    command -v "$tool" >/dev/null || fail "Required guest capability is missing: $tool"
done
test -x /bin/bash || fail 'The configured login shell /bin/bash is missing.'
if ! systemctl cat ssh.service >/dev/null 2>&1 && ! systemctl cat sshd.service >/dev/null 2>&1; then
    fail 'The cloud image must include OpenSSH (ssh.service or sshd.service).'
fi
username={shlex.quote(username)}
if ! getent passwd "$username" >/dev/null && getent group "$username" >/dev/null; then
    useradd --create-home --gid "$username" --shell /bin/bash --password '!' -- "$username"
fi
echo 'AppleTart guest capability checks passed: Linux ARM64, cloud-init, systemd, OpenSSH, sudo and account tools.'
"""


STATUS_FALLBACK = """sh -c 'for path in /run/cloud-init/status.json /var/lib/cloud/data/status.json; do
    if test -s "$path"; then cat "$path"; exit 0; fi
done
exit 1'"""

GOLDEN_PASSWORD_CLEANUP = r'''set -eu
command -v usermod >/dev/null
command -v python3 >/dev/null
minimum=1000
if [ -r /etc/login.defs ]; then minimum=$(awk '$1 == "UID_MIN" {print $2; exit}' /etc/login.defs); fi
minimum=${minimum:-1000}
case "$minimum" in *[!0-9]*) echo 'Invalid UID_MIN for golden account cleanup.' >&2; exit 1;; esac
accounts=$(getent passwd)
printf '%s\n' "$accounts" | while IFS=: read -r login password uid gid comment home shell; do
    if [ "$uid" = 0 ] || [ "$uid" -ge "$minimum" ]; then
        usermod --password '!' -- "$login"
    fi
done
python3 - <<'APPLETART_PASSWORD_POLICY'
from pathlib import Path
import os
import re
import stat
import tempfile
path = Path('/etc/ssh/sshd_config')
if path.is_symlink() or not path.is_file():
    raise SystemExit('A regular SSH configuration is required for golden cleanup.')
original = path.read_text()
# Remove our marked blocks and the exact unmarked form used by older builds.
updated = re.sub(r'(?m)^# BEGIN APPLETART PASSWORD ACCESS\n.*?^# END APPLETART PASSWORD ACCESS\n', '', original, flags=re.S)
updated = re.sub(r'\nMatch all\nMatch User [a-z_][a-z0-9_.-]*(?:,[a-z_][a-z0-9_.-]*)*\n    PasswordAuthentication yes\n    AuthenticationMethods any\nMatch all\n', '\n', updated)
if updated != original:
    info = path.stat()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(updated)
            output.flush()
            os.chmod(temporary, stat.S_IMODE(info.st_mode))
            os.chown(temporary, info.st_uid, info.st_gid)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
APPLETART_PASSWORD_POLICY
echo 'Inherited local account passwords and AppleTart password SSH rules cleared.'
'''


GOLDEN_CLEANUP = GOLDEN_PASSWORD_CLEANUP + """help=$(cloud-init clean --help 2>&1)
set --
for option in --logs --machine-id --seed; do
    if printf '%s' "$help" | grep -q -- "$option"; then set -- "$@" "$option"; fi
done
cloud-init clean "$@"
printf 'uninitialized\\n' > /etc/machine-id
chmod 444 /etc/machine-id
if [ -d /var/lib/dbus ]; then ln -sf /etc/machine-id /var/lib/dbus/machine-id; fi
minimum=1000
if [ -r /etc/login.defs ]; then minimum=$(awk '$1 == "UID_MIN" {print $2; exit}' /etc/login.defs); fi
minimum=${minimum:-1000}
accounts=$(getent passwd)
printf '%s\\n' "$accounts" | while IFS=: read -r login password uid gid comment home shell; do
    if [ "$uid" = 0 ] || [ "$uid" -ge "$minimum" ]; then
        case "$home" in /|"") continue;; /*) ;; *) continue;; esac
        if test -d "$home" && ! test -L "$home"; then
            find "$home" -xdev -type f -name authorized_keys -exec truncate -s 0 {} +
        fi
    fi
done
rm -f /etc/ssh/ssh_host_*
"""


def linux_golden_preparation(username):
    """Enable per-clone NoCloud setup only after checking the copy's features."""
    return cloud_preflight(username) + """
test "$(id -u)" = 0 || fail 'Golden-image preparation needs a privileged guest agent.'
case "$(cat /proc/cmdline)" in *cloud-init=disabled*) fail 'The template disables cloud-init in its kernel command line.';; esac
command -v python3 >/dev/null || fail 'The template needs Python 3 to check cloud-init capabilities.'
python3 - <<'APPLETART_CLOUD_CAPABILITIES'
from pathlib import Path
import sys
try:
    import yaml
    from cloudinit.sources import DataSourceNoCloud
    config = {}
    paths = [Path('/etc/cloud/cloud.cfg'), *sorted(Path('/etc/cloud/cloud.cfg.d').glob('*.cfg'))]
    for path in paths:
        value = yaml.safe_load(path.read_text()) or {}
        if not isinstance(value, dict) or 'merge_how' in value or 'merge_type' in value:
            raise ValueError(f'Unsupported cloud-init configuration merge in {path}')
        config.update(value)
    for stage, required in {
        'cloud_init_modules': {'bootcmd', 'users_groups', 'ssh', 'set_hostname', 'growpart', 'resizefs'},
        'cloud_config_modules': {'runcmd'},
        'cloud_final_modules': {'scripts_user'},
    }.items():
        modules = config.get(stage, [])
        # cloud-init accepts both users-groups and users_groups (and module
        # entries with an explicit frequency). Compare their canonical names.
        enabled = {(item if isinstance(item, str) else item[0]).replace('-', '_') for item in modules}
        missing = required - enabled
        if missing:
            raise ValueError(f'{stage} is missing required modules: {", ".join(sorted(missing))}')
except Exception as error:
    sys.exit(f'APPLETART_PREFLIGHT_ERROR: {error}')
print('Verified NoCloud datasource and account, identity, disk-growth and script modules.')
APPLETART_CLOUD_CAPABILITIES
units=''
for unit in cloud-init-local.service cloud-init.service cloud-init-network.service cloud-init-main.service cloud-config.service cloud-final.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then units="$units $unit"; fi
done
case "$units" in *cloud-init.service*|*cloud-init-network.service*|*cloud-init-main.service*) ;; *) fail 'No supported cloud-init boot service is installed.';; esac
# A final lexical override preserves the publisher's other configuration.
install -d -m 755 /etc/cloud/cloud.cfg.d
cat > /etc/cloud/cloud.cfg.d/zzzz-appletart-nocloud.cfg <<'APPLETART_NOCLOUD'
datasource_list: [ NoCloud ]
manual_cache_clean: false
ssh_pwauth: false
APPLETART_NOCLOUD
chmod 644 /etc/cloud/cloud.cfg.d/zzzz-appletart-nocloud.cfg
rm -f /etc/cloud/cloud-init.disabled
for unit in $units; do systemctl unmask "$unit"; systemctl enable "$unit"; done
""" + GOLDEN_CLEANUP + """
sync
echo 'Linux image copy prepared for fresh NoCloud instance identities.'
"""


def boot_failure(path, start=0):
    """Surface an explicit guest capability failure while waiting for networking."""
    try:
        size = path.stat().st_size
        with path.open("rb") as output:
            output.seek(max(start, size - 65536))
            tail = output.read().decode(errors="replace")
        for match in re.finditer(r"APPLETART_(?:PREFLIGHT|NETWORK)_ERROR: ([^\r\n]+)", tail):
            if "$*" not in match[1]:
                return match[1][:500]
    except OSError:
        pass
    return ""
