"""The process boundary to Tart; VM state remains owned by Tart."""

import json
import os
import platform
import shutil
import subprocess

from .deployment import DeploymentError, VM
from .ssh import check_ssh_tools, provision_keys, read_public_keys
from .operations import run


def check_host() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise DeploymentError("AppleTart v0.1 requires an Apple Silicon Mac (native arm64 Python).")
    version = platform.mac_ver()[0]
    if not version or int(version.split(".")[0]) < 13:
        raise DeploymentError("Linux guests require macOS 13 or later.")


class Tart:
    def __init__(self) -> None:
        check_host()
        self.binary = shutil.which("tart")
        if self.binary is None:
            raise DeploymentError("Tart is not installed. Install it with: brew install cirruslabs/cli/tart")

    def run(self, args: list[str], *, capture: bool = False, timeout=None) -> str:
        try:
            result = run(
                [self.binary, *args], check=True, text=True,
                capture_output=capture,
                timeout=timeout,
                env={**os.environ, "TART_NO_AUTO_PRUNE": "1"},
            )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip()
            raise DeploymentError(f"Tart {args[0]} failed (exit {error.returncode})" + (f": {detail}" if detail else ".")) from error
        except OSError as error:
            raise DeploymentError(f"Cannot execute Tart: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise DeploymentError(f"Tart {args[0]} timed out.") from error
        return result.stdout if capture else ""

    def inventory(self) -> list[dict]:
        output = self.run(["list", "--source", "local", "--format", "json"], capture=True)
        try:
            items = json.loads(output)
        except json.JSONDecodeError as error:
            raise DeploymentError("Tart returned invalid JSON for its VM inventory.") from error
        if not isinstance(items, list) or not all(
            isinstance(item, dict) and isinstance(item.get("Name"), str)
            and type(item.get("Running")) is bool for item in items
        ):
            raise DeploymentError("Unexpected Tart inventory format; check your Tart version.")
        return items

    def deploy(self, vms: list[VM]) -> None:
        # Validate every key before cloning the first VM.
        if any(vm.ssh_public_keys for vm in vms):
            check_ssh_tools()
        public_keys = {vm.name: read_public_keys(vm.ssh_public_keys) for vm in vms}
        inventory = {item["Name"]: item for item in self.inventory()}
        existing = [vm.name for vm in vms if vm.name in inventory]
        if existing:
            raise DeploymentError(f"Destinations already exist: {', '.join(existing)}. Choose new names; no VMs were changed.")
        for vm in vms:
            if "/" not in vm.image:
                template = inventory.get(vm.image)
                if template is None:
                    raise DeploymentError(f"Local template {vm.image!r} does not exist; no VMs were changed.")
                if template["Running"] or template.get("State", "Stopped").lower() != "stopped":
                    raise DeploymentError(f"Template {vm.image!r} must be stopped before cloning; no VMs were changed.")
            if vm.cpu > (os.cpu_count() or 1):
                raise DeploymentError(f"{vm.name}: requested CPU count exceeds host CPUs; no VMs were changed.")
        completed = []
        for vm in vms:
            try:
                for command in vm.commands():
                    self.run(command)
                if public_keys[vm.name]:
                    provision_keys(self, vm, public_keys[vm.name])
            except DeploymentError as error:
                raise DeploymentError(
                    f"Deployment stopped at {vm.name}: {error}\n"
                    f"Completed: {', '.join(completed) or 'none'}. Any partially created VM is preserved. "
                    "Inspect with appletart list; rename destinations or remove completed entries before retrying."
                ) from error
            completed.append(vm.name)
            print(f"Deployed {vm.name} ({vm.os}). Start with: appletart run {vm.name}")
