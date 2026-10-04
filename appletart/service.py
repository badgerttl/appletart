"""Manage a detached dashboard process that inherits its launcher's network access."""

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import sys
import time
import webbrowser

from .deployment import DeploymentError


class DashboardService:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.directory = self.root / "service"
        digest = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        self.label = f"com.appletart.dashboard.{digest}"
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{self.label}"
        self.ready_file = self.directory / "ready.json"
        self.log_file = self.directory / "dashboard.log"
        self.process_file = self.directory / "process.json"
        self.child = None

    def _launchctl(self, *args):
        if platform.system() != "Darwin":
            raise DeploymentError("Background service requires macOS. Use ui --foreground on other hosts.")
        try:
            return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as error:
            raise DeploymentError(f"macOS service manager timed out during {args[0]}. Try service status.") from error

    def _legacy_pid(self):
        if platform.system() != "Darwin":
            return None
        result = self._launchctl("print", self.target)
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        return int(match[1]) if result.returncode == 0 and match else None

    def _command(self, port):
        return [str(Path(sys.executable).absolute()), "-m", "appletart", "ui", "--foreground", "--no-browser",
                "--data-dir", str(self.root), "--port", str(port), "--ready-file", str(self.ready_file)]

    def _detached_pid(self):
        try:
            record = json.loads(self.process_file.read_text())
            pid, port = record["pid"], record["port"]
            if type(pid) is not int or pid <= 1 or type(port) is not int or not 0 <= port <= 65535:
                return None
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "uid=,stat=,command="],
                                    capture_output=True, text=True, timeout=5)
            fields = result.stdout.strip().split(None, 2)
            command = self._command(port)
            executables = {command[0], str(Path(sys.executable).resolve())}
            # Framework Python replaces its command-line launcher with this interpreter.
            interpreter = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"
            if interpreter.is_file():
                executables.add(str(interpreter.resolve()))
            commands = {" ".join([executable, *command[1:]]) for executable in executables}
            # PID files alone are insufficient: never signal a reused or unrelated PID.
            if (result.returncode == 0 and len(fields) == 3 and fields[0] == str(os.getuid())
                    and not fields[1].startswith("Z") and fields[2] in commands):
                return pid
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            pass
        return None

    def _pid(self):
        if self.child is not None and self.child.poll() is None:
            return self.child.pid
        return self._detached_pid() or self._legacy_pid()

    def _abort_start(self):
        if self.child is not None:
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)
        self.process_file.unlink(missing_ok=True)
        self.ready_file.unlink(missing_ok=True)

    def status(self):
        pid = self._pid()
        try:
            ready = json.loads(self.ready_file.read_text())
        except (OSError, ValueError):
            ready = {}
        if not isinstance(ready, dict):
            ready = {}
        return {"running": pid is not None, "pid": pid,
                "url": ready.get("url") if pid and ready.get("pid") == pid else None,
                "log": str(self.log_file)}

    @contextmanager
    def _control(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "control.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DeploymentError("Another service command is still running for this data directory. Wait for it to finish or interrupt it with Ctrl+C.") from error
            yield

    def start(self, port=4991, *, open_browser=True):
        if not 0 <= port <= 65535:
            raise DeploymentError("Dashboard port must be between 0 and 65535.")
        with self._control():
            state = self.status()
            if not state["running"]:
                if port:
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                            probe.bind(("127.0.0.1", port))
                    except OSError as error:
                        if error.errno == errno.EADDRINUSE:
                            raise DeploymentError(f"Dashboard port {port} is already in use. Reopen the running dashboard using its data directory, stop it first, or choose another --port.") from error
                        raise
                self.ready_file.unlink(missing_ok=True)
                # Retain the virtualenv executable path rather than resolving its symlink.
                source = str(Path(__file__).resolve().parent.parent)
                environment = {key: os.environ[key] for key in
                               ("PATH", "HOME", "TMPDIR", "TART_HOME", "SSH_AUTH_SOCK", "LANG", "LC_ALL")
                               if key in os.environ}
                environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source, os.environ.get("PYTHONPATH"))))
                self.log_file.touch(mode=0o600)
                log_offset = self.log_file.stat().st_size
                try:
                    with self.log_file.open("ab") as log:
                        self.child = subprocess.Popen(self._command(port), cwd=source, env=environment,
                                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                      start_new_session=True)
                    self.process_file.touch(mode=0o600)
                    self.process_file.chmod(0o600)
                    self.process_file.write_text(json.dumps({"pid": self.child.pid, "port": port}))
                except OSError as error:
                    self._abort_start()
                    raise DeploymentError(f"Cannot start dashboard service: {error}") from error
            else:
                log_offset = self.log_file.stat().st_size if self.log_file.exists() else 0
            deadline = time.monotonic() + 20
            while True:
                state = self.status()
                if state["url"]:
                    break
                if self.child is not None:
                    exit_code = self.child.poll()
                    if exit_code is not None:
                        self.process_file.unlink(missing_ok=True)
                        detail = ""
                        try:
                            with self.log_file.open("rb") as log:
                                log.seek(log_offset)
                                detail = log.read(4096).decode("utf-8", errors="replace").strip()
                        except OSError:
                            pass
                        raise DeploymentError(f"Dashboard exited during startup (exit {exit_code}). {detail or f'Inspect {self.log_file}'}")
                if time.monotonic() >= deadline:
                    self._abort_start()
                    raise DeploymentError(f"Dashboard did not become ready. Inspect {self.log_file}")
                time.sleep(0.2)
        print(f"AppleTart dashboard: {state['url']} (background service, PID {state['pid']})")
        print(f"Service log: {self.log_file}")
        if open_browser:
            webbrowser.open(state["url"])
        return state

    def stop(self):
        with self._control():
            pid = self._pid()
            if pid:
                if pid == self._detached_pid() or (self.child is not None and pid == self.child.pid):
                    try:
                        os.kill(pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                else:
                    result = self._launchctl("kill", "SIGINT", self.target)
                    if result.returncode:
                        raise DeploymentError(f"Cannot stop dashboard service: {result.stderr.strip()}")
                deadline = time.monotonic() + 50
                while self._pid():
                    if time.monotonic() >= deadline:
                        raise DeploymentError("Dashboard is still cleaning up. Check service status and try again.")
                    time.sleep(0.2)
            if platform.system() == "Darwin":
                self._launchctl("bootout", self.target)
            self.ready_file.unlink(missing_ok=True)
            self.process_file.unlink(missing_ok=True)
        print("Dashboard service stopped. Running VMs remain managed by Tart.")
