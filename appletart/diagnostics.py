"""Durable operation transcripts, separate from the dashboard's small live tail."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import threading
import time

from .deployment import DeploymentError, vm_name

_local = threading.local()
PREVIEW_BYTES = 128 * 1024


def current():
    return getattr(_local, "journal", None)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"-----BEGIN [^\n]*PRIVATE KEY-----.*?(?:-----END [^\n]*PRIVATE KEY-----|$)", "[private key redacted]", text, flags=re.S)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
    return re.sub(r"(?i)((?:password|token|secret|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,]+", r"\1[redacted]", text)


class Journal:
    def __init__(self, root, name, identifier, action, config=None, secrets=()):
        self.root = Path(root)
        self.directory = self.root / "logs" / "operations" / vm_name(name)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not re.fullmatch(r"[a-f0-9]{16}", identifier):
            raise DeploymentError("Invalid operation log identifier.")
        self.path = self.directory / (identifier + ".log")
        self.metadata_path = self.path.with_suffix(".json")
        self.file = self.path.open("x", encoding="utf-8")
        self.path.chmod(0o600)
        self.secrets = secrets
        self.lock = threading.RLock()
        self.consoles = {}
        self.private_key = False
        self.started = time.monotonic()
        self.metadata = {"id": identifier, "name": name, "action": action, "status": "running", "started_at": timestamp()}
        self._save()
        self.write("operation", f"{name} · {action} · {identifier}")
        if config:
            self.write("configuration", json.dumps(config, indent=2))

    def _save(self):
        temp = self.metadata_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.metadata), encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(self.metadata_path)

    def write(self, kind, message):
        with self.lock:
            text = str(message)
            if self.private_key:
                end = re.search(r"-----END [^\n]*PRIVATE KEY-----", text)
                if not end:
                    return
                text, self.private_key = text[end.end():], False
            begins = list(re.finditer(r"-----BEGIN [^\n]*PRIVATE KEY-----", text))
            if begins and not re.search(r"-----END [^\n]*PRIVATE KEY-----", text[begins[-1].end():]):
                self.private_key = True
            self.file.write(f"[{timestamp()}] [{kind}] {redact(text, self.secrets)}\n")
            self.file.flush()

    def console(self, path):
        path = Path(path)
        self.consoles.setdefault(path, path.stat().st_size if path.exists() else 0)

    @contextmanager
    def scope(self):
        previous = current()
        _local.journal = self
        try:
            yield self
        finally:
            _local.journal = previous

    def finish(self, status):
        try:
            for path, offset in self.consoles.items():
                if path.is_file():
                    self.write("console", f"Tart / serial output: {path.name}")
                    with path.open("r", encoding="utf-8", errors="replace") as source:
                        source.seek(offset)
                        for line in source:
                            self.write("console", line.rstrip("\n"))
            self.metadata.update(status=status, finished_at=timestamp(), elapsed_seconds=round(time.monotonic() - self.started, 3))
            self.write("operation", f"{status}; elapsed {self.metadata['elapsed_seconds']:.3f}s")
            self._save()
        finally:
            self.file.close()


def console(path):
    if journal := current():
        journal.console(path)


def recover_interrupted(root):
    """The dashboard lock guarantees the previous logger is no longer active."""
    for path in (Path(root) / "logs" / "operations").glob("*/*.json"):
        item = json.loads(path.read_text())
        if item.get("status") == "running":
            item.update(status="interrupted", finished_at=timestamp())
            path.write_text(json.dumps(item), encoding="utf-8")
            with path.with_suffix(".log").open("a") as file:
                file.write(f"[{timestamp()}] [operation] interrupted: the dashboard exited before recording a final result.\n")


def command_start(args):
    if journal := current():
        # Never record stdin (keys, provisioning scripts, agent binary) or env.
        safe = [str(arg) if len(str(arg)) <= 4096 else "[large argument omitted]" for arg in args]
        journal.write("command", shlex.join(safe))


def command_result(result, elapsed):
    if journal := current():
        journal.write("exit", f"code={result.returncode}; elapsed={elapsed:.3f}s")
        for kind in ("stdout", "stderr"):
            output = getattr(result, kind, None)
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            if isinstance(output, str) and output:
                journal.write(kind, output)


def sources(root, name):
    root, name = Path(root), vm_name(name)
    entries = []
    directory = root / "logs" / "operations" / name
    for path in directory.glob("*.json"):
        if not re.fullmatch(r"[a-f0-9]{16}", path.stem):
            continue
        item = json.loads(path.read_text())
        log = path.with_suffix(".log")
        if log.is_file():
            entries.append({**item, "source": path.stem, "size_bytes": log.stat().st_size})
    entries.sort(key=lambda item: item["started_at"], reverse=True)
    runtime = root / "logs" / f"{name}.log"
    if runtime.is_file():
        entries.append({"source": "runtime", "action": "Tart / serial console", "status": "saved", "size_bytes": runtime.stat().st_size})
    return entries


def source_path(root, name, source):
    root, name = Path(root), vm_name(name)
    if source == "runtime":
        path = root / "logs" / f"{name}.log"
    elif isinstance(source, str) and re.fullmatch(r"[a-f0-9]{16}", source):
        path = root / "logs" / "operations" / name / (source + ".log")
    else:
        raise DeploymentError("Choose a saved operation or runtime log.")
    if not path.is_file() or path.is_symlink():
        raise DeploymentError("That log is unavailable.")
    return path


def read(root, name, source=None, before=None, after=None):
    entries = sources(root, name)
    if not entries:
        return {"log": "No saved logs yet. New operations retain detailed diagnostics.", "sources": [], "source": None, "start": 0, "end": 0, "size_bytes": 0}
    if source is None:
        detailed = next((item for item in entries if item["action"] in {"build", "create", "deploy-profile", "golden", "finish", "agent", "diagnostics"}), entries[0])
        source = (entries[0] if entries[0]["status"] in {"failed", "cancelled"} else detailed)["source"]
    path = source_path(root, name, source)
    size = path.stat().st_size
    if before is not None and (type(before) is not int or before < 0):
        raise DeploymentError("Invalid log offset.")
    if after is not None and (type(after) is not int or after < 0 or before is not None):
        raise DeploymentError("Invalid log offset.")
    if after is not None:
        start = min(size, after)
        end = min(size, start + PREVIEW_BYTES)
    else:
        end = min(size, before) if before is not None else size
        start = max(0, end - PREVIEW_BYTES)
    with path.open("rb") as file:
        if start and after is None:
            # Keep pages on UTF-8 boundaries so copying all pages preserves text.
            for _ in range(3):
                file.seek(start)
                if file.read(1)[0] & 0xc0 != 0x80:
                    break
                start -= 1
        file.seek(start)
        raw = file.read(end - start)
        if after is not None and end < size:
            while raw and raw[-1] & 0xc0 == 0x80:
                raw = raw[:-1]
            if raw and raw[-1] & 0x80:
                raw = raw[:-1]
            end = start + len(raw)
        text = raw.decode(errors="replace")
    return {"log": text, "sources": entries, "source": source, "start": start, "end": end, "size_bytes": size}
