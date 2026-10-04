import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from appletart.catalog import Machine
from appletart.deployment import DeploymentError
from appletart.downloads import fetch_image
from appletart.lifecycle import Lifecycle
from appletart.storage import guard
from appletart import guest_access, settings
from test_lifecycle import FakeBackend


class DiskBackend(FakeBackend):
    def __init__(self, home):
        super().__init__()
        self.home = home

    def identity(self, name):
        info = (self.home / "vms" / name).stat()
        return {"home": str(self.home), "device": info.st_dev, "inode": info.st_ino}

    def run(self, args, **kwargs):
        if args[0] in ("clone", "create"):
            path = self.home / "vms" / args[-1]
            path.mkdir(parents=True)
            if args[0] == "clone" and (self.home / "vms" / args[1] / "disk.img").exists():
                shutil.copyfile(self.home / "vms" / args[1] / "disk.img", path / "disk.img")
            else:
                (path / "disk.img").write_bytes(b"original disk")
        result = super().run(args, **kwargs)
        if args[0] == "set" and "--disk" in args:
            shutil.copyfile(args[args.index("--disk") + 1], self.home / "vms" / args[1] / "disk.img")
        if args[0] == "delete":
            shutil.rmtree(self.home / "vms" / args[1])
        return result


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = DiskBackend(self.root / "tart")
        self.api = Lifecycle(self.root / "data", lambda report=print: self.backend)
        self.machine = Machine.from_dict({"name": "dev"})
        self.report = Mock()
        for target in ("appletart.lifecycle.guest_agent.prepare", "appletart.lifecycle.provision_keys"):
            stub = patch(target)
            stub.start()
            self.addCleanup(stub.stop)
        self.api.build(self.machine, self.report)
        self.disk = self.root / "tart" / "vms" / "dev" / "disk.img"

    def test_ssh_config_uses_the_current_ip_and_refuses_stopped_vms(self):
        with patch("appletart.lifecycle.ssh_config.save") as save:
            with self.assertRaisesRegex(DeploymentError, "stopped"):
                self.api.save_ssh_config("dev")
            save.assert_not_called()
            self.backend.vms["dev"]["Running"] = True
            with patch("appletart.lifecycle.resolve_ip", side_effect=["192.0.2.10", "192.0.2.11"]):
                self.api.save_ssh_config("dev")
                self.api.save_ssh_config("dev")
            self.assertEqual([call.args[2] for call in save.call_args_list], ["192.0.2.10", "192.0.2.11"])

    def test_terminal_preference_persists_and_rejects_custom_commands(self):
        app = Path("/Applications/iTerm.app")
        with patch("appletart.settings.application", return_value=app):
            self.assertEqual(self.api.save_settings({"terminal": "iterm2"})["terminal"], "iterm2")
        self.assertEqual(settings.load(self.api.store.root), {"terminal": "iterm2", "default_public_key": "~/.ssh/id_ed25519.pub", "install_ssh_key_by_default": True})
        self.assertEqual((self.api.store.root / "settings.json").stat().st_mode & 0o777, 0o600)
        for value in ({"terminal": "iterm2", "command": "echo unsafe"}, {"terminal": ["iterm2"]}, {"terminal": "/tmp/custom;open"}):
            with self.subTest(value=value), self.assertRaises(DeploymentError):
                self.api.save_settings(value)
        with patch("appletart.settings.application", side_effect=DeploymentError("not installed")), self.assertRaisesRegex(DeploymentError, "not installed"):
            self.api.save_settings({"terminal": "warp"})
        self.assertEqual(settings.load(self.api.store.root)["terminal"], "iterm2")

    def test_terminal_launcher_uses_selected_app_and_quotes_ssh_arguments(self):
        root = self.root / "workspace with spaces"
        root.mkdir()
        (root / "settings.json").write_text('{"terminal":"iterm2"}')
        public = root / "a $(touch unwanted).pub"
        public.with_suffix("").write_text("placeholder; contents are not read")
        machine = Machine.from_dict({"name": "dev", "ssh_public_keys": [str(public)]})
        with patch("appletart.settings.application", return_value=Path("/Applications/iTerm.app")), patch("appletart.guest_access.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as launch:
            label = guest_access.open_terminal(root, machine, "192.0.2.10")
        path = root / "connections" / "dev.command"
        self.assertEqual(label, "iTerm2")
        self.assertEqual(launch.call_args.args[0], ["open", "-a", "/Applications/iTerm.app", str(path)])
        import shlex
        command = path.read_text().splitlines()[1]
        self.assertEqual(shlex.split(command)[1:], guest_access.ssh_args(root, machine, "192.0.2.10"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertFalse((root / "unwanted").exists())

    def test_checkpoint_restore_preserves_identity_and_keeps_current_disk(self):
        original = self.api.store.get("dev")
        identifier = self.api.create_checkpoint("dev", "baseline", "before upgrade", self.report)
        self.disk.write_bytes(b"changed disk")
        changed = {**original["config"], "cpu": 1, "network": "bridged", "bridge": "en0", "bridges": ["en0"]}
        self.api.store.put({**original, "config": changed})
        self.api.restore_checkpoint("dev", identifier, "dev", self.report)
        self.assertEqual(self.disk.read_bytes(), b"original disk")
        restored = self.api.store.get("dev")
        self.assertEqual(restored["identity"], original["identity"])
        self.assertEqual(restored["config"], original["config"])
        points = self.api.checkpoints("dev")
        self.assertEqual(len(points), 2)
        undo = next(i for i in points if i["id"] != identifier)
        record = self.api.recoveries.get(undo["id"])
        self.assertEqual((Path(record["identity"]["home"]) / "vms" / record["tart_name"] / "disk.img").read_bytes(), b"changed disk")
        self.assertNotIn("--random-mac", self.backend.calls[-1])
        self.assertEqual([m["config"]["name"] for m in self.api.listing()["machines"]], ["dev"])

    def test_cloud_checkpoint_restores_seed_and_instance_id(self):
        original = self.api.store.get("dev")
        key = self.root / "user.pub"
        config = Machine.from_dict({"name": "dev", "os": "kali", "ssh_public_keys": [str(key)], "install_default_packages": False}).config()
        directory = self.api.store.root / "cloud-init" / "dev"
        directory.mkdir(parents=True, exist_ok=True)
        seed = directory / "seed.iso"
        seed.write_bytes(b"old seed")
        (directory / "known_hosts").write_text("old public host key")
        self.api.store.put({**original, "config": config, "seed": str(seed), "cloud_instance_id": "original-instance", "cloud_imported": True, "cloud_configured": True})
        identifier = self.api.create_checkpoint("dev", "cloud", report=self.report)
        seed.write_bytes(b"changed seed")
        self.api.restore_checkpoint("dev", identifier, "dev", self.report)
        self.assertEqual(seed.read_bytes(), b"old seed")
        self.assertEqual(self.api.store.get("dev")["cloud_instance_id"], "original-instance")

    def test_restore_failure_keeps_backup_and_blocks_start(self):
        identifier = self.api.create_checkpoint("dev", "baseline", report=self.report)
        self.backend.fail_set = True
        with self.assertRaisesRegex(DeploymentError, "configuration failure"):
            self.api.restore_checkpoint("dev", identifier, "dev", self.report)
        self.assertEqual(len(self.api.checkpoints("dev")), 2)
        self.assertEqual(self.api.store.get("dev")["phase"], "restore-failed")
        with self.assertRaisesRegex(DeploymentError, "restore is incomplete"):
            self.api.start("dev", self.report)
        self.backend.fail_set = False
        self.api.restore_checkpoint("dev", identifier, "dev", self.report)
        self.assertEqual(self.api.store.get("dev")["phase"], "ready")

    def test_checkpoint_guards_running_vm_confirmation_and_changed_identity(self):
        self.backend.vms["dev"]["Running"] = True
        with self.assertRaisesRegex(DeploymentError, "stopped"):
            self.api.create_checkpoint("dev", "baseline", report=self.report)
        self.backend.vms["dev"]["Running"] = False
        identifier = self.api.create_checkpoint("dev", "baseline", report=self.report)
        with self.assertRaisesRegex(DeploymentError, "confirm"):
            self.api.restore_checkpoint("dev", identifier, "wrong", self.report)
        point = self.api.recoveries.get(identifier)
        point["identity"]["inode"] = -1
        self.api.recoveries.put(point)
        with self.assertRaisesRegex(DeploymentError, "identity"):
            self.api.restore_checkpoint("dev", identifier, "dev", self.report)
        self.assertEqual(self.disk.read_bytes(), b"original disk")

    def test_destroy_requires_removing_checkpoints_first(self):
        identifier = self.api.create_checkpoint("dev", "baseline", report=self.report)
        with self.assertRaisesRegex(DeploymentError, "checkpoints"):
            self.api.destroy("dev", "dev", self.report)
        with self.assertRaisesRegex(DeploymentError, "exact name"):
            self.api.delete_checkpoint("dev", identifier, "wrong", self.report)
        self.api.delete_checkpoint("dev", identifier, "baseline", self.report)
        self.assertEqual(self.api.checkpoints("dev"), [])
        self.api.destroy("dev", "dev", self.report)
        self.assertFalse(self.disk.exists())

    def test_shutdown_uses_ssh_and_never_forces_power_off(self):
        self.backend.vms["dev"]["Running"] = True
        def shut_down(*args, **kwargs):
            self.backend.vms["dev"]["Running"] = False
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch("appletart.guest_agent.root_available", return_value=False), patch("appletart.lifecycle.run", side_effect=shut_down) as ssh:
            self.api.shutdown("dev", self.report)
        self.assertIn("sync; shutdown -h now", ssh.call_args.args[0][-1])
        self.assertIn("BatchMode=yes", ssh.call_args.args[0])
        self.assertNotIn("stop", [args[0] for args in self.backend.calls])

    def test_failed_shutdown_leaves_guest_running_and_restart_does_not_start(self):
        self.backend.vms["dev"]["Running"] = True
        result = subprocess.CompletedProcess([], 1, "", "sudo requires password")
        with patch("appletart.guest_agent.root_available", return_value=False), patch("appletart.lifecycle.run", return_value=result), patch.object(self.api, "start") as start:
            with self.assertRaisesRegex(DeploymentError, "Passwordless sudo"):
                self.api.restart("dev", self.report)
        start.assert_not_called()
        self.assertTrue(self.backend.vms["dev"]["Running"])
        self.assertNotIn("stop", [args[0] for args in self.backend.calls])

    def test_shutdown_timeout_does_not_force_stop(self):
        self.backend.vms["dev"]["Running"] = True
        with patch("appletart.guest_agent.root_available", return_value=False), patch("appletart.lifecycle.run", return_value=subprocess.CompletedProcess([], 0, "", "")), self.assertRaisesRegex(DeploymentError, "not force-stopped"):
            self.api.shutdown("dev", self.report, timeout=0)
        self.assertTrue(self.backend.vms["dev"]["Running"])

    def test_agent_shutdown_needs_no_ip_and_does_not_force_stop(self):
        self.backend.vms["dev"]["Running"] = True
        def shut_down(*args, **kwargs):
            self.backend.vms["dev"]["Running"] = False
            return subprocess.CompletedProcess(args, 1, "", "RPC connection closed")
        with patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.guest_access.resolve", side_effect=AssertionError("must not require IP")), \
             patch("appletart.lifecycle.run", side_effect=shut_down) as command:
            self.api.shutdown("dev", self.report)
        self.assertEqual(command.call_args.args[0], ["fake-tart", "exec", "dev", "/bin/sh", "-c", "sync; shutdown -h now"])
        self.assertNotIn("stop", [args[0] for args in self.backend.calls])

    def test_agent_disconnect_waits_for_shutdown_without_reissuing_command(self):
        self.backend.vms["dev"]["Running"] = True
        def powered_off(_):
            self.backend.vms["dev"]["Running"] = False
        with patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.lifecycle.run", return_value=subprocess.CompletedProcess([], 1, "", "RPC connection closed")) as command, \
             patch("appletart.lifecycle.pause", side_effect=powered_off):
            self.api.shutdown("dev", self.report)
        command.assert_called_once()
        self.assertNotIn("stop", [args[0] for args in self.backend.calls])

    def test_agent_silent_disconnect_waits_for_tart_poweroff_confirmation(self):
        self.backend.vms["dev"]["Running"] = True
        def powered_off(_):
            self.backend.vms["dev"]["Running"] = False
        with patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.lifecycle.run", return_value=subprocess.CompletedProcess([], 1, "", "")) as command, \
             patch("appletart.lifecycle.pause", side_effect=powered_off):
            self.api.shutdown("dev", self.report)
        command.assert_called_once()
        self.assertFalse(self.backend.vms["dev"]["Running"])
        self.assertNotIn("stop", [args[0] for args in self.backend.calls])

    def test_silent_agent_shutdown_failure_is_not_reported_as_success(self):
        self.backend.vms["dev"]["Running"] = True
        with patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.lifecycle.run", return_value=subprocess.CompletedProcess([], 1, "", "")), \
             self.assertRaisesRegex(DeploymentError, "not force-stopped"):
            self.api.shutdown("dev", self.report, timeout=0)
        self.assertTrue(self.backend.vms["dev"]["Running"])

    def test_edit_profile_updates_same_record_without_changing_existing_vm(self):
        config = self.machine.config()
        before = self.api.store.get("dev")
        original = self.api.save_profile("recipe", config, "Original notes")
        updated = self.api.save_profile("recipe", {**config, "cpu": 3, "packages": ["git", "jq"]}, "Updated notes")
        self.assertEqual(len(self.api.profiles.all()), 1)
        self.assertEqual(updated["config"]["name"], "recipe")
        self.assertEqual(updated["config"]["cpu"], 3)
        self.assertEqual(updated["config"]["packages"], ["git", "jq"])
        self.assertEqual(updated["notes"], "Updated notes")
        self.assertEqual(updated["created_at"], original["created_at"])
        self.assertEqual(self.api.store.get("dev"), before)
        self.assertEqual(self.api.profile_deployment("recipe", "next").vm.cpu, 3)

    def test_profile_roundtrip_keeps_sha512_and_excludes_credentials(self):
        machine = Machine.from_dict({"name": "debian", "os": "other", "source_kind": "iso", "source": "https://example.com/debian-arm64.iso", "sha512": "a" * 128})
        profile = self.api.save_profile("debian-small", machine.config(), "Debian installer")
        self.assertEqual(profile["config"]["sha512"], "a" * 128)
        portable = json.loads(json.dumps(profile))
        portable["name"] = "debian-copy"
        imported = self.api.import_profile(portable)
        self.assertEqual(imported["config"]["name"], "debian-copy")
        self.assertEqual(imported["config"]["sha512"], "a" * 128)
        with self.assertRaises(DeploymentError):
            self.api.import_profile({**portable, "password": "must-not-save"})
        with self.assertRaises(DeploymentError):
            self.api.save_profile("bad", {**machine.config(), "private_key": "must-not-save"})
        with self.assertRaisesRegex(DeploymentError, "confirm"):
            self.api.delete_profile("debian-copy", "wrong")
        self.api.delete_profile("debian-copy", "debian-copy")
        self.assertIsNone(self.api.profiles.get("debian-copy"))

    def test_cleanup_protects_referenced_files_and_validates_entire_selection(self):
        cache = self.api.store.root / "downloads"
        cache.mkdir()
        used = cache / ("a" * 128 + ".iso")
        unused = cache / ("b" * 128 + ".iso")
        used.write_bytes(b"used")
        unused.write_bytes(b"unused")
        record = self.api.store.get("dev")
        self.api.store.put({**record, "artifact": str(used)})
        items = self.api.storage_listing()["items"]
        self.assertTrue(next(i for i in items if i["id"] == "downloads/" + used.name)["protected"])
        with self.assertRaisesRegex(DeploymentError, "protected"):
            self.api.cleanup_storage(["downloads/" + unused.name, "downloads/" + used.name], "DELETE", self.report)
        self.assertTrue(unused.exists())
        self.api.cleanup_storage(["downloads/" + unused.name], "DELETE", self.report)
        self.assertFalse(unused.exists())
        self.assertTrue(used.exists())

    def test_cleanup_cannot_overlap_build_or_delete_arbitrary_paths(self):
        with self.api.operation("vm-dev"), self.assertRaisesRegex(DeploymentError, "cannot run together"):
            self.api.cleanup_storage(["downloads/a"], "DELETE", self.report)
        with guard(self.api.store.root, exclusive=True), self.assertRaisesRegex(DeploymentError, "cannot run together"):
            self.api.configure("dev", self.machine.config(), self.report)
        with self.assertRaisesRegex(DeploymentError, "protected or unavailable"):
            self.api.cleanup_storage(["../outside"], "DELETE", self.report)

    def test_cache_purge_supports_all_unused_files_and_rechecks_protection(self):
        cache = self.api.store.root / "downloads"
        cache.mkdir()
        paths = [cache / (format(index, '064x') + '.qcow2') for index in range(102)]
        for path in paths:
            path.write_bytes(b'cached disk')
        items = ['downloads/' + path.name for path in paths]
        # A download can become referenced after the UI showed its preview.
        record = self.api.store.get('dev')
        self.api.store.put({**record, 'artifact': str(paths[0])})
        with self.assertRaisesRegex(DeploymentError, 'protected'):
            self.api.cleanup_storage(items, 'DELETE', self.report, cache_only=True)
        self.assertTrue(all(path.exists() for path in paths))
        self.api.cleanup_storage(items[1:], 'DELETE', self.report, cache_only=True)
        self.assertTrue(paths[0].exists())
        self.assertTrue(all(not path.exists() for path in paths[1:]))

    def test_cache_purge_cannot_delete_unreferenced_golden_images(self):
        image = self.golden_image()
        self.assertFalse(next(item for item in self.api.storage_listing()['items'] if item['id'] == 'golden/' + image['name'])['protected'])
        with self.assertRaisesRegex(DeploymentError, 'protected or unavailable'):
            self.api.cleanup_storage(['golden/' + image['name']], 'DELETE', self.report, cache_only=True)
        self.assertIsNotNone(self.api.images.get(image['name']))
        with self.assertRaisesRegex(DeploymentError, 'cache_only'):
            self.api.cleanup_storage(['golden/' + image['name']], 'DELETE', self.report, cache_only='true')

    def test_profile_references_protect_golden_image_from_cleanup(self):
        self.backend.run(["clone", "dev", "golden-internal"])
        config = Machine.from_dict({"name": "golden", "os": "kali", "ssh_public_keys": ["/tmp/user.pub"], "install_default_packages": False}).config()
        self.api.images.put({"config": config, "tart_name": "golden-internal", "identity": self.backend.identity("golden-internal"), "phase": "ready", "source_vm": "dev", "created_at": "2026-10-03"})
        profile = {**config, "source_kind": "golden", "source": "golden"}
        self.api.save_profile("from-golden", profile)
        self.assertTrue(next(i for i in self.api.storage_listing()["items"] if i["id"] == "golden/golden")["protected"])
        with self.assertRaisesRegex(DeploymentError, "protected"):
            self.api.cleanup_storage(["golden/golden"], "DELETE", self.report)

    def golden_image(self):
        self.backend.run(["clone", "dev", "golden-internal"])
        config = Machine.from_dict({"name": "golden", "os": "kali", "ssh_public_keys": ["/tmp/user.pub"], "install_default_packages": False}).config()
        self.api.images.put({"config": config, "tart_name": "golden-internal", "identity": self.backend.identity("golden-internal"), "phase": "ready", "source_vm": "dev", "created_at": "2026-10-03"})
        return config

    def test_profile_deployment_reuses_saved_configuration_with_a_new_identity(self):
        config = Machine.from_dict({"name": "kali-recipe", "os": "kali", "cpu": 2, "memory_mb": 8192, "disk_gb": 80,
                                    "source_kind": "cloud", "source": str(self.root / "kali-arm64.qcow2"), "sha256": "",
                                    "ssh_user": "jay.morris", "ssh_public_keys": ["/tmp/user.pub"], "hostname": "original",
                                    "install_default_packages": False, "packages": ["git", "jq"],
                                    "port_forwards": [{"listen_address": "127.0.0.1", "host_port": 8443, "guest_port": 443}],
                                    "directory_shares": [{"host_path": str(self.root), "guest_path": "/mnt/share", "read_only": True}]}).config()
        saved = self.api.save_profile("kali-recipe", config)
        deployed = self.api.profile_deployment("kali-recipe", "kali-next").config()
        self.assertEqual(deployed, {**saved["config"], "name": "kali-next", "hostname": "kali-next"})
        self.assertEqual(self.api.profiles.get("kali-recipe"), saved)
        self.assertIsNone(self.api.store.get("kali-next"))
        with self.assertRaisesRegex(DeploymentError, "already in use"):
            self.api.profile_deployment("kali-recipe", "dev")
        with self.assertRaisesRegex(DeploymentError, "no longer exists"):
            self.api.profile_deployment("missing", "new-vm")
        with self.assertRaises(DeploymentError):
            self.api.profile_deployment("kali-recipe", "../invalid")

    def test_delete_golden_image_checks_confirmation_and_removes_only_its_disk(self):
        self.golden_image()
        with self.assertRaisesRegex(DeploymentError, "confirm"):
            self.api.delete_image("golden", "wrong", self.report)
        self.assertTrue((self.backend.home / "vms" / "golden-internal").exists())
        self.api.delete_image("golden", "golden", self.report)
        self.assertIsNone(self.api.images.get("golden"))
        self.assertFalse((self.backend.home / "vms" / "golden-internal").exists())
        self.assertTrue(self.disk.exists())

    def test_golden_deletion_rejects_references_running_images_and_changed_identity(self):
        config = self.golden_image()
        self.api.save_profile("from-golden", {**config, "source_kind": "golden", "source": "golden"})
        self.assertEqual(self.api.image_listing()[0]["references"], ["Referenced by from-golden"])
        with self.assertRaisesRegex(DeploymentError, "protected"):
            self.api.delete_image("golden", "golden", self.report)
        self.api.delete_profile("from-golden", "from-golden")
        self.backend.vms["golden-internal"]["Running"] = True
        with self.assertRaisesRegex(DeploymentError, "running"):
            self.api.delete_image("golden", "golden", self.report)
        self.backend.vms["golden-internal"]["Running"] = False
        record = self.api.images.get("golden")
        record["identity"]["inode"] += 1
        self.api.images.put(record)
        with self.assertRaisesRegex(DeploymentError, "identity"):
            self.api.delete_image("golden", "golden", self.report)
        self.assertTrue((self.backend.home / "vms" / "golden-internal").exists())

    def test_checksum_optional_download_and_stamp_are_available_for_cleanup(self):
        response = io.BytesIO(b"cloud disk")
        response.url = "https://example.com/arm64.qcow2"
        with patch("appletart.downloads.urllib.request.urlopen", return_value=response):
            image = fetch_image(response.url, "", self.api.store.root / "downloads", self.report)
        self.assertTrue(image.with_suffix(".sha256").exists())
        item = next(i for i in self.api.storage_listing()["items"] if i["name"] == image.name)
        self.api.cleanup_storage([item["id"]], "DELETE", self.report)
        self.assertFalse(image.exists())
        self.assertFalse(image.with_suffix(".sha256").exists())


class ChecksumTests(unittest.TestCase):
    def test_no_hash_download_reuses_cache_and_detects_changed_cache_content(self):
        payload = b"ARM64 cloud disk"
        def response():
            result = io.BytesIO(payload)
            result.url = "https://example.com/disk.qcow2"
            return result
        with tempfile.TemporaryDirectory() as directory, patch("appletart.downloads.urllib.request.urlopen", side_effect=lambda *a, **k: response()) as download:
            root = Path(directory)
            report = Mock()
            path = fetch_image(response().url, "", root, report)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(fetch_image(response().url, "", root, report), path)
            self.assertEqual(fetch_image(str(path), "", root, report), path)
            self.assertEqual(download.call_count, 1)
            self.assertFalse(any("verified" in str(c).lower() for c in report.call_args_list))
            path.write_bytes(b"changed content")
            with self.assertRaisesRegex(DeploymentError, "cached image changed"):
                fetch_image(str(path), "", root, report)
            self.assertEqual(fetch_image(response().url, "", root, report).read_bytes(), payload)
            self.assertEqual(download.call_count, 2)

    def test_optional_hash_download_failures_never_become_cached_inputs(self):
        class Interrupted(io.BytesIO):
            url = "https://example.com/arm64.img"
            def read(self, size=-1):
                if self.tell():
                    raise OSError("download interrupted")
                return super().read(size)
        truncated = io.BytesIO(b"part of disk")
        truncated.headers = {"Content-Length": "1000"}
        redirected = io.BytesIO(b"disk")
        redirected.url = "http://example.com/arm64.img"
        for response, message in ((io.BytesIO(b""), "empty"), (Interrupted(b"part of disk"), "interrupted"), (truncated, "incomplete"), (redirected, "away from HTTPS")):
            response.url = getattr(response, "url", "https://example.com/arm64.img")
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory, patch("appletart.downloads.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(DeploymentError, message):
                    fetch_image("https://example.com/arm64.img", "", Path(directory), Mock())
                self.assertFalse(any(p.suffix in (".img", ".sha256", ".part") for p in Path(directory).iterdir()))

    def test_local_cloud_image_without_a_hash_is_used_in_place(self):
        with tempfile.TemporaryDirectory(prefix="cloud images ") as directory:
            path = Path(directory) / "debian-arm64.qcow2"
            path.write_bytes(b"local disk")
            cache = Path(directory) / "downloads"
            with patch("appletart.downloads.urllib.request.urlopen") as download:
                self.assertEqual(fetch_image(str(path), "", cache, Mock()), path)
                download.assert_not_called()
            self.assertFalse(cache.exists())
            path.write_bytes(b"")
            with self.assertRaisesRegex(DeploymentError, "empty"):
                fetch_image(str(path), "", cache, Mock())

    def test_sha256_and_sha512_verify_remote_cached_and_local_images(self):
        payload = b"verified ARM64 cloud disk"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for algorithm in ("sha256", "sha512"):
                checksum = hashlib.new(algorithm, payload).hexdigest()
                response = io.BytesIO(payload)
                response.url = "https://example.com/disk.img"
                with patch("appletart.downloads.urllib.request.urlopen", return_value=response) as fetch:
                    result = fetch_image(response.url, checksum.upper(), root, Mock())
                    self.assertEqual(result.name, checksum + ".img")
                    fetch_image("https://example.com/disk.img", checksum, root, Mock())
                    self.assertEqual(fetch.call_count, 1)
                self.assertEqual(fetch_image(str(result), checksum, root, Mock()), result)
                result.write_bytes(b"tampered disk")
                with self.assertRaisesRegex(DeploymentError, algorithm.upper()):
                    fetch_image(str(result), checksum, root, Mock())

    def test_bad_sha512_and_ambiguous_checksums_are_rejected(self):
        common = {"name": "debian", "os": "other", "source_kind": "iso", "source": "https://example.com/debian-arm64.iso"}
        for changes in ({"sha512": "a" * 64}, {"sha512": "z" * 128}, {"sha256": "a" * 64, "sha512": "b" * 128}):
            with self.subTest(changes=changes), self.assertRaises(DeploymentError):
                Machine.from_dict({**common, **changes})

    def test_sha512_mismatch_never_becomes_a_cached_build_input(self):
        with tempfile.TemporaryDirectory() as directory:
            response = io.BytesIO(b"wrong disk")
            response.url = "https://example.com/disk.img"
            with patch("appletart.downloads.urllib.request.urlopen", return_value=response), self.assertRaisesRegex(DeploymentError, "SHA512 verification"):
                fetch_image(response.url, "a" * 128, Path(directory), Mock())
            self.assertFalse(any(p.suffix in (".img", ".part") for p in Path(directory).iterdir()))


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.machine = Machine.from_dict({"name": "dev"})
        self.backend = Mock()
        self.backend.run.return_value = "192.0.2.10"
        self.socket = MagicMock()
        self.socket.__enter__.return_value.recv.return_value = b"SSH-2.0-OpenSSH"

    def test_banner_during_pam_nologin_is_not_ssh_ready(self):
        result = subprocess.CompletedProcess([], 255, "", "System is booting up. Unprivileged users are not permitted to log in yet.")
        with patch("appletart.guest_access.socket.create_connection", return_value=self.socket), patch("appletart.guest_access.run", return_value=result):
            health = guest_access.health(Path("/tmp/nonexistent"), self.machine, {}, self.backend)
        self.assertTrue(health["ssh_available"])
        self.assertFalse(health["ssh_ready"])
        self.assertEqual(health["status"], "running")
        self.assertIn("authentication is not ready", health["issues"][0])

    def test_authentication_confirms_readiness(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch("appletart.guest_access.socket.create_connection", return_value=self.socket), patch("appletart.guest_access.run", return_value=result) as ssh:
            health = guest_access.health(Path("/tmp/nonexistent"), self.machine, {}, self.backend)
        self.assertTrue(health["ssh_ready"])
        self.assertEqual(health["status"], "ssh-ready")
        self.assertEqual(ssh.call_args.args[0][-1], "true")
        self.assertIn("StrictHostKeyChecking=yes", ssh.call_args.args[0])

    def test_cloud_without_personal_keys_reports_agent_ready_without_ssh_auth_errors(self):
        machine = Machine.from_dict({"name": "dev", "os": "kali", "ssh_public_keys": []})
        with patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.guest_access.socket.create_connection", return_value=self.socket), \
             patch("appletart.guest_access.ssh_args", side_effect=AssertionError("Personal key installation was disabled")), \
             patch("appletart.guest_access.run") as command:
            health = guest_access.health(Path("/tmp/nonexistent"), machine, {"guest_agent_privileged": True}, self.backend)
        command.assert_not_called()
        self.assertEqual(health["status"], "agent-ready")
        self.assertEqual(health["management_transport"], "agent")
        self.assertFalse(health["ssh_ready"])
        self.assertEqual(health["issues"], [])

    def test_details_check_interfaces_and_mount_read_only_state(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = Machine.from_dict({"name": "dev", "directory_shares": [{"host_path": directory, "guest_path": "/mnt/shared", "read_only": True}]})
            output = json.dumps([{"ifname": "eth0", "operstate": "UP", "addr_info": [{"family": "inet", "local": "192.0.2.10"}]}]) + "\nAPPLETART_MOUNTS\n" + json.dumps({"filesystems": [{"target": "/mnt/shared", "fstype": "virtiofs", "options": "ro,relatime"}]})
            result = subprocess.CompletedProcess([], 0, output, "")
            with patch("appletart.guest_agent.root_available", return_value=False), patch("appletart.guest_access.socket.create_connection", return_value=self.socket), patch("appletart.guest_access.run", return_value=result):
                health = guest_access.health(Path(directory), machine, {}, self.backend, details=True)
            self.assertEqual(health["interfaces"][0]["addresses"], ["192.0.2.10"])
            self.assertTrue(health["shares"][0]["mounted"])
            self.assertTrue(health["shares"][0]["read_only_verified"])
            self.assertEqual(health["issues"], [])

    def test_agent_details_work_without_network_or_ssh(self):
        self.backend.binary = "fake-tart"
        self.backend.run.side_effect = DeploymentError("no IP address found")
        output = json.dumps([{"ifname": "eth0", "operstate": "DOWN", "addr_info": []}]) + "\nAPPLETART_MOUNTS\n{}"
        with patch("appletart.guest_agent.root_available", return_value=True), \
             patch("appletart.guest_access.resolve", side_effect=DeploymentError("no guest network")), \
             patch("appletart.guest_access.socket.create_connection", side_effect=AssertionError("must not require TCP")), \
             patch("appletart.guest_access.ssh_args", side_effect=AssertionError("must not require SSH")), \
             patch("appletart.guest_access.run", return_value=subprocess.CompletedProcess([], 0, output, "")) as command:
            health = guest_access.health(Path("/tmp/nonexistent"), self.machine, {}, self.backend, details=True)
        self.assertEqual(health["status"], "agent-ready")
        self.assertEqual(health["management_transport"], "agent")
        self.assertTrue(health["agent_privileged"])
        self.assertFalse(health["ssh_ready"])
        self.assertEqual(health["ip"], "")
        self.assertEqual(health["interfaces"][0]["name"], "eth0")
        self.assertEqual(command.call_args.args[0], ["fake-tart", "exec", "-i", "dev", "/bin/sh", "-s"])


if __name__ == "__main__":
    unittest.main()
