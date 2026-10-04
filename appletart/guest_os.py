"""Observe the guest's release metadata without using it to select provisioning."""

import shlex
import subprocess

from . import guest_agent
from .operations import run


def parse_release(text):
    if len(text.encode()) > 65536:
        return {}
    result = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ID", "NAME", "PRETTY_NAME", "VERSION", "VERSION_ID", "BUILD_ID"}:
            try:
                fields = shlex.split(value, comments=True)
            except ValueError:
                continue
            if len(fields) == 1 and len(fields[0]) <= 500:
                result[key.lower()] = fields[0]
    return result


def observe(backend, name):
    try:
        result = run(guest_agent.exec_args(backend, name, ["cat", "/etc/os-release"]),
                     capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_release(result.stdout) if result.returncode == 0 else {}
