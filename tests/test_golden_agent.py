from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart.catalog import Machine
from appletart.cloud import BuildConnection, cloud_config, provision_cloud, wait_for_build_agent
from appletart.deployment import DeploymentError
from appletart.operations import JobCancelled
from test_ssh import PUBLIC_KEY


class GoldenAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.public = self.root / "user.pub"
        self.public.write_text(PUBLIC_KEY + "\n")
        self.machine = Machine.from_dict({"name": "clone", "os": "ubuntu", "source_kind": "golden",
            "source": "ubuntu-golden", "ssh_user": "new.user", "ssh_public_keys": [str(self.public)],
            "install_default_packages": False})
        self.backend = Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10"))
        self.report = Mock()

    def test_privileged_golden_clone_never_waits_for_or_uses_ssh(self):
        commands = []
        def guest(command, **kwargs):
            commands.append((command, kwargs.get("input", "")))
            self.assertNotEqual(command[0], "ssh", "Golden clone used SSH instead of its inherited agent")
            output = PUBLIC_KEY if "for key_file in /etc/ssh/ssh_host_" in kwargs.get("input", "") else json.dumps({"status": "done", "errors": []}) if "json" in command else "status: done"
            return subprocess.CompletedProcess(command, 0, output, "")
        with patch("appletart.cloud.subprocess", Mock(Popen=Mock(return_value=Mock(poll=Mock(return_value=None))), TimeoutExpired=subprocess.TimeoutExpired)), \
             patch("appletart.cloud.wait_for_ssh", side_effect=AssertionError("Golden clone used SSH readiness")) as ssh, \
             patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.cloud.run", side_effect=guest), \
             patch("appletart.cloud.install_agent") as install:
            provision_cloud(self.backend, self.machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/clone.log", self.report)
        ssh.assert_not_called()
        install.assert_not_called()
        self.assertTrue(any("cloud-init" in command and "--wait" in command for command, _ in commands))
        self.assertTrue(all("sudo" not in command for command, _ in commands if "cloud-init" in command))
        revoke = next(command for command, script in commands if "authorized_keys.appletart" in script)
        self.assertEqual(revoke[:4], ["fake-tart", "exec", "-i", "clone"])
        self.assertIn("new.user", revoke, "Key checks and revocation must run in the new user's home, not root's")
        self.assertIn("-H", revoke)
        self.assertEqual((self.root / "known_hosts").read_text(), "appletart-clone " + PUBLIC_KEY + "\n")
        self.assertEqual((self.root / "known_hosts").stat().st_mode & 0o777, 0o600)

    def test_invalid_agent_host_keys_do_not_replace_the_trust_record(self):
        known = self.root / "known_hosts"
        known.write_text("preserved host key\n")
        connection = BuildConnection(self.backend, self.machine)
        with patch.object(connection, "run", return_value=subprocess.CompletedProcess([], 0, "invalid key\n", "")), \
             self.assertRaises(DeploymentError):
            connection.save_host_keys(self.root, self.report)
        self.assertEqual(known.read_text(), "preserved host key\n")
        self.assertFalse(list(self.root.glob("host-keys-*")))

    def test_unsupported_dsa_host_key_does_not_discard_supported_host_keys(self):
        connection = BuildConnection(self.backend, self.machine)
        with patch.object(connection, "run", return_value=subprocess.CompletedProcess([], 0,
                "ssh-dss obsolete-host-key\n" + PUBLIC_KEY + "\n", "")):
            connection.save_host_keys(self.root, self.report)
        self.assertEqual((self.root / "known_hosts").read_text(), "appletart-clone " + PUBLIC_KEY + "\n")
        self.assertTrue(any("ssh-dss" in str(call) for call in self.report.call_args_list))

    def test_supported_malformed_host_key_and_all_unsupported_keys_fail_closed(self):
        connection = BuildConnection(self.backend, self.machine)
        for output in ("ssh-dss obsolete-host-key\n", PUBLIC_KEY + "\nssh-ed25519 invalid\n"):
            with self.subTest(output=output), patch.object(connection, "run", return_value=subprocess.CompletedProcess([], 0, output, "")), self.assertRaises(DeploymentError):
                connection.save_host_keys(self.root, self.report)
        self.assertFalse((self.root / "known_hosts").exists())

    def test_legacy_image_can_fall_back_to_ssh_before_starting_setup(self):
        with patch("appletart.cloud.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))), \
             patch("appletart.cloud.wait_for_build_agent", return_value=False), \
             patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10") as ssh, \
             patch("appletart.cloud.run", return_value=subprocess.CompletedProcess([], 0, "status: done", "")) as run, \
             patch("appletart.cloud.verify_management"), \
             patch("appletart.cloud.install_agent") as install, \
             patch("appletart.cloud.read_public_keys", return_value=[]):
            provision_cloud(self.backend, self.machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/clone.log", self.report)
        ssh.assert_called_once()
        install.assert_called_once()
        self.assertTrue(all(call.args[0][0] == "ssh" for call in run.call_args_list))

    def test_agent_failure_preserves_credentials_and_never_switches_to_ssh(self):
        def guest(command, **kwargs):
            if "json" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps({"status": "running", "errors": []}), "")
            return subprocess.CompletedProcess(command, 0, "status: done", "")
        with patch("appletart.cloud.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))), \
             patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.cloud.wait_for_ssh") as ssh, \
             patch("appletart.cloud.run", side_effect=guest) as run, \
             patch("appletart.cloud.capture_guest", return_value=True) as capture, \
             self.assertRaisesRegex(DeploymentError, "completion could not be verified"):
            provision_cloud(self.backend, self.machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/clone.log", self.report)
        ssh.assert_not_called()
        self.assertFalse(any("authorized_keys.appletart" in call.kwargs.get("input", "") for call in run.call_args_list))
        self.assertEqual(capture.call_args.kwargs["command"], ["fake-tart", "exec", "-i", "clone", "sh", "-s"])

    def test_agent_readiness_is_bounded_cancellable_and_detects_early_guest_exit(self):
        with patch("appletart.cloud.time.monotonic", side_effect=[0, 91]):
            self.assertFalse(wait_for_build_agent(self.backend, self.machine, Mock(), self.report))
        with patch("appletart.guest_agent.root_available", return_value=False), \
             patch("appletart.cloud.pause", side_effect=JobCancelled("cancelled")), self.assertRaises(JobCancelled):
            wait_for_build_agent(self.backend, self.machine, Mock(poll=Mock(return_value=None)), self.report)
        with self.assertRaisesRegex(DeploymentError, "Tart exited before the guest agent"):
            wait_for_build_agent(self.backend, self.machine, Mock(poll=Mock(return_value=1)), self.report)

    def test_administrative_commands_elevate_only_on_ssh_transport(self):
        root = BuildConnection(self.backend, self.machine)
        ssh = BuildConnection(self.backend, self.machine, ["ssh", "user@guest"])
        self.assertEqual(root.command("cloud-init status --wait", root=True), ["fake-tart", "exec", "clone", "cloud-init", "status", "--wait"])
        self.assertEqual(ssh.command("cloud-init status --wait", root=True), ["ssh", "user@guest", "sudo -n -- cloud-init status --wait"])

    def test_clone_boot_seed_upgrades_once_without_waiting_for_network_target(self):
        from appletart import guest_agent
        script = cloud_config(self.machine, ["ssh-ed25519 user"])["bootcmd"][0][2]
        self.assertEqual(script, guest_agent.reuse_script())
        for prefix in ("/usr/local/bin", "/etc/systemd/system"):
            directory = self.root / prefix.lstrip("/")
            directory.mkdir(parents=True)
            script = script.replace(prefix, str(directory))
        binary = self.root / "usr/local/bin/appletart-guest-agent"
        binary.write_bytes(b"inherited verified binary")
        binary.chmod(0o755)
        unit = self.root / "etc/systemd/system/appletart-guest-agent.service"
        unit.write_text("User=nobody\nExecStart=agent --exec-wrapper=/usr/bin/false\n")
        commands = self.root / "bin"
        commands.mkdir()
        systemctl = commands / "systemctl"
        systemctl.write_text('''#!/bin/sh
case "$*" in
    *--no-block*) ;;
    *--now*|start*|restart*)
        echo 'Synchronous agent startup waits for network.target while cloud-init bootcmd is still running' >&2
        exit 1 ;;
esac
printf "%s\\n" "$*" >> "$AGENT_TEST_CALLS"
''')
        systemctl.chmod(0o755)
        # Early cloud-init runs as root, before PAM can create login sessions.
        # Calling sudo here reproduces the RHEL/Rocky boot dependency stall.
        (commands / "id").write_text('#!/bin/sh\necho 0\n')
        (commands / "id").chmod(0o755)
        (commands / "sudo").write_text('#!/bin/sh\necho sudo >> "$AGENT_TEST_CALLS"\nexit 99\n')
        (commands / "sudo").chmod(0o755)
        calls = self.root / "service-calls"
        for _ in range(2):
            result = subprocess.run(["sh", "-s"], input=script, text=True, capture_output=True,
                env={**os.environ, "PATH": str(commands) + ":/usr/bin:/bin", "AGENT_TEST_CALLS": str(calls)})
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("User=root", unit.read_text())
        self.assertNotIn("exec-wrapper", unit.read_text())
        self.assertEqual(calls.read_text().count("restart appletart-guest-agent.service"), 1)
        self.assertNotIn("sudo", calls.read_text())
        self.assertIn("disable tart-guest-agent.service", calls.read_text())
        self.assertEqual(binary.read_bytes(), b"inherited verified binary")


if __name__ == "__main__":
    unittest.main()
