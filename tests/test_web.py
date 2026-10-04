import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import ANY, Mock, patch

from appletart.deployment import DeploymentError
from appletart.catalog import Machine
from appletart.web import AppServer, Jobs
from appletart.operations import pause
from test_ssh import PUBLIC_KEY


class WebTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = Mock()
        self.lifecycle.listing.return_value = {"machines": [], "errors": []}
        self.lifecycle._preflight.return_value = []
        self.server = AppServer(("127.0.0.1", 0), self.lifecycle)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        request_headers = {"X-Appletart-Token": self.server.token}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection.request(method, path, json.dumps(data) if data is not None else None, request_headers)
        response = connection.getresponse()
        result = (response.status, dict(response.headers), response.read())
        connection.close()
        return result

    def test_dashboard_assets_and_authenticated_state(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/app.js")[0], 200)
        for icon in ("appletart", "ubuntu", "kali", "start", "shutdown", "restart", "force-stop", "configure", "golden", "agent", "destroy", "ssh", "details", "log", "build", "finish", "cancel", "copy"):
            status, headers, body = self.request("GET", f"/icons/{icon}.svg")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "image/svg+xml")
            self.assertIn(b"<svg", body)
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["machines"], [])
        self.assertEqual(self.request("GET", "/icons/../settings.json")[0], 404)

    def test_wrong_token_origin_and_host_cannot_mutate(self):
        for headers in ({"X-Appletart-Token": "wrong"}, {"Origin": "https://example.com"}, {"Host": "attacker.example"}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/action", {"action": "destroy", "name": "vm"}, headers)
                self.assertEqual(status, 403)
        self.lifecycle.destroy.assert_not_called()

    def test_preview_validates_source_and_resources(self):
        status, _, body = self.request("POST", "/api/preview", {"config": {"name": "vm", "size": "medium"}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["config"]["memory_mb"], 8192)
        status, _, _ = self.request("POST", "/api/preview", {"config": {"name": "vm", "source_kind": "iso", "source": "http://example.com/arm64.iso"}})
        self.assertEqual(status, 400)

    def test_preview_accepts_remote_and_local_cloud_images_without_a_hash(self):
        with tempfile.TemporaryDirectory(prefix="local cloud ") as directory:
            local = Path(directory) / "debian-arm64.qcow2"
            local.write_bytes(b"cloud disk")
            for source in (str(local), "https://example.com/debian-arm64.qcow2"):
                with self.subTest(source=source):
                    status, _, body = self.request("POST", "/api/preview", {"config": {
                        "name": "debian", "os": "other", "source_kind": "cloud", "source": source,
                        "ssh_public_keys": ["~/.ssh/user.pub"], "sha256": ""}})
                    self.assertEqual(status, 200)
                    result = json.loads(body)
                    self.assertFalse(result["installation_required"])
                    self.assertEqual(result["config"]["source"], source if source.startswith("https://") else str(local.resolve()))
                    self.assertEqual(result["config"]["sha256"], "")
            status, _, body = self.request("POST", "/api/preview", {"config": {
                "name": "kali", "os": "kali", "ssh_public_keys": ["~/.ssh/user.pub"], "sha256": ""}})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["config"]["sha256"], "")

    def test_address_lookup_returns_an_address_without_creating_a_job(self):
        self.lifecycle.ip.return_value = "172.20.10.11"
        status, _, body = self.request("POST", "/api/ip", {"name": "bridge"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ip": "172.20.10.11"})
        self.assertEqual(self.server.jobs.snapshot(), [])

    def test_saved_logs_and_full_download_are_authenticated_and_survive_new_jobs_manager(self):
        from appletart.lifecycle import Lifecycle
        from appletart import diagnostics
        with tempfile.TemporaryDirectory() as directory:
            api = Lifecycle(Path(directory))
            def download(machine, report):
                report('FIRST RETAINED MESSAGE')
                report('x' * 5000 + 'FULL ERROR END')
                for n in range(150):
                    report(f'progress {n}')
            api.download = download
            self.server.lifecycle = api
            self.server.jobs = Jobs(api)
            status, _, body = self.request('POST', '/api/action', {'action': 'download', 'config': {'name': 'logs'}})
            self.assertEqual(status, 202)
            identifier = json.loads(body)['id']
            deadline = time.monotonic() + 2
            while self.server.jobs.snapshot()[0]['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.01)
            self.server.jobs.shutdown()
            self.server.jobs = Jobs(api)
            data = {'name': 'logs', 'source': identifier}
            self.assertEqual(self.request('POST', '/api/log', data, {'X-Appletart-Token': 'wrong'})[0], 403)
            status, _, body = self.request('POST', '/api/log', data)
            self.assertEqual(status, 200)
            preview = json.loads(body)
            self.assertIn('FIRST RETAINED MESSAGE', preview['log'])
            self.assertEqual(preview['sources'][0]['status'], 'complete')
            self.assertEqual(self.request('POST', '/api/log/download', data, {'X-Appletart-Token': 'wrong'})[0], 403)
            status, headers, body = self.request('POST', '/api/log/download', data)
            self.assertEqual(status, 200)
            self.assertIn('attachment', headers['Content-Disposition'])
            self.assertEqual(body, diagnostics.source_path(api.store.root, 'logs', identifier).read_bytes())
            self.assertIn(b'FULL ERROR END', body)
            self.assertEqual(self.request('POST', '/api/log/download', {'name': 'logs', 'source': '../machines/logs.json'})[0], 400)
            self.assertEqual(self.request('POST', '/api/log', {**data, 'after': -1})[0], 400)

    def test_management_endpoints_require_session_token_and_return_structured_data(self):
        self.lifecycle.health.return_value = {"status": "ssh-ready", "ip": "192.0.2.10"}
        self.lifecycle.connection.return_value = {"command": "ssh admin@192.0.2.10"}
        self.lifecycle.checkpoints.return_value = []
        self.lifecycle.storage_listing.return_value = {"items": []}
        for method, path, data in (("POST", "/api/health", {"name": "vm"}), ("POST", "/api/connection", {"name": "vm"}), ("POST", "/api/checkpoints", {"name": "vm"}), ("GET", "/api/storage", None)):
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, data, {"X-Appletart-Token": "wrong"})[0], 403)
                self.assertEqual(self.request(method, path, data)[0], 200)
        self.assertEqual(self.request("GET", "/management.js")[0], 200)

    def test_sha512_is_accepted_by_preview(self):
        status, _, body = self.request("POST", "/api/preview", {"config": {"name": "debian", "os": "other", "source_kind": "iso", "source": "https://example.com/debian-arm64.iso", "sha512": "a" * 128}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["config"]["sha512"], "a" * 128)

    def test_user_preview_requires_token_validates_yaml_and_does_not_mutate_guests(self):
        data = {"yaml": f'version: 1\nusers:\n  - username: jay.morris\n    ssh_authorized_keys: ["{PUBLIC_KEY}"]\n'}
        self.assertEqual(self.request("POST", "/api/users/preview", data, {"X-Appletart-Token": "wrong"})[0], 403)
        status, _, body = self.request("POST", "/api/users/preview", data)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["users"][0]["username"], "jay.morris")
        self.assertEqual(result["summary"][0]["key_count"], 1)
        self.lifecycle.add_users.assert_not_called()
        self.assertEqual(self.request("POST", "/api/users/preview", {"users": [{"username": "root", "ssh_authorized_keys": [PUBLIC_KEY]}]})[0], 400)

    def test_user_setup_is_a_background_vm_job_and_invalid_keys_cannot_enqueue(self):
        entry = {"username": "developer", "ssh_authorized_keys": [PUBLIC_KEY]}
        status, _, _ = self.request("POST", "/api/action", {"action": "users", "name": "vm", "users": [entry]})
        self.assertEqual(status, 202)
        self.server.jobs.shutdown()
        self.lifecycle.add_users.assert_called_once_with("vm", [entry], ANY)
        count = len(self.server.jobs.snapshot())
        entry["ssh_authorized_keys"] = ["ssh-ed25519 invalid"]
        self.assertEqual(self.request("POST", "/api/action", {"action": "users", "name": "vm", "users": [entry]})[0], 400)
        self.assertEqual(len(self.server.jobs.snapshot()), count)

    def test_user_template_script_and_icon_are_served(self):
        status, headers, body = self.request("GET", "/templates/users.yaml")
        self.assertEqual(status, 200)
        self.assertIn("application/yaml", headers["Content-Type"])
        self.assertIn(b"passwordless sudo for all commands", body)
        self.assertIn(b"ssh_authorized_keys:", body)
        for path in ("/users.js", "/icons/users.svg"):
            self.assertEqual(self.request("GET", path)[0], 200)

    def test_native_image_picker_requires_the_session_token(self):
        with patch("appletart.web.choose_image", return_value={"cancelled": False, "path": "/tmp/local-arm64.qcow2"}) as picker:
            self.assertEqual(self.request("POST", "/api/image/browse", {"source_kind": "cloud"}, {"X-Appletart-Token": "wrong"})[0], 403)
            picker.assert_not_called()
            status, _, body = self.request("POST", "/api/image/browse", {"source_kind": "cloud"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["path"], "/tmp/local-arm64.qcow2")
            picker.assert_called_once_with("cloud", picker_id=None)

    def test_native_picker_cancellation_is_authenticated_and_scoped_to_the_request(self):
        with patch("appletart.web.cancel_picker", return_value=True) as cancel:
            self.assertEqual(self.request("POST", "/api/picker/cancel", {"picker_id": "request"}, {"X-Appletart-Token": "wrong"})[0], 403)
            cancel.assert_not_called()
            status, _, body = self.request("POST", "/api/picker/cancel", {"picker_id": "request"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"cancelled": True})
            cancel.assert_called_once_with("request")

    def test_ssh_config_write_requires_token_and_uses_only_the_saved_vm_name(self):
        self.lifecycle.save_ssh_config.return_value = {"host": "vm", "command": "ssh vm", "changed": True}
        self.assertEqual(self.request("POST", "/api/ssh-config", {"name": "vm"}, {"X-Appletart-Token": "wrong"})[0], 403)
        self.lifecycle.save_ssh_config.assert_not_called()
        status, _, body = self.request("POST", "/api/ssh-config", {"name": "vm"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["command"], "ssh vm")
        self.lifecycle.save_ssh_config.assert_called_once_with("vm")
        self.assertEqual(self.request("GET", "/icons/ssh-config.svg")[0], 200)

    def test_native_directory_picker_requires_the_session_token(self):
        with patch("appletart.web.choose_directory", return_value={"cancelled": False, "path": "/tmp/shared folder"}) as picker:
            self.assertEqual(self.request("POST", "/api/directory/browse", {}, {"X-Appletart-Token": "wrong"})[0], 403)
            picker.assert_not_called()
            status, _, body = self.request("POST", "/api/directory/browse", {})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["path"], "/tmp/shared folder")
            picker.assert_called_once_with(picker_id=None)

    def test_terminal_settings_require_token_and_preserve_selected_terminal(self):
        self.lifecycle.settings.return_value = {"terminal": "iterm2", "terminals": []}
        self.lifecycle.save_settings.return_value = {"terminal": "iterm2", "terminals": []}
        for method, data in (("GET", None), ("POST", {"terminal": "iterm2"})):
            self.assertEqual(self.request(method, "/api/settings", data, {"X-Appletart-Token": "wrong"})[0], 403)
            status, _, body = self.request(method, "/api/settings", data)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["terminal"], "iterm2")
        self.lifecycle.save_settings.assert_called_once_with({"terminal": "iterm2"})

    def test_kali_preview_uses_cloud_init_without_manual_installation(self):
        self.lifecycle._preflight.return_value = ["ssh-ed25519 public"]
        status, _, body = self.request("POST", "/api/preview", {"config": {
            "name": "kali-dev", "os": "kali", "ssh_public_keys": ["~/.ssh/key.pub"], "packages": ["git"]}})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(result["installation_required"])
        self.assertEqual(result["config"]["source_kind"], "cloud")
        self.assertEqual(result["config"]["hostname"], "kali-dev")
        self.assertEqual(result["key_count"], 1)


class JobTests(unittest.TestCase):
    def test_macos_profile_deploy_defaults_to_graphics(self):
        lifecycle = Mock()
        machine = Machine.from_dict({"name": "mac-vm", "os": "macos"})
        lifecycle.profile_deployment.return_value = machine
        jobs = Jobs(lifecycle)
        jobs.submit("deploy-profile", {"profile": "mac-recipe", "name": "mac-vm"})
        deadline = time.monotonic() + 2
        while jobs.snapshot()[-1]["status"] in {"running", "cancelling"} and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(jobs.snapshot()[-1]["status"], "complete")
        lifecycle.start.assert_called_once_with("mac-vm", ANY, headless=False)

    def test_profile_deploy_uses_server_saved_settings_and_builds_then_starts(self):
        lifecycle = Mock()
        machine = Machine.from_dict({"name": "new-vm", "size": "medium"})
        lifecycle.profile_deployment.return_value = machine
        jobs = Jobs(lifecycle)
        jobs.submit("deploy-profile", {"profile": "saved-recipe", "name": "new-vm"})
        deadline = time.monotonic() + 2
        while jobs.snapshot()[-1]["status"] in {"running", "cancelling"} and time.monotonic() < deadline:
            time.sleep(0.01)
        lifecycle.profile_deployment.assert_called_once_with("saved-recipe", "new-vm")
        lifecycle.build.assert_called_once_with(machine, ANY, "")
        lifecycle.start.assert_called_once_with("new-vm", ANY, headless=True)
        self.assertEqual(jobs.snapshot()[-1]["status"], "complete")
        self.assertEqual(jobs.snapshot()[-1]["resources"], ["vm:new-vm"])

    def test_golden_image_delete_dispatches_and_conflicts_with_image_build(self):
        entered, release = threading.Event(), threading.Event()
        lifecycle = Mock()
        def build(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
        lifecycle.create_golden.side_effect = build
        jobs = Jobs(lifecycle)
        jobs.submit("golden", {"name": "source", "image_name": "golden"})
        self.assertTrue(entered.wait(timeout=1))
        try:
            with self.assertRaisesRegex(DeploymentError, "already running"):
                jobs.submit("delete-image", {"name": "golden", "confirmation": "golden"})
        finally:
            release.set()
            jobs.shutdown()
        jobs.submit("delete-image", {"name": "golden", "confirmation": "golden"})
        jobs.shutdown()
        lifecycle.delete_image.assert_called_once_with("golden", "golden", ANY)
        self.assertEqual(jobs.snapshot()[-1]["status"], "complete")

    def test_conflicting_jobs_are_rejected_and_secrets_are_not_retained(self):
        entered = threading.Event()
        release = threading.Event()
        lifecycle = Mock()
        def build(machine, report, password):
            entered.set()
            release.wait(timeout=3)
            report("password was " + password)
        lifecycle.build.side_effect = build
        jobs = Jobs(lifecycle)
        data = {"config": {"name": "vm"}, "password": "a-secret-password"}
        jobs.submit("build", data)
        self.assertTrue(entered.wait(timeout=2))
        try:
            with self.assertRaisesRegex(DeploymentError, "already running"):
                jobs.submit("start", {"name": "vm"})
        finally:
            release.set()
        deadline = time.monotonic() + 3
        while jobs.snapshot()[0]["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(jobs.snapshot()[0]["status"], "complete")
        self.assertNotIn("a-secret-password", json.dumps(jobs.snapshot()))
        self.assertNotIn("password", data)

    def test_guest_and_additional_passwords_are_redacted_and_released(self):
        lifecycle = Mock()
        def build(machine, report, password, **credentials):
            report(credentials["guest_password"] + " " + credentials["user_passwords"]["analyst"])
        lifecycle.build.side_effect = build
        jobs = Jobs(lifecycle)
        data = {"config": {"name": "password-build", "password_login": True,
                          "users": [{"username": "analyst", "ssh_authorized_keys": [], "password_login": True}]},
                "guest_password": "primary secret", "user_passwords": {"analyst": "additional secret"}}
        jobs.submit("build", data)
        deadline = time.monotonic() + 3
        while jobs.snapshot()[0]["status"] == "running" and time.monotonic() < deadline: time.sleep(0.01)
        self.assertEqual(jobs.snapshot()[0]["status"], "complete")
        self.assertNotIn("primary secret", json.dumps(jobs.snapshot()))
        self.assertNotIn("additional secret", json.dumps(jobs.snapshot()))
        self.assertNotIn("guest_password", data)
        self.assertNotIn("user_passwords", data)

    def test_different_vms_build_concurrently(self):
        entered = {name: threading.Event() for name in ("one", "two")}
        release = threading.Event()
        lifecycle = Mock()
        def build(machine, report, password):
            entered[machine.vm.name].set()
            release.wait(timeout=3)
        lifecycle.build.side_effect = build
        jobs = Jobs(lifecycle)
        try:
            jobs.submit("build", {"config": {"name": "one"}})
            self.assertTrue(entered["one"].wait(timeout=1))
            jobs.submit("build", {"config": {"name": "two"}})
            self.assertTrue(entered["two"].wait(timeout=1))
            self.assertEqual(sum(job["status"] == "running" for job in jobs.snapshot()), 2)
        finally:
            release.set()

    def test_long_build_can_be_cancelled_without_waiting_for_its_timeout(self):
        entered = threading.Event()
        lifecycle = Mock()
        def build(machine, report, password):
            entered.set()
            pause(1800)
        lifecycle.build.side_effect = build
        jobs = Jobs(lifecycle)
        result = jobs.submit("build", {"config": {"name": "vm"}})
        self.assertTrue(entered.wait(timeout=1))
        jobs.cancel(result["id"])
        deadline = time.monotonic() + 2
        while jobs.snapshot()[0]["status"] in {"running", "cancelling"} and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(jobs.snapshot()[0]["status"], "cancelled")
        self.assertEqual(jobs.submit("start", {"name": "vm"}).keys(), {"id"})


if __name__ == "__main__":
    unittest.main()
