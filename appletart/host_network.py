"""Current Mac interface names and IPv4 addresses."""

import re
import socket
import subprocess

from .deployment import DeploymentError


def network_interfaces():
    return sorted((name for _, name in socket.if_nameindex() if re.fullmatch(r"(?:en|bridge)\d+", name)),
                  key=lambda name: (name != "en0", name))


def interface_address(interface):
    try:
        result = subprocess.run(["/sbin/ifconfig", interface], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError(f"Cannot inspect Mac interface {interface}.") from error
    addresses = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)\b", result.stdout)
    if result.returncode or not addresses:
        raise DeploymentError(f"Mac interface {interface} has no IPv4 address. Connect it to a network or choose another listener.")
    return addresses[0]


def listen_addresses():
    result = []
    for interface in network_interfaces():
        try:
            result.append({"interface": interface, "address": interface_address(interface)})
        except DeploymentError:
            pass
    return result
