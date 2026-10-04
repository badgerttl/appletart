from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart.catalog import Machine
from appletart.deployment import DeploymentError
from appletart.lifecycle import Lifecycle
from test_lifecycle import FakeBackend


class GoldenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.mac_address = Mock(return_value="02:00:00:00:00:01")
        self.api = Lifecycle(self.root, lambda report=print: self.backend)
        self.machine = Machine.from_dict({"name": "source", "os": "kali", "ssh_user": "jay.morris",
                                         "ssh_public_keys": ["/tmp/user.pub"], "install_default_packages": False})
        self.backend.run(["create", "source"])
        self.api.store.put({"config": self.machine.config(), "owned": True, "phase": "ready",
                            "identity": self.backend.identity("source")})

    def bootstrap(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        key = directory / "bootstrap"
        key.write_text("temporary")
        key.with_suffix(".pub").write_text("public")
        return key, "ssh-ed25519 build"

    def seed(self, machine, keys, directory, instance, mac):
        path = directory / "seed.iso"
        path.write_bytes(b"ISO")
        return path

    def test_template_is_cleaned_and_reused_with_new_instance_ids(self):
        original = self.api.store.get("source")
        with patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.lifecycle.bootstrap_key", side_effect=self.bootstrap), \
             patch("appletart.lifecycle.write_seed", side_effect=self.seed), \
             patch("appletart.lifecycle.check_cloud_tools"), \
             patch("appletart.lifecycle.prepare_linux_golden"), \
             patch("appletart.lifecycle.provision_cloud") as provision, \
             patch("appletart.lifecycle.convert_disk") as convert:
            image = self.api.create_golden("source", "kali-golden", Mock())
            self.assertTrue(provision.call_args.kwargs["prepare_image"])
            self.assertEqual(self.api.store.get("source"), original)
            self.assertEqual([item["config"]["name"] for item in self.api.listing()["machines"]], ["source"])
            for name in ["clone1", "clone2"]:
                clone = Machine.from_dict({**self.machine.config(), "name": name, "source_kind": "golden", "source": "kali-golden"})
                self.api.build(clone, Mock())
                self.assertEqual(self.api.store.get(name)["phase"], "ready")
            convert.assert_not_called()
        self.assertNotEqual(self.api.store.get("clone1")["cloud_instance_id"], self.api.store.get("clone2")["cloud_instance_id"])
        self.assertEqual(sum(args[0] == "clone" and args[1] == image["tart_name"] for args in self.backend.calls), 2)

    def test_running_source_and_changed_template_are_rejected(self):
        self.backend.vms["source"]["Running"] = True
        with self.assertRaisesRegex(DeploymentError, "Stop"):
            self.api.create_golden("source", "golden", Mock())
        self.backend.vms["source"]["Running"] = False
        self.api.images.put({"config": {**self.machine.config(), "name": "golden"}, "phase": "ready", "tart_name": "source", "identity": {"inode": "different"}})
        with patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), self.assertRaisesRegex(DeploymentError, "identity"):
            self.api.download(Machine.from_dict({**self.machine.config(), "name": "clone", "source_kind": "golden", "source": "golden"}), Mock())

    def test_cloud_source_prepares_only_copy_before_fresh_nocloud_seed(self):
        source = self.api.store.get('source')
        order = []
        with patch('appletart.lifecycle.read_public_keys', return_value=[]), \
             patch('appletart.lifecycle.prepare_linux_golden', side_effect=lambda *args: order.append('prepare')) as prepare, \
             patch('appletart.lifecycle.bootstrap_key', side_effect=self.bootstrap), \
             patch('appletart.lifecycle.write_seed', side_effect=lambda *args: (order.append('seed'), self.seed(*args))[1]), \
             patch('appletart.lifecycle.provision_cloud', side_effect=lambda *args, **kwargs: order.append('provision')):
            image = self.api.create_golden('source', 'cloud-golden', Mock())
        self.assertEqual(order, ['prepare', 'seed', 'provision'])
        self.assertEqual(prepare.call_args.args[1].vm.name, image['tart_name'])
        self.assertNotEqual(prepare.call_args.args[1].vm.name, 'source')
        self.assertEqual(self.api.store.get('source'), source)

    def test_linux_tart_golden_prepares_only_its_copy_and_uses_nocloud(self):
        machine = Machine.from_dict({**self.machine.config(), "os": "fedora", "source_kind": "tart", "source": "ghcr.io/cirruslabs/fedora:42", "hostname": ""})
        source = self.api.store.get("source")
        source["config"] = machine.config()
        self.api.store.put(source)
        with patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.lifecycle.bootstrap_key", side_effect=self.bootstrap), \
             patch("appletart.lifecycle.write_seed", side_effect=self.seed), \
             patch("appletart.lifecycle.prepare_linux_golden") as prepare, \
             patch("appletart.lifecycle.provision_cloud") as provision:
            image = self.api.create_golden("source", "fedora-golden", Mock())
        self.assertEqual(prepare.call_args.args[1].vm.name, image["tart_name"])
        self.assertEqual(provision.call_args.args[1].source_kind, "golden")
        self.assertEqual(self.api.store.get("source"), source)

    def test_failed_tart_capability_check_removes_copy_and_preserves_source(self):
        machine = Machine.from_dict({**self.machine.config(), "source_kind": "tart", "source": "local-template", "hostname": ""})
        source = self.api.store.get("source")
        source["config"] = machine.config()
        self.api.store.put(source)
        with patch("appletart.lifecycle.read_public_keys", return_value=[]), \
             patch("appletart.lifecycle.prepare_linux_golden", side_effect=DeploymentError("missing cloud-init")), \
             self.assertRaisesRegex(DeploymentError, "missing cloud-init"):
            self.api.create_golden("source", "unusable", Mock())
        self.assertEqual(self.api.store.get("source"), source)
        self.assertFalse(self.api.images.get("unusable"))
        self.assertEqual(list(self.backend.vms), ["source"])

    def test_resuming_an_older_golden_build_refreshes_cached_cloud_init_once(self):
        clone = Machine.from_dict({**self.machine.config(), "name": "legacy-clone", "source_kind": "golden", "source": "golden"})
        self.backend.run(["create", "legacy-clone"])
        self.api.store.put({"config": clone.config(), "owned": True, "phase": "created",
                            "identity": self.backend.identity("legacy-clone"), "cloud_imported": True,
                            "cloud_configured": True, "cloud_instance_id": "old-cached-instance"})
        directory = self.root / "cloud-init" / "legacy-clone"
        directory.mkdir(parents=True)
        (directory / "known_hosts").write_text("old host key")
        with patch("appletart.lifecycle.read_public_keys", return_value=["ssh-ed25519 user"]), \
             patch("appletart.lifecycle.bootstrap_key", side_effect=self.bootstrap), \
             patch("appletart.lifecycle.write_seed", side_effect=self.seed) as seed, \
             patch("appletart.lifecycle.check_cloud_tools"), \
             patch("appletart.lifecycle.provision_cloud", side_effect=[DeploymentError("retry interrupted"), None]):
            with self.assertRaisesRegex(DeploymentError, "retry interrupted"):
                self.api.build(clone, Mock())
            self.api.build(clone, Mock())
        instances = [call.args[3] for call in seed.call_args_list]
        self.assertNotEqual(instances[0], "old-cached-instance")
        self.assertTrue(all(instance == instances[0] for instance in instances))
        self.assertFalse((directory / "known_hosts").exists())
        self.assertEqual((directory / "known_hosts.before-identity-repair").read_text(), "old host key")
        self.assertFalse(any(args[0] == "clone" or "--random-mac" in args for args in self.backend.calls))


if __name__ == "__main__":
    unittest.main()
