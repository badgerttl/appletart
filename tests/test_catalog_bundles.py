import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart import bundles, image_catalog, profiles, settings
from appletart.catalog import Machine, choices
from appletart.deployment import DeploymentError
from appletart.downloads import fetch_image
from appletart.lifecycle import Lifecycle
from test_lifecycle import FakeBackend


class CatalogBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = FakeBackend()
        self.api = Lifecycle(self.root, lambda report=print: self.backend)

    def bundle(self, name="Tools", packages=None, platforms=None, **fields):
        return self.api.save_bundle({"name": name, "packages": packages or ["git", "jq"], "platforms": platforms or ["linux"], **fields})

    def test_bundles_merge_deduplicate_and_freeze_existing_builds(self):
        first = self.bundle()
        second = self.bundle("More tools", ["jq", "ripgrep"])
        machine = self.api.machine({"name": "dev", "software_bundles": [first["id"], second["id"]], "packages": ["git", "curl"]})
        self.assertEqual(machine.effective_packages, ("git", "curl", "jq", "ripgrep"))
        frozen = machine.config()
        self.bundle(packages=["btop"], id=first["id"])
        self.api.delete_bundle(second["id"])
        self.assertEqual(self.api.machine(frozen).effective_packages, machine.effective_packages)
        self.assertEqual(self.api.machine({"name": "new", "software_bundles": [first["id"]]}).effective_packages, ("btop",))
        with self.assertRaisesRegex(DeploymentError, "no longer exists"):
            self.api.machine({"name": "new", "software_bundles": [second["id"]]})

    def test_empty_bundle_snapshot_stays_empty_after_bundle_edit(self):
        value = self.api.save_bundle({"name": "Empty", "platforms": ["linux"], "packages": []})
        frozen = self.api.machine({"name": "dev", "software_bundles": [value["id"]]}).config()
        self.bundle(name="Empty", packages=["git"], id=value["id"])
        self.assertEqual(self.api.machine(frozen).effective_packages, ())

    def test_profile_deployments_resolve_latest_bundle_without_mutating_profile(self):
        value = self.bundle()
        profile = self.api.save_profile("tools-profile", {"name": "base", "software_bundles": [value["id"]]})
        self.bundle(packages=["btop"], id=value["id"])
        machine = profiles.deployment(self.api, "tools-profile", "new-dev")
        self.assertEqual(machine.effective_packages, ("btop",))
        self.assertEqual(self.api.profiles.get("tools-profile"), profile)

    def test_incompatible_bundle_duplicate_names_and_shell_payloads_are_rejected(self):
        value = self.bundle(platforms=["kali"])
        with self.assertRaisesRegex(DeploymentError, "not compatible"):
            self.api.machine({"name": "dev", "os": "macos", "software_bundles": [value["id"]]})
        with self.assertRaisesRegex(DeploymentError, "already has"):
            self.bundle("TOOLS")
        for package in ["--help", "git; id", "$(id)", "git\njq"]:
            with self.subTest(package=package), self.assertRaises(DeploymentError):
                self.bundle("Unsafe", [package])
        self.assertEqual(bundles.packages(["NetworkManager", "libX11-devel", "foo_bar", "jq=1.7", "openai/tools/tart-guest-agent"])[0], "NetworkManager")

    def test_new_kali_has_no_implicit_software_and_old_recipe_is_migrated(self):
        config = {"name": "kali", "os": "kali", "ssh_public_keys": ["/tmp/key.pub"]}
        self.assertEqual(self.api.machine(config).effective_packages, ())
        legacy = Machine.from_dict({**config, "install_default_packages": True})
        self.assertIn("kali-linux-headless", legacy.effective_packages)
        self.api.store.put({"config": {**legacy.config(), "install_default_packages": True}, "phase": "created"})
        restored = self.api.store.get("kali")["config"]
        self.assertNotIn("install_default_packages", restored)
        self.assertEqual(self.api.machine(restored).effective_packages, legacy.effective_packages)

    def test_custom_catalog_controls_new_defaults_without_distro_branches(self):
        catalog = {"custom-9000": {"label": "Custom guest", "family": "linux", "source_kind": "tart", "source": "custom-template", "ssh_user": "ops", "icon": "other"}}
        self.api.save_catalog(catalog)
        machine = self.api.machine({"name": "custom", "os": "custom-9000"})
        self.assertEqual((machine.source, machine.vm.ssh_user, machine.guest_family), ("custom-template", "ops", "linux"))
        self.assertEqual(image_catalog.load(self.root), catalog)
        self.assertEqual((self.root / "image-catalog.json").stat().st_mode & 0o777, 0o600)
        # A saved recipe still loads after a platform definition is removed.
        self.api.save_catalog({"other": {**catalog["custom-9000"], "label": "Other"}})
        self.assertEqual(self.api.machine(machine.config()), machine)

    def test_catalog_contains_arm64_published_platforms_versions_and_icons(self):
        catalog = image_catalog.load()
        self.assertTrue({"ubuntu", "debian", "fedora", "rocky", "ubuntu-runner-arm64", "macos", "kali", "rhel", "other"} <= set(catalog))
        for identifier, definition in catalog.items():
            with self.subTest(platform=identifier):
                self.assertTrue((image_catalog.DATA.parent / "static/icons" / (definition["icon"] + ".svg")).is_file())
        versions = catalog["macos"]["versions"]
        self.assertGreater(len(versions), 100)
        self.assertTrue(any("Tahoe · Base" in value["label"] for value in versions))
        self.assertTrue(any("Sequoia · Xcode" in value["label"] for value in versions))
        self.assertTrue(all("amd64" not in definition["source"] for definition in catalog.values()))
        self.assertEqual(Machine.from_dict({"name": "mac", "os": "macos", "size": "small"}).vm.disk_gb, 50)
        xcode = next(value for value in versions if " · Xcode · " in value["label"])
        self.assertEqual(Machine.from_dict({"name": "mac", "os": "macos", "source": xcode["source"], "disk_gb": 40}).vm.disk_gb, 140)

    def test_catalog_rejects_invalid_sources_hashes_and_macos_cloud_sources(self):
        baseline = {"label": "Guest", "family": "macos", "source_kind": "tart", "source": "template", "icon": "macos"}
        for fields in ({"source_kind": "cloud"}, {"source": "--overwrite"}, {"sha512": "bad"}, {"icon": "../file"}, {"minimum_disk_gb": True}, {"versions": [{"label": "Version", "source": "https://example.org/disk.img", "source_kind": "cloud"}]}):
            with self.subTest(fields=fields), self.assertRaises(DeploymentError):
                image_catalog.validate({"guest": {**baseline, **fields}})

    def test_kali_default_is_a_named_version_and_added_versions_have_their_own_hash(self):
        catalog = image_catalog.load()
        kali = catalog["kali"]
        latest = next(version for version in kali["versions"] if version["source"] == kali["source"])
        self.assertIn("2026.2", latest["label"])
        self.assertEqual(latest["sha256"], kali["sha256"])
        custom = {"label": "Older release", "source": "https://example.org/kali-arm64.qcow2", "source_kind": "cloud", "sha512": "b" * 128}
        kali["versions"].append(custom)
        self.api.save_catalog(catalog)
        config = {"name": "custom-kali", "os": "kali", "source": custom["source"], "ssh_public_keys": ["/tmp/key.pub"]}
        machine = self.api.machine(config)
        self.assertEqual((machine.source_kind, machine.sha256, machine.sha512), ("cloud", "", custom["sha512"]))
        self.assertEqual(self.api.machine({**config, "sha256": ""}).checksum, "")
        self.assertEqual(self.api.machine({**config, "sha256": "a" * 64}).checksum, "a" * 64)
        default = self.api.machine({"name": "latest-kali", "os": "kali", "ssh_public_keys": ["/tmp/key.pub"]})
        self.assertEqual((default.source, default.sha256), (kali["source"], kali["sha256"]))

    def test_tart_software_build_uses_template_metadata_and_preserves_larger_disk(self):
        self.backend.disk_capacity_gb = Mock(return_value=60)
        value = self.bundle()
        machine = self.api.machine({"name": "dev", "software_bundles": [value["id"]]})
        with patch("appletart.lifecycle.guest_agent.prepare", return_value=b"agent"), patch("appletart.lifecycle.provision_keys") as provision:
            record = self.api.build(machine, Mock())
        self.assertEqual(record["config"]["disk_gb"], 60)
        self.assertTrue(record["guest_agent_privileged"])
        self.assertEqual(provision.call_args.args[3], "admin")
        self.assertIn("apt-get install", provision.call_args.kwargs["extra_script"])
        self.assertTrue(any("--disk-size" in command and "60" in command for command in self.backend.calls))

    def test_private_bootstrap_password_is_not_in_profile_or_vm_config(self):
        machine = self.api.machine({"name": "dev"})
        self.assertNotIn("password", json.dumps(machine.config()))
        self.assertNotIn("password", json.dumps(self.api.save_profile("template", machine.config())))

    def test_initial_linux_template_upgrades_without_keys_or_bundles_and_only_once(self):
        machine = self.api.machine({"name": "update-only"})
        with patch("appletart.lifecycle.guest_agent.prepare", return_value=b"agent"), patch("appletart.lifecycle.provision_keys") as provision:
            record = self.api.build(machine, Mock())
            self.api.build(machine, Mock())
        provision.assert_called_once()
        self.assertEqual(provision.call_args.args[2], [])
        script = provision.call_args.kwargs["extra_script"]
        self.assertIn("apt-get update", script)
        self.assertIn("apt-get upgrade -y", script)
        self.assertNotIn("apt-get install", script)
        self.assertEqual(record["phase"], "ready")

    def test_failed_initial_upgrade_keeps_template_build_resumable(self):
        machine = self.api.machine({"name": "update-fails"})
        with patch("appletart.lifecycle.guest_agent.prepare", return_value=b"agent"), patch("appletart.lifecycle.provision_keys", side_effect=DeploymentError("upgrade failed")):
            with self.assertRaisesRegex(DeploymentError, "upgrade failed"):
                self.api.build(machine, Mock())
        self.assertEqual(self.api.store.get(machine.vm.name)["phase"], "created")

    def test_macos_template_with_no_selections_does_not_run_linux_upgrades(self):
        machine = self.api.machine({"name": "mac-template", "os": "macos"})
        with patch("appletart.lifecycle.guest_agent.prepare", return_value=b"fixture-agent"), \
             patch("appletart.lifecycle.provision_keys") as provision:
            record = self.api.build(machine, Mock())
        provision.assert_called_once()
        self.assertEqual(provision.call_args.kwargs["extra_script"], "")
        self.assertEqual(provision.call_args.kwargs["family"], "macos")
        self.assertEqual(record["phase"], "ready")

    def test_refresh_download_updates_unpinned_source_and_keeps_cache_on_bad_hash(self):
        class Response(io.BytesIO):
            url = "https://example.org/disk.img"
            headers = {}
        url = Response.url
        with patch("appletart.downloads.urllib.request.urlopen", return_value=Response(b"first")) as download:
            path = fetch_image(url, "", self.root, Mock())
        with patch("appletart.downloads.urllib.request.urlopen", side_effect=AssertionError("cache should be used")):
            self.assertEqual(fetch_image(url, "", self.root, Mock()).read_bytes(), b"first")
        with patch("appletart.downloads.urllib.request.urlopen", return_value=Response(b"second")):
            self.assertEqual(fetch_image(url, "", self.root, Mock(), refresh=True).read_bytes(), b"second")
        checksum = hashlib.sha512(b"first").hexdigest()
        with patch("appletart.downloads.urllib.request.urlopen", return_value=Response(b"first")):
            verified = fetch_image(url, checksum, self.root, Mock())
        with patch("appletart.downloads.urllib.request.urlopen", return_value=Response(b"bad")), self.assertRaisesRegex(DeploymentError, "SHA512"):
            fetch_image(url, checksum, self.root, Mock(), refresh=True)
        self.assertEqual(verified.read_bytes(), b"first")
        self.assertFalse(list(self.root.glob("*.part")))

    def test_public_key_setting_is_explicit_and_preserved_when_terminal_changes(self):
        with patch("appletart.settings.application", return_value=Path("/Applications/iTerm.app")):
            settings.save(self.root, {"terminal": "iterm2", "default_public_key": "~/.ssh/custom.pub"})
            settings.save(self.root, {"terminal": "terminal"})
        self.assertEqual(settings.load(self.root)["default_public_key"], "~/.ssh/custom.pub")
        (self.root / "settings.json").write_text("invalid")
        with patch("appletart.settings.application", return_value=Path("/Applications/iTerm.app")):
            settings.save(self.root, {"terminal": "iterm2", "default_public_key": "~/.ssh/custom.pub"})
        self.assertEqual(settings.load(self.root)["terminal"], "iterm2")

    def test_default_key_installation_can_be_disabled_and_survives_other_settings_edits(self):
        with patch("appletart.settings.application", return_value=Path("/Applications/iTerm.app")):
            settings.save(self.root, {"terminal": "iterm2", "default_public_key": "~/.ssh/custom.pub", "install_ssh_key_by_default": False})
            settings.save(self.root, {"terminal": "terminal"})
            self.assertFalse(settings.listing(self.root)["install_ssh_key_by_default"])
            self.assertFalse(choices(self.root)["install_ssh_key_by_default"])
            self.assertEqual(choices(self.root)["default_public_key"], "~/.ssh/custom.pub")
            for invalid in ("false", 0, None):
                with self.subTest(invalid=invalid), self.assertRaises(DeploymentError):
                    settings.save(self.root, {"terminal": "terminal", "install_ssh_key_by_default": invalid})


if __name__ == '__main__':
    unittest.main()
