import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock, patch

from appletart.catalog import Machine
from appletart.deployment import DeploymentError
from appletart.downloads import fetch_iso
from appletart.lifecycle import Lifecycle


class FakeBackend:
    binary = "fake-tart"

    def __init__(self):
        self.vms = {}
        self.calls = []
        self.identities = {}
        self.fail_set = False

    def inventory(self):
        return [dict(item) for item in self.vms.values()]

    def identity(self, name):
        return self.identities[name]

    def disk_capacity_gb(self, name):
        return 0

    def disk_format(self, name):
        return "raw"

    def run(self, args, capture=False, timeout=None):
        self.calls.append(args)
        if args[0] in ("clone", "create"):
            name = args[-1]
            self.vms[name] = {"Name": name, "Running": False, "State": "Stopped"}
            self.identities[name] = {"inode": name + "-original"}
        if args[0] == "set" and self.fail_set:
            raise DeploymentError("fake configuration failure")
        if args[0] == "delete":
            self.vms.pop(args[1])
        if args[0] == "stop":
            self.vms[args[1]]["Running"] = False
        if args[0] == "ip":
            return "192.0.2.10"
        return ""


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.backend = FakeBackend()
        self.factory = lambda report=print: self.backend
        self.api = Lifecycle(self.root, self.factory)
        self.machine = Machine.from_dict({"name": "ubuntu-test", "size": "small"})
        self.report = Mock()
        for target in ("appletart.lifecycle.guest_agent.prepare", "appletart.lifecycle.provision_keys"):
            stub = patch(target)
            stub.start()
            self.addCleanup(stub.stop)

    def test_bridged_ip_uses_guest_agent_when_arp_is_empty(self):
        machine = Machine.from_dict({"name": "bridge", "network": "bridged", "bridge": "en0"})
        with patch("appletart.lifecycle.socket.if_nameindex", return_value=[(1, "en0")]):
            self.api.build(machine, self.report)
        self.backend.vms["bridge"]["Running"] = True
        def lookup(args, **kwargs):
            if args[-1] == "agent":
                return "172.20.10.11\n"
            raise DeploymentError("Tart ip failed (exit 1): Error: arp command yielded invalid output: empty output")
        self.backend.run = Mock(side_effect=lookup)
        self.assertEqual(self.api.ip("bridge"), "172.20.10.11")
        self.assertEqual(self.backend.run.call_args.args[0][-1], "agent")

    def test_tart_build_installs_agent_during_ssh_setup(self):
        machine = Machine.from_dict({"name": "agent-test", "ssh_public_keys": ["/tmp/user.pub"]})
        with patch("appletart.lifecycle.check_ssh_tools"), \
             patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.lifecycle.guest_agent.prepare", return_value=b"verified binary"), \
             patch("appletart.lifecycle.provision_keys") as provision:
            record = self.api.build(machine, self.report)
        self.assertEqual(provision.call_args.kwargs["agent_binary"], b"verified binary")
        self.assertEqual(record["guest_agent_version"], "0.15.0")

    def test_bridged_fallback_never_uses_an_old_nat_lease(self):
        machine = Machine.from_dict({"name": "bridge", "network": "bridged", "bridge": "en0"})
        with patch("appletart.lifecycle.socket.if_nameindex", return_value=[(1, "en0")]):
            self.api.build(machine, self.report)
        self.backend.vms["bridge"]["Running"] = True
        def lookup(args, **kwargs):
            if args[-1] == "dhcp":
                return "192.168.64.17"
            raise DeploymentError("no IP address found")
        self.backend.run = Mock(side_effect=lookup)
        with self.assertRaisesRegex(DeploymentError, "guest agent.*bridged ARP"):
            self.api.ip("bridge")
        self.assertEqual([call.args[0][-1] for call in self.backend.run.call_args_list], ["agent", "arp"])

    def test_stopped_vm_has_no_current_address(self):
        self.api.build(self.machine, self.report)
        before = len(self.backend.calls)
        with self.assertRaisesRegex(DeploymentError, "stopped"):
            self.api.ip(self.machine.vm.name)
        self.assertEqual(len(self.backend.calls), before)

    def test_agent_upgrade_uses_nat_and_preserves_bridge_disk_and_keys(self):
        machine = Machine.from_dict({"name": "bridge", "network": "bridged", "bridge": "en0"})
        with patch("appletart.lifecycle.socket.if_nameindex", return_value=[(1, "en0")]):
            self.api.build(machine, self.report)
        original = self.api.store.get("bridge")
        process = Mock(poll=Mock(return_value=None))
        with patch("appletart.lifecycle.socket.if_nameindex", return_value=[(1, "en0")]), \
             patch("appletart.lifecycle.guest_agent.prepare", return_value=b"verified binary"), \
             patch("appletart.lifecycle.guest_agent.install") as install, \
             patch("appletart.lifecycle.guest_agent.verify_management") as verify, \
             patch("appletart.lifecycle.guest_agent.wait_for_root", return_value=False), \
             patch("appletart.lifecycle.subprocess.Popen", return_value=process) as boot, \
             patch("appletart.ssh.wait_for_ssh", return_value="192.0.2.10") as ready:
            self.api.install_guest_agent("bridge", self.report)
        self.assertNotIn("--net-bridged", boot.call_args.args[0])
        self.assertEqual(ready.call_args.args[1].network, "nat")
        self.assertEqual(install.call_args.kwargs["binary"], b"verified binary")
        updated = self.api.store.get("bridge")
        self.assertEqual(updated["config"], original["config"])
        self.assertEqual(updated["identity"], original["identity"])
        self.assertEqual(updated["guest_agent_version"], "0.15.0")
        self.assertTrue(updated["guest_agent_privileged"])
        verify.assert_called_once_with(self.backend, "bridge")
        process.send_signal.assert_called_once()

    def test_running_vm_upgrade_preserves_runtime_disk_and_settings(self):
        self.api.build(self.machine, self.report)
        self.backend.vms[self.machine.vm.name]["Running"] = True
        original = self.api.store.get(self.machine.vm.name)
        with patch("appletart.lifecycle.guest_agent.install") as install, \
             patch("appletart.lifecycle.guest_agent.root_available", return_value=False), \
             patch("appletart.lifecycle.guest_agent.verify_management") as verify, \
             patch("appletart.lifecycle.subprocess.Popen") as boot:
            self.api.install_guest_agent(self.machine.vm.name, self.report)
        boot.assert_not_called()
        self.assertNotIn("stop", [args[0] for args in self.backend.calls])
        self.assertTrue(self.backend.vms[self.machine.vm.name]["Running"])
        self.assertIn("BatchMode=yes", install.call_args.args[0])
        verify.assert_called_once_with(self.backend, self.machine.vm.name)
        record = self.api.store.get(self.machine.vm.name)
        self.assertTrue(record["guest_agent_privileged"])
        self.assertEqual(record["config"], original["config"])
        self.assertEqual(record["identity"], original["identity"])

    def test_unverified_running_upgrade_does_not_record_success_or_stop_vm(self):
        self.api.build(self.machine, self.report)
        record = self.api.store.get(self.machine.vm.name)
        record.pop("guest_agent_privileged", None)
        record.pop("guest_agent_version", None)
        self.api.store.put(record)
        self.backend.vms[self.machine.vm.name]["Running"] = True
        with patch("appletart.lifecycle.guest_agent.install"), \
             patch("appletart.lifecycle.guest_agent.root_available", return_value=False), \
             patch("appletart.lifecycle.guest_agent.verify_management", side_effect=DeploymentError("RPC unavailable")), \
             self.assertRaisesRegex(DeploymentError, "RPC unavailable"):
            self.api.install_guest_agent(self.machine.vm.name, self.report)
        self.assertNotIn("guest_agent_privileged", self.api.store.get(self.machine.vm.name))
        self.assertTrue(self.backend.vms[self.machine.vm.name]["Running"])
        self.assertNotIn("stop", [args[0] for args in self.backend.calls])

    def test_presets_overrides_and_network_validation(self):
        machine = Machine.from_dict({"name": "vm", "size": "large", "cpu": 2, "network": "bridged", "bridge": "en0"})
        self.assertEqual((machine.vm.cpu, machine.vm.memory_mb, machine.vm.disk_gb), (2, 16384, 160))
        self.assertEqual(machine.vm.run_args(), ["run", "vm", "--net-bridged", "en0"])
        self.assertIn("arp", machine.vm.ip_args())
        for data in ({"network": "bridged"}, {"network": "nat", "bridge": "en0"}, {"size": "huge"}, {"os": "windows"}, {"source_kind": "iso", "source": "http://example.com/a.iso"}):
            with self.subTest(data=data), self.assertRaises(DeploymentError):
                Machine.from_dict({"name": "vm", **data})

    def test_download_is_separate_from_build_and_state_survives_restart(self):
        self.api.download(self.machine, self.report)
        self.assertEqual(self.backend.calls, [["pull", self.machine.source]])
        self.assertEqual(self.api.store.get(self.machine.vm.name)["phase"], "downloaded")
        restarted = Lifecycle(self.root, self.factory)
        restarted.build(self.machine, self.report)
        self.assertEqual([args[0] for args in self.backend.calls], ["pull", "clone", "set"])
        self.assertEqual(restarted.listing()["machines"][0]["phase"], "ready")

    def test_collision_and_configuration_mismatch_are_rejected(self):
        self.backend.vms["ubuntu-test"] = {"Name": "ubuntu-test", "Running": False}
        with self.assertRaisesRegex(DeploymentError, "already exists"):
            self.api.download(self.machine, self.report)
        self.assertEqual(self.backend.calls, [])
        self.backend.vms.clear()
        self.api.download(self.machine, self.report)
        changed = Machine.from_dict({**self.machine.config(), "cpu": 1})
        with self.assertRaisesRegex(DeploymentError, "different saved configuration"):
            self.api.build(changed, self.report)

    def test_failed_build_preserves_disk_and_can_resume(self):
        self.backend.fail_set = True
        with self.assertRaises(DeploymentError):
            self.api.build(self.machine, self.report)
        record = self.api.store.get("ubuntu-test")
        self.assertTrue(record["owned"])
        self.assertEqual(record["phase"], "created")
        self.backend.fail_set = False
        self.api.build(self.machine, self.report)
        self.assertEqual(sum(args[0] == "clone" for args in self.backend.calls), 1)
        self.assertEqual(self.api.store.get("ubuntu-test")["phase"], "ready")

    def test_installation_is_pending_until_explicit_finish(self):
        iso = self.root / "arm64.iso"
        iso.write_bytes(b"test installer")
        machine = Machine.from_dict({"name": "kali", "os": "kali", "source_kind": "iso", "source": str(iso)})
        self.api.build(machine, self.report)
        self.assertEqual(self.api.store.get("kali")["phase"], "awaiting-installation")
        self.assertEqual(self.backend.calls[0], ["create", "--linux", "--disk-size", "40", "kali"])
        self.backend.vms["kali"]["Running"] = True
        with self.assertRaisesRegex(DeploymentError, "shut down"):
            self.api.finish_installation("kali", self.report)
        self.backend.vms["kali"]["Running"] = False
        self.api.finish_installation("kali", self.report)
        self.assertEqual(self.api.store.get("kali")["phase"], "ready")

    def test_start_uses_saved_bridge_and_installer_then_boots_without_iso(self):
        iso = self.root / "arm64.iso"
        iso.write_bytes(b"installer")
        machine = Machine.from_dict({"name": "kali", "os": "kali", "source_kind": "iso", "source": str(iso), "network": "bridged", "bridge": "en0"})
        with patch("appletart.lifecycle.socket.if_nameindex", return_value=[(1, "en0")]):
            self.api.build(machine, self.report)
        def launch(args, **kwargs):
            self.backend.vms["kali"]["Running"] = True
            return Mock(poll=Mock(return_value=None))
        with patch("appletart.lifecycle.subprocess.Popen", side_effect=launch) as process:
            self.api.start("kali", self.report, headless=True)
        args = process.call_args.args[0]
        self.assertIn("--net-bridged", args)
        self.assertIn(str(iso.resolve()) + ":ro", args)
        self.assertNotIn("--no-graphics", args) # Installer remains interactive.
        self.backend.vms["kali"]["Running"] = False
        with patch("appletart.lifecycle.socket.if_nameindex", return_value=[(1, "en0")]):
            self.api.finish_installation("kali", self.report)
        with patch("appletart.lifecycle.subprocess.Popen", side_effect=launch) as process:
            self.api.start("kali", self.report, headless=True)
        self.assertNotIn("--disk", process.call_args.args[0])
        self.assertIn("--no-graphics", process.call_args.args[0])
        self.assertEqual(self.api.ip("kali"), "192.0.2.10")
        self.assertIn("agent", self.backend.calls[-1])

    def test_edit_preserves_mac_and_rejects_shrink_or_running_vm(self):
        self.api.build(self.machine, self.report)
        config = {**self.machine.config(), "cpu": 1, "disk_gb": 80}
        self.api.configure("ubuntu-test", config, self.report)
        self.assertNotIn("--random-mac", self.backend.calls[-1])
        with self.assertRaisesRegex(DeploymentError, "only grow"):
            self.api.configure("ubuntu-test", {**config, "disk_gb": 40}, self.report)
        self.backend.vms["ubuntu-test"]["Running"] = True
        with self.assertRaisesRegex(DeploymentError, "stopped"):
            self.api.configure("ubuntu-test", config, self.report)

    def ready_cloud(self, source_kind="cloud"):
        machine = Machine.from_dict({**self.machine.config(), "source_kind": source_kind,
                                     "source": "https://example.test/cloud.qcow2" if source_kind == "cloud" else "base-golden",
                                     "ssh_public_keys": [str(self.root / "user.pub")]})
        self.backend.run(["create", machine.vm.name])
        self.backend.mac_address = Mock(return_value="02:00:00:00:00:01")
        seed = self.root / "original-seed.iso"
        seed.write_bytes(b"original seed")
        self.api.store.put({"config": machine.config(), "owned": True, "phase": "ready",
                            "identity": self.backend.identity(machine.vm.name), "seed": str(seed),
                            "cloud_instance_id": "original-instance"})
        return machine, seed

    def test_cloud_and_golden_share_edits_use_a_new_seed_on_next_start(self):
        for kind in ("cloud", "golden"):
            with self.subTest(kind=kind):
                machine, original = self.ready_cloud(kind)
                def seed_for_edit(machine, keys, directory, instance, mac, **kwargs):
                    self.assertEqual(keys, [])
                    self.assertTrue(kwargs["boot_only"])
                    self.assertEqual(mac, "02:00:00:00:00:01")
                    directory.mkdir(parents=True)
                    seed = directory / "seed.iso"
                    seed.write_bytes(b"mounts only")
                    return seed
                with patch.object(self.api, "_preflight"), \
                     patch("appletart.lifecycle.write_seed", side_effect=seed_for_edit) as write:
                    for shares in ([{"host_path": str(self.root.resolve()), "guest_path": "/mnt/work", "read_only": True}], []):
                        self.api.configure(machine.vm.name, {**machine.config(), "directory_shares": shares}, self.report)
                        record = self.api.store.get(machine.vm.name)
                        self.assertEqual(record["config"]["directory_shares"], shares)
                        self.assertNotEqual(record["cloud_instance_id"], "original-instance")
                        def launch(args, **kwargs):
                            self.backend.vms[machine.vm.name]["Running"] = True
                            return Mock(poll=Mock(return_value=None))
                        with patch("appletart.lifecycle.subprocess.Popen", side_effect=launch) as start:
                            self.api.start(machine.vm.name, self.report)
                        self.assertIn(record["seed"] + ":ro", start.call_args.args[0])
                        self.assertEqual("--dir" in start.call_args.args[0], bool(shares))
                        self.backend.vms[machine.vm.name]["Running"] = False
                self.assertEqual(write.call_count, 2)
                self.assertEqual(original.read_bytes(), b"original seed")

    def test_failed_share_edit_keeps_original_seed_and_configuration(self):
        machine, seed = self.ready_cloud()
        previous = self.api.store.get(machine.vm.name)
        config = {**machine.config(), "directory_shares": [{"host_path": str(self.root), "guest_path": "/mnt/work"}]}
        with patch.object(self.api, "_preflight"), \
             patch("appletart.lifecycle.write_seed", side_effect=DeploymentError("seed failure")), \
             self.assertRaisesRegex(DeploymentError, "seed failure"):
            self.api.configure(machine.vm.name, config, self.report)
        self.assertEqual(self.api.store.get(machine.vm.name), previous)
        self.backend.fail_set = True
        with patch.object(self.api, "_preflight"), \
             patch("appletart.lifecycle.write_seed", return_value=self.root / "new-seed.iso"), \
             self.assertRaisesRegex(DeploymentError, "configuration failure"):
            self.api.configure(machine.vm.name, config, self.report)
        self.assertEqual(self.api.store.get(machine.vm.name), previous)
        self.assertEqual(seed.read_bytes(), b"original seed")

    def test_running_or_incomplete_cloud_share_edits_do_not_generate_a_seed(self):
        machine, _ = self.ready_cloud()
        config = {**machine.config(), "directory_shares": [{"host_path": str(self.root), "guest_path": "/mnt/work"}]}
        with patch.object(self.api, "_preflight"), patch("appletart.lifecycle.write_seed") as write:
            self.backend.vms[machine.vm.name]["Running"] = True
            with self.assertRaisesRegex(DeploymentError, "stopped"):
                self.api.configure(machine.vm.name, config, self.report)
            record = self.api.store.get(machine.vm.name)
            record["phase"] = "created"
            self.api.store.put(record)
            self.backend.vms[machine.vm.name]["Running"] = False
            with self.assertRaisesRegex(DeploymentError, "Complete the cloud build"):
                self.api.configure(machine.vm.name, config, self.report)
            write.assert_not_called()

    def test_start_defaults_to_linux_headless_and_macos_graphics(self):
        for guest in ("ubuntu", "macos"):
            with self.subTest(guest=guest):
                machine = Machine.from_dict({"name": guest, "os": guest})
                self.api.build(machine, self.report)
                def launch(args, **kwargs):
                    self.backend.vms[guest]["Running"] = True
                    return Mock(poll=Mock(return_value=None))
                with patch("appletart.lifecycle.subprocess.Popen", side_effect=launch) as process:
                    self.api.start(guest, self.report)
                self.assertEqual("--no-graphics" in process.call_args.args[0], guest == "ubuntu")

    def test_invalid_forwarding_ip_is_rejected_before_booting_the_vm(self):
        machine = Machine.from_dict({**self.machine.config(), "port_forwards": [
            {"listen_address": "192.0.2.255", "host_port": 8443, "guest_port": 443}]})
        self.api.build(machine, self.report)
        with patch("appletart.lifecycle.subprocess.Popen") as launch, \
             self.assertRaisesRegex(DeploymentError, "192.0.2.255.*not assigned to the Mac"):
            self.api.start(machine.vm.name, self.report)
        launch.assert_not_called()
        self.assertFalse(self.backend.vms[machine.vm.name]["Running"])
        self.assertFalse(any(args[0] == "stop" for args in self.backend.calls))

    def test_destroy_requires_confirmation_and_stopped_vm_and_keeps_cache(self):
        self.api.build(self.machine, self.report)
        cache = self.root / "downloads"
        cache.mkdir()
        (cache / "image.iso").write_bytes(b"cached")
        with self.assertRaisesRegex(DeploymentError, "exact VM name"):
            self.api.destroy("ubuntu-test", "wrong", self.report)
        self.backend.vms["ubuntu-test"]["Running"] = True
        with self.assertRaisesRegex(DeploymentError, "Stop"):
            self.api.destroy("ubuntu-test", "ubuntu-test", self.report)
        self.backend.vms["ubuntu-test"]["Running"] = False
        self.api.destroy("ubuntu-test", "ubuntu-test", self.report)
        self.assertIsNone(self.api.store.get("ubuntu-test"))
        self.assertTrue((cache / "image.iso").exists())
        self.assertEqual(self.backend.calls[-1], ["delete", "ubuntu-test"])

    def test_external_replacement_is_never_destroyed(self):
        self.api.build(self.machine, self.report)
        self.backend.identities["ubuntu-test"] = {"inode": "replacement"}
        with self.assertRaisesRegex(DeploymentError, "identity"):
            self.api.destroy("ubuntu-test", "ubuntu-test", self.report)
        self.assertNotEqual(self.backend.calls[-1][0], "delete")

    def test_vm_lock_blocks_conflicting_operations_but_allows_other_vms(self):
        other = Lifecycle(self.root, self.factory)
        with self.api.operation("vm-ubuntu-test"):
            with self.assertRaisesRegex(DeploymentError, "Another AppleTart"):
                other.download(self.machine, self.report)
            other.download(Machine.from_dict({"name": "another-vm"}), self.report)

    def test_saved_relative_seed_is_resolved_from_its_original_workspace(self):
        data = self.root / "original-workspace" / ".appletart"
        api = Lifecycle(data, self.factory)
        seed = data / "cloud-init" / "vm" / "seed.iso"
        seed.parent.mkdir(parents=True)
        seed.write_bytes(b"seed")
        api.store.put({"config": self.machine.config(), "phase": "ready", "owned": True,
                       "seed": ".appletart/cloud-init/vm/seed.iso", "artifact": "template"})
        restarted = Lifecycle(data, self.factory)
        self.assertEqual(Path(restarted.store.get(self.machine.vm.name)["seed"]).read_bytes(), b"seed")
        self.assertEqual(restarted.store.get(self.machine.vm.name)["artifact"], "template")


class DownloadTests(unittest.TestCase):
    def test_concurrent_downloads_share_one_verified_cache_entry(self):
        payload = b"shared image"
        checksum = hashlib.sha256(payload).hexdigest()
        for checksum in (checksum, ""):
            with self.subTest(checksum_supplied=bool(checksum)):
                entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
                class Response(io.BytesIO):
                    url = "https://example.com/arm64.iso"
                    def read(self, size=-1):
                        entered.set()
                        release.wait(timeout=2)
                        return super().read(size)
                def report(message):
                    if "Waiting for another build" in message:
                        waiting.set()
                with tempfile.TemporaryDirectory() as directory, \
                     patch("appletart.downloads.urllib.request.urlopen", side_effect=lambda *a, **k: Response(payload)) as download, \
                     ThreadPoolExecutor(max_workers=2) as workers:
                    first = workers.submit(fetch_iso, Response.url, checksum, Path(directory), report)
                    self.assertTrue(entered.wait(timeout=1))
                    second = workers.submit(fetch_iso, Response.url, checksum, Path(directory), report)
                    try:
                        self.assertTrue(waiting.wait(timeout=1))
                        self.assertEqual(download.call_count, 1)
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=2), second.result(timeout=2))
                    self.assertEqual(first.result().read_bytes(), payload)
                    self.assertEqual(download.call_count, 1)
                    self.assertFalse(list(Path(directory).glob("*.part")))

    def test_verified_download_is_cached_and_bad_hash_is_not_accepted(self):
        payload = b"a sample installer"
        checksum = hashlib.sha256(payload).hexdigest()
        def response():
            result = io.BytesIO(payload)
            result.url = "https://example.com/arm64.iso"
            return result
        with tempfile.TemporaryDirectory() as directory, patch("appletart.downloads.urllib.request.urlopen", side_effect=lambda *a, **k: response()) as download:
            path = fetch_iso("https://example.com/arm64.iso", checksum, Path(directory), Mock())
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(fetch_iso("https://example.com/arm64.iso", checksum, Path(directory), Mock()), path)
            self.assertEqual(download.call_count, 1)
            with self.assertRaisesRegex(DeploymentError, "SHA256"):
                fetch_iso("https://example.com/arm64.iso", "0" * 64, Path(directory), Mock())
            self.assertFalse(list(Path(directory).glob("*.part")))
            self.assertFalse((Path(directory) / ("0" * 64 + ".iso")).exists())

    def test_local_iso_verification_rejects_changed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "arm64.iso"
            path.write_bytes(b"modified")
            with self.assertRaisesRegex(DeploymentError, "SHA256"):
                fetch_iso(str(path), "0" * 64, Path(directory), Mock())


if __name__ == "__main__":
    unittest.main()
