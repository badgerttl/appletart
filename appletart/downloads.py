"""Cached image downloads with optional publisher checksum verification."""

import hashlib
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit

from .deployment import DeploymentError
from .operations import checkpoint, resource_lock


def digest(path: Path, algorithm="sha256") -> str:
    checksum = hashlib.new(algorithm)
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            checkpoint()
            checksum.update(block)
    return checksum.hexdigest()


def fetch_iso(source: str, checksum: str, cache: Path, report=print, *, refresh=False) -> Path:
    return fetch_image(source, checksum, cache, report, suffix=".iso", refresh=refresh)


def fetch_image(source: str, checksum: str, cache: Path, report=print, *, suffix: str = "", refresh=False) -> Path:
    import re
    if checksum and not re.fullmatch(r"(?:[a-fA-F0-9]{64}|[a-fA-F0-9]{128})", checksum):
        raise DeploymentError("Provide a 64-character SHA256 or 128-character SHA512 checksum.")
    checksum = checksum.lower()
    algorithm = "sha512" if len(checksum) == 128 else "sha256"
    if not source.startswith("https://"):
        path = Path(source).expanduser()
        if not path.is_file() or path.stat().st_size == 0:
            raise DeploymentError("The local image does not exist or is empty.")
        if checksum and digest(path, algorithm) != checksum:
            raise DeploymentError(f"The local image does not match its {algorithm.upper()} checksum.")
        if not checksum and path.parent.resolve() == cache.resolve() and path.name.startswith("source-"):
            try:
                expected = path.with_suffix(".sha256").read_text().strip()
            except OSError:
                expected = ""
            if not expected or digest(path) != expected:
                raise DeploymentError("The cached image changed. Download the source again before building.")
        return path
    cache.mkdir(parents=True, exist_ok=True)
    filename = urlsplit(source).path.lower()
    suffix = suffix or next((s for s in (".tar.xz", ".qcow2.xz", ".qcow2", ".raw", ".img") if filename.endswith(s)), "")
    if not suffix:
        raise DeploymentError("Cloud source must end in .tar.xz, .qcow2.xz, .qcow2, .raw or .img.")
    cache_key = checksum or "source-" + hashlib.sha256(source.encode()).hexdigest()
    with resource_lock(cache / f"{cache_key}.download.lock", wait=True, report=report):
        return _download(source, checksum, cache, report, suffix, cache_key, refresh)


def _download(source, checksum, cache, report, suffix, cache_key, refresh=False):
    algorithm = "sha512" if len(checksum) == 128 else "sha256"
    destination = cache / f"{cache_key}{suffix}"
    stamp = destination.with_suffix(".sha256")
    expected = checksum
    if not expected:
        try:
            expected = stamp.read_text().strip()
        except OSError:
            expected = ""
    if not refresh and expected and destination.is_file() and destination.stat().st_size and digest(destination, algorithm) == expected:
        report("Using verified cached image." if checksum else "Using cached image (no publisher checksum supplied).")
        return destination
    partial = destination.with_suffix(".part")
    received = 0
    last_report = 0
    hasher = hashlib.new(algorithm)
    try:
        request = urllib.request.Request(source, headers={"User-Agent": "AppleTart/0.2"})
        with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as file:
            if not response.url.startswith("https://"):
                raise DeploymentError("The image server redirected the download away from HTTPS.")
            length = getattr(response, "headers", {}).get("Content-Length")
            expected_bytes = int(length) if length is not None else None
            for block in iter(lambda: response.read(1024 * 1024), b""):
                checkpoint()
                file.write(block)
                hasher.update(block)
                received += len(block)
                if received - last_report >= 64 * 1024 * 1024:
                    report(f"Downloaded {received // (1024 * 1024)} MB…")
                    last_report = received
        if received == 0:
            raise DeploymentError("The downloaded image is empty. It was not accepted for building.")
        if expected_bytes is not None and received != expected_bytes:
            raise DeploymentError("The downloaded image is incomplete. It was not accepted for building.")
        if checksum and hasher.hexdigest() != checksum:
            raise DeploymentError(f"Downloaded image failed {algorithm.upper()} verification. It was not accepted for building.")
        checkpoint()
        partial.replace(destination)
        if not checksum:
            # This detects changes to the cached file; it is not publisher verification.
            stamp.write_text(hasher.hexdigest() + "\n")
        report(f"Verified image ({received // (1024 * 1024)} MB)." if checksum else
               f"Downloaded image ({received // (1024 * 1024)} MB; no publisher checksum supplied).")
        return destination
    except (OSError, ValueError) as error:
        raise DeploymentError(f"Image download failed: {error}") from error
    finally:
        partial.unlink(missing_ok=True)
