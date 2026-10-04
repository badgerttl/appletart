import asyncio
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart.catalog import Machine
from appletart.bundles import legacy_packages
from appletart.cloud import cloud_config
from appletart.deployment import DeploymentError
from appletart.forwarder import Worker, start as start_forwarder


class ConfigurationTests(unittest.TestCase):
    def machine(self, **fields):
        return Machine.from_dict({"name": "kali-lab", "os": "kali", "ssh_user": "jay.morris",
                                  "ssh_public_keys": ["~/.ssh/id_ed25519.pub"], **fields})

    def test_dotted_user_and_default_or_custom_applications(self):
        default = self.machine()
        self.assertEqual(default.vm.ssh_user, "jay.morris")
        self.assertEqual(default.effective_packages, ())
        legacy = self.machine(install_default_packages=True)
        self.assertEqual(legacy.effective_packages, legacy_packages())
        self.assertIn("kali-essentials", legacy.software_bundles)
        self.assertNotIn("install_default_packages", legacy.config())
        custom = self.machine(install_default_packages=False, packages=["git", "btop"])
        self.assertEqual(custom.effective_packages, ("git", "btop"))

    def test_nat_forwards_shares_and_round_trip(self):
        machine = self.machine(network="nat",
            port_forwards=[{"listen_address": "192.0.2.2", "host_port": 8443, "guest_port": 443}],
            directory_shares=[{"host_path": "~/Projects", "guest_path": "/mnt/projects", "read_only": True}])
        self.assertEqual(Machine.from_dict(machine.config()), machine)
        args = machine.vm.run_args()
        self.assertNotIn("--net-bridged", args)
        self.assertIn(str(Path.home() / "Projects") + ":ro,tag=appletart-share0", args)
        data = cloud_config(machine, ["ssh-ed25519 test"])
        self.assertTrue(any("mount -t virtiofs -o ro appletart-share0 /mnt/projects" in command[-1] for command in data["bootcmd"]))

    def test_forwarding_can_follow_a_mac_interface_instead_of_a_saved_ip(self):
        machine = self.machine(port_forwards=[{"listen_interface": "en0", "host_port": 8443, "guest_port": 443}])
        self.assertEqual(machine.port_forwards[0].listen_interface, "en0")
        self.assertEqual(Machine.from_dict(machine.config()), machine)

    def test_multiple_bridges(self):
        machine = self.machine(network="bridged", bridges=["en0", "en1"])
        self.assertEqual(machine.vm.run_args().count("--net-bridged"), 2)

    def test_listener_conflicts_and_unsafe_mounts_are_rejected(self):
        for changes in [
            {"port_forwards": [{"host_port": 8080, "guest_port": 80}, {"listen_address": "192.0.2.2", "host_port": 8080, "guest_port": 443}]},
            {"network": "bridged", "bridge": "en0", "port_forwards": [{"host_port": 8080, "guest_port": 80}]},
            {"port_forwards": [{"listen_address": "example.com", "host_port": 8080, "guest_port": 80}]},
            {"directory_shares": [{"host_path": "/tmp", "guest_path": "/etc"}]},
            {"directory_shares": [{"host_path": "/tmp", "guest_path": "/mnt/../../etc"}]},
        ]:
            with self.subTest(changes=changes), self.assertRaises(DeploymentError):
                self.machine(**changes)


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="at-")
        self.addCleanup(self.directory.cleanup)
        self.worker = None

    async def launch(self, rules):
        self.worker = Worker({"target": "127.0.0.1", "rules": rules, "token": "test-token",
                              "control": str(Path(self.directory.name) / "control.sock")})
        self.run_task = asyncio.create_task(self.worker.run(watch=False))
        for _ in range(100):
            if self.worker.ready:
                return
            if self.run_task.done():
                await self.run_task
            await asyncio.sleep(0.01)
        self.fail("Listener did not start")

    async def asyncTearDown(self):
        if self.worker:
            self.worker.done.set()
            await self.run_task

    async def test_tcp_external_listener_reaches_guest_port_and_stops(self):
        async def service(reader, writer):
            writer.write(b"guest-storage:" + await reader.read(100))
            await writer.drain()
            writer.close()
        guest = await asyncio.start_server(service, "127.0.0.1", 0)
        self.addAsyncCleanup(guest.wait_closed)
        self.addCleanup(guest.close)
        guest_port = guest.sockets[0].getsockname()[1]
        host_port = free_port()
        await self.launch([{"listen_address": "0.0.0.0", "host_port": host_port, "guest_port": guest_port}])
        reader, writer = await asyncio.open_connection("127.0.0.1", host_port)
        writer.write(b"read-file")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(100), 2), b"guest-storage:read-file")
        writer.close()
        await writer.wait_closed()
        self.worker.done.set()
        await self.run_task
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", host_port)

    async def test_udp_returns_responses_to_the_external_client(self):
        class Echo(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport
            def datagram_received(self, data, address):
                self.transport.sendto(b"guest:" + data, address)
        loop = asyncio.get_running_loop()
        guest, _ = await loop.create_datagram_endpoint(Echo, local_addr=("127.0.0.1", 0))
        self.addCleanup(guest.close)
        host_port = free_port(socket.SOCK_DGRAM)
        await self.launch([{"listen_address": "0.0.0.0", "host_port": host_port, "guest_port": guest.get_extra_info("sockname")[1], "protocol": "udp"}])
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.setblocking(False)
        self.addCleanup(client.close)
        await loop.sock_sendto(client, b"storage", ("127.0.0.1", host_port))
        reply, _ = await asyncio.wait_for(loop.sock_recvfrom(client, 100), 2)
        self.assertEqual(reply, b"guest:storage")

    async def test_failed_second_listener_rolls_back_first_listener(self):
        port = free_port()
        worker = Worker({"target": "127.0.0.1", "token": "token", "control": str(Path(self.directory.name) / "socket"),
                         "rules": [{"listen_address": "127.0.0.1", "host_port": port, "guest_port": 80},
                                   {"listen_address": "192.0.2.255", "host_port": port, "guest_port": 80}]})
        with self.assertRaisesRegex(DeploymentError, "192.0.2.255.*not assigned to the Mac"):
            await worker.run(watch=False)
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)

    async def test_unavailable_mac_ip_identifies_the_rule_and_recovery(self):
        for protocol in ("tcp", "udp"):
            with self.subTest(protocol=protocol):
                worker = Worker({"target": "127.0.0.1", "token": "token",
                                 "control": str(Path(self.directory.name) / "socket"),
                                 "rules": [{"listen_address": "192.0.2.255", "host_port": 8443,
                                            "guest_port": 443, "protocol": protocol}]})
                with self.assertRaisesRegex(DeploymentError, rf"{protocol.upper()} 192.0.2.255:8443.*not assigned to the Mac.*Configure"):
                    await worker.run(watch=False)
                self.assertFalse(worker.ready)

    async def test_occupied_port_identifies_the_conflict(self):
        for protocol, kind in (("tcp", socket.SOCK_STREAM), ("udp", socket.SOCK_DGRAM)):
            with self.subTest(protocol=protocol), socket.socket(socket.AF_INET, kind) as occupied:
                occupied.bind(("127.0.0.1", 0))
                if protocol == "tcp":
                    occupied.listen()
                port = occupied.getsockname()[1]
                worker = Worker({"target": "127.0.0.1", "token": "token",
                                 "control": str(Path(self.directory.name) / "socket"),
                                 "rules": [{"listen_address": "127.0.0.1", "host_port": port,
                                            "guest_port": 443, "protocol": protocol}]})
                with self.assertRaisesRegex(DeploymentError, rf"{protocol.upper()} 127.0.0.1:{port}.*already in use"):
                    await worker.run(watch=False)

    async def test_interface_forwarding_recovers_when_the_mac_interface_returns(self):
        port = free_port()
        rules = [{"listen_interface": "en0", "listen_address": "192.0.2.255", "host_port": port, "guest_port": 443}]
        with patch("appletart.forwarder.interface_address", return_value="127.0.0.1"):
            await self.launch(rules)
            self.assertEqual(self.worker.bound_addresses[0], "127.0.0.1")
        with patch("appletart.forwarder.interface_address", side_effect=DeploymentError("Interface is offline")):
            await self.worker.refresh_interfaces()
        self.assertFalse(self.worker.ready)
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
        with patch("appletart.forwarder.interface_address", return_value="127.0.0.1"):
            await self.worker.refresh_interfaces()
        self.assertTrue(self.worker.ready)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()


class ForwarderStartupTests(unittest.TestCase):
    def test_failed_attempt_does_not_repeat_previous_log_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "logs" / "vm-forwarding.log"
            log.parent.mkdir()
            log.write_text("previous failure\nprevious failure\n")
            machine = Machine.from_dict({"name": "vm", "port_forwards": [{"host_port": 8443, "guest_port": 443}]})
            backend = Mock(binary="fake-tart")
            backend.run.return_value = "127.0.0.1"
            def launch(*args, **kwargs):
                kwargs["stdout"].write("current failure\n")
                kwargs["stdout"].flush()
                return Mock(poll=Mock(return_value=1))
            with patch("appletart.forwarder.subprocess.Popen", side_effect=launch), \
                 self.assertRaisesRegex(DeploymentError, "Cannot start port forwarding: current failure$"):
                start_forwarder(root, machine, backend, {})
