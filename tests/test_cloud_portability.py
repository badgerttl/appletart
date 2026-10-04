"""Replay cloud-init package and service setup against minimal guest fixtures."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from appletart.catalog import Machine
from appletart.cloud import cloud_config


class CloudPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.machine = Machine.from_dict({"name": "rhel-test", "os": "other", "source_kind": "cloud",
                                         "source": "/tmp/rhel-aarch64.qcow2", "ssh_user": "jay.morris",
                                         "ssh_public_keys": ["/tmp/user.pub"], "packages": []})
        self.env = {**os.environ, "PATH": str(self.bin) + ":" + os.environ["PATH"],
                    "APPLETART_FIXTURE_ROOT": str(self.root)}

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
        path.chmod(0o755)

    def test_initial_cloud_image_updates_and_upgrades_without_extra_packages(self):
        config = cloud_config(self.machine, [])
        self.assertNotIn("packages", config, "cloud-init's package schema requires a nonempty list")
        self.assertTrue(config["package_update"])
        self.assertTrue(config["package_upgrade"])
        self.assertFalse(config["package_reboot_if_required"], "Deployment boots the upgraded image after build verification")

    def test_golden_clone_reuses_upgraded_packages_without_repository_access(self):
        machine = Machine.from_dict({**self.machine.config(), "source_kind": "golden", "source": "prepared"})
        config = cloud_config(machine, [])
        self.assertFalse(config["package_update"])
        self.assertFalse(config["package_upgrade"])
        self.assertNotIn("packages", config)
        config = cloud_config(Machine.from_dict({**machine.config(), "packages": ["git"]}), [])
        self.assertTrue(config["package_update"])
        self.assertFalse(config["package_upgrade"])
        self.assertEqual(config["packages"], ["git"])

    def test_ssh_service_setup_supports_both_debian_and_red_hat_units(self):
        self.command("systemctl", '''
case "$1" in
    cat) test "$2" = "$APPLETART_SSH_UNIT" ;;
    enable) test "$2" = "--now"; test "${3%.service}.service" = "$APPLETART_SSH_UNIT"
            echo "$APPLETART_SSH_UNIT" > "$APPLETART_FIXTURE_ROOT/enabled" ;;
    set-default) test "$2" = "multi-user.target" ;;
    *) exit 1 ;;
esac''')
        for unit in ("ssh.service", "sshd.service"):
            with self.subTest(unit=unit):
                (self.root / "enabled").unlink(missing_ok=True)
                for command in cloud_config(self.machine, [])["runcmd"]:
                    result = subprocess.run(command, env={**self.env, "APPLETART_SSH_UNIT": unit},
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, f"{unit}: {result.stderr}")
                self.assertEqual((self.root / "enabled").read_text().strip(), unit)

    def test_requested_packages_still_update_and_install(self):
        machine = Machine.from_dict({**self.machine.config(), "packages": ["git", "curl", "git"]})
        config = cloud_config(machine, [])
        self.assertTrue(config["package_update"])
        self.assertTrue(config["package_upgrade"])
        self.assertEqual(config["packages"], ["git", "curl"])


if __name__ == "__main__":
    unittest.main()
