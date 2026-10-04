"""Validate user imports and add guest accounts through the saved SSH identity."""

import base64
import hashlib
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import tempfile

from .deployment import DeploymentError
from .operations import run
from .ssh import read_public_keys


MAX_BYTES = 48 * 1024
MAX_USERS = 20


def parse_yaml(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise DeploymentError("User YAML must be UTF-8 text under 48 KB.")
    try:
        import yaml
    except ImportError as error:
        raise DeploymentError("YAML import requires PyYAML. Install this project with python -m pip install -e . and restart the dashboard.") from error

    class UserLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise DeploymentError("Use explicit user entries; YAML aliases are not supported.")
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise DeploymentError("YAML fields must be unique text names.")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        document = yaml.load(text, Loader=UserLoader)
    except (yaml.YAMLError, RecursionError) as error:
        location = getattr(error, "problem_mark", None)
        where = f" at line {location.line + 1}" if location else ""
        raise DeploymentError("Invalid user YAML" + where + ". Use the downloadable template.") from error
    if not isinstance(document, dict) or set(document) != {"version", "users"} or type(document.get("version")) is not int or document["version"] != 1:
        raise DeploymentError("Expected version: 1 and a users list. Use the downloadable template.")
    return document["users"]


def password(value):
    if not isinstance(value, str) or len(value) > 1024 or any(c in value for c in "\0\r\n:"):
        raise DeploymentError("Passwords must be text under 1024 characters without colons or line breaks.")
    return value


def validate(entries, *, recipe=False):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_USERS:
        raise DeploymentError(f"Supply between 1 and {MAX_USERS} users.")
    users, names, verified = [], set(), {}
    with tempfile.TemporaryDirectory(prefix="appletart-public-key-") as directory:
        path = Path(directory) / "key.pub"
        for entry in entries:
            allowed = {"username", "ssh_authorized_keys", "password_login"} | (set() if recipe else {"password"})
            if not isinstance(entry, dict) or "username" not in entry or set(entry) - allowed:
                raise DeploymentError("Each user needs username and SSH public keys or a password. Passwords cannot be saved in a recipe.")
            name = entry["username"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", name) or name == "root":
                raise DeploymentError("Usernames must start with a lowercase letter or underscore and contain up to 32 lowercase letters, digits, dots, underscores or hyphens. The root account cannot be changed.")
            if name in names:
                raise DeploymentError(f"Duplicate username: {name}.")
            names.add(name)
            secret = password(entry.get("password", ""))
            login = entry.get("password_login", bool(secret))
            if type(login) is not bool or (secret and not login):
                raise DeploymentError(f"{name}: invalid password login selection.")
            keys = entry.get("ssh_authorized_keys", [])
            if not isinstance(keys, list) or len(keys) > 16:
                raise DeploymentError(f"{name}: supply up to 16 SSH public keys.")
            if not keys and not login:
                raise DeploymentError(f"{name}: supply an SSH public key or password.")
            if login and not recipe and not secret:
                raise DeploymentError(f"{name}: enter a password for this deployment.")
            normalized = []
            for key in keys:
                if not isinstance(key, str) or len(key) > 8192 or "\n" in key.strip() or "\r" in key.strip() or "\0" in key:
                    raise DeploymentError(f"{name}: each SSH public key must be a single line under 8 KB.")
                if key not in verified:
                    path.write_text(key.strip() + "\n")
                    try:
                        verified[key] = read_public_keys((path,))[0]
                    except DeploymentError as error:
                        raise DeploymentError(f"{name}: invalid SSH public key. Paste the contents of a .pub file, including the key type and encoded key.") from error
                if verified[key] not in normalized:
                    normalized.append(verified[key])
            user = {"username": name, "ssh_authorized_keys": normalized}
            if login:
                user["password_login"] = True
            if secret:
                user["password"] = secret
            users.append(user)
    return users


def preview(data):
    if not isinstance(data, dict) or ("yaml" in data) == ("users" in data):
        raise DeploymentError("Supply either user entries or YAML text.")
    users = validate(parse_yaml(data["yaml"]) if "yaml" in data else data["users"])
    return {"users": users, "summary": [{"username": user["username"], "key_count": len(user["ssh_authorized_keys"]), "password_login": user.get("password_login", False),
        "fingerprints": ["SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(key.split()[1] + "=" * (-len(key.split()[1]) % 4))).digest()).decode().rstrip("=")
                         for key in user["ssh_authorized_keys"]]} for user in users]}


def provisioning(machine, guest_password="", user_passwords=None):
    """Join public recipes with credentials held only for the current operation."""
    guest_password = password(guest_password)
    user_passwords = user_passwords if user_passwords is not None else {}
    names = {user["username"] for user in machine.users if user.get("password_login")}
    if not isinstance(user_passwords, dict) or set(user_passwords) - names:
        raise DeploymentError("Supply passwords only for the additional users that request password login.")
    if machine.password_login != bool(guest_password):
        raise DeploymentError("Enter the guest password selected in this VM's recipe, or clear password login for a new build.")
    entries = []
    if guest_password:
        entries.append({"username": machine.vm.ssh_user, "password": guest_password})
    entries.extend({**user, **({"password": user_passwords.get(user["username"], "")} if user.get("password_login") else {})} for user in machine.users)
    return validate(entries) if entries else []


def install_script(users, family="linux"):
    if family == "macos":
        return macos_script(users)
    # All target accounts are checked before any changes. Guest-side checks protect
    # service accounts and prevent root from following user-controlled SSH symlinks.
    credential_script = ""
    script = r'''set -eu
umask 077
test "$(id -u)" = 0 || { echo 'Passwordless sudo is required.' >&2; exit 1; }
minimum=$(awk '$1 == "UID_MIN" { print $2; exit }' /etc/login.defs)
minimum=${minimum:-1000}
check_account() {
    account=$(getent passwd "$1") || return 0
    IFS=: read -r login password uid gid comment home shell <<EOF
$account
EOF
    case "$uid" in ''|*[!0-9]*) echo "Invalid account: $1" >&2; exit 1;; esac
    test "$uid" -ge "$minimum" || { echo "Refusing system account: $1" >&2; exit 1; }
    case "$shell" in */nologin|*/false) echo "Refusing non-login account: $1" >&2; exit 1;; esac
    case "$home" in /home/*|/Users/*) ;; *) echo "Unsupported home directory for $1: $home" >&2; exit 1;; esac
    test ! -L "$home" && test -d "$home" && test "$(stat -c %u "$home")" = "$uid" || { echo "Unsafe home directory for $1" >&2; exit 1; }
    for target in "$home/.ssh" "$home/.ssh/authorized_keys"; do
        test ! -L "$target" || { echo "Refusing SSH symlink for $1" >&2; exit 1; }
        if test -e "$target"; then
            test "$(stat -c %u "$target")" = "$uid" || { echo "Unexpected SSH file owner for $1" >&2; exit 1; }
        fi
    done
    if test -e "$home/.ssh"; then test -d "$home/.ssh" || { echo "Invalid SSH directory for $1" >&2; exit 1; }; fi
    if test -e "$home/.ssh/authorized_keys"; then
        test -f "$home/.ssh/authorized_keys" && test "$(stat -c %h "$home/.ssh/authorized_keys")" = 1 || { echo "Unsafe authorized_keys for $1" >&2; exit 1; }
    fi
}
command -v useradd >/dev/null
command -v getent >/dev/null
command -v visudo >/dev/null || { echo 'Install sudo before adding users.' >&2; exit 1; }
visudo -c >/dev/null
test ! -L /etc/sudoers.d && test -d /etc/sudoers.d && test "$(stat -c %u /etc/sudoers.d)" = 0 || { echo 'A root-owned /etc/sudoers.d directory is required.' >&2; exit 1; }
grep -Eq '^[[:space:]]*[@#]includedir[[:space:]]+/etc/sudoers[.]d/?([[:space:]]|$)' /etc/sudoers || { echo 'Enable the /etc/sudoers.d include in /etc/sudoers before adding users.' >&2; exit 1; }
test -x /bin/bash
'''
    for user in users:
        script += "check_account " + shlex.quote(user["username"]) + "\n"
    if any(user.get("password") for user in users):
        script += "command -v chpasswd >/dev/null\n"
    for user in users:
        name = shlex.quote(user["username"])
        sudoers_name = "zz-appletart-user-" + user["username"].encode().hex()
        script += f"username={name}\n"
        script += r'''if ! getent passwd "$username" >/dev/null; then
    if getent group "$username" >/dev/null; then
        useradd --create-home --gid "$username" --shell /bin/bash --password '!' -- "$username"
    else
        useradd --create-home --shell /bin/bash --password '!' -- "$username"
    fi
fi
check_account "$username"
install -d -m 700 -o "$uid" -g "$gid" "$home/.ssh"
temporary=$(mktemp "$home/.ssh/.appletart-keys.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
if test -f "$home/.ssh/authorized_keys"; then cat "$home/.ssh/authorized_keys" > "$temporary"; fi
'''
        for key in user["ssh_authorized_keys"]:
            script += "key=" + shlex.quote(key) + "\n"
            script += r'''if ! awk -v key="$key" '{ for (i=1; i<NF; i++) if ($i " " $(i+1) == key) found=1 } END { exit !found }' "$temporary"; then
    printf '\n%s\n' "$key" >> "$temporary"
fi
'''
        script += r'''chmod 600 "$temporary"
chown "$uid:$gid" "$temporary"
mv -f "$temporary" "$home/.ssh/authorized_keys"
trap - EXIT HUP INT TERM
if command -v restorecon >/dev/null 2>&1; then restorecon -RF "$home/.ssh"; fi
'''
        if user.get("password"):
            # stdin is not included in command diagnostics or process arguments.
            credential_script += "printf '%s\\n' " + shlex.quote(user["username"] + ":" + user["password"]) + " | chpasswd\n"
        script += "sudoers=/etc/sudoers.d/" + sudoers_name + "\n"
        script += r'''test ! -L "$sudoers" || { echo "Refusing sudoers symlink for $username" >&2; exit 1; }
temporary=$(mktemp /etc/sudoers.d/appletart-pending.XXXXXX)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$username" > "$temporary"
chmod 440 "$temporary"
chown 0:0 "$temporary"
visudo -cf "$temporary" >/dev/null
mv -f "$temporary" "$sudoers"
trap - EXIT HUP INT TERM
if command -v restorecon >/dev/null 2>&1; then restorecon -F "$sudoers"; fi
sudo -u "$username" -- sudo -n sh -c 'test "$(id -u)" = 0'
printf 'Configured SSH keys and passwordless sudo for %s\n' "$username"
'''
    return script + password_access_script(users, family) + credential_script


def install(command, users, report=print, *, family="linux"):
    from .diagnostics import redact
    sensitive = tuple(user.get("password", "") for user in users)
    report(f"Configuring {len(users)} user account(s)…")
    try:
        result = run(command, input=install_script(users, family), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError("User setup could not finish over the guest management connection. Check the detailed log; retrying appends only missing keys.") from error
    if result.stdout:
        report(redact(result.stdout.strip(), sensitive))
    if result.returncode:
        raise DeploymentError("Guest user setup failed: " + redact(result.stderr[-1500:].strip(), sensitive) + ". Check the detailed log; completed accounts are preserved and can be retried.")
    report("User accounts, SSH public keys and passwordless sudo configured.")


def macos_password_script(username, value):
    """Change and verify a Directory Services password through stdin."""
    quoted = '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return ("output=$(dscl -q . 2>&1 <<'APPLETART_PASSWORD_INPUT'\npasswd /Users/" + username + ' ' + quoted +
            "\nauthonly " + username + ' ' + quoted + "\nquit\nAPPLETART_PASSWORD_INPUT\n) || { echo 'Directory Services password setup failed.' >&2; exit 1; }\n"
            "case \"$output\" in *DS\\ Error*|*eDS*|*error*) echo 'Directory Services password setup failed.' >&2; exit 1;; esac\n")


def retire_template_password(command, username, report=print, *, family="linux"):
    """Retire inherited credentials only after root VSOCK is verified."""
    if family == "macos":
        # macOS shadow hashes cannot be locked with Linux's usermod. Replace
        # the inherited password with an unretained, unguessable credential;
        # explicit passwords are applied by user setup after this step.
        script = ("set -eu\ntest \"$(id -u)\" = 0\ncommand -v dscl >/dev/null\n"
                  "test \"$(id -u " + shlex.quote(username) + ")\" -ge 501 || { echo 'Refusing system account password retirement.' >&2; exit 1; }\n" +
                  macos_password_script(username, secrets.token_hex(32)))
    elif family == "linux":
        script = "set -eu\ncommand -v usermod >/dev/null\nusermod --password '!' -- " + shlex.quote(username) + "\n"
    else:
        raise DeploymentError("Template password retirement supports Linux and macOS.")
    result = run(command, input=script, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise DeploymentError("Could not retire the template login password: " + result.stderr[-1000:])
    report("Template login password retired. Selected passwords are applied separately.")


def password_access_script(entries, family):
    names = [user['username'] for user in entries if user.get('password')]
    if not names:
        return ''
    # A scoped override enables password SSH only for explicitly selected users.
    header = '\n# BEGIN APPLETART PASSWORD ACCESS\nMatch all\nMatch User ' + ','.join(names) + '\n    PasswordAuthentication yes\n    AuthenticationMethods any\nMatch all\n# END APPLETART PASSWORD ACCESS\n'
    script = r'''
sshd=$(command -v sshd || true)
sshd=${sshd:-/usr/sbin/sshd}
test -x "$sshd" || { echo 'OpenSSH server is required for password login.' >&2; exit 1; }
test ! -L /etc/ssh/sshd_config && test -f /etc/ssh/sshd_config || { echo 'A regular SSH server configuration is required.' >&2; exit 1; }
temporary=$(mktemp /etc/ssh/appletart-auth.XXXXXX)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
'''
    script += 'cat /etc/ssh/sshd_config > "$temporary"\n'
    script += "printf '%s' " + shlex.quote(header) + ' >> "$temporary"\n'
    script += r'''
chmod 600 "$temporary"
chown 0:0 "$temporary"
"$sshd" -t -f "$temporary"
'''
    for name in names:
        script += 'effective=$("$sshd" -T -f "$temporary" -C ' + shlex.quote('user=' + name + ',host=localhost,addr=127.0.0.1') + ')\n'
        script += '''printf '%s\\n' "$effective" | grep -qix 'passwordauthentication yes' || { echo 'Existing SSH policy blocks password login for the selected user.' >&2; exit 1; }\nprintf '%s\\n' "$effective" | grep -qix 'authenticationmethods any' || { echo 'Existing SSH policy requires additional authentication.' >&2; exit 1; }\n'''
    script += r'''
mv -f "$temporary" /etc/ssh/sshd_config
trap - EXIT HUP INT TERM
'''
    if family == 'linux':
        script += r'''if command -v restorecon >/dev/null 2>&1; then restorecon -F /etc/ssh/sshd_config; fi
if command -v systemctl >/dev/null 2>&1; then
    systemctl reload ssh.service 2>/dev/null || systemctl reload sshd.service
elif command -v service >/dev/null 2>&1; then
    service ssh reload 2>/dev/null || service sshd reload
else
    echo 'Cannot reload the SSH service for password login.' >&2; exit 1
fi
'''
    return script + "echo 'Password SSH access configured for selected accounts.'\n"


def macos_script(entries):
    """Native Directory Services account setup; passwords travel through stdin."""
    credential_script = ""
    script = r'''set -eu
umask 077
test "$(id -u)" = 0
command -v dscl >/dev/null
command -v createhomedir >/dev/null
command -v visudo >/dev/null
visudo -c >/dev/null
test ! -L /etc/sudoers.d && test -d /etc/sudoers.d && test "$(stat -f %u /etc/sudoers.d)" = 0
grep -Eq '^[[:space:]]*[@#]includedir[[:space:]]+(/private)?/etc/sudoers[.]d/?([[:space:]]|$)' /etc/sudoers
check_account() {
    id -u "$1" >/dev/null 2>&1 || return 0
    uid=$(id -u "$1")
    test "$uid" -ge 501 || { echo "Refusing system account: $1" >&2; exit 1; }
    home=$(dscl . -read "/Users/$1" NFSHomeDirectory | sed 's/^NFSHomeDirectory: //')
    shell=$(dscl . -read "/Users/$1" UserShell | sed 's/^UserShell: //')
    case "$shell" in */false|*/nologin) echo 'Refusing non-login account.' >&2; exit 1;; esac
    case "$home" in /Users/*) ;; *) echo 'Unsupported home directory.' >&2; exit 1;; esac
    test ! -L "$home" && test -d "$home" && test "$(stat -f %u "$home")" = "$uid"
    for target in "$home/.ssh" "$home/.ssh/authorized_keys"; do
        test ! -L "$target" || { echo 'Refusing SSH symlink.' >&2; exit 1; }
        if test -e "$target"; then test "$(stat -f %u "$target")" = "$uid"; fi
    done
    if test -e "$home/.ssh"; then test -d "$home/.ssh"; fi
    if test -e "$home/.ssh/authorized_keys"; then test -f "$home/.ssh/authorized_keys" && test "$(stat -f %l "$home/.ssh/authorized_keys")" = 1; fi
}
'''
    for user in entries:
        script += 'check_account ' + shlex.quote(user['username']) + '\n'
    for user in entries:
        name = user['username']
        script += 'username=' + shlex.quote(name) + '\n'
        script += r'''if ! id -u "$username" >/dev/null 2>&1; then
    uid=501
    while dscl . -search /Users UniqueID "$uid" | grep -q .; do uid=$((uid + 1)); done
    dscl . -create "/Users/$username"
    dscl . -create "/Users/$username" UniqueID "$uid"
    dscl . -create "/Users/$username" PrimaryGroupID 20
    dscl . -create "/Users/$username" UserShell /bin/bash
    dscl . -create "/Users/$username" NFSHomeDirectory "/Users/$username"
    dscl . -create "/Users/$username" Password '*'
    createhomedir -c -u "$username" >/dev/null
fi
check_account "$username"
mkdir -p "$home/.ssh"
chmod 700 "$home/.ssh"
chown "$uid:20" "$home/.ssh"
temporary=$(mktemp "$home/.ssh/.appletart-keys.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
if test -f "$home/.ssh/authorized_keys"; then cat "$home/.ssh/authorized_keys" > "$temporary"; fi
'''
        for key in user['ssh_authorized_keys']:
            script += 'key=' + shlex.quote(key) + '\n'
            script += '''if ! awk -v key="$key" '{ for (i=1; i<NF; i++) if ($i " " $(i+1) == key) found=1 } END { exit !found }' "$temporary"; then printf '\\n%s\\n' "$key" >> "$temporary"; fi\n'''
        script += '''chmod 600 "$temporary"\nchown "$uid:20" "$temporary"\nmv -f "$temporary" "$home/.ssh/authorized_keys"\ntrap - EXIT HUP INT TERM\n'''
        if user.get('password'):
            credential_script += macos_password_script(name, user['password'])
        sudoers = '/etc/sudoers.d/zz-appletart-user-' + name.encode().hex()
        script += 'sudoers=' + shlex.quote(sudoers) + '\n'
        script += r'''test ! -L "$sudoers"
temporary=$(mktemp /etc/sudoers.d/appletart-pending.XXXXXX)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$username" > "$temporary"
chmod 440 "$temporary"
chown 0:0 "$temporary"
visudo -cf "$temporary" >/dev/null
mv -f "$temporary" "$sudoers"
trap - EXIT HUP INT TERM
sudo -u "$username" -- sudo -n sh -c 'test "$(id -u)" = 0'
printf 'Configured account and passwordless sudo for %s\n' "$username"
'''
    return script + password_access_script(entries, 'macos') + credential_script
