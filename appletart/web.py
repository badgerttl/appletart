"""Loopback-only dashboard with cancellable background lifecycle jobs."""

import fcntl
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
from pathlib import Path
import secrets
import threading
import time
import traceback
import webbrowser

from .catalog import Machine, choices
from .deployment import DeploymentError
from .lifecycle import Lifecycle
from .operations import Cancellation, JobCancelled, checkpoint, scope, uncancellable
from .host_network import network_interfaces, listen_addresses
from .image_picker import cancel_picker, choose_directory, choose_image
from . import diagnostics
from . import users, bundles, image_catalog


class Jobs:
    def __init__(self, lifecycle: Lifecycle):
        self.lifecycle = lifecycle
        self.lock = threading.RLock()
        self.jobs = []
        self.cancellations = {}
        self.workers = {}

    def shutdown(self):
        with self.lock:
            for job in self.jobs:
                if job["status"] in {"running", "cancelling"} and job["cancellable"]:
                    job["status"] = "cancelling"
                    self.cancellations[job["id"]].cancel()
            workers = list(self.workers.values())
        deadline = time.monotonic() + 40
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))

    def cancel(self, identifier):
        with self.lock:
            job = next((job for job in self.jobs if job["id"] == identifier), None)
            if not job:
                raise DeploymentError("That operation was not found. Refresh Activity.")
            if job["status"] not in {"running", "cancelling"}:
                raise DeploymentError("That operation has already finished.")
            if not job["cancellable"]:
                raise DeploymentError("This short operation must finish before another action on this VM.")
            job["status"] = "cancelling"
            self.cancellations[identifier].cancel()
            return {"id": identifier, "status": "cancelling"}

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.jobs))

    def submit(self, action: str, data: dict):
        if action not in {"download", "build", "create", "deploy-profile", "start", "stop", "shutdown", "restart", "force-stop", "finish", "configure", "destroy", "golden", "delete-image", "agent", "checkpoint", "restore", "delete-checkpoint", "cleanup", "diagnostics", "users"}:
            raise DeploymentError("Unknown lifecycle action.")
        if action == "users":
            data["users"] = users.validate(data.get("users"))
        if action in {"download", "build", "create", "configure"}:
            machine = self.lifecycle.machine(data.get("config")) if isinstance(self.lifecycle, Lifecycle) else Machine.from_dict(data.get("config"))
        elif action == "deploy-profile":
            machine = self.lifecycle.profile_deployment(data.get("profile"), data.get("name"))
        else:
            machine = None
        name = machine.vm.name if machine else "storage" if action == "cleanup" else data.get("name")
        from .deployment import vm_name
        vm_name(name)
        resources = {"storage:cleanup"} if action == "cleanup" else {("image:" if action == "delete-image" else "vm:") + name}
        if action == "golden":
            resources.add("image:" + vm_name(data.get("image_name")))
        if action in {"restore", "delete-checkpoint"}:
            resources.add("checkpoint:" + vm_name(data.get("checkpoint_id")))
        password = data.get("password", "")
        if not isinstance(password, str) or len(password) > 1024 or "\0" in password:
            raise DeploymentError("Invalid bootstrap password.")
        guest_password = users.password(data.get("guest_password", ""))
        user_passwords = data.get("user_passwords", {})
        if not isinstance(user_passwords, dict):
            raise DeploymentError("User passwords must be supplied by username.")
        for secret in user_passwords.values():
            users.password(secret)
        credential_machine = machine
        if action == "finish" and isinstance(self.lifecycle, Lifecycle):
            _, credential_machine = self.lifecycle._managed(name)
        if credential_machine and action in {"build", "create", "deploy-profile", "finish"} and credential_machine.source_kind != "iso":
            users.provisioning(credential_machine, guest_password, user_passwords)
        elif credential_machine and action == "finish":
            users.provisioning(credential_machine, guest_password, user_passwords)
        sensitive = tuple(value for value in [password, guest_password, *user_passwords.values(),
                    *[entry.get("password", "") for entry in (data["users"] if action == "users" else [])]] if value)
        if type(data.get("headless", False)) is not bool:
            raise DeploymentError("headless must be true or false.")
        with self.lock:
            if any(job["status"] in {"running", "cancelling"} and resources.intersection(job["resources"]) for job in self.jobs):
                raise DeploymentError("An operation is already running for this VM or image. Cancel it or wait for it to finish.")
            job = {"id": secrets.token_hex(8), "action": action, "name": name,
                   "status": "running", "lines": [], "started_at": time.time(), "resources": sorted(resources),
                   "cancellable": action in {"download", "build", "create", "deploy-profile", "finish", "golden", "agent", "diagnostics"}}
            saved = self.lifecycle.store.get(name) if isinstance(self.lifecycle, Lifecycle) and not machine else None
            journal = diagnostics.Journal(self.lifecycle.store.root, name, job["id"], action,
                                          machine.config() if machine else saved.get("config") if saved else None, secrets=sensitive) if isinstance(self.lifecycle, Lifecycle) else None
            completed = [item for item in self.jobs if item["status"] not in {"running", "cancelling"}][-11:]
            self.jobs = [item for item in self.jobs if item["status"] in {"running", "cancelling"}] + completed + [job]
            cancellation = Cancellation()
            self.cancellations[job["id"]] = cancellation
        def report(message):
            with self.lock:
                # A bootstrap secret must never enter retained logs.
                text = str(message)
                for secret in sorted(sensitive, key=len, reverse=True):
                    text = text.replace(secret, "[redacted]")
                if journal:
                    journal.write("progress", text)
                job["lines"] = (job["lines"] + [text[:2000]])[-100:]
        def work():
            try:
                lock = self.lifecycle.operation(*[item.replace(":", "-", 1) for item in resources]) if isinstance(self.lifecycle, Lifecycle) and action not in {"cleanup", "delete-image"} else nullcontext()
                with journal.scope() if journal else nullcontext(), scope(cancellation if job["cancellable"] else None), lock:
                    api = self.lifecycle
                    if action == "download":
                        api.download(machine, report)
                    elif action in {"build", "create", "deploy-profile"}:
                        api.build(machine, report, password, **({"guest_password": guest_password, "user_passwords": user_passwords} if guest_password or user_passwords else {}))
                        checkpoint()
                        if action in {"create", "deploy-profile"}:
                            api.start(name, report, headless=data.get("headless", machine.guest_family != "macos"))
                            if cancellation.requested.is_set():
                                with uncancellable():
                                    api.stop(name, report)
                                checkpoint()
                    elif action == "start":
                        if "headless" in data:
                            api.start(name, report, headless=data["headless"])
                        else:
                            api.start(name, report)
                    elif action == "stop":
                        api.stop(name, report)
                    elif action == "shutdown":
                        api.shutdown(name, report)
                    elif action == "restart":
                        api.restart(name, report)
                    elif action == "force-stop":
                        api.force_stop(name, report)
                    elif action == "finish":
                        api.finish_installation(name, report, password, **({"guest_password": guest_password, "user_passwords": user_passwords} if guest_password or user_passwords else {}))
                    elif action == "configure":
                        api.configure(name, machine.config(), report)
                    elif action == "destroy":
                        api.destroy(name, data.get("confirmation", ""), report)
                    elif action == "golden":
                        api.create_golden(name, data.get("image_name"), report, version=data.get("version", "1"), notes=data.get("notes", ""))
                    elif action == "delete-image":
                        api.delete_image(name, data.get("confirmation", ""), report)
                    elif action == "agent":
                        api.install_guest_agent(name, report, **({"password": password} if password else {}))
                    elif action == "diagnostics":
                        api.collect_logs(name, report)
                    elif action == "users":
                        api.add_users(name, data["users"], report)
                    elif action == "checkpoint":
                        api.create_checkpoint(name, data.get("label"), data.get("notes", ""), report)
                    elif action == "restore":
                        api.restore_checkpoint(name, data.get("checkpoint_id"), data.get("confirmation"), report)
                    elif action == "delete-checkpoint":
                        api.delete_checkpoint(name, data.get("checkpoint_id"), data.get("confirmation"), report)
                    elif action == "cleanup":
                        api.cleanup_storage(data.get("items"), data.get("confirmation"), report, **({"cache_only": data["cache_only"]} if "cache_only" in data else {}))
                with self.lock:
                    job["status"] = "complete"
            except JobCancelled as error:
                report(str(error))
                if action in {"build", "create", "deploy-profile", "finish"}:
                    report("The VM disk is preserved. Resume build if setup is incomplete.")
                with self.lock:
                    job["status"] = "cancelled"
            except Exception as error:
                report(str(error))
                if journal:
                    journal.write("exception", traceback.format_exc())
                with self.lock:
                    job["status"] = "failed"
            finally:
                data.pop("password", None)
                data.pop("guest_password", None)
                data.pop("user_passwords", None)
                data.pop("users", None)
                if journal:
                    try:
                        journal.finish(job["status"])
                    except OSError as error:
                        with self.lock:
                            job["lines"].append(f"Could not finish saving diagnostics: {error}")
                with self.lock:
                    self.cancellations.pop(job["id"], None)
                    self.workers.pop(job["id"], None)
        worker = threading.Thread(target=work)
        with self.lock:
            self.workers[job["id"]] = worker
            worker.start()
        return {"id": job["id"]}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, lifecycle):
        self.lifecycle = lifecycle
        self.jobs = Jobs(lifecycle)
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def respond(self, status, body, content_type="application/json"):
        encoded = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(encoded)

    def authorized(self, api=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            self.respond(403, {"error": "Invalid local host."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{expected}":
            self.respond(403, {"error": "Cross-origin requests are not allowed."})
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Appletart-Token", ""), self.server.token):
            self.respond(403, {"error": "Invalid session token. Reload the dashboard."})
            return False
        return True

    def do_GET(self):
        if not self.authorized(self.path.startswith("/api/")):
            return
        try:
            if self.path == "/":
                page = (Path(__file__).parent / "static" / "index.html").read_bytes()
                self.respond(200, page.replace(b"SESSION_TOKEN", self.server.token.encode()), "text/html; charset=utf-8")
            elif self.path in ("/app.js", "/management.js", "/logs.js", "/users.js", "/software.js", "/style.css"):
                mime = "text/javascript" if self.path.endswith(".js") else "text/css"
                self.respond(200, (Path(__file__).parent / "static" / self.path[1:]).read_bytes(), mime)
            elif self.path == "/templates/users.yaml":
                self.respond(200, (Path(__file__).parent / "static" / "users-template.yaml").read_bytes(), "application/yaml; charset=utf-8")
            elif self.path in tuple(f"/icons/{name}.svg" for name in ("appletart", "ubuntu", "kali", "rhel", "fedora", "debian", "rocky", "macos", "other", "start", "shutdown", "restart", "force-stop", "configure", "golden", "agent", "destroy", "ssh", "ssh-config", "details", "log", "build", "finish", "cancel", "copy", "download", "users")):
                self.respond(200, (Path(__file__).parent / "static" / self.path[1:]).read_bytes(), "image/svg+xml")
            elif self.path == "/api/state":
                self.respond(200, {**self.server.lifecycle.listing(), "jobs": self.server.jobs.snapshot()})
            elif self.path == "/api/choices":
                self.respond(200, {**choices(self.server.lifecycle.store.root if isinstance(self.server.lifecycle, Lifecycle) else None), "interfaces": network_interfaces(), "listen_addresses": listen_addresses()})
            elif self.path == "/api/storage":
                self.respond(200, self.server.lifecycle.storage_listing())
            elif self.path == "/api/settings":
                self.respond(200, self.server.lifecycle.settings())
            elif self.path == "/api/bundles":
                self.respond(200, {"bundles": bundles.listing(self.server.lifecycle.store.root)})
            elif self.path == "/api/catalog":
                self.respond(200, {"images": image_catalog.load(self.server.lifecycle.store.root)})
            else:
                self.respond(404, {"error": "Not found."})
        except (DeploymentError, OSError, ValueError) as error:
            self.respond(400, {"error": str(error)})

    def do_POST(self):
        if not self.authorized(api=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = 512 * 1024 if self.path == "/api/catalog" else 65536
            if not 0 < length <= limit or self.headers.get("Content-Type") != "application/json":
                raise DeploymentError("Expected a JSON request within the endpoint size limit.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise DeploymentError("Expected a JSON object.")
            if self.path == "/api/preview":
                machine = self.server.lifecycle.machine(data.get("config")) if isinstance(self.server.lifecycle, Lifecycle) else Machine.from_dict(data.get("config"))
                keys = self.server.lifecycle._preflight(machine)
                if "guest_password" in data or "user_passwords" in data:
                    users.provisioning(machine, data.get("guest_password", ""), data.get("user_passwords", {}))
                self.respond(200, {"config": machine.config(), "key_count": len(keys), "installation_required": machine.source_kind == "iso"})
            elif self.path == "/api/users/preview":
                self.respond(200, users.preview(data))
            elif self.path == "/api/action":
                self.respond(202, self.server.jobs.submit(data.get("action"), data))
            elif self.path == "/api/cancel":
                self.respond(202, self.server.jobs.cancel(data.get("id")))
            elif self.path == "/api/ip":
                self.respond(200, {"ip": self.server.lifecycle.ip(data.get("name"))})
            elif self.path == "/api/log":
                self.respond(200, self.server.lifecycle.logs(data.get("name"), data.get("source"), data.get("before"), data.get("after")))
            elif self.path == "/api/log/download":
                path = self.server.lifecycle.log_path(data.get("name"), data.get("source"))
                with path.open("rb") as file:
                    size = path.stat().st_size
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", f'attachment; filename="{data["name"]}-{path.name}"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    remaining = size
                    while remaining:
                        chunk = file.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            elif self.path == "/api/health":
                self.respond(200, self.server.lifecycle.health(data.get("name"), details=data.get("details", False)))
            elif self.path == "/api/connection":
                if type(data.get("terminal", False)) is not bool:
                    raise DeploymentError("terminal must be true or false.")
                self.respond(200, self.server.lifecycle.connection(data.get("name"), terminal=data.get("terminal", False)))
            elif self.path == "/api/ssh-config":
                self.respond(200, self.server.lifecycle.save_ssh_config(data.get("name")))
            elif self.path == "/api/settings":
                self.respond(200, self.server.lifecycle.save_settings(data))
            elif self.path == "/api/bundle/save":
                self.respond(200, self.server.lifecycle.save_bundle(data.get("bundle")))
            elif self.path == "/api/bundle/delete":
                self.server.lifecycle.delete_bundle(data.get("id"))
                self.respond(200, {"deleted": True})
            elif self.path == "/api/catalog":
                self.respond(200, {"images": self.server.lifecycle.save_catalog(data.get("images"))})
            elif self.path == "/api/checkpoints":
                self.respond(200, {"checkpoints": self.server.lifecycle.checkpoints(data.get("name"))})
            elif self.path == "/api/profile/save":
                self.respond(200, self.server.lifecycle.save_profile(data.get("name"), data.get("config"), data.get("notes", "")))
            elif self.path == "/api/profile/import":
                self.respond(200, self.server.lifecycle.import_profile(data.get("profile")))
            elif self.path == "/api/profile/delete":
                self.server.lifecycle.delete_profile(data.get("name"), data.get("confirmation"))
                self.respond(200, {"deleted": True})
            elif self.path == "/api/image/update":
                self.server.lifecycle.update_image(data.get("name"), data.get("version"), data.get("notes", ""))
                self.respond(200, {"saved": True})
            elif self.path == "/api/image/browse":
                self.respond(200, choose_image(data.get("source_kind"), picker_id=data.get("picker_id")))
            elif self.path == "/api/directory/browse":
                self.respond(200, choose_directory(picker_id=data.get("picker_id")))
            elif self.path == "/api/picker/cancel":
                self.respond(200, {"cancelled": cancel_picker(data.get("picker_id"))})
            else:
                self.respond(404, {"error": "Not found."})
        except (DeploymentError, ValueError, OSError, TypeError) as error:
            self.respond(400, {"error": str(error)})


def serve(root: Path, port: int = 4991, *, open_browser: bool = True, ready_file: Path | None = None):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "dashboard.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentError("A dashboard is already using this data directory.") from error
        diagnostics.recover_interrupted(root)
        with AppServer(("127.0.0.1", port), Lifecycle(root)) as server:
            url = f"http://127.0.0.1:{server.server_port}"
            print(f"AppleTart dashboard: {url}", flush=True)
            print("Closing the browser leaves the dashboard and VMs running.", flush=True)
            if open_browser:
                webbrowser.open(url)
            def terminate(signum, frame):
                raise KeyboardInterrupt

            previous = signal.signal(signal.SIGTERM, terminate) if threading.current_thread() is threading.main_thread() else None
            try:
                if ready_file is not None:
                    temporary = ready_file.with_suffix(".tmp")
                    temporary.write_text(json.dumps({"pid": os.getpid(), "url": url}))
                    temporary.replace(ready_file)
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                # Ignore additional termination requests while cancellable work cleans up.
                if previous is not None:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                print("Dashboard stopping. Cancelling active builds and waiting for cleanup…", flush=True)
                server.jobs.shutdown()
                if ready_file is not None:
                    ready_file.unlink(missing_ok=True)
                if previous is not None:
                    signal.signal(signal.SIGTERM, previous)
                print("Dashboard stopped. Running VMs remain managed by Tart.")
