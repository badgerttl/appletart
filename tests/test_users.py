import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart import users
from appletart.catalog import Machine
from appletart.deployment import DeploymentError
from appletart.lifecycle import Lifecycle
from test_lifecycle import FakeBackend
from test_ssh import PUBLIC_KEY


def entry(name="jay.morris", keys=None):
    return {"username": name, "ssh_authorized_keys": keys or [PUBLIC_KEY]}


class UserValidationTests(unittest.TestCase):
    def test_dotted_names_keys_and_fingerprints_round_trip(self):
        result = users.preview({"users": [entry(keys=[PUBLIC_KEY + " first", PUBLIC_KEY + " second"])]})
        self.assertEqual(result["users"], [entry()])
        self.assertEqual(result["summary"][0]["key_count"], 1)
        self.assertTrue(result["summary"][0]["fingerprints"][0].startswith("SHA256:"))
        key = PUBLIC_KEY.rstrip("=")
        self.assertEqual(users.preview({"users": [entry(keys=[key])]})["summary"], result["summary"])

    def test_template_import_and_multiple_users(self):
        template = (Path(__file__).resolve().parents[1] / "appletart/static/users-template.yaml").read_text()
        yaml = template.replace("ssh-ed25519 REPLACE_WITH_YOUR_PUBLIC_KEY", PUBLIC_KEY)
        yaml += f'  - username: another.user\n    ssh_authorized_keys: ["{PUBLIC_KEY}"]\n'
        self.assertEqual([user["username"] for user in users.preview({"yaml": yaml})["users"]], ["developer", "another.user"])

    def test_rejects_bad_keys_private_keys_names_duplicates_and_options(self):
        samples = [[], [entry("root")], [entry("x;id")], [entry("a\nb")], [entry(), entry()],
                   [entry(keys=["-----BEGIN OPENSSH PRIVATE KEY-----"])], [entry(keys=["ssh-ed25519 invalid"])],
                   [entry(keys=[PUBLIC_KEY + "\n" + PUBLIC_KEY])], [{**entry(), "sudo": False}],
                   [{"username": "dev", "ssh_authorized_keys": []}]]
        for sample in samples:
            with self.subTest(sample=sample), self.assertRaises(DeploymentError):
                users.validate(sample)

    def test_password_only_and_mixed_access_yaml_are_supported(self):
        result = users.preview({"yaml": 'version: 1\nusers:\n  - username: password.user\n    password: "temporary test password"\n'})
        self.assertEqual(result["users"][0]["ssh_authorized_keys"], [])
        self.assertTrue(result["summary"][0]["password_login"])
        self.assertNotIn("temporary test password", json.dumps(result["summary"]))
        mixed = users.validate([{**entry(), "password": "test password"}])
        self.assertEqual(mixed[0]["password"], "test password")
        for password in (True, "bad\npassword", "bad:password", "x" * 1025):
            with self.subTest(password=str(password)[:20]), self.assertRaises(DeploymentError):
                users.validate([{"username": "dev", "password": password}])

    def test_build_recipes_keep_access_requirements_without_passwords(self):
        machine = Machine.from_dict({"name": "with-users", "source_kind": "cloud",
            "source": "https://example.com/arm64.img", "password_login": True,
            "users": [{"username": "developer", "ssh_authorized_keys": [], "password_login": True}]})
        with self.assertRaisesRegex(DeploymentError, "password"):
            users.provisioning(machine)
        accounts = users.provisioning(machine, "primary secret", {"developer": "extra secret"})
        self.assertEqual([user["username"] for user in accounts], [machine.vm.ssh_user, "developer"])
        self.assertNotIn("secret", json.dumps(machine.config()))
        with self.assertRaises(DeploymentError):
            Machine.from_dict({**machine.config(), "users": [{**entry(), "password": "secret"}]})

    def test_yaml_rejects_duplicate_fields_tags_aliases_wrong_schema_and_large_files(self):
        samples = ["version: 1\nversion: 1\nusers: []", "!!python/object/apply:os.system ['true']",
                   "version: 1\nusers: &users [*users]", "version: true\nusers: []",
                   "version: 1\nusers: []\npassword: secret", "users: [", "x" * (users.MAX_BYTES + 1)]
        for sample in samples:
            with self.subTest(sample=sample[:80]), self.assertRaises(DeploymentError):
                users.preview({"yaml": sample})


class GuestScriptTests(unittest.TestCase):
    """Run the real POSIX script against guest command fixtures, never real users."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ("bin", "home", "sudoers.d", "ssh"):
            (self.root / folder).mkdir()
        (self.root / "ssh/sshd_config").write_text("PasswordAuthentication no\n")
        self.passwd = self.root / "passwd.json"
        self.passwd.write_text("{}")
        (self.root / "login.defs").write_text(f"UID_MIN {os.getuid()}\n")
        (self.root / "sudoers").write_text(f"@includedir {self.root / 'sudoers.d'}\n")
        helper = self.root / "guest-tool"
        helper.write_text("#!" + sys.executable + "\n" + r'''
import hashlib, json, os, pathlib, re, sys
root = pathlib.Path(os.environ['USER_TEST_ROOT'])
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
path = root / 'passwd.json'
records = json.loads(path.read_text())
if tool == 'id':
    print(0)
elif tool == 'getent':
    record = records.get(args[1])
    if not record: sys.exit(2)
    print(':'.join([args[1], 'x', str(record['uid']), str(os.getgid()), '', record['home'], '/bin/bash']))
elif tool == 'useradd':
    assert args[:5] == ['--create-home', '--shell', '/bin/bash', '--password', '!']
    name = args[-1]
    home = root / 'home' / name
    home.mkdir()
    records[name] = {'uid': os.getuid(), 'home': str(home)}
    path.write_text(json.dumps(records))
elif tool == 'stat':
    target = pathlib.Path(args[2])
    info = target.stat()
    print(0 if args[1] == '%u' and str(target).startswith(str(root / 'sudoers.d')) else info.st_uid if args[1] == '%u' else info.st_nlink)
elif tool == 'install':
    target = pathlib.Path(args[-1])
    target.mkdir(exist_ok=True)
    target.chmod(0o700)
elif tool == 'visudo':
    if '-cf' in args:
        if os.environ.get('USER_TEST_FAIL_VISUDO'): sys.exit(1)
        assert re.fullmatch(r'[a-z_][a-z0-9_.-]* ALL=\(ALL:ALL\) NOPASSWD: ALL\n', pathlib.Path(args[-1]).read_text())
elif tool == 'chpasswd':
    assert not args
    name, password = sys.stdin.read().strip().split(':', 1)
    records[name]['password_digest'] = hashlib.sha256(password.encode()).hexdigest()
    path.write_text(json.dumps(records))
elif tool == 'sshd':
    if '-T' in args: print('passwordauthentication yes\nauthenticationmethods any')
elif tool == 'systemctl':
    assert args[0] == 'reload'
elif tool in ('chown', 'sudo'):
    pass
else:
    raise AssertionError(tool)
''')
        helper.chmod(0o700)
        for command in ("id", "getent", "useradd", "stat", "install", "chown", "visudo", "sudo", "chpasswd", "sshd", "systemctl"):
            (self.root / "bin" / command).symlink_to(helper)

    def execute(self, entries, **env):
        script = users.install_script(entries).replace("/etc/sudoers.d", str(self.root / "sudoers.d")).replace("/etc/sudoers", str(self.root / "sudoers")).replace("/etc/login.defs", str(self.root / "login.defs")).replace("/etc/ssh", str(self.root / "ssh"))
        # The include is checked against the sandbox's real sudoers fixture.
        script = script.replace("/home/*|/Users/*", str(self.root / "home") + "/*")
        return subprocess.run(["sh", "-s"], input=script, text=True, capture_output=True,
            env={**os.environ, "USER_TEST_ROOT": str(self.root), "PATH": str(self.root / "bin") + ":/usr/bin:/bin", **env})

    def test_creates_users_preserves_keys_retries_and_installs_valid_passwordless_sudo(self):
        first = self.execute([entry()])
        self.assertEqual(first.returncode, 0, first.stderr)
        home = self.root / "home/jay.morris"
        authorized = home / ".ssh/authorized_keys"
        authorized.write_text(PUBLIC_KEY + " existing comment")
        for _ in range(2):
            result = self.execute([entry()])
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(authorized.read_text(), PUBLIC_KEY + " existing comment")
        self.assertEqual((home / ".ssh").stat().st_mode & 0o777, 0o700)
        self.assertEqual(authorized.stat().st_mode & 0o777, 0o600)
        sudoers = list((self.root / "sudoers.d").iterdir())
        self.assertEqual(len(sudoers), 1)
        self.assertNotIn(".", sudoers[0].name, "sudo ignores filenames containing dots")
        self.assertEqual(sudoers[0].read_text(), "jay.morris ALL=(ALL:ALL) NOPASSWD: ALL\n")
        self.assertEqual(sudoers[0].stat().st_mode & 0o777, 0o440)

    def test_password_only_and_mixed_accounts_set_passwords_via_stdin(self):
        entries = users.validate([{"username": "password.user", "password": "quoted ' \" $ password"},
                                  {**entry(), "password": "another password"}])
        result = self.execute(entries)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(self.passwd.read_text())
        self.assertEqual(records["password.user"]["password_digest"], hashlib.sha256(entries[0]["password"].encode()).hexdigest())
        self.assertIn("password_digest", records["jay.morris"])
        self.assertNotIn("another password", result.stdout + result.stderr + self.passwd.read_text())
        self.assertIn("Match User password.user,jay.morris", (self.root / "ssh/sshd_config").read_text())
        self.assertIn("NOPASSWD: ALL", next((self.root / "sudoers.d").iterdir()).read_text())
        self.assertNotIn(PUBLIC_KEY, (self.root / "home/password.user/.ssh/authorized_keys").read_text())

    def test_preflight_rejects_system_users_and_symlinks_before_creating_other_accounts(self):
        self.passwd.write_text(json.dumps({"service": {"uid": 0, "home": str(self.root / "home/service")}}))
        result = self.execute([entry("new"), entry("service")])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("system account", result.stderr)
        self.assertFalse((self.root / "home/new").exists())
        self.passwd.write_text("{}")
        self.assertEqual(self.execute([entry()]).returncode, 0)
        authorized = self.root / "home/jay.morris/.ssh/authorized_keys"
        authorized.unlink()
        protected = self.root / "protected"
        protected.write_text("preserved")
        authorized.symlink_to(protected)
        result = self.execute([entry("new"), entry()])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSH symlink", result.stderr)
        self.assertFalse((self.root / "home/new").exists())
        self.assertEqual(protected.read_text(), "preserved")

    def test_real_openssh_accepts_scoped_password_policy(self):
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        if not Path(sshd).is_file(): self.skipTest("OpenSSH server is unavailable")
        key = self.root / "host_key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, capture_output=True)
        (self.root / "ssh/sshd_config").write_text("HostKey " + str(key) + "\nPasswordAuthentication no\n")
        binary = self.root / "bin/sshd"
        binary.unlink(); binary.symlink_to(sshd)
        entries = users.validate([{"username": "analyst", "password": "test-only password"}])
        result = self.execute(entries)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, expected in (("analyst", "yes"), ("other", "no")):
            result = subprocess.run([sshd, "-T", "-f", str(self.root / "ssh/sshd_config"), "-C", f"user={name},host=localhost,addr=127.0.0.1"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("passwordauthentication " + expected, result.stdout.lower().splitlines())

    def test_policy_failure_preserves_existing_passwords_and_configuration(self):
        self.assertEqual(self.execute([entry('analyst')]).returncode, 0)
        records = json.loads(self.passwd.read_text())
        records['analyst']['password_digest'] = 'original digest'
        self.passwd.write_text(json.dumps(records))
        helper = self.root / 'guest-tool'
        helper.write_text(helper.read_text().replace("passwordauthentication yes\\nauthenticationmethods any", "passwordauthentication no\\nauthenticationmethods any"))
        before = (self.root / 'ssh/sshd_config').read_text()
        result = self.execute(users.validate([{'username': 'analyst', 'password': 'new test password'}]))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('policy blocks', result.stderr)
        self.assertEqual(json.loads(self.passwd.read_text())['analyst']['password_digest'], 'original digest')
        self.assertEqual((self.root / 'ssh/sshd_config').read_text(), before)
        self.assertEqual(list((self.root / 'ssh').glob('appletart-auth.*')), [])

    def test_macos_native_accounts_password_stdin_keys_and_sudo(self):
        (self.root / "sudoers").write_text("@includedir /private" + str(self.root / "sudoers.d") + "\n")
        helper = self.root / "guest-tool"
        helper.write_text("#!" + sys.executable + "\n" + r'''
import hashlib, json, os, pathlib, shlex, sys
root = pathlib.Path(os.environ['USER_TEST_ROOT'])
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
path = root / 'passwd.json'
records = json.loads(path.read_text())
if tool == 'id':
    if len(args) == 2:
        if args[1] not in records: sys.exit(1)
        print(records[args[1]].get('UniqueID', 501))
    else: print(0)
elif tool == 'dscl':
    if args == ['-q', '.']:
        lines = sys.stdin.read().splitlines()
        command = shlex.split(lines[0])
        assert command[0] == 'passwd'
        records[command[1].split('/')[-1]]['password_digest'] = hashlib.sha256(command[2].encode()).hexdigest()
    elif args[1] == '-search':
        for name, record in records.items():
            if str(record.get('UniqueID')) == args[-1]: print(name)
    elif args[1] == '-create':
        name = args[2].split('/')[-1]
        record = records.setdefault(name, {})
        if len(args) > 3: record[args[3]] = args[4]
    elif args[1] == '-read':
        name = args[2].split('/')[-1]
        print(args[3] + ': ' + str(records[name][args[3]]))
    else: raise AssertionError(args)
    path.write_text(json.dumps(records))
elif tool == 'createhomedir':
    pathlib.Path(records[args[-1]]['NFSHomeDirectory']).mkdir()
elif tool == 'stat':
    target = pathlib.Path(args[2])
    print(0 if str(target).startswith(str(root / 'sudoers.d')) else 501 if args[1] == '%u' else target.stat().st_nlink)
elif tool == 'sshd':
    if '-T' in args: print('passwordauthentication yes\nauthenticationmethods any')
elif tool in ('chown', 'sudo', 'visudo'): pass
else: raise AssertionError(tool)
''')
        for command in ('dscl', 'createhomedir'):
            (self.root / 'bin' / command).symlink_to(helper)
        entries = users.validate([{**entry('analyst'), 'password': 'quote " $ \\ secret'}])
        script = users.install_script(entries, 'macos').replace('/etc/sudoers.d', str(self.root / 'sudoers.d')).replace('/etc/sudoers', str(self.root / 'sudoers')).replace('/etc/ssh', str(self.root / 'ssh')).replace('/Users/', str(self.root / 'home') + '/')
        result = subprocess.run(['sh', '-s'], input=script, text=True, capture_output=True,
            env={**os.environ, 'USER_TEST_ROOT': str(self.root), 'PATH': str(self.root / 'bin') + ':/usr/bin:/bin'})
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.passwd.read_text())['analyst']
        self.assertEqual(record['password_digest'], hashlib.sha256(entries[0]['password'].encode()).hexdigest())
        self.assertEqual(record['UniqueID'], '501')
        self.assertIn(PUBLIC_KEY, (self.root / 'home/analyst/.ssh/authorized_keys').read_text())
        self.assertIn('NOPASSWD: ALL', next((self.root / 'sudoers.d').iterdir()).read_text())
        self.assertNotIn('secret', result.stdout + result.stderr + self.passwd.read_text())

    def test_invalid_sudoers_is_never_installed_and_pending_file_is_cleaned(self):
        result = self.execute([entry()], USER_TEST_FAIL_VISUDO="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list((self.root / "sudoers.d").iterdir()), [])


class UserLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = FakeBackend()
        self.api = Lifecycle(Path(self.temp.name), lambda report=print: self.backend)
        for target in ("appletart.lifecycle.guest_agent.prepare", "appletart.lifecycle.provision_keys"):
            stub = patch(target)
            stub.start()
            self.addCleanup(stub.stop)
        self.api.build(Machine.from_dict({"name": "dev"}), Mock())

    def test_stopped_or_replaced_guest_cannot_be_changed(self):
        with patch("appletart.users.install") as install:
            with self.assertRaisesRegex(DeploymentError, "Start the VM"):
                self.api.add_users("dev", [entry()])
            self.backend.vms["dev"]["Running"] = True
            self.backend.identities["dev"] = {"inode": "replacement"}
            with self.assertRaisesRegex(DeploymentError, "identity"):
                self.api.add_users("dev", [entry()])
        install.assert_not_called()

    def test_success_uses_saved_identity_and_records_completion_only_after_install(self):
        self.backend.vms["dev"]["Running"] = True
        with patch("appletart.users.install") as install, patch("appletart.guest_agent.root_available", return_value=False):
            self.api.add_users("dev", [entry()], Mock())
        self.assertIn("StrictHostKeyChecking=yes", install.call_args.args[0])
        self.assertIn("admin@192.0.2.10", install.call_args.args[0])
        self.assertEqual(self.api.store.get("dev")["last_user_setup"]["usernames"], ["jay.morris"])
        original = self.api.store.get("dev")["last_user_setup"]
        with patch("appletart.users.install", side_effect=DeploymentError("sudo failed")):
            with self.assertRaisesRegex(DeploymentError, "sudo failed"):
                self.api.add_users("dev", [entry("other")])
        self.assertEqual(self.api.store.get("dev")["last_user_setup"], original)

    def test_agent_user_setup_needs_no_ip_and_never_retries_failed_mutation_over_ssh(self):
        self.backend.vms["dev"]["Running"] = True
        with patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.guest_access.resolve", side_effect=AssertionError("must not need an IP")), \
             patch("appletart.guest_access.ssh_args", side_effect=AssertionError("must not use SSH")), \
             patch("appletart.users.install") as install:
            self.api.add_users("dev", [entry()], Mock())
            self.assertEqual(install.call_args.args[0], ["fake-tart", "exec", "-i", "dev", "/bin/sh", "-s"])
            original = self.api.store.get("dev")["last_user_setup"]
            self.assertEqual(original["transport"], "agent")
            install.side_effect = DeploymentError("agent disconnected after mutation")
            with self.assertRaisesRegex(DeploymentError, "agent disconnected"):
                self.api.add_users("dev", [entry("other")], Mock())
            self.assertEqual(install.call_count, 2)
            self.assertEqual(self.api.store.get("dev")["last_user_setup"], original)
