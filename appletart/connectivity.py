"""Validated host listeners and VirtioFS shares."""

from dataclasses import asdict, dataclass
import ipaddress
from pathlib import Path, PurePosixPath
import re

from .deployment import DeploymentError, integer


@dataclass(frozen=True)
class PortForward:
    listen_address: str
    host_port: int
    guest_port: int
    protocol: str = "tcp"
    listen_interface: str = ""

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) - {"listen_address", "listen_interface", "host_port", "guest_port", "protocol"}:
            raise DeploymentError("A port forward requires a Mac listen IP, host port, guest port and protocol.")
        address = data.get("listen_address", "0.0.0.0")
        try:
            ip = ipaddress.IPv4Address(address)
        except (ValueError, TypeError) as error:
            raise DeploymentError("The forward listen address must be a Mac IPv4 address, or 0.0.0.0 for all interfaces.") from error
        if ip.is_multicast or ip == ipaddress.IPv4Address("255.255.255.255"):
            raise DeploymentError("Choose a unicast Mac IP for the forward listener.")
        protocol = data.get("protocol", "tcp")
        if protocol not in ("tcp", "udp"):
            raise DeploymentError("Forward protocol must be tcp or udp.")
        interface = data.get("listen_interface", "")
        if not isinstance(interface, str) or (interface and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", interface)):
            raise DeploymentError("Choose a valid Mac interface name for the forward listener.")
        return cls(str(ip), integer(data.get("host_port"), "host_port", 1, 65535),
                   integer(data.get("guest_port"), "guest_port", 1, 65535), protocol, interface)

    def config(self):
        return asdict(self)


@dataclass(frozen=True)
class DirectoryShare:
    host_path: str
    guest_path: str
    read_only: bool = True

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) - {"host_path", "guest_path", "read_only"}:
            raise DeploymentError("A directory share requires host_path, guest_path and read_only.")
        host = data.get("host_path")
        guest = data.get("guest_path", "/mnt/share")
        if not isinstance(host, str) or not host or any(c in host for c in (":", "\n", "\0")):
            raise DeploymentError("Choose a local directory path without colons or newlines.")
        host = str(Path(host).expanduser().resolve())
        if not isinstance(guest, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", guest) or ".." in PurePosixPath(guest).parts:
            raise DeploymentError("Guest mount paths must be absolute, using letters, numbers, dots, underscores or hyphens.")
        guest = str(PurePosixPath(guest))
        if guest == "/" or not guest.startswith(("/mnt/", "/media/", "/srv/")):
            raise DeploymentError("Choose a guest mount below /mnt, /media or /srv.")
        read_only = data.get("read_only", True)
        if type(read_only) is not bool:
            raise DeploymentError("read_only must be true or false.")
        return cls(host, guest, read_only)

    def config(self):
        return asdict(self)

    def tag(self, index):
        return f"appletart-share{index}"

    def tart_argument(self, index):
        return self.host_path + ":" + ("ro," if self.read_only else "") + f"tag={self.tag(index)}"

    def mount_command(self, index):
        return ["mount", "-t", "virtiofs", "-o", "ro" if self.read_only else "rw", self.tag(index), self.guest_path]


def port_forwards(data, network):
    if not isinstance(data, list) or len(data) > 32:
        raise DeploymentError("Provide up to 32 port forwarding rules.")
    rules = tuple(PortForward.from_dict(item) for item in data)
    if rules and network != "nat":
        raise DeploymentError("Port forwarding requires a NAT interface.")
    for index, rule in enumerate(rules):
        for other in rules[:index]:
            wildcard = any(not item.listen_interface and item.listen_address == "0.0.0.0" for item in (rule, other))
            same = (rule.listen_interface == other.listen_interface if rule.listen_interface or other.listen_interface else rule.listen_address == other.listen_address)
            if (rule.protocol, rule.host_port) == (other.protocol, other.host_port) and (same or wildcard):
                raise DeploymentError("Port forward listeners overlap. Choose different Mac ports or IPs.")
    return rules


def directory_shares(data):
    if not isinstance(data, list) or len(data) > 8:
        raise DeploymentError("Provide up to eight directory shares.")
    shares = tuple(DirectoryShare.from_dict(item) for item in data)
    paths = [item.guest_path for item in shares]
    if len(paths) != len(set(paths)):
        raise DeploymentError("Each shared directory needs a different guest mount path.")
    return shares
