from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from appletart.deployment import DeploymentError
from appletart.ip_resolver import resolve


class ResolverTests(unittest.TestCase):
    def test_installed_agent_avoids_the_failed_dhcp_wait_on_every_connection(self):
        backend = Mock()
        def lookup(args, **kwargs):
            if args[-1] == "dhcp":
                self.fail("An available agent must not wait for DHCP first")
            return "192.0.2.10"
        backend.run.side_effect = lookup
        vm = SimpleNamespace(name="vm", network="nat")
        for _ in range(2):
            self.assertEqual(resolve(backend, vm, agent_installed=True), "192.0.2.10")

    def test_unavailable_agent_falls_back_to_dhcp_for_nat(self):
        backend = Mock()
        backend.run.side_effect = [DeploymentError("agent unavailable"), "192.0.2.11"]
        self.assertEqual(resolve(backend, SimpleNamespace(name="vm", network="nat"), agent_installed=True), "192.0.2.11")
        self.assertEqual([call.args[0][-1] for call in backend.run.call_args_list], ["agent", "dhcp"])

    def test_bridge_never_uses_a_stale_nat_lease(self):
        backend = Mock()
        backend.run.side_effect = [DeploymentError("agent unavailable"), "192.0.2.12"]
        self.assertEqual(resolve(backend, SimpleNamespace(name="vm", network="bridged")), "192.0.2.12")
        self.assertEqual([call.args[0][-1] for call in backend.run.call_args_list], ["agent", "arp"])

    def test_nat_without_an_agent_keeps_dhcp_first(self):
        backend = Mock()
        backend.run.return_value = "192.0.2.13"
        self.assertEqual(resolve(backend, SimpleNamespace(name="vm", network="nat")), "192.0.2.13")
        self.assertEqual(backend.run.call_args.args[0][-1], "dhcp")
