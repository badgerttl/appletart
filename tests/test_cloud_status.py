"""Regression coverage for Ubuntu's completed-but-degraded first boot."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart.catalog import Machine
from appletart.cloud import provision_cloud, write_seed
from appletart.deployment import DeploymentError

# Captured Ubuntu 26.04 result: setup completed; renaming its live NIC failed.
DEGRADED_OUTPUT = '''status: done
extended_status: degraded done
errors: []
recoverable_errors:
WARNING:
 - Failed to rename devices: [busy] Error renaming enp0s1 to eth0
'''
DEGRADED_STATUS = {"status": "done", "extended_status": "degraded done", "errors": [],
                   "recoverable_errors": {"WARNING": ["[busy] Error renaming enp0s1 to eth0"]}}


class CloudStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.machine = Machine.from_dict({"name": "ubuntu-test", "os": "ubuntu", "source_kind": "cloud",
            "source": "https://example.invalid/ubuntu.img", "ssh_public_keys": [str(self.root / "user.pub")],
            "ssh_user": "jay.morris", "packages": [], "install_default_packages": False})
        self.calls = []
        self.report = Mock()

    def provision(self, *, status=DEGRADED_STATUS, health_exit=0, exit_code=2, setup_output=""):
        def ssh(args, **kwargs):
            self.calls.append((args[-1], kwargs.get("input", "")))
            if args[-1] == "sudo -n -- cloud-init status --wait --long":
                return subprocess.CompletedProcess(args, exit_code, DEGRADED_OUTPUT if exit_code == 2 else "status: error", "")
            if args[-1] == "sudo -n -- cloud-init status --format json":
                output = status if isinstance(status, str) else json.dumps(status)
                return subprocess.CompletedProcess(args, 2, output, "")
            if args[-1] == "sudo -n -- tail -n 60 /var/log/cloud-init-output.log":
                return subprocess.CompletedProcess(args, 0, setup_output, "")
            if "=== Cloud build health ===" in kwargs.get("input", ""):
                return subprocess.CompletedProcess(args, health_exit, "guest health probe", "network is not ready" if health_exit else "")
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch("appletart.cloud.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))), \
             patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.cloud.run", side_effect=ssh), \
             patch("appletart.cloud.stop_build"), \
             patch("appletart.cloud.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.cloud.verify_management"), \
             patch("appletart.cloud.install_agent") as agent:
            self.agent = agent
            provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10")), self.machine,
                self.root / "seed.iso", self.root / "bootstrap", "ssh-ed25519 build", self.root / "logs/vm.log", self.report)

    def test_degraded_completion_keeps_state_and_finishes_after_health_checks(self):
        self.provision()
        self.agent.assert_called_once()
        self.assertNotIn("sudo -n -- cloud-init clean", [command for command, _ in self.calls])
        self.assertTrue(any("authorized_keys.appletart" in script for _, script in self.calls))
        self.assertTrue(any("warnings" in call.args[0].lower() for call in self.report.call_args_list))

    def test_degraded_result_with_bad_network_preserves_state_and_build_key(self):
        with self.assertRaisesRegex(DeploymentError, "health"):
            self.provision(health_exit=1)
        self.agent.assert_not_called()
        self.assertNotIn("sudo -n -- cloud-init clean", [command for command, _ in self.calls])
        self.assertFalse(any("authorized_keys.appletart" in script for _, script in self.calls))

    def test_code_two_is_not_accepted_without_completed_error_free_status(self):
        for status in ("not JSON", {**DEGRADED_STATUS, "status": "running"},
                       {**DEGRADED_STATUS, "errors": ["fatal setup failure"]},
                       {**DEGRADED_STATUS, "modules-final": {"errors": ["script failed"]}},
                       {**DEGRADED_STATUS, "modules-final": {"start": 10, "finished": None}}):
            with self.subTest(status=status), self.assertRaises(DeploymentError):
                self.provision(status=status)
            self.assertNotIn("sudo -n -- cloud-init clean", [command for command, _ in self.calls])

    def test_repository_failure_explains_how_to_retry_rhel_customization(self):
        with self.assertRaisesRegex(DeploymentError, "publisher.*registration"):
            self.provision(exit_code=1, setup_output="Error: There are no enabled repositories")
        self.assertIn("sudo -n -- cloud-init clean", [command for command, _ in self.calls])

    def test_mac_matched_seed_preserves_names_and_repairs_eni_alias(self):
        def iso(args):
            Path(args[args.index("-o") + 1]).write_bytes(b"ISO")
            return ""
        with patch("appletart.cloud.run_tool", side_effect=iso):
            write_seed(self.machine, [], self.root / "cloud", "instance-test", "02:00:00:00:00:01")
        network = json.loads((self.root / "cloud/seed-data/network-config").read_text())
        nic = network["ethernets"]["appletart"]
        self.assertEqual(nic["match"], {"macaddress": "02:00:00:00:00:01"})
        self.assertTrue(nic["dhcp4"])
        self.assertNotIn("set-name", nic)
        config = json.loads((self.root / "cloud/seed-data/user-data").read_text().split('\n',1)[1])
        self.assertTrue(any('APPLETART_NETWORK_ERROR' in command[-1] and '02:00:00:00:00:01' in command[-1]
                            for command in config['bootcmd']), 'ENI must resolve the MAC to a real interface during first boot')


if __name__ == "__main__":
    unittest.main()
