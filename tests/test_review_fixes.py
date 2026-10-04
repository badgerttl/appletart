"""Regressions for template credentials, cloud recovery and bundle references."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from appletart import bundles, cloud, users
from appletart.catalog import Machine
from appletart.deployment import DeploymentError
from appletart.lifecycle import Lifecycle
from appletart.ssh import provision_keys
from test_lifecycle import FakeBackend
from test_ssh import PUBLIC_KEY


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.mac_address = Mock(return_value="02:00:00:00:00:01")
        self.api = Lifecycle(self.root, lambda report=print: self.backend)

    def test_macos_without_keys_or_packages_still_secures_the_template(self):
        machine = self.api.machine({"name": "mac", "os": "macos", "ssh_public_keys": []})
        with patch("appletart.lifecycle.guest_agent.prepare"), patch("appletart.lifecycle.provision_keys") as provision:
            record = self.api.build(machine, Mock())
        provision.assert_called_once()
        self.assertEqual(provision.call_args.kwargs["family"], "macos")
        self.assertEqual(record["phase"], "ready")
        self.assertTrue(record["guest_agent_privileged"])

    def test_macos_password_retirement_runs_after_verification_before_selected_password(self):
        events = []
        machine = self.api.machine({"name": "mac", "os": "macos"})
        with patch("appletart.ssh.subprocess.Popen", return_value=Mock(poll=Mock(return_value=None))), \
             patch("appletart.ssh.wait_for_ssh", return_value="192.0.2.10"), \
             patch("appletart.ssh.ssh_install"), \
             patch("appletart.ssh.stop_build"), \
             patch("appletart.guest_agent.root_available", return_value=False), \
             patch("appletart.guest_agent.verify_management", side_effect=lambda *a: events.append("verify")), \
             patch("appletart.users.retire_template_password", side_effect=lambda *a, **k: events.append("retire")) as retire, \
             patch("appletart.users.install", side_effect=lambda *a, **k: events.append("users")):
            provision_keys(Mock(binary="tart", run=Mock(return_value="192.0.2.10")), machine.vm, [],
                           agent_binary=b"fixture", family="macos", known_hosts=self.root / "known_hosts",
                           users=[{"username": "admin", "password": "selected fixture", "ssh_authorized_keys": []}])
        self.assertEqual(events, ["verify", "retire", "users"])
        self.assertEqual(retire.call_args.kwargs["family"], "macos")

    def test_macos_password_retirement_updates_native_credentials_without_exporting_them(self):
        commands = self.root / "bin"
        commands.mkdir()
        state = self.root / "credentials"
        state.write_text("publisher-default")
        dscl = commands / "dscl"
        dscl.write_text("#!" + os.sys.executable + "\n" +
                        "import os,pathlib,shlex,sys\n"
                        "assert sys.argv[1:] == ['-q', '.']\n"
                        "state=pathlib.Path(os.environ['CREDENTIAL_FIXTURE'])\n"
                        "for line in sys.stdin:\n"
                        "    args=shlex.split(line)\n"
                        "    if args and args[0]=='passwd':\n"
                        "        assert args[1]=='/Users/admin'\n"
                        "        state.write_text(args[2])\n"
                        "    elif args and args[0]=='authonly':\n"
                        "        assert args[1]=='admin' and args[2]==state.read_text()\n")
        dscl.chmod(0o755)
        identity = commands / "id"
        identity.write_text('#!/bin/sh\nif [ "$#" = 1 ]; then echo 0; else echo 501; fi\n')
        identity.chmod(0o755)
        real_run = subprocess.run
        calls = []
        def guest(command, **kwargs):
            calls.append(command)
            return real_run(command, **kwargs, env={**os.environ, "PATH": str(commands) + ":/usr/bin:/bin",
                                                    "CREDENTIAL_FIXTURE": str(state)})
        report = Mock()
        with patch("appletart.users.run", side_effect=guest):
            users.retire_template_password(["sh", "-s"], "admin", report, family="macos")
        replacement = state.read_text()
        self.assertNotEqual(replacement, "publisher-default")
        self.assertGreaterEqual(len(replacement), 64)
        self.assertNotIn(replacement, str(calls) + str(report.call_args_list))

    def test_macos_native_password_error_fails_without_leaking_generated_credential(self):
        commands = self.root / "bin"
        commands.mkdir()
        for name, body in {
            "id": 'if [ "$#" = 1 ]; then echo 0; else echo 501; fi',
            # dscl can return zero while reporting a failed operation.
            "dscl": "cat\necho 'DS Error: eDSAuthFailed'",
        }.items():
            path = commands / name
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o755)
        real_run = subprocess.run
        def guest(command, **kwargs):
            return real_run(command, **kwargs, env={**os.environ, "PATH": str(commands) + ":/usr/bin:/bin"})
        with patch("appletart.users.run", side_effect=guest), \
             patch("appletart.users.secrets.token_hex", return_value="private fixture credential"), \
             self.assertRaisesRegex(DeploymentError, "password setup failed") as error:
            users.retire_template_password(["sh", "-s"], "admin", Mock(), family="macos")
        self.assertNotIn("private fixture credential", str(error.exception))

    def test_final_seed_failure_resumes_through_agent_without_replacing_disk_or_instance(self):
        artifact = self.root / "source.img"
        artifact.write_bytes(b"disk fixture")
        machine = self.api.machine({"name": "cloud", "os": "other", "source_kind": "cloud",
                                    "source": str(artifact), "ssh_public_keys": []})
        directory = self.root / "cloud-init/cloud"
        directory.mkdir(parents=True)
        key = directory / "bootstrap"
        key.write_text("private fixture")
        key.with_suffix(".pub").write_text(PUBLIC_KEY)
        seeds = []
        def seed(machine, keys, directory, instance, mac):
            seeds.append((keys, instance))
            if len(seeds) == 2:
                raise DeploymentError("final seed fixture failure")
            path = directory / "seed.iso"
            path.write_bytes(b"seed fixture")
            return path
        def provision(*args, **kwargs):
            if len(seeds) == 1:
                return {}  # Completed guest setup has already revoked its build key.
            def command(args, **kwargs):
                self.assertNotEqual(args[0], "ssh", "Resume attempted the revoked bootstrap key")
                output = PUBLIC_KEY if "for key_file" in kwargs.get("input", "") else json.dumps({"status": "done", "errors": []})
                return subprocess.CompletedProcess(args, 0, output, "")
            with patch("appletart.cloud.subprocess", Mock(Popen=Mock(return_value=Mock(poll=Mock(return_value=None))), TimeoutExpired=subprocess.TimeoutExpired)), \
                 patch("appletart.cloud.stop_build"), \
                 patch("appletart.cloud.wait_for_ssh", side_effect=AssertionError("Revoked SSH bootstrap used")), \
                 patch("appletart.guest_agent.root_available", return_value=True), \
                 patch("appletart.cloud.run", side_effect=command), \
                 patch("appletart.cloud.read_public_keys", return_value=[]):
                return cloud.provision_cloud(*args, **kwargs)
        with patch("appletart.lifecycle.check_cloud_tools"), \
             patch("appletart.lifecycle.convert_disk", return_value=artifact), \
             patch("appletart.lifecycle.bootstrap_key", return_value=(key, PUBLIC_KEY)), \
             patch("appletart.lifecycle.write_seed", side_effect=seed), \
             patch("appletart.lifecycle.provision_cloud", side_effect=provision):
            with self.assertRaisesRegex(DeploymentError, "final seed fixture failure"):
                self.api.build(machine, Mock())
            failed = self.api.store.get("cloud")
            self.assertEqual(failed["phase"], "created")
            reopened = Lifecycle(self.root, lambda report=print: self.backend)
            completed = reopened.build(machine, Mock())
        self.assertEqual(completed["phase"], "ready")
        self.assertTrue(all(instance == failed["cloud_instance_id"] for _, instance in seeds))
        self.assertEqual(sum(args[0] == "create" for args in self.backend.calls), 1)
        self.assertEqual(sum("--disk" in args for args in self.backend.calls), 1)
        self.assertEqual(sum("--random-mac" in args for args in self.backend.calls), 1)
        self.assertFalse(key.exists())

    def test_referenced_bundle_deletion_preserves_bundle_and_profile_deployability(self):
        bundle = self.api.save_bundle({"id": "tools", "name": "Tools", "platforms": ["linux"], "packages": ["git"]})
        self.api.save_profile("recipe", {"name": "source", "software_bundles": [bundle["id"]]})
        with self.assertRaisesRegex(DeploymentError, "recipe"):
            self.api.delete_bundle(bundle["id"])
        self.assertEqual(bundles.listing(self.root)[-1], bundle)
        self.assertEqual(self.api.profile_deployment("recipe", "new-vm").effective_packages, ("git",))
        self.api.delete_profile("recipe", "recipe")
        self.api.delete_bundle(bundle["id"])
        self.assertNotIn(bundle["id"], [item["id"] for item in bundles.listing(self.root)])

    def test_profile_validation_and_save_lock_out_bundle_deletion(self):
        self.api.save_bundle({"id": "tools", "name": "Tools", "platforms": ["linux"], "packages": ["git"]})
        entered, release = threading.Event(), threading.Event()
        machine = self.api.machine
        def validation(config):
            result = machine(config)
            entered.set()
            self.assertTrue(release.wait(5))
            return result
        with ThreadPoolExecutor(max_workers=1) as workers, patch.object(self.api, "machine", side_effect=validation):
            save = workers.submit(self.api.save_profile, "recipe", {"name": "source", "software_bundles": ["tools"]})
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(DeploymentError, "Another AppleTart operation"):
                    self.api.delete_bundle("tools")
            finally:
                release.set()
            save.result(timeout=5)
        self.assertEqual(self.api.profile_deployment("recipe", "new-vm").effective_packages, ("git",))


if __name__ == "__main__":
    unittest.main()
