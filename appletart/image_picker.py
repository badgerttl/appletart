"""Native local image and shared-directory selection without uploading files."""

from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .deployment import DeploymentError
from .connectivity import DirectoryShare


_picker = threading.Lock()
_active = None
_cancelled = {}


def cancel_picker(picker_id):
    """Cancel only the matching request; a late click cannot close the next panel."""
    if not isinstance(picker_id, str) or not picker_id or len(picker_id) > 128:
        raise DeploymentError("Invalid chooser request.")
    with _picker:
        if _active is not None and _active["id"] == picker_id:
            _active["cancel"].set()
            return True
        # HTTP worker scheduling can deliver Cancel before the browse request.
        now = time.monotonic()
        for identifier, expires in list(_cancelled.items()):
            if expires < now:
                _cancelled.pop(identifier)
        if len(_cancelled) >= 64:
            _cancelled.pop(next(iter(_cancelled)))
        _cancelled[picker_id] = now + 30
    return False


def _run_picker(mode, cancelled):
    if cancelled.is_set():
        return None
    command = ["/usr/bin/osascript", "-l", "JavaScript",
               str(Path(__file__).parent / "native" / "file_picker.js"), mode]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        deadline = time.monotonic() + 600
        try:
            while True:
                if cancelled.is_set():
                    return None
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, 600)
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


def _choose_path(mode, description, picker_id=None):
    global _active
    if sys.platform != "darwin":
        raise DeploymentError(f"The native {description} chooser requires macOS. Enter a local path instead.")
    if picker_id is not None and (not isinstance(picker_id, str) or not picker_id or len(picker_id) > 128):
        raise DeploymentError("Invalid chooser request.")
    request = {"id": picker_id or uuid.uuid4().hex, "cancel": threading.Event()}
    with _picker:
        if _cancelled.pop(request["id"], 0) > time.monotonic():
            return None
        if _active is not None:
            raise DeploymentError("A file or directory chooser is already open. Select an item or cancel it first.")
        _active = request
    try:
        try:
            result = _run_picker(mode, request["cancel"])
        except subprocess.TimeoutExpired as error:
            raise DeploymentError(f"The {description} chooser timed out. Choose Browse again or enter a path.") from error
        except OSError as error:
            raise DeploymentError(f"Could not open the macOS {description} chooser: {error}") from error
        if result is None or request["cancel"].is_set():
            return None
        if result.returncode:
            if "(-128)" in result.stderr:
                return None
            raise DeploymentError(f"Could not select a local {description}: " + result.stderr[-500:])
        selected = result.stdout.removesuffix("\n")
        if not selected:
            return None
        if any(character in selected for character in ("\0", "\n", "\r")):
            raise DeploymentError("Choose a local path without newlines.")
        return Path(selected).resolve()
    finally:
        with _picker:
            _active = None


def choose_image(kind, *, picker_id=None):
    if kind not in {"cloud", "iso"}:
        raise DeploymentError("Browse is available for cloud images and installer ISOs.")
    path = _choose_path(kind, "image", picker_id)
    if path is None:
        return {"cancelled": True, "path": ""}
    suffixes = (".tar.xz", ".qcow2.xz", ".qcow2", ".raw", ".img") if kind == "cloud" else (".iso",)
    if not path.is_file() or not path.stat().st_size or not str(path).lower().endswith(suffixes) or ":" in str(path):
        raise DeploymentError("Select a nonempty " + (".tar.xz, .qcow2.xz, .qcow2, .raw or .img cloud image." if kind == "cloud" else ".iso installer."))
    return {"cancelled": False, "path": str(path)}


def choose_directory(*, picker_id=None):
    path = _choose_path("directory", "directory", picker_id)
    if path is None:
        return {"cancelled": True, "path": ""}
    share = DirectoryShare.from_dict({"host_path": str(path)})
    if not path.is_dir():
        raise DeploymentError("Select an existing local directory to share.")
    return {"cancelled": False, "path": share.host_path}
