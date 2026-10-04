import base64
import struct
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import signal

from appletart.deployment import DeploymentError, VM, load_manifest
from appletart.ssh import install_script, provision_keys, read_public_keys, wait_for_ssh
from appletart.tart import Tart


KEY_TYPE = b"ssh-ed25519"
KEY_BLOB = struct.pack(">I", len(KEY_TYPE)) + KEY_TYPE + struct.pack(">I", 32) + bytes(range(32))
PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(KEY_BLOB).decode()


class KeyTests(unittest.TestCase):
    def test_reads_valid_key_and_deduplicates_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.pub"
            second = Path(directory) / "second.pub"
            first.write_text(PUBLIC_KEY + " first\n")
            second.write_text(PUBLIC_KEY + " second\n")
            self.assertEqual(read_public_keys((first, second)), [PUBLIC_KEY])

    def test_rejects_private_invalid_missing_and_multiple_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.pub"
            with self.assertRaises(DeploymentError):
                read_public_keys((path,))
            for content in ("-----BEGIN OPENSSH PRIVATE KEY-----", "ssh-ed25519 invalid", PUBLIC_KEY + "\n" + PUBLIC_KEY):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(DeploymentError):
                    read_public_keys((path,))
            with self.assertRaises(DeploymentError):
                read_public_keys((Path(directory) / "id_ed25519",))

    def test_relative_key_paths_are_relative_to_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "vm.toml"
            manifest.write_text('version=1\n[[vms]]\nname="vm"\nos="ubuntu"\nssh_public_keys=["keys/me.pub"]\n')
            self.assertEqual(load_manifest(manifest)[0].ssh_public_keys, (Path(directory).resolve() / "keys/me.pub",))

    def test_kali_requires_existing_username_and_fields_are_validated(self):
        for fields in ({"ssh_public_keys": "key.pub"}, {"ssh_public_keys": [1]}, {"ssh_user": "a; id"}, {"os": "kali", "image": "template", "ssh_public_keys": ["key.pub"]}):
            with self.subTest(fields=fields), self.assertRaises(DeploymentError):
                VM.from_dict({"name": "vm", "os": "ubuntu", **fields})

    def test_guest_script_preserves_keys_deduplicates_and_sets_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            ssh = Path(directory) / ".ssh"
            ssh.mkdir()
            authorized = ssh / "authorized_keys"
            original = PUBLIC_KEY + " existing comment"
            authorized.write_text(original) # No final newline.
            extra = "ssh-ed25519 another-test-key"
            script = install_script([PUBLIC_KEY, extra]).replace("$HOME", directory)
            for _ in range(2):
                subprocess.run(["sh", "-s"], input=script, text=True, check=True)
            self.assertEqual(authorized.read_text().splitlines(), [original, extra])
            self.assertEqual(ssh.stat().st_mode & 0o777, 0o700)
            self.assertEqual(authorized.stat().st_mode & 0o777, 0o600)

    def test_invalid_key_prevents_inventory_or_clone(self):
        tart = object.__new__(Tart)
        tart.inventory = Mock()
        with patch("appletart.tart.check_ssh_tools"), self.assertRaises(DeploymentError):
            tart.deploy([VM.from_dict({"name": "vm", "os": "ubuntu", "ssh_public_keys": ["missing.pub"]})])
        tart.inventory.assert_not_called()

    def test_key_provisioning_occurs_after_clone_and_configuration(self):
        vm = VM.from_dict({"name": "vm", "os": "ubuntu", "ssh_public_keys": ["test.pub"]})
        tart = object.__new__(Tart)
        tart.inventory = Mock(return_value=[])
        tart.run = Mock()
        def installed(backend, target, keys):
            self.assertEqual([call.args[0] for call in tart.run.call_args_list], vm.commands())
            self.assertEqual(keys, [PUBLIC_KEY])
        with patch("appletart.tart.check_ssh_tools"), patch("appletart.tart.read_public_keys", return_value=[PUBLIC_KEY]), patch("appletart.tart.provision_keys", side_effect=installed) as provision:
            tart.deploy([vm])
        provision.assert_called_once()

    def test_auth_failure_stops_the_boot_process(self):
        vm = VM.from_dict({"name": "vm", "os": "ubuntu"})
        tart = Mock(binary="tart")
        process = Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, patch("appletart.ssh.subprocess.Popen", return_value=process), patch("appletart.ssh.wait_for_ssh", return_value="192.0.2.10"), patch("appletart.ssh.subprocess.run", side_effect=subprocess.CalledProcessError(255, "ssh")) as ssh:
            with self.assertRaisesRegex(DeploymentError, "Guest provisioning failed"):
                provision_keys(tart, vm, [PUBLIC_KEY], known_hosts=Path(directory) / "known_hosts")
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.wait.assert_called_once_with(timeout=30)
        self.assertIn("StrictHostKeyChecking=accept-new", ssh.call_args.args[0])
        self.assertIn(PUBLIC_KEY, ssh.call_args.kwargs["input"])

    def test_early_boot_exit_is_reported(self):
        process = Mock()
        process.poll.return_value = 1
        with self.assertRaisesRegex(DeploymentError, "exited before SSH"):
            wait_for_ssh(Mock(), VM.from_dict({"name": "vm", "os": "ubuntu"}), process)


if __name__ == "__main__":
    unittest.main()
