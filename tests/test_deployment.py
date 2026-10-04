import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from appletart.cli import main
from appletart.deployment import DeploymentError, VM, load_manifest
from appletart.tart import Tart


class ValidationTests(unittest.TestCase):
    def test_plan_needs_no_tart_or_supported_host(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "ubuntu.toml"
            manifest.write_text('version = 1\n[[vms]]\nname = "ubuntu-dev"\nos = "ubuntu"\nimage = "ghcr.io/cirruslabs/ubuntu:latest"\ncpu = 2\nmemory_mb = 4096\ndisk_gb = 40\nssh_user = "admin"\nssh_public_keys = ["~/.ssh/id_ed25519.pub"]\n')
            with patch("appletart.cli.Tart", side_effect=AssertionError("Must not touch Tart")), patch("appletart.cli.read_public_keys", return_value=[]), contextlib.redirect_stdout(output):
                self.assertEqual(main(["plan", str(manifest)]), 0)
        self.assertIn("tart clone ghcr.io/cirruslabs/ubuntu:latest ubuntu-dev", output.getvalue())
        self.assertIn("--memory 4096", output.getvalue())

    def test_rejects_unsupported_and_invalid_inputs(self):
        for fields in (
            {"os": "windows"}, {"os": "kali"}, {"cpu": True},
            {"memory_mb": -1}, {"disk_gb": 0}, {"name": "--overwrite"},
            {"image": "--overwrite"}, {"cpus": 4}, {"name": "../vm"},
        ):
            with self.subTest(fields=fields), self.assertRaises(DeploymentError):
                VM.from_dict({"name": "test", "os": "ubuntu", **fields})

    def test_manifest_rejects_duplicates_and_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lab.toml"
            for second in ('name="one"\nos="ubuntu"', 'name="two"\nos="kali"\nimage="one"'):
                path.write_text('version=1\n[[vms]]\nname="one"\nos="ubuntu"\n[[vms]]\n' + second)
                with self.subTest(second=second), self.assertRaises(DeploymentError):
                    load_manifest(path)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        executable = self.root / "tart"
        executable.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with Path(os.environ["FAKE_TART_LOG"]).open("a") as log:
    log.write(json.dumps({"args": sys.argv[1:], "no_prune": os.environ.get("TART_NO_AUTO_PRUNE")}) + "\\n")
if sys.argv[1] == "list":
    print(os.environ.get("FAKE_TART_INVENTORY", "[]"))
if sys.argv[1] == os.environ.get("FAKE_TART_FAIL"):
    print("fake failure", file=sys.stderr)
    sys.exit(7)
''')
        executable.chmod(0o755)
        self.log = self.root / "log.jsonl"
        env = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"], "FAKE_TART_LOG": str(self.log)})
        env.start()
        self.addCleanup(env.stop)
        host = patch("appletart.tart.check_host")
        host.start()
        self.addCleanup(host.stop)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_real_process_arguments_and_environment(self):
        vm = VM.from_dict({"name": "vm", "os": "ubuntu", "disk_gb": 40})
        with contextlib.redirect_stdout(io.StringIO()):
            Tart().deploy([vm])
        calls = self.calls()
        self.assertEqual([item["args"] for item in calls], [["list", "--source", "local", "--format", "json"], *vm.commands()])
        self.assertTrue(all(item["no_prune"] == "1" for item in calls))

    def test_existing_destination_prevents_all_mutations(self):
        os.environ["FAKE_TART_INVENTORY"] = '[{"Name":"existing","Running":false}]'
        vms = [VM.from_dict({"name": name, "os": "ubuntu"}) for name in ("new", "existing")]
        with self.assertRaisesRegex(DeploymentError, "already exist"):
            Tart().deploy(vms)
        self.assertEqual(len(self.calls()), 1)

    def test_missing_or_running_template_prevents_all_mutations(self):
        for inventory in ("[]", '[{"Name":"kali-template","Running":true}]'):
            os.environ["FAKE_TART_INVENTORY"] = inventory
            self.log.unlink(missing_ok=True)
            vms = [VM.from_dict({"name": "ubuntu", "os": "ubuntu"}), VM.from_dict({"name": "kali", "os": "kali", "image": "kali-template"})]
            with self.assertRaises(DeploymentError):
                Tart().deploy(vms)
            self.assertEqual(len(self.calls()), 1)

    def test_failure_stops_batch_and_preserves_vm(self):
        os.environ["FAKE_TART_FAIL"] = "set"
        vms = [VM.from_dict({"name": name, "os": "ubuntu"}) for name in ("one", "two")]
        with self.assertRaisesRegex(DeploymentError, "partially created VM is preserved"):
            Tart().deploy(vms)
        self.assertEqual([item["args"][0] for item in self.calls()], ["list", "clone", "set"])

    def test_malformed_inventory_fails_cleanly(self):
        for inventory in ("not JSON", "{}", '[{"Name":"vm"}]'):
            os.environ["FAKE_TART_INVENTORY"] = inventory
            with self.subTest(inventory=inventory), self.assertRaises(DeploymentError):
                Tart().inventory()


if __name__ == "__main__":
    unittest.main()
