"""Resolve the current guest IPv4 without confusing bridge and NAT leases."""

import ipaddress

from .deployment import DeploymentError
from .operations import JobCancelled, checkpoint


def address(value):
    ip = ipaddress.IPv4Address(value.strip())
    if ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast or int(ip) == 0xffffffff:
        raise ValueError("Not a usable guest IPv4 address")
    return str(ip)


def resolve(backend, vm, *, wait=2, agent_installed=False):
    # A previous NAT lease for this MAC must never be displayed for a bridge.
    resolvers = (("agent", "arp") if vm.network == "bridged" else
                 ("agent", "dhcp") if agent_installed else ("dhcp", "agent"))
    for resolver in resolvers:
        checkpoint()
        try:
            return address(backend.run(["ip", vm.name, "--wait", str(wait), "--resolver", resolver],
                                       capture=True, timeout=wait + 3))
        except JobCancelled:
            raise
        except (DeploymentError, ValueError):
            pass
    if vm.network == "bridged":
        action = "Repair guest agent" if agent_installed else "Install guest agent"
        hint = ("The guest agent has not reported an IPv4 address and no bridged ARP entry was found. "
                f"Wait for guest boot and DHCP; if it persists, stop the VM and choose {action}, then start it again.")
    else:
        hint = "Waiting for a guest IPv4 address. Check guest DHCP and the VM log."
    raise DeploymentError(hint)
