"""Regression coverage for the October 4 security and lifecycle review."""

import os
import hashlib
import plistlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart import capabilities, guest_agent
from appletart.cloud import cloud_config, convert_disk
from appletart.catalog import Machine
from appletart.deployment import DeploymentError
from appletart.lifecycle import Backend, Lifecycle
from appletart.ssh import provision_keys
from test_management import DiskBackend


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img required")
    def test_external_qcow2_data_is_rejected_before_qemu_opens_it(self):
        external = self.root / "host-only.raw"
        image = self.root / "image.qcow2"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-o",
                        f"data_file={external},data_file_raw=on", str(image), "1M"],
                       check=True, capture_output=True, timeout=10)
        marker = b"HOST-ONLY-FIXTURE\n"
        external.write_bytes(marker + bytes(1024 * 1024 - len(marker)))
        with patch("appletart.cloud.run_tool", wraps=__import__("appletart.cloud", fromlist=["run_tool"]).run_tool) as qemu:
            with self.assertRaisesRegex(DeploymentError, "external"):
                convert_disk(image, self.root / "cache", 1, Mock())
            qemu.assert_not_called()
        self.assertTrue(external.read_bytes().startswith(marker))

    def test_template_password_retirement_follows_management_verification(self):
        events = []
        backend = Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10"))
        vm = Machine.from_dict({"name": "fixture", "ssh_public_keys": []}).vm
        process = Mock(poll=Mock(return_value=None))
        with patch("appletart.ssh.subprocess.Popen", return_value=process), \
             patch("appletart.ssh.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.ssh.ssh_install"), \
             patch("appletart.guest_agent.root_available", return_value=False), \
             patch("appletart.guest_agent.verify_management", side_effect=lambda *a: events.append("verify")), \
             patch("appletart.users.retire_template_password", create=True, side_effect=lambda *a: events.append("retire")) as retire, \
             patch("appletart.users.install", side_effect=lambda *a, **k: events.append("users")), \
             patch("appletart.guest_os.observe", return_value={}):
            provision_keys(backend, vm, [], agent_binary=b"fixture-agent", known_hosts=self.root / "known_hosts",
                           users=[{"username": "admin", "password": "fixture-password", "ssh_authorized_keys": []}])
        self.assertEqual(events, ["verify", "retire", "users"])
        self.assertEqual(retire.call_args.args[:2], (["fake-tart", "exec", "-i", "fixture", "/bin/sh", "-s"], "admin"))

    def test_golden_cleanup_removes_passwords_and_app_password_policy(self):
        for folder in ("etc/ssh", "var/lib/dbus", "home/developer/.ssh", "bin"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        shadow = self.root / "etc/shadow"
        shadow.write_text("developer:old-fixture-hash:20000:0:99999:7:::\n")
        keys = self.root / "home/developer/.ssh/authorized_keys"
        keys.write_text("fixture-public-key\n")
        (self.root / "etc/login.defs").write_text("UID_MIN 1000\n")
        policy = self.root / "etc/ssh/sshd_config"
        policy.write_text("PasswordAuthentication no\n\nMatch all\nMatch User developer\n    PasswordAuthentication yes\n    AuthenticationMethods any\nMatch all\n"
                          "# BEGIN APPLETART PASSWORD ACCESS\nMatch all\nMatch User other\n    PasswordAuthentication yes\n    AuthenticationMethods any\nMatch all\n# END APPLETART PASSWORD ACCESS\n"
                          "Match User manual-policy\n    AllowTcpForwarding no\nMatch all\n")
        def command(name, body):
            path = self.root / "bin" / name
            path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
            path.chmod(0o700)
        command("cloud-init", 'if [ "$2" = "--help" ]; then echo "--logs --machine-id --seed"; fi')
        command("getent", 'printf "developer:x:1001:1001::%s/home/developer:/bin/bash\\n" "$APPLETART_FIXTURE_ROOT"')
        command("usermod", 'test "$1" = "--password"; test "$2" = "!"; test "$3" = "--"; test "$4" = "developer"\nsed "s/old-fixture-hash/!/" "$APPLETART_FIXTURE_ROOT/etc/shadow" > "$APPLETART_FIXTURE_ROOT/etc/shadow.new"\nmv "$APPLETART_FIXTURE_ROOT/etc/shadow.new" "$APPLETART_FIXTURE_ROOT/etc/shadow"')
        script = "set -eu\n" + capabilities.GOLDEN_CLEANUP
        for source in ("/etc", "/var/lib/dbus"):
            script = script.replace(source, str(self.root) + source)
        result = subprocess.run(["/bin/sh", "-s"], input=script, text=True, capture_output=True, timeout=5,
                                env={**os.environ, "PATH": str(self.root / "bin") + ":" + os.environ["PATH"], "APPLETART_FIXTURE_ROOT": str(self.root)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("old-fixture-hash", shadow.read_text())
        self.assertNotIn("Match User developer", policy.read_text())
        self.assertNotIn("APPLETART PASSWORD ACCESS", policy.read_text())
        self.assertNotIn("Match User other", policy.read_text())
        self.assertIn("Match User manual-policy\n    AllowTcpForwarding no", policy.read_text())
        self.assertEqual(keys.read_text(), "")

    def test_failed_account_enumeration_aborts_golden_cleanup(self):
        binaries = self.root / "bin"
        binaries.mkdir()
        for name, body in (("usermod", "exit 0"), ("getent", "exit 1")):
            command = binaries / name
            command.write_text("#!/bin/sh\n" + body + "\n")
            command.chmod(0o700)
        result = subprocess.run(["/bin/sh", "-s"], input=capabilities.GOLDEN_PASSWORD_CLEANUP,
                                capture_output=True, text=True, timeout=5,
                                env={**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"]})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("passwords and AppleTart password SSH rules cleared", result.stdout)

    def test_legacy_golden_clone_sanitizes_passwords_once_before_new_password_setup(self):
        machine = Machine.from_dict({"name": "clone", "os": "other", "source_kind": "golden", "source": "legacy-golden"})
        commands = cloud_config(machine, ["fixture-public-key"])["runcmd"]
        self.assertTrue(any("usermod" in str(command) and "--password" in str(command) for command in commands))
        boot_only = cloud_config(machine, [], boot_only=True)
        self.assertNotIn("usermod", str(boot_only))
        self.assertNotIn("usermod", str(cloud_config(machine, [])['bootcmd']))

    def test_disk_capacity_uses_tarts_decimal_gb(self):
        directory = self.root / "vms/template"
        directory.mkdir(parents=True)
        disk = directory / "disk.img"
        backend = Backend.__new__(Backend)
        for size, expected in ((80_000_000_000, 80), (80_000_000_001, 81)):
            with disk.open("wb") as output:
                output.truncate(size)
            with patch.object(backend, "identity", return_value={"home": str(self.root)}):
                self.assertEqual(backend.disk_capacity_gb("template"), expected)

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img required")
    def test_external_qcow2_backing_is_rejected_before_inspection(self):
        backing = self.root / "host.raw"
        backing.write_bytes(bytes(1024 * 1024))
        image = self.root / "backed.qcow2"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", str(backing), str(image)],
                       check=True, capture_output=True, timeout=10)
        with patch("appletart.cloud.run_tool") as tool, self.assertRaisesRegex(DeploymentError, "external"):
            convert_disk(image, self.root / "cache", 1, Mock())
        tool.assert_not_called()

    def test_template_build_resume_uses_root_agent_after_password_retirement(self):
        backend = Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10"))
        vm = Machine.from_dict({"name": "retry", "ssh_public_keys": []}).vm
        with patch("appletart.ssh.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))), \
             patch("appletart.ssh.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.ssh.ssh_install") as ssh, \
             patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.guest_agent.install_via_agent") as upgrade, \
             patch("appletart.ssh.stream") as provision, \
             patch("appletart.guest_agent.verify_management"), \
             patch("appletart.users.retire_template_password"), \
             patch("appletart.guest_os.observe", return_value={}):
            provision_keys(backend, vm, ["ssh-ed25519 fixture-key"], agent_binary=b"fixture-agent", known_hosts=self.root / "known_hosts")
        ssh.assert_not_called()
        upgrade.assert_called_once()
        self.assertEqual(provision.call_args.args[0], ["fake-tart", "exec", "-i", "retry", "/usr/bin/sudo", "-n", "-H", "-u", "admin", "--", "/bin/sh", "-s"])
        self.assertIn("fixture-key", provision.call_args.kwargs["input"])

    def test_unverified_agent_never_retires_the_template_password(self):
        backend = Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10"))
        with patch("appletart.ssh.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))), \
             patch("appletart.ssh.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.ssh.ssh_install"), \
             patch("appletart.guest_agent.root_available", return_value=False), \
             patch("appletart.guest_agent.verify_management", side_effect=DeploymentError("unverified")), \
             patch("appletart.users.retire_template_password") as retire:
            with self.assertRaisesRegex(DeploymentError, "unverified"):
                provision_keys(backend, Machine.from_dict({"name": "fixture"}).vm, [], agent_binary=b"fixture-agent", known_hosts=self.root / "known_hosts")
        retire.assert_not_called()


class AgentUpgradeRegressionTests(unittest.TestCase):
    def test_macos_upgrade_schedules_independent_launchd_helper_and_verifies_binary(self):
        binary = b"\xca\xfe\xba\xbe" + b"fixture macOS agent"
        with patch("appletart.guest_agent.run", side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "APPLETART_AGENT_VERIFIED\n", "")]) as transport:
            guest_agent.install_via_agent(Mock(binary="tart"), "dev", None, Mock(), binary=binary, family="macos")
        stage = transport.call_args_list[0].kwargs["input"]
        verification = transport.call_args_list[1].kwargs["input"]
        xml = stage.split("<<'APPLETART_RESTART_PLIST'\n", 1)[1].split("APPLETART_RESTART_PLIST\n", 1)[0]
        helper = plistlib.loads(xml.encode())
        self.assertTrue(helper["Label"].startswith("org.appletart.guest-management-upgrade."))
        self.assertTrue(helper["RunAtLoad"])
        self.assertEqual(helper["ProgramArguments"][:2], ["/bin/sh", "-c"])
        restart = helper["ProgramArguments"][2]
        self.assertIn("launchctl bootstrap system /Library/LaunchDaemons/org.appletart.guest-management.plist", restart)
        self.assertIn("rm -f /Library/LaunchDaemons/" + helper["Label"], restart)
        self.assertIn(hashlib.sha256(binary).hexdigest(), verification)
        for script in (stage, restart, verification):
            result = subprocess.run(["/bin/sh", "-n"], input=script, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_linux_rpc_upgrade_survives_restart_and_verifies_new_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for part in ("bin", "etc/systemd/system", "usr/local/bin", "var/run"):
                (root / part).mkdir(parents=True)
            (root / "usr/local/bin/appletart-guest-agent").write_bytes(b"old agent")
            def command(name, body):
                path = root / "bin" / name
                path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
                path.chmod(0o700)
            command("sudo", 'if [ "$1" = -n ]; then shift; fi\nexec "$@"')
            command("id", 'echo 0')
            command("sha256sum", 'exec shasum -a 256 "$@"')
            command("systemctl", 'case "$1" in cat) exit 1;; restart) touch "$APPLETART_FIXTURE_ROOT/restarted";; esac')
            command("systemd-run", 'while [ "$1" != /bin/sh ]; do shift; done\n"$@"\ntouch "$APPLETART_FIXTURE_ROOT/scheduled-outside-agent"')
            attempts = []
            def transport(args, **kwargs):
                script = kwargs["input"]
                attempts.append(script)
                if len(attempts) == 2:
                    return subprocess.CompletedProcess(args, 255, "", "RPC unavailable during restart")
                for prefix in ("/usr/local", "/etc/systemd", "/var/run"):
                    script = script.replace(prefix, str(root) + prefix)
                return subprocess.run(["/bin/sh", "-s"], input=script, text=True, capture_output=True, timeout=5,
                                      env={**os.environ, "PATH": str(root / "bin") + ":" + os.environ["PATH"], "APPLETART_FIXTURE_ROOT": str(root)})
            with patch("appletart.guest_agent.run", side_effect=transport), patch("appletart.guest_agent.pause"):
                guest_agent.install_via_agent(Mock(binary="tart"), "dev", None, Mock(), binary=b"new agent fixture")
            self.assertEqual((root / "usr/local/bin/appletart-guest-agent").read_bytes(), b"new agent fixture")
            self.assertTrue((root / "restarted").is_file())
            self.assertTrue((root / "scheduled-outside-agent").is_file())
            self.assertEqual(list((root / "var/run").iterdir()), [])
            self.assertEqual(len(attempts), 3)

    def test_zero_exit_without_restart_marker_does_not_claim_success(self):
        with patch("appletart.guest_agent.run", return_value=subprocess.CompletedProcess([], 0, "0", "")), \
             patch("appletart.guest_agent.time.monotonic", side_effect=[0, 0, 45, 45]), \
             patch("appletart.guest_agent.pause"), self.assertRaisesRegex(DeploymentError, "could not be verified"):
            guest_agent.install_via_agent(Mock(binary="tart"), "dev", None, Mock(), binary=b"fixture")

    def test_password_fallback_uses_askpass_without_secret_in_arguments(self):
        secret = "fixture login password"
        with patch("appletart.guest_agent.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            guest_agent.install(["ssh", "admin@192.0.2.10"], None, Mock(), binary=b"fixture", password=secret)
        self.assertNotIn(secret, str(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["env"]["APPLETART_SSH_PASSWORD"], secret)
        self.assertFalse(Path(run.call_args.kwargs["env"]["SSH_ASKPASS"]).exists())


class LifecycleSecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = DiskBackend(self.root / "tart")
        self.api = Lifecycle(self.root / "data", lambda report=print: self.backend)
        with patch("appletart.lifecycle.guest_agent.prepare"), patch("appletart.lifecycle.provision_keys"):
            self.api.build(Machine.from_dict({"name": "dev", "ssh_public_keys": []}), Mock())

    def test_suspended_vm_restore_is_rejected_without_touching_disk_or_metadata(self):
        identifier = self.api.create_checkpoint("dev", "baseline", report=Mock())
        disk = self.root / "tart/vms/dev/disk.img"
        disk.write_bytes(b"current disk")
        saved = disk.parent / "state.vzvmsave"
        saved.write_bytes(b"current RAM state")
        self.backend.vms["dev"].update(Running=False, State="Suspended")
        record = self.api.store.get("dev")
        with self.assertRaisesRegex(DeploymentError, "stopped|suspended"):
            self.api.restore_checkpoint("dev", identifier, "dev", Mock())
        self.assertEqual(disk.read_bytes(), b"current disk")
        self.assertEqual(saved.read_bytes(), b"current RAM state")
        self.assertEqual(self.api.store.get("dev"), record)
        self.assertEqual(len(self.api.checkpoints("dev")), 1)

    def test_checkpoint_with_saved_state_is_rejected_even_if_inventory_says_stopped(self):
        (self.root / "tart/vms/dev/state.vzvmsave").write_bytes(b"saved state")
        with self.assertRaisesRegex(DeploymentError, "stopped|saved|suspended"):
            self.api.create_checkpoint("dev", "unsafe", report=Mock())
        self.assertEqual(self.api.checkpoints("dev"), [])

    def test_running_agent_only_vm_repairs_over_agent_without_ssh_or_ip(self):
        self.backend.vms["dev"]["Running"] = True
        with patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.guest_agent.install_via_agent", create=True) as install, \
             patch("appletart.lifecycle.resolve_ip", side_effect=AssertionError("IP lookup must not be needed")), \
             patch("appletart.guest_agent.install", side_effect=AssertionError("SSH must not be used")):
            self.api.install_guest_agent("dev", Mock())
        install.assert_called_once()
        self.assertTrue(self.api.store.get("dev")["guest_agent_privileged"])

    def test_stopped_agent_only_vm_repairs_then_stops_its_temporary_runtime(self):
        process = Mock(poll=Mock(return_value=None))
        with patch("appletart.lifecycle.subprocess.Popen", return_value=process), \
             patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.guest_agent.install_via_agent", create=True) as install, \
             patch("appletart.ssh.wait_for_ssh", side_effect=AssertionError("SSH must not be used")), \
             patch("appletart.lifecycle.guest_agent.prepare", return_value=b"fixture-agent"):
            self.api.install_guest_agent("dev", Mock())
        install.assert_called_once()
        process.send_signal.assert_called_once()
        self.assertFalse(self.backend.vms["dev"]["Running"])
