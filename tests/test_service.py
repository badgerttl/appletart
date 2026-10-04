import contextlib
import errno
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart.cli import main
from appletart.deployment import DeploymentError
from appletart.service import DashboardService
from appletart.web import serve


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="appletart service ")
        self.addCleanup(self.temp.cleanup)
        self.service = DashboardService(Path(self.temp.name))

    def ready(self, pid=1234, port=4991):
        self.service.directory.mkdir(exist_ok=True)
        self.service.ready_file.write_text(json.dumps({"pid": pid, "url": f"http://127.0.0.1:{port}"}))

    def test_status_does_not_trust_stale_readiness(self):
        self.ready(pid=12)
        with patch.object(self.service, "_pid", return_value=1234):
            self.assertEqual(self.service.status()["url"], None)
        with patch.object(self.service, "_pid", return_value=None):
            self.assertFalse(self.service.status()["running"])

    def test_start_reuses_running_service_without_bootstrap(self):
        self.ready()
        with patch.object(self.service, "_pid", return_value=1234), patch.object(self.service, "_launchctl") as ctl, patch("appletart.service.webbrowser.open") as browser, contextlib.redirect_stdout(io.StringIO()):
            state = self.service.start()
        ctl.assert_not_called()
        browser.assert_called_once_with("http://127.0.0.1:4991")
        self.assertEqual(state["pid"], 1234)

    def test_background_process_inherits_terminal_network_access_without_launchd(self):
        child = Mock(pid=1234)
        child.poll.return_value = None
        def spawn(*args, **kwargs):
            self.ready(port=51234)
            return child
        with patch.object(self.service, "_pid", side_effect=[None, 1234]), \
             patch.object(self.service, "_launchctl") as ctl, \
             patch("appletart.service.subprocess.Popen", side_effect=spawn) as process, \
             contextlib.redirect_stdout(io.StringIO()):
            state = self.service.start(0, open_browser=False)
        self.assertEqual(state["pid"], 1234)
        self.assertTrue(process.call_args.kwargs["start_new_session"])
        self.assertEqual(process.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIn("--foreground", process.call_args.args[0])
        self.assertFalse(any(call.args[0] == "bootstrap" for call in ctl.call_args_list))

    def test_start_definition_and_dynamic_port_readiness(self):
        running = False

        child = Mock(pid=1234)
        child.poll.return_value = None
        def spawn(*args, **kwargs):
            nonlocal running
            running = True
            self.ready(port=51234)
            return child

        with patch.object(self.service, "_pid", side_effect=lambda: 1234 if running else None), patch("appletart.service.subprocess.Popen", side_effect=spawn) as process, patch("appletart.service.webbrowser.open") as browser, contextlib.redirect_stdout(io.StringIO()):
            state = self.service.start(0, open_browser=False)
        command = process.call_args.args[0]
        self.assertIn("--foreground", command)
        self.assertIn("--no-browser", command)
        self.assertIn(str(self.service.root), command)
        self.assertEqual(process.call_args.kwargs["env"]["PATH"], __import__("os").environ["PATH"])
        self.assertEqual(json.loads(self.service.process_file.read_text()), {"pid":1234, "port":0})
        self.assertEqual(self.service.process_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(state["url"], "http://127.0.0.1:51234")
        browser.assert_not_called()

    def test_occupied_port_fails_before_bootstrap_or_readiness_wait(self):
        with patch.object(self.service, "_pid", return_value=None), patch.object(self.service, "_launchctl", return_value=subprocess.CompletedProcess([], 0, "", "")) as ctl, patch("socket.socket") as socket_class, patch("appletart.service.time.sleep", side_effect=AssertionError("Startup waited instead of reporting the port conflict")):
            socket_class.return_value.__enter__.return_value.bind.side_effect = OSError(errno.EADDRINUSE, "Address already in use")
            with self.assertRaisesRegex(DeploymentError, "4991.*already in use"):
                self.service.start(open_browser=False)
        self.assertFalse(any(call.args[0] == "bootstrap" for call in ctl.call_args_list))

    def test_failed_child_reports_exit_and_log_without_waiting(self):
        child = Mock(pid=1234)
        child.poll.return_value = 1
        def spawn(*args, **kwargs):
            self.service.log_file.write_text("error: A dashboard is already using this data directory.\n")
            return child
        with patch.object(self.service, "_pid", return_value=None), patch("appletart.service.subprocess.Popen", side_effect=spawn), patch("appletart.service.time.sleep", side_effect=AssertionError("Startup waited after the child exited")):
            with self.assertRaisesRegex(DeploymentError, "already using this data directory"):
                self.service.start(0, open_browser=False)

    def test_busy_control_lock_reports_instead_of_hanging(self):
        import fcntl
        self.service.directory.mkdir()
        with (self.service.directory / "control.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DeploymentError, "Another service command"):
                with self.service._control():
                    self.fail("Acquired a busy control lock")

    def test_failed_spawn_reports_error(self):
        with patch.object(self.service, "_pid", return_value=None), patch("appletart.service.subprocess.Popen", side_effect=OSError("permission denied")):
            with self.assertRaisesRegex(DeploymentError, "permission denied"):
                self.service.start(0, open_browser=False)

    def test_failed_readiness_removes_job_and_reports_log(self):
        child = Mock(pid=1234)
        child.poll.return_value = None
        with patch.object(self.service, "_pid", return_value=None), patch("appletart.service.subprocess.Popen", return_value=child), patch("appletart.service.time.monotonic", side_effect=[0, 21]):
            with self.assertRaisesRegex(DeploymentError, "dashboard.log"):
                self.service.start(0, open_browser=False)
        child.terminate.assert_called_once()

    def test_process_record_does_not_trust_reused_pid_or_another_user(self):
        self.service.directory.mkdir()
        self.service.process_file.write_text(json.dumps({"pid":1234, "port":4991}))
        import os
        expected = " ".join(self.service._command(4991))
        for output, wanted in ((f"{os.getuid()} S {expected}",1234),
                               (f"{os.getuid()} S unrelated-program",None),
                               (f"{os.getuid()+1} S {expected}",None),
                               (f"{os.getuid()} Z {expected}",None)):
            with self.subTest(output=output), patch("appletart.service.subprocess.run", return_value=subprocess.CompletedProcess([],0,output,"")):
                self.assertEqual(self.service._detached_pid(),wanted)

    def test_detached_stop_signals_only_the_verified_dashboard(self):
        self.ready()
        self.service.process_file.write_text('{"pid":1234,"port":4991}')
        with patch.object(self.service,"_pid",side_effect=[1234,None]), \
             patch.object(self.service,"_detached_pid",return_value=1234), \
             patch.object(self.service,"_launchctl") as ctl, \
             patch("appletart.service.os.kill") as kill, contextlib.redirect_stdout(io.StringIO()):
            self.service.stop()
        kill.assert_called_once_with(1234,signal.SIGINT)
        self.assertFalse(self.service.process_file.exists())
        self.assertFalse(any(call.args[0]=="kill" for call in ctl.call_args_list))

    def test_framework_python_process_is_recognized_after_launcher_exec(self):
        import os
        framework = self.service.root / "framework"
        interpreter = framework / "Resources/Python.app/Contents/MacOS/Python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        self.service.directory.mkdir()
        self.service.process_file.write_text('{"pid":1234,"port":4991}')
        command = " ".join([str(interpreter), *self.service._command(4991)[1:]])
        with patch("appletart.service.sys.base_prefix",str(framework)), \
             patch("appletart.service.subprocess.run",return_value=subprocess.CompletedProcess([],0,f"{os.getuid()} Ss {command}","")):
            self.assertEqual(self.service._detached_pid(),1234)

    def test_stop_waits_for_cleanup_before_unloading(self):
        self.ready()
        with patch.object(self.service, "_pid", side_effect=[1234, 1234, None]), patch.object(self.service, "_launchctl", return_value=subprocess.CompletedProcess([], 0, "", "")) as ctl, patch("appletart.service.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.service.stop()
        self.assertEqual([call.args for call in ctl.call_args_list], [("kill", "SIGINT", self.service.target), ("bootout", self.service.target)])
        self.assertFalse(self.service.ready_file.exists())

    def test_default_cli_launches_service_and_foreground_stays_available(self):
        with patch("appletart.service.DashboardService") as service:
            self.assertEqual(main([]), 0)
            service.return_value.start.assert_called_once_with(4991, open_browser=True)
        with patch("appletart.web.serve") as serve:
            self.assertEqual(main(["ui", "--foreground", "--no-browser"]), 0)
            self.assertFalse(serve.call_args.kwargs["open_browser"])

    def test_sigterm_cleans_up_jobs_and_readiness(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.service.directory.mkdir()
        with patch("appletart.web.AppServer") as server_class, patch("appletart.web.Lifecycle"), patch("appletart.web.diagnostics.recover_interrupted"), contextlib.redirect_stdout(io.StringIO()):
            server = server_class.return_value.__enter__.return_value
            server.server_port = 54321

            def terminate():
                ready = json.loads(self.service.ready_file.read_text())
                self.assertEqual(ready["url"], "http://127.0.0.1:54321")
                signal.raise_signal(signal.SIGTERM)

            server.serve_forever.side_effect = terminate
            serve(self.service.root, 0, open_browser=False, ready_file=self.service.ready_file)
            server.jobs.shutdown.assert_called_once()
        self.assertFalse(self.service.ready_file.exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_labels_are_per_resolved_data_directory(self):
        alias = DashboardService(self.service.root / "child" / "..")
        other = DashboardService(self.service.root / "other")
        self.assertEqual(alias.label, self.service.label)
        self.assertNotEqual(other.label, self.service.label)


if __name__ == "__main__":
    unittest.main()
