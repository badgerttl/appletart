import os
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from appletart import diagnostics
from appletart.operations import Cancellation, JobCancelled, run, scope, stream


class CancellationTests(unittest.TestCase):
    def test_cancellation_interrupts_a_long_subprocess_and_reaps_it(self):
        cancellation = Cancellation()
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            def work():
                try:
                    with scope(cancellation):
                        run([sys.executable, "-c", "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(1800)", str(marker)],
                            capture_output=True, text=True, timeout=1800)
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            try:
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
            finally:
                cancellation.cancel()
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], JobCancelled)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_repeated_communication_keeps_subprocess_input_intact(self):
        with scope(Cancellation()):
            result = run([sys.executable, "-c", "import sys,time; data=sys.stdin.read(); time.sleep(.25); print(data)"],
                         input="build input", text=True, capture_output=True, check=True, timeout=2)
        self.assertEqual(result.stdout.strip(), "build input")


class StreamingTests(unittest.TestCase):
    def test_large_input_and_live_package_output_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = diagnostics.Journal(Path(directory), "vm", "0123456789abcdef", "build")
            output = []
            def report(line):
                output.append(line)
                journal.write("progress", line)
            payload = "package configuration\n" * 100000
            code = "import sys; print('install started', flush=True); data=sys.stdin.read(); print(len(data), flush=True); print('package configured', file=sys.stderr, flush=True)"
            with journal.scope():
                stream([sys.executable, "-c", code], report, env=os.environ,
                       input=payload, timeout=5, label="Guest provisioning")
            journal.finish("complete")
            self.assertEqual(output, ["install started", str(len(payload)), "package configured"])
            saved = journal.path.read_text()
            self.assertIn("package configured", saved)
            self.assertIn("[exit] code=0", saved)

    def test_timeout_reaps_the_streaming_process(self):
        output = []
        with self.assertRaises(subprocess.TimeoutExpired):
            stream([sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(1800)"],
                   output.append, env=os.environ, timeout=.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(output[0]), 0)

    def test_live_stream_can_be_cancelled(self):
        cancellation = Cancellation()
        output = []
        def report(line):
            output.append(line)
            cancellation.cancel()
        with scope(cancellation), self.assertRaises(JobCancelled):
            stream([sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(1800)"],
                   report, env=os.environ, input="configuration", timeout=5)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(output[0]), 0)
