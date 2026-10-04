from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from appletart import ssh_config
from appletart.catalog import Machine
from appletart.deployment import DeploymentError


class SSHConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="appletart ssh ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        home = patch("appletart.ssh_config.Path.home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.config = self.home / ".ssh" / "config"
        self.data = self.root / "Application Support"
        self.machine = Machine.from_dict({"name": "dev", "ssh_user": "jay.morris"})

    def original(self, content):
        self.config.parent.mkdir(mode=0o700)
        self.config.write_bytes(content)

    def save(self, ip="192.0.2.10", machine=None):
        return ssh_config.save(self.data, machine or self.machine, ip)

    def test_creates_private_config_with_vm_settings_that_openssh_can_use(self):
        public = self.home / 'key "quoted" %d.pub'
        public.with_suffix("").write_text("private fixture; never read")
        machine = Machine.from_dict({**self.machine.config(), "ssh_public_keys": [str(public)]})
        result = self.save(machine=machine)
        self.assertEqual(result["command"], "ssh dev")
        self.assertEqual(result["backup"], "")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.parent.stat().st_mode & 0o777, 0o700)
        parsed = subprocess.run(["ssh", "-G", "-F", str(self.config), "dev"], check=True, capture_output=True, text=True, timeout=10).stdout.splitlines()
        for setting in ("hostname 192.0.2.10", "user jay.morris", "port 22", "hostkeyalias appletart-dev", "stricthostkeychecking accept-new"):
            self.assertIn(setting, parsed)
        self.assertIn("IdentityFile ", self.config.read_text())
        self.assertIn("%%d", self.config.read_text())
        self.assertIn("UserKnownHostsFile ", self.config.read_text())

    def test_preserves_user_bytes_and_global_scope_and_backs_up_before_changes(self):
        original = b'# Personal configuration\r\nUser fallback\r\nHost existing\r\n    HostName 192.0.2.1\r\n# caf\xc3\xa9'
        self.original(original)
        result = self.save()
        self.assertTrue(self.config.read_bytes().endswith(original))
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        parsed = subprocess.run(["ssh", "-G", "-F", str(self.config), "existing"], check=True, capture_output=True, text=True, timeout=10).stdout.splitlines()
        self.assertIn("user fallback", parsed)
        self.assertIn("hostname 192.0.2.1", parsed)

    def test_repeated_save_is_idempotent_and_ip_changes_replace_only_that_vm(self):
        self.original(b"Host original\n    HostName 192.0.2.1\n")
        self.save()
        second = Machine.from_dict({"name": "second"})
        self.save("192.0.2.20", second)
        before = self.config.read_bytes()
        backups = list((self.config.parent / "appletart-backups").iterdir())
        result = self.save()
        self.assertFalse(result["changed"])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list((self.config.parent / "appletart-backups").iterdir()), backups)
        result = self.save("192.0.2.11")
        self.assertEqual(Path(result["backup"]).read_bytes(), before)
        self.assertEqual(self.config.read_text().count("Host dev\n"), 1)
        self.assertIn("HostName 192.0.2.11", self.config.read_text())
        self.assertIn("HostName 192.0.2.20", self.config.read_text())
        self.assertTrue(self.config.read_bytes().endswith(b"Host original\n    HostName 192.0.2.1\n"))

    def test_unmanaged_alias_conflicts_and_damaged_markers_preserve_existing_file(self):
        contents = [b"Host = DEV other\n    HostName 192.0.2.1\n",
                    b"# BEGIN APPLETART SSH CONFIG: dev\nHost dev\n",
                    b"# END APPLETART SSH CONFIG: dev\n"]
        self.config.parent.mkdir()
        for original in contents:
            with self.subTest(original=original):
                self.config.write_bytes(original)
                with self.assertRaises(DeploymentError):
                    self.save()
                self.assertEqual(self.config.read_bytes(), original)
                self.assertFalse((self.config.parent / "appletart-backups").exists())

    def test_preserves_dotfile_symlink_when_updating_its_target(self):
        self.config.parent.mkdir()
        target = self.root / "dotfiles-config"
        target.write_bytes(b"Host personal\n    HostName 192.0.2.1\n")
        self.config.symlink_to(target)
        self.save()
        self.assertTrue(self.config.is_symlink())
        self.assertIn("Host dev\n", target.read_text())
        self.assertTrue(target.read_bytes().endswith(b"Host personal\n    HostName 192.0.2.1\n"))

    def test_failed_atomic_replace_keeps_original_config_and_removes_temporary_file(self):
        original = b"Host personal\n    HostName 192.0.2.1\n"
        self.original(original)
        replace = Path.replace
        def failed(path, target):
            if target == self.config:
                raise OSError("test write failure")
            return replace(path, target)
        with patch("appletart.ssh_config.Path.replace", new=failed):
            with self.assertRaisesRegex(DeploymentError, "Cannot update"):
                self.save()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.config.parent.glob(".appletart-config-*.tmp")), [])

    def test_external_edit_during_save_is_preserved(self):
        self.original(b"# original\n")
        write = ssh_config._private_write
        def edited(path, data):
            write(path, data)
            if path.suffix == ".tmp":
                self.config.write_bytes(b"# an editor changed this file\n")
        with patch("appletart.ssh_config._private_write", side_effect=edited):
            with self.assertRaisesRegex(DeploymentError, "changed while saving"):
                self.save()
        self.assertEqual(self.config.read_bytes(), b"# an editor changed this file\n")
        self.assertEqual(list(self.config.parent.glob(".appletart-config-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
