"""Run the dashboard under the current macOS login session's launchd."""

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
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

    def _launchctl(self, *args):
        if platform.system() != "Darwin":
            raise DeploymentError("Background service requires macOS. Use ui --foreground on other hosts.")
        try:
            return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as error:
            raise DeploymentError(f"macOS service manager timed out during {args[0]}. Try service status.") from error

    def _pid(self):
        result = self._launchctl("print", self.target)
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        return int(match[1]) if result.returncode == 0 and match else None

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
                            probe.bind(("127.0.0.1", port))
                    except OSError as error:
                        if error.errno == errno.EADDRINUSE:
                            raise DeploymentError(f"Dashboard port {port} is already in use. Reopen the running dashboard using its data directory, stop it first, or choose another --port.") from error
                        raise
                self._launchctl("bootout", self.target)
                self.ready_file.unlink(missing_ok=True)
                # Retain the virtualenv executable path rather than resolving its symlink.
                python = str(Path(sys.executable).absolute())
                source = str(Path(__file__).resolve().parent.parent)
                environment = {key: os.environ[key] for key in
                               ("PATH", "HOME", "TMPDIR", "TART_HOME", "SSH_AUTH_SOCK", "LANG", "LC_ALL")
                               if key in os.environ}
                environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source, os.environ.get("PYTHONPATH"))))
                definition = {
                    "Label": self.label,
                    "ProgramArguments": [python, "-m", "appletart", "ui", "--foreground", "--no-browser",
                                         "--data-dir", str(self.root), "--port", str(port),
                                         "--ready-file", str(self.ready_file)],
                    "WorkingDirectory": source,
                    "EnvironmentVariables": environment,
                    "RunAtLoad": True,
                    "ExitTimeOut": 50,
                    "StandardOutPath": str(self.log_file),
                    "StandardErrorPath": str(self.log_file),
                }
                plist = self.directory / "dashboard.plist"
                plist.write_bytes(plistlib.dumps(definition))
                plist.chmod(0o600)
                self.log_file.touch(mode=0o600)
                log_offset = self.log_file.stat().st_size
                result = self._launchctl("bootstrap", self.domain, str(plist))
                if result.returncode:
                    raise DeploymentError(f"Cannot start dashboard service: {result.stderr.strip()}")
            else:
                log_offset = self.log_file.stat().st_size if self.log_file.exists() else 0
            deadline = time.monotonic() + 20
            while True:
                state = self.status()
                if state["url"]:
                    break
                if not state["running"]:
                    result = self._launchctl("print", self.target)
                    exited = re.search(r"^\s*last exit code = (-?\d+)\s*$", result.stdout, re.MULTILINE)
                    if exited:
                        self._launchctl("bootout", self.target)
                        detail = ""
                        try:
                            with self.log_file.open("rb") as log:
                                log.seek(log_offset)
                                detail = log.read(4096).decode("utf-8", errors="replace").strip()
                        except OSError:
                            pass
                        raise DeploymentError(f"Dashboard exited during startup (exit {exited[1]}). {detail or f'Inspect {self.log_file}'}")
                if time.monotonic() >= deadline:
                    self._launchctl("bootout", self.target)
                    raise DeploymentError(f"Dashboard did not become ready. Inspect {self.log_file}")
                time.sleep(0.2)
        print(f"AppleTart dashboard: {state['url']} (background service, PID {state['pid']})")
        print(f"Service log: {self.log_file}")
        if open_browser:
            webbrowser.open(state["url"])
        return state

    def stop(self):
        with self._control():
            if self._pid():
                result = self._launchctl("kill", "SIGINT", self.target)
                if result.returncode:
                    raise DeploymentError(f"Cannot stop dashboard service: {result.stderr.strip()}")
                deadline = time.monotonic() + 50
                while self._pid():
                    if time.monotonic() >= deadline:
                        raise DeploymentError("Dashboard is still cleaning up. Check service status and try again.")
                    time.sleep(0.2)
            self._launchctl("bootout", self.target)
            self.ready_file.unlink(missing_ok=True)
        print("Dashboard service stopped. Running VMs remain managed by Tart.")
