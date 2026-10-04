"""Cancellation and resource locks shared by dashboard lifecycle workers."""

from contextlib import contextmanager
import fcntl
import os
import queue
import signal
import subprocess
import threading
import time

from .deployment import DeploymentError
from . import diagnostics


class JobCancelled(DeploymentError):
    pass


class Cancellation:
    def __init__(self):
        self.requested = threading.Event()

    def cancel(self):
        self.requested.set()

    def check(self):
        if self.requested.is_set():
            raise JobCancelled("Operation cancelled.")


_local = threading.local()


def current():
    return getattr(_local, "cancellation", None)


@contextmanager
def scope(cancellation):
    previous = current()
    _local.cancellation = cancellation
    try:
        yield
    finally:
        _local.cancellation = previous


def uncancellable():
    # Finish a disk mutation and persist its ownership before honoring cancellation.
    return scope(None)


def checkpoint():
    if cancellation := current():
        cancellation.check()


def pause(seconds):
    if cancellation := current():
        cancellation.requested.wait(seconds)
        cancellation.check()
    else:
        time.sleep(seconds)


def terminate(process):
    """Terminate only the subprocess group created by this worker."""
    if process.poll() is not None:
        return
    for sig, timeout in ((signal.SIGTERM, 2), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
    raise DeploymentError("A tool process did not exit after cancellation. Check its log before retrying.")


def stop_build(process, name, backend=None, report=print):
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5 if current() and current().requested.is_set() else 30)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            if backend:
                with uncancellable():
                    inventory = backend.inventory()
                if isinstance(inventory, list) and any(item["Name"] == name and not item["Running"] for item in inventory):
                    report("VM stopped. Tart is still exiting in macOS; other VM operations can continue.")
                    return
            raise DeploymentError(f"{name}: Tart did not exit after stopping the build. The VM disk is preserved; check Tart before resuming.") from error


def run(args, **kwargs):
    started = time.monotonic()
    diagnostics.command_start(args)
    try:
        result = _run(args, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        diagnostics.command_result(subprocess.CompletedProcess(args, getattr(error, "returncode", "timeout"),
                                  error.stdout, error.stderr), time.monotonic() - started)
        raise
    except BaseException as error:
        if journal := diagnostics.current():
            journal.write("command-error", f"{type(error).__name__}: {error}; elapsed={time.monotonic() - started:.3f}s")
        raise
    else:
        diagnostics.command_result(result, time.monotonic() - started)
        return result


def _run(args, **kwargs):
    """subprocess.run with prompt cancellation for long tool and SSH waits."""
    if current() is None:
        return subprocess.run(args, **kwargs)
    checkpoint()
    timeout = kwargs.pop("timeout", None)
    check = kwargs.pop("check", False)
    input_data = kwargs.pop("input", None)
    if kwargs.pop("capture_output", False):
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input_data is not None:
        kwargs["stdin"] = subprocess.PIPE
    kwargs["start_new_session"] = True
    deadline = time.monotonic() + timeout if timeout is not None else None
    process = subprocess.Popen(args, **kwargs)
    try:
        while True:
            checkpoint()
            remaining = deadline - time.monotonic() if deadline else 0.2
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                stdout, stderr = process.communicate(input_data, timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                input_data = None
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    except BaseException:
        terminate(process)
        raise
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe:
                pipe.close()


def stream(args, report, *, env, input=None, timeout=None, label="Tart"):
    """Stream tool output and optional stdin with bounded, cancellable execution."""
    checkpoint()
    started = time.monotonic()
    diagnostics.command_start(args)
    process = subprocess.Popen(args, text=True, stdin=subprocess.PIPE if input is not None else None,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    deadline = time.monotonic() + timeout if timeout is not None else None
    writer_errors = []
    def write():
        try:
            process.stdin.write(input)
            process.stdin.close()
        except (OSError, ValueError) as error:
            writer_errors.append(error)
    writer = threading.Thread(target=write, daemon=True) if input is not None else None
    lines = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    if writer:
        writer.start()
    tail = []
    try:
        while True:
            checkpoint()
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            text = line.rstrip()
            if text:
                report(text)
                tail = (tail + [text])[-8:]
        while process.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(args, timeout)
            pause(0.1)
        diagnostics.command_result(subprocess.CompletedProcess(args, process.returncode), time.monotonic() - started)
        if process.returncode:
            raise DeploymentError(label + " failed: " + "\n".join(tail))
        if writer:
            writer.join(timeout=1)
        if writer_errors:
            raise DeploymentError(label + " could not send its provisioning input: " + str(writer_errors[0]))
        return ""
    except BaseException:
        terminate(process)
        raise
    finally:
        reader.join(timeout=1)
        if writer:
            writer.join(timeout=1)
            if not writer.is_alive() and not process.stdin.closed:
                process.stdin.close()
        if not reader.is_alive():
            process.stdout.close()


@contextmanager
def resource_lock(path, *, wait=False, report=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as lock:
        notified = False
        while True:
            checkpoint()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if not wait:
                    raise DeploymentError("Another AppleTart operation is changing this VM or image.") from error
                if report and not notified:
                    report("Waiting for another build to finish preparing this shared image…")
                    notified = True
                pause(0.1)
        yield
