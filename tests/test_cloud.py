import io
import json
from pathlib import Path
import signal
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from appletart.catalog import Machine
from appletart.cloud import NETWORK_BACKEND_SETUP, cloud_config, convert_disk, provision_cloud, write_seed
from appletart.deployment import DeploymentError
from appletart.lifecycle import Lifecycle
from appletart.operations import Cancellation, JobCancelled, scope
from test_lifecycle import FakeBackend


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.machine = Machine.from_dict({"name": "kali-test", "os": "kali", "ssh_public_keys": [str(self.root / "user.pub")], "packages": ["git", "vim"], "install_default_packages": False})
        agent = patch("appletart.cloud.install_agent", create=True)
        self.install_agent = agent.start()
        self.addCleanup(agent.stop)
        management = patch("appletart.cloud.verify_management")
        self.verify_management = management.start()
        self.addCleanup(management.stop)

    def test_build_installs_agent_before_revoking_the_build_key(self):
        events = []
        process = Mock(poll=Mock(return_value=None))
        self.install_agent.side_effect = lambda *args, **kwargs: events.append("agent")
        self.verify_management.side_effect = lambda *args, **kwargs: events.append("verify")
        def ssh(*args, **kwargs):
            if "authorized_keys.appletart" in kwargs.get("input", ""):
                events.append("revoke")
            return Mock(returncode=0, stdout="status: done", stderr="")
        with patch("appletart.cloud.subprocess.Popen", return_value=process), \
             patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.cloud.subprocess.run", side_effect=ssh), \
             patch("appletart.cloud.read_public_keys", return_value=["ssh-ed25519 user"]):
            provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10")), self.machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/vm.log", Mock())
        self.assertEqual(events, ["agent", "verify", "revoke"])

    def test_build_applies_users_through_verified_agent_before_revocation(self):
        events = []
        process = Mock(poll=Mock(return_value=None))
        entries = [{"username": "analyst", "ssh_authorized_keys": [], "password": "test secret", "password_login": True}]
        self.verify_management.side_effect = lambda *args: events.append("verify")
        def ssh(*args, **kwargs):
            if "authorized_keys.appletart" in kwargs.get("input", ""): events.append("revoke")
            return Mock(returncode=0, stdout="status: done", stderr="")
        with patch("appletart.cloud.subprocess.Popen", return_value=process), \
             patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.cloud.subprocess.run", side_effect=ssh), \
             patch("appletart.cloud.read_public_keys", return_value=[]), \
             patch("appletart.users.install", side_effect=lambda *args: events.append("users")) as install:
            provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10")), self.machine,
                            self.root / "seed.iso", self.root / "bootstrap", "ssh-ed25519 build",
                            self.root / "logs/vm.log", Mock(), users=entries)
        self.assertEqual(events, ["verify", "users", "revoke"])
        self.assertEqual(install.call_args.args[0], ["fake-tart", "exec", "-i", self.machine.vm.name, "/bin/sh", "-s"])
        self.assertEqual(install.call_args.args[1], entries)

    def test_early_guest_exit_includes_the_boot_error_in_activity(self):
        process = Mock(poll=Mock(return_value=1))
        def boot(*args, **kwargs):
            kwargs["stdout"].write("guest has stopped the virtual machine\n")
            kwargs["stdout"].flush()
            return process
        with patch("appletart.cloud.subprocess.Popen", side_effect=boot), \
             self.assertRaisesRegex(DeploymentError, "guest has stopped the virtual machine"):
            provision_cloud(Mock(binary="fake-tart"), self.machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/vm.log", Mock())

    def test_kali_defaults_to_arm64_cloud_and_validates_customization(self):
        self.assertEqual(self.machine.source_kind, "cloud")
        self.assertIn("genericcloud-arm64.tar.xz", self.machine.source)
        self.assertEqual(Machine.from_dict(self.machine.config()), self.machine)
        for fields in ({"ssh_user": "root"}, {"packages": ["--option"]},
                       {"packages": ["vim; command"]}, {"hostname": "invalid_name"}, {"desktop": "unknown"}):
            with self.subTest(fields=fields), self.assertRaises(DeploymentError):
                Machine.from_dict({**self.machine.config(), **fields})

    def test_cloud_and_golden_builds_allow_agent_management_without_personal_ssh_keys(self):
        for kind in ("cloud", "golden"):
            with self.subTest(kind=kind):
                machine = Machine.from_dict({**self.machine.config(), "source_kind": kind,
                                             "source": self.machine.source if kind == "cloud" else "base-golden",
                                             "ssh_public_keys": []})
                self.assertEqual(machine.vm.ssh_public_keys, ())
                initial = cloud_config(machine, ["ssh-ed25519 temporary-build-key"])
                self.assertEqual(initial["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 temporary-build-key"])
                stable = cloud_config(machine, [])
                self.assertEqual(stable["users"][0]["ssh_authorized_keys"], [])
                self.assertEqual(stable["users"][0]["sudo"], "ALL=(ALL) NOPASSWD:ALL")
                self.assertFalse(stable["ssh_pwauth"])

    def test_build_without_personal_keys_revokes_bootstrap_only_after_agent_verification(self):
        machine = Machine.from_dict({**self.machine.config(), "ssh_public_keys": []})
        for ready in (True, False):
            with self.subTest(agent_ready=ready):
                events = []
                self.install_agent.side_effect = lambda *a, **k: events.append("install-agent")
                def verify(*args):
                    events.append("verify-agent")
                    if not ready:
                        raise DeploymentError("agent verification failed")
                self.verify_management.side_effect = verify
                def ssh(*args, **kwargs):
                    if "authorized_keys.appletart" in kwargs.get("input", ""):
                        events.append("revoke-bootstrap")
                    return Mock(returncode=0, stdout="status: done", stderr="")
                with patch("appletart.cloud.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))), \
                     patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10"), \
                     patch("appletart.cloud.subprocess.run", side_effect=ssh), \
                     patch("appletart.cloud.read_public_keys", return_value=[]):
                    if ready:
                        provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10")), machine,
                                        self.root / "seed.iso", self.root / "bootstrap", "ssh-ed25519 build",
                                        self.root / "logs/vm.log", Mock())
                    else:
                        with self.assertRaisesRegex(DeploymentError, "agent verification failed"):
                            provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10")), machine,
                                            self.root / "seed.iso", self.root / "bootstrap", "ssh-ed25519 build",
                                            self.root / "logs/vm.log", Mock())
                self.assertEqual(events, ["install-agent", "verify-agent"] + (["revoke-bootstrap"] if ready else []))

    def test_seed_creates_account_keys_network_and_custom_packages(self):
        directory = self.root / "cloud"
        def make_iso(args):
            self.assertIn("CIDATA", args)
            Path(args[args.index("-o") + 1]).write_bytes(b"ISO")
            return ""
        with patch("appletart.cloud.run_tool", side_effect=make_iso):
            seed = write_seed(self.machine, ["ssh-ed25519 public"], directory, "instance-1", "02:00:00:00:00:01")
        user = json.loads((directory / "seed-data/user-data").read_text().split("\n", 1)[1])
        self.assertEqual(user["users"][0]["name"], "kali")
        self.assertEqual(user["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 public"])
        self.assertTrue(user["users"][0]["lock_passwd"])
        self.assertFalse(user["ssh_pwauth"])
        self.assertNotIn("kali-desktop-xfce", user["packages"])
        self.assertIn("vim", user["packages"])
        self.assertIn(["systemctl", "set-default", "multi-user.target"], user["runcmd"])
        self.assertIn(["sh", "-c", NETWORK_BACKEND_SETUP], user["runcmd"])
        self.assertIn("systemctl enable netplan-configure.service", NETWORK_BACKEND_SETUP)
        self.assertEqual(json.loads((directory / "seed-data/meta-data").read_text())["instance-id"], "instance-1")
        network = json.loads((directory / "seed-data/network-config").read_text())["ethernets"]["appletart"]
        self.assertEqual(network["match"]["macaddress"], "02:00:00:00:00:01")
        self.assertEqual(network["dhcp-identifier"], "mac")
        self.assertEqual(seed.stat().st_mode & 0o777, 0o600)

    def archive(self, names):
        path = self.root / "source.tar.xz"
        with tarfile.open(path, "w:xz") as archive:
            for name in names:
                member = tarfile.TarInfo(name)
                member.size = 4
                archive.addfile(member, io.BytesIO(b"disk"))
        return path

    def test_share_edit_seed_runs_only_mount_network_and_disk_modules(self):
        machine = Machine.from_dict({**self.machine.config(), "directory_shares": [
            {"host_path": str(self.root), "guest_path": "/mnt/work", "read_only": False}]})
        directory = self.root / "share-edit"
        def make_iso(args):
            Path(args[args.index("-o") + 1]).write_bytes(b"ISO")
        with patch("appletart.cloud.run_tool", side_effect=make_iso):
            seed = write_seed(machine, [], directory, "edited-instance", "02:00:00:00:00:01", boot_only=True)
        user = json.loads((directory / "seed-data/user-data").read_text().split("\n", 1)[1])
        self.assertEqual(user["cloud_init_modules"], ["bootcmd", "growpart", "resizefs"])
        self.assertEqual(user["cloud_config_modules"], [])
        self.assertEqual(user["cloud_final_modules"], [])
        self.assertFalse(user["ssh_deletekeys"])
        self.assertFalse(user["package_update"])
        self.assertFalse(user["package_upgrade"])
        for key in ("users", "ssh_authorized_keys", "packages", "runcmd"):
            self.assertNotIn(key, user)
        self.assertIn("mount -t virtiofs -o rw appletart-share0 /mnt/work", json.dumps(user["bootcmd"]))
        self.assertEqual(json.loads((directory / "seed-data/meta-data").read_text())["instance-id"], "edited-instance")
        self.assertTrue(seed.is_file())

    def test_share_edit_removal_drops_old_mounts_and_reindexes_remaining_shares(self):
        config = {**self.machine.config(), "directory_shares": [
            {"host_path": str(self.root), "guest_path": "/mnt/keep", "read_only": True}]}
        user = cloud_config(Machine.from_dict(config), [], boot_only=True)
        self.assertIn("mount -t virtiofs -o ro appletart-share0 /mnt/keep", json.dumps(user))
        self.assertNotIn("appletart-share1", json.dumps(user))
        empty = cloud_config(Machine.from_dict({**config, "directory_shares": []}), [], boot_only=True)
        self.assertEqual(empty["bootcmd"], [])

    def test_archive_paths_are_never_extracted_and_converted_cache_is_verified(self):
        archive = self.archive(["../../outside/disk.raw"])
        def qemu(args):
            if args[1] == "info":
                return json.dumps({"virtual-size": 4})
            Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
            return ""
        with patch("appletart.cloud.run_tool", side_effect=qemu) as run:
            raw = convert_disk(archive, self.root / "cache", 40, Mock())
            self.assertEqual(raw.read_bytes(), b"disk")
            convert_disk(archive, self.root / "cache", 40, Mock())
            self.assertEqual(run.call_count, 2)
            raw.write_bytes(b"changed")
            convert_disk(archive, self.root / "cache", 40, Mock())
            self.assertEqual(run.call_count, 4)
        self.assertFalse((self.root.parent / "outside").exists())

    def test_multiple_disks_and_external_backing_are_rejected(self):
        with self.assertRaisesRegex(DeploymentError, "one regular disk"):
            convert_disk(self.archive(["disk.raw", "extra.img"]), self.root / "cache", 40, Mock())
        path = self.root / "disk.qcow2"
        path.write_bytes(b"qcow")
        with patch("appletart.cloud.run_tool", return_value=json.dumps({"virtual-size": 4, "backing-filename": "/external"})), self.assertRaisesRegex(DeploymentError, "backing"):
            convert_disk(path, self.root / "cache", 40, Mock())

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is needed for the disk-format regression")
    def test_qcow2_with_img_extension_converts_to_a_bootable_raw_disk_and_repairs_old_cache(self):
        raw_source = self.root / "source.raw"
        with raw_source.open("wb") as output:
            output.write(b"EFI boot sector fixture".ljust(510, b"\0") + b"\x55\xaa")
            output.truncate(8 * 1024 * 1024)
        artifact = self.root / "ubuntu-cloud.img"
        subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(raw_source), str(artifact)],
                       check=True, capture_output=True, timeout=10)
        raw = convert_disk(artifact, self.root / "cache", 40, Mock())
        self.assertEqual(raw.read_bytes(), raw_source.read_bytes(), "The imported raw disk still contains the QCOW2 container")
        # Older AppleTart conversions were self-consistent caches of the wrong format.
        from appletart.downloads import digest
        shutil.copyfile(artifact, raw)
        raw.with_suffix(".sha256").write_text(digest(raw) + "\n")
        repaired = convert_disk(artifact, self.root / "cache", 40, Mock())
        self.assertEqual(repaired.read_bytes(), raw_source.read_bytes())

    def test_failed_cloud_init_stops_setup_and_preserves_bootstrap_for_retry(self):
        process = Mock(poll=Mock(return_value=None))
        key = self.root / "bootstrap"
        key.write_text("test key")
        with patch("appletart.cloud.subprocess.Popen", return_value=process), \
             patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.cloud.subprocess.run", side_effect=[Mock(returncode=0), Mock(returncode=1, stdout="status: error", stderr=""),
                                                                Mock(returncode=0, stdout="There are no enabled repositories", stderr=""), Mock(returncode=0)]) as ssh, \
             self.assertRaisesRegex(DeploymentError, "Cloud-init reported"):
            report = Mock()
            provision_cloud(Mock(binary="fake-tart"), self.machine, self.root / "seed.iso", key,
                            "ssh-ed25519 build", self.root / "logs/vm.log", report)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertTrue(key.exists())
        self.assertIn("There are no enabled repositories", [call.args[0] for call in report.call_args_list])
        self.assertEqual(ssh.call_args.args[0][-1], "sudo -n -- cloud-init clean")

    def test_unavailable_guest_error_log_does_not_prevent_retry_cleanup(self):
        process = Mock(poll=Mock(return_value=None))
        with patch("appletart.cloud.subprocess.Popen", return_value=process), \
             patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.cloud.subprocess.run", side_effect=[Mock(returncode=0), Mock(returncode=1, stdout="status: error", stderr=""),
                                                                subprocess.TimeoutExpired("ssh", 30), Mock(returncode=0)]) as ssh, \
             self.assertRaisesRegex(DeploymentError, "Cloud-init reported"):
            provision_cloud(Mock(binary="fake-tart"), self.machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/vm.log", Mock())
        self.assertEqual(ssh.call_args.args[0][-1], "sudo -n -- cloud-init clean")

    def test_cloud_build_survives_ssh_becoming_ready_after_five_minutes(self):
        # Exercise the real readiness loop through cloud provisioning without
        # spending six minutes or opening sockets during the test.
        elapsed = [0.0]
        process = Mock(poll=Mock(return_value=None))
        connection = MagicMock()
        connection.__enter__.return_value.recv.return_value = b"SSH-2.0-OpenSSH\r\n"
        def connect(*args, **kwargs):
            if elapsed[0] < 360:
                raise ConnectionRefusedError("guest SSH still starting")
            return connection
        def sleep(seconds):
            elapsed[0] += seconds
        backend = Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10"))
        with patch("appletart.cloud.subprocess.Popen", return_value=process), \
             patch("appletart.cloud.subprocess.run", return_value=Mock(returncode=0, stdout="status: done", stderr="")), \
             patch("appletart.cloud.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.ssh.socket.create_connection", side_effect=connect), \
             patch("appletart.ssh.time.monotonic", side_effect=lambda: elapsed[0]), \
             patch("appletart.ssh.time.sleep", side_effect=sleep):
            provision_cloud(backend, self.machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/vm.log", Mock())
        self.assertEqual(elapsed[0], 360)
        process.send_signal.assert_called_once_with(signal.SIGINT)

    def test_authentication_waits_for_pam_nologin_during_first_boot(self):
        elapsed = [0.0]
        attempts = []
        def ssh(args, **kwargs):
            if args[-1] == "true":
                attempts.append(elapsed[0])
            if args[-1] == "true" and elapsed[0] < 76:
                return Mock(returncode=255, stdout="", stderr="System is booting up. Unprivileged users are not permitted to log in yet. pam_nologin(8).")
            return Mock(returncode=0, stdout="status: done", stderr="")
        with patch("appletart.cloud.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))), \
             patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.cloud.subprocess.run", side_effect=ssh), \
             patch("appletart.cloud.read_public_keys", return_value=[]), \
             patch("appletart.cloud.time", Mock(monotonic=lambda: elapsed[0]), create=True), \
             patch("appletart.cloud.pause", side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)):
            report = Mock()
            provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10")), self.machine,
                            self.root / "seed.iso", self.root / "bootstrap", "ssh-ed25519 build",
                            self.root / "logs/vm.log", report)
        self.assertGreaterEqual(elapsed[0], 76)
        self.assertLess(elapsed[0], 91)
        self.assertLess(len(attempts), 12, "Early PAM denials must not flood the SSH server")
        self.assertTrue(any("booting up" in call.args[0] for call in report.call_args_list))

    def test_ssh_readiness_stops_after_thirty_minutes_and_preserves_build_key(self):
        elapsed = [0.0]
        process = Mock(poll=Mock(return_value=None))
        key = self.root / "bootstrap"
        key.write_text("test key")
        def sleep(seconds):
            elapsed[0] += seconds
        with patch("appletart.cloud.subprocess.Popen", return_value=process), \
             patch("appletart.cloud.subprocess.run") as ssh, \
             patch("appletart.ssh.socket.create_connection", side_effect=ConnectionRefusedError), \
             patch("appletart.ssh.time.monotonic", side_effect=lambda: elapsed[0]), \
             patch("appletart.ssh.time.sleep", side_effect=sleep), \
             self.assertRaisesRegex(DeploymentError, "SSH was not ready within 30 minutes.*Resume build"):
            provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10")), self.machine,
                            self.root / "seed.iso", key, "ssh-ed25519 build", self.root / "logs/vm.log", Mock())
        self.assertEqual(elapsed[0], 1800)
        self.assertTrue(key.exists())
        ssh.assert_not_called()
        process.send_signal.assert_called_once_with(signal.SIGINT)

    def test_cancelling_ssh_wait_stops_only_the_build_process_and_keeps_its_key(self):
        process = Mock(poll=Mock(return_value=None))
        cancellation = Cancellation()
        key = self.root / "bootstrap"
        key.write_text("test build key")
        def report(message):
            if "Waiting for SSH (" in message:
                cancellation.cancel()
        with scope(cancellation), \
             patch("appletart.cloud.subprocess.Popen", return_value=process), \
             patch("appletart.ssh.socket.create_connection", side_effect=ConnectionRefusedError("still starting")), \
             self.assertRaises(JobCancelled):
            provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="127.0.0.1")), self.machine,
                            self.root / "seed.iso", key, "ssh-ed25519 build", self.root / "logs/vm.log", report)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.wait.assert_called_once_with(timeout=5)
        self.assertTrue(key.exists())

    def test_cancelled_build_does_not_wait_forever_for_tart_to_exit(self):
        process = Mock(poll=Mock(return_value=None), wait=Mock(side_effect=subprocess.TimeoutExpired("tart", 5)))
        cancellation = Cancellation()
        with scope(cancellation), patch("appletart.cloud.subprocess.Popen", return_value=process), \
             patch("appletart.cloud.wait_for_ssh", side_effect=JobCancelled("cancelled")), \
             self.assertRaisesRegex(DeploymentError, "Tart did not exit.*disk is preserved"):
            cancellation.cancel()
            provision_cloud(Mock(binary="fake-tart", inventory=Mock(return_value=[])), self.machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/vm.log", Mock())
        self.assertEqual([call.kwargs for call in process.wait.call_args_list], [{"timeout": 5}, {"timeout": 5}])

    def test_bridged_cloud_setup_uses_nat_without_changing_deployment_settings(self):
        machine = Machine.from_dict({**self.machine.config(), "network": "bridged", "bridges": ["en0"]})
        process = Mock(poll=Mock(return_value=None))
        with patch("appletart.cloud.subprocess.Popen", return_value=process) as boot, \
             patch("appletart.cloud.wait_for_ssh", return_value="192.0.2.10") as ready, \
             patch("appletart.cloud.subprocess.run", return_value=Mock(returncode=0, stdout="status: done", stderr="")), \
             patch("appletart.cloud.read_public_keys", return_value=["ssh-ed25519 user"]):
            provision_cloud(Mock(binary="fake-tart", run=Mock(return_value="192.0.2.10")), machine, self.root / "seed.iso", self.root / "bootstrap",
                            "ssh-ed25519 build", self.root / "logs/vm.log", Mock())
        self.assertNotIn("--net-bridged", boot.call_args.args[0])
        self.assertEqual(ready.call_args.args[1].network, "nat")
        self.assertEqual(machine.vm.network, "bridged")
        self.assertIn("--net-bridged", machine.vm.run_args())

    def test_local_cloud_image_build_uses_the_file_without_a_download_or_hash(self):
        backend = FakeBackend()
        backend.mac_address = Mock(return_value="02:00:00:00:00:01")
        api = Lifecycle(self.root / "local-data", lambda report=print: backend)
        artifact = self.root / "debian-arm64.qcow2"
        artifact.write_bytes(b"local cloud disk")
        machine = Machine.from_dict({"name": "debian-local", "os": "other", "source_kind": "cloud",
                                     "source": str(artifact), "ssh_user": "jay.morris", "ssh_public_keys": ["/tmp/user.pub"]})
        key = self.root / "local-bootstrap"
        key.write_text("test")
        key.with_suffix(".pub").write_text("public")
        seed = self.root / "local-seed.iso"
        seed.write_bytes(b"ISO")
        with patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.lifecycle.check_cloud_tools"), \
             patch("appletart.downloads.urllib.request.urlopen") as download, \
             patch("appletart.lifecycle.convert_disk", return_value=artifact) as convert, \
             patch("appletart.lifecycle.bootstrap_key", return_value=(key, "ssh-ed25519 build")), \
             patch("appletart.lifecycle.write_seed", return_value=seed), \
             patch("appletart.lifecycle.provision_cloud") as provision:
            record = api.build(machine, Mock())
        self.assertEqual(record["phase"], "ready")
        self.assertEqual(record["artifact"], str(artifact.resolve()))
        self.assertEqual(record["config"]["sha256"], "")
        download.assert_not_called()
        self.assertEqual(convert.call_args.args[0], artifact.resolve())
        self.assertEqual(provision.call_args.args[1].source_kind, "cloud")
        self.assertTrue(artifact.exists())

    def test_resume_keeps_disk_mac_and_instance_id_then_starts_with_seed(self):
        backend = FakeBackend()
        backend.mac_address = Mock(return_value="02:00:00:00:00:01")
        api = Lifecycle(self.root, lambda report=print: backend)
        artifact = self.root / "cloud.raw"
        artifact.write_bytes(b"disk")
        key = self.root / "bootstrap"
        key.write_text("test")
        key.with_suffix(".pub").write_text("public")
        seed = self.root / "seed.iso"
        seed.write_bytes(b"ISO")
        with patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.lifecycle.check_cloud_tools"), \
             patch("appletart.lifecycle.fetch_image", return_value=artifact), \
             patch("appletart.lifecycle.convert_disk", return_value=artifact), \
             patch("appletart.lifecycle.bootstrap_key", return_value=(key, "ssh-ed25519 build")), \
             patch("appletart.lifecycle.write_seed", return_value=seed) as write, \
             patch("appletart.lifecycle.provision_cloud", side_effect=[DeploymentError("first boot failed"), None]):
            with self.assertRaisesRegex(DeploymentError, "first boot failed"):
                api.build(self.machine, Mock())
            instance = api.store.get("kali-test")["cloud_instance_id"]
            api.build(self.machine, Mock())
            self.assertTrue(all(call.args[3] == instance for call in write.call_args_list))
        self.assertEqual(sum(args[0] == "create" for args in backend.calls), 1)
        self.assertEqual(sum("--disk" in args for args in backend.calls), 1)
        self.assertEqual(sum("--random-mac" in args for args in backend.calls), 1)
        self.assertFalse(key.exists())
        self.assertEqual(api.store.get("kali-test")["phase"], "ready")
        def start(args, **kwargs):
            backend.vms["kali-test"]["Running"] = True
            return Mock(poll=Mock(return_value=None))
        with patch("appletart.lifecycle.subprocess.Popen", side_effect=start) as launch:
            api.start("kali-test", Mock())
        self.assertIn(str(seed) + ":ro", launch.call_args.args[0])

    def test_old_cloud_build_refreshes_cached_seed_once_without_replacing_disk(self):
        backend = FakeBackend()
        backend.mac_address = Mock(return_value="02:00:00:00:00:01")
        backend.disk_format = Mock(return_value="raw")
        backend.run(["create", self.machine.vm.name])
        api = Lifecycle(self.root, lambda report=print: backend)
        api.store.put({"config": self.machine.config(), "owned": True, "phase": "created",
                       "identity": backend.identity(self.machine.vm.name), "cloud_imported": True,
                       "cloud_configured": True, "cloud_instance_id": "old-cached-instance"})
        key = self.root / "cloud-init" / self.machine.vm.name / "bootstrap"
        key.parent.mkdir(parents=True)
        key.write_text("test")
        key.with_suffix(".pub").write_text("public")
        (key.parent / "known_hosts").write_text("old-host-key")
        with patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.lifecycle.check_cloud_tools"), \
             patch("appletart.lifecycle.bootstrap_key", return_value=(key, "ssh-ed25519 build")), \
             patch("appletart.lifecycle.write_seed", return_value=self.root / "seed.iso") as seed, \
             patch("appletart.lifecycle.provision_cloud", side_effect=[DeploymentError("interrupted"), None]):
            with self.assertRaisesRegex(DeploymentError, "interrupted"):
                api.build(self.machine, Mock())
            api.build(self.machine, Mock())
        instances = [call.args[3] for call in seed.call_args_list]
        self.assertNotEqual(instances[0], "old-cached-instance", "NoCloud reused the previous build configuration")
        self.assertTrue(all(instance == instances[0] for instance in instances))
        self.assertEqual((key.parent / "known_hosts.before-cloud-update").read_text(), "old-host-key")
        self.assertFalse(any("--disk" in args or "--random-mac" in args for args in backend.calls))

    def test_resume_repairs_only_an_incomplete_cloud_disk_containing_container_data(self):
        for disk_format in ("qcow2", "xz"):
            with self.subTest(disk_format=disk_format):
                backend = FakeBackend()
                backend.mac_address = Mock(return_value="02:00:00:00:00:01")
                backend.disk_format = Mock(return_value=disk_format)
                backend.run(["create", self.machine.vm.name])
                api = Lifecycle(self.root / disk_format, lambda report=print: backend)
                api.store.put({"config": self.machine.config(), "owned": True, "phase": "created",
                               "identity": backend.identity(self.machine.vm.name), "cloud_imported": True,
                               "cloud_configured": True, "cloud_instance_id": "old-instance", "artifact": "cached-image"})
                key = self.root / "bootstrap"
                key.write_text("test")
                key.with_suffix(".pub").write_text("public")
                with patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), \
                     patch("appletart.lifecycle.check_cloud_tools"), \
                     patch("appletart.lifecycle.fetch_image", return_value=self.root / "source.img"), \
                     patch("appletart.lifecycle.convert_disk", return_value=self.root / "fixed.raw") as convert, \
                     patch("appletart.lifecycle.bootstrap_key", return_value=(key, "ssh-ed25519 build")), \
                     patch("appletart.lifecycle.write_seed", return_value=self.root / "seed.iso"), \
                     patch("appletart.lifecycle.provision_cloud"):
                    api.build(self.machine, Mock())
                    convert.assert_called_once()
                    backend.disk_format.reset_mock()
                    api.build(self.machine, Mock())
                    # Ready VMs must never have their installed disks replaced.
                    backend.disk_format.assert_not_called()
                    convert.assert_called_once()
                self.assertIn(["set", self.machine.vm.name, "--disk", str(self.root / "fixed.raw")], backend.calls)
                self.assertEqual(sum(args[0] == "create" for args in backend.calls), 1)
                self.assertFalse(any("--random-mac" in args for args in backend.calls))
                self.assertEqual(api.store.get(self.machine.vm.name)["phase"], "ready")


if __name__ == "__main__":
    unittest.main()
