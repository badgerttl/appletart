"""Regressions for the current-source lifecycle and networking review."""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import lzma
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart import guest_access, image_catalog
from appletart.catalog import Machine
from appletart.cloud import convert_disk
from appletart.connectivity import PortForward
from appletart.deployment import DeploymentError, VM
from appletart.downloads import digest, fetch_image
from appletart.forwarder import UDPListener
from appletart.lifecycle import Lifecycle
from appletart.ssh import known_hosts_option, ssh_install
from test_lifecycle import FakeBackend
from test_ssh import PUBLIC_KEY


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.backend = FakeBackend()
        self.api = Lifecycle(self.root / "data", lambda report=print: self.backend)
        self.report = Mock()
        for target in ("appletart.lifecycle.guest_agent.prepare", "appletart.lifecycle.provision_keys"):
            stub = patch(target)
            stub.start()
            self.addCleanup(stub.stop)

    def test_initial_setup_uses_configured_identity_and_private_host_key_file(self):
        public = self.root / "custom.pub"
        public.with_suffix("").write_text("identity fixture; never read")
        known = self.root / "known_hosts"
        vm = VM.from_dict({"name": "custom", "os": "ubuntu", "ssh_public_keys": [str(public)]})
        with patch("appletart.ssh.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as command:
            ssh_install("192.0.2.10", vm, [], batch=True, known_hosts=known)
        args = command.call_args.args[0]
        self.assertEqual(args[args.index("-i") + 1], str(public.with_suffix("")))
        self.assertIn(f"UserKnownHostsFile={known}", args)
        self.assertIn("StrictHostKeyChecking=accept-new", args)
        self.assertEqual(known.stat().st_mode & 0o777, 0o600)

    def test_destroy_recreate_tart_and_iso_get_fresh_host_key_files(self):
        iso = self.root / "installer.iso"
        iso.write_bytes(b"installer fixture")
        for kind in ("tart", "iso"):
            with self.subTest(kind=kind):
                machine = self.api.machine({"name": kind, **({"source_kind": "iso", "source": str(iso)} if kind == "iso" else {})})
                with patch("appletart.lifecycle.provision_keys") as provision:
                    self.api.build(machine, self.report)
                    if kind == "iso":
                        self.api.finish_installation(kind, self.report)
                known = self.api.store.root / "cloud-init" / kind / "known_hosts"
                self.assertEqual(provision.call_args.kwargs["known_hosts"], known)
                known.write_text(f"appletart-{kind} {PUBLIC_KEY}\n")
                self.api.destroy(kind, kind, self.report)
                self.assertFalse(known.exists())
                self.api.build(machine, self.report)
                if kind == "iso":
                    self.api.finish_installation(kind, self.report)
                self.assertEqual(known.read_text(), "")

    def test_existing_template_trust_migrates_without_changing_global_known_hosts(self):
        home = self.root / "home"
        global_known = home / ".ssh" / "known_hosts"
        global_known.parent.mkdir(parents=True)
        content = f"appletart-legacy {PUBLIC_KEY}\nother-vm {PUBLIC_KEY}\n"
        global_known.write_text(content)
        machine = Machine.from_dict({"name": "legacy"})
        with patch("appletart.ssh.Path.home", return_value=home):
            args = guest_access.ssh_args(self.api.store.root, machine, "192.0.2.10", batch=True)
        known = self.api.store.root / "cloud-init" / "legacy" / "known_hosts"
        self.assertIn(f"UserKnownHostsFile={known}", args)
        self.assertEqual(known.read_text(), f"appletart-legacy {PUBLIC_KEY}\n")
        self.assertEqual(global_known.read_text(), content)
        self.assertIn("StrictHostKeyChecking=yes", args)

    def test_hashed_legacy_host_key_aliases_migrate(self):
        home = self.root / "home"
        legacy = home / ".ssh" / "known_hosts"
        legacy.parent.mkdir(parents=True)
        salt = b"legacy trust fixture"
        hashed = hmac.new(salt, b"appletart-legacy", hashlib.sha1).digest()
        alias = "|1|" + base64.b64encode(salt).decode() + "|" + base64.b64encode(hashed).decode()
        content = f"{alias} {PUBLIC_KEY}\n"
        legacy.write_text(content)
        with patch("appletart.ssh.Path.home", return_value=home):
            guest_access.ssh_args(self.api.store.root, Machine.from_dict({"name": "legacy"}), "192.0.2.10", batch=True)
        known = self.api.store.root / "cloud-init" / "legacy" / "known_hosts"
        self.assertEqual(known.read_text(), content)
        self.assertEqual(legacy.read_text(), content)

    def test_openssh_accepts_host_key_files_under_paths_with_spaces(self):
        known = self.root / "Application Support" / "known hosts"
        result = subprocess.run(["ssh", "-G", "-F", "/dev/null", "-o", known_hosts_option(known), "192.0.2.10"],
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertIn(f"userknownhostsfile {known}", result.stdout.splitlines())

    def test_tart_provisioning_retry_keeps_its_mac_after_successful_hardware_setup(self):
        machine = self.api.machine({"name": "retry"})
        with patch("appletart.lifecycle.provision_keys", side_effect=DeploymentError("package failure")):
            with self.assertRaisesRegex(DeploymentError, "package failure"):
                self.api.build(machine, self.report)
        # Recovery also works after restarting the dashboard process.
        restored = Lifecycle(self.api.store.root, lambda report=print: self.backend)
        restored.build(machine, self.report)
        self.assertEqual(sum(args[0] == "clone" for args in self.backend.calls), 1)
        self.assertEqual(sum("--random-mac" in args for args in self.backend.calls), 1)
        self.assertEqual(restored.store.get("retry")["phase"], "ready")

    def test_uppercase_local_compressed_images_are_decompressed_before_conversion(self):
        payload = b"disk fixture"
        for extension in ("TAR.XZ", "QCOW2.XZ"):
            with self.subTest(extension=extension):
                source = self.root / f"source.{extension}"
                if extension == "TAR.XZ":
                    with tarfile.open(source, "w:xz") as archive:
                        member = tarfile.TarInfo("disk.raw")
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))
                else:
                    source.write_bytes(lzma.compress(payload))
                machine = Machine.from_dict({"name": "compressed", "os": "other", "source_kind": "cloud", "source": str(source), "ssh_public_keys": [str(self.root / "user.pub")]})
                artifact = fetch_image(machine.source, "", self.root / "downloads", self.report)
                def qemu(args):
                    if args[1] == "info":
                        return json.dumps({"virtual-size": len(payload)})
                    Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
                    return ""
                with patch("appletart.cloud.run_tool", side_effect=qemu):
                    raw = convert_disk(artifact, self.root / extension, 40, self.report)
                self.assertEqual(raw.read_bytes(), payload)

    def test_listing_survives_a_record_removed_after_directory_enumeration(self):
        machine = self.api.machine({"name": "vanishing"})
        self.api.store.put({"config": machine.config(), "phase": "downloaded", "owned": False})
        original = self.api.store.get
        def removed(name):
            self.api.store.remove(name)
            return original(name)
        with patch.object(self.api.store, "get", side_effect=removed):
            self.assertEqual(self.api.listing()["machines"], [])

    def test_previously_cached_compressed_bytes_are_reconverted(self):
        source = self.root / "source.QCOW2.XZ"
        source.write_bytes(lzma.compress(b"disk fixture"))
        cache = self.root / "converted"
        cache.mkdir()
        raw = cache / (digest(source) + ".disk.img")
        raw.write_bytes(source.read_bytes())
        raw.with_suffix(".sha256").write_text(digest(raw) + "\n")
        def qemu(args):
            if args[1] == "info":
                return json.dumps({"virtual-size": 12})
            Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
            return ""
        with patch("appletart.cloud.run_tool", side_effect=qemu):
            self.assertEqual(convert_disk(source, cache, 40, self.report).read_bytes(), b"disk fixture")

    def test_catalog_without_default_source_supports_an_explicit_source(self):
        catalog = {"custom": {"label": "Custom", "family": "linux", "source_kind": "cloud"}}
        self.api.save_catalog(catalog)
        machine = self.api.machine({"name": "explicit", "os": "custom", "source": str(self.root / "disk.img"), "ssh_public_keys": [str(self.root / "user.pub")]})
        self.assertEqual(machine.source, str((self.root / "disk.img").resolve()))
        self.assertEqual(image_catalog.load(self.api.store.root)["custom"]["source"], "")
        with self.assertRaises(DeploymentError):
            self.api.machine({"name": "missing", "os": "custom"})

    def test_display_choice_survives_stop_start_restart_and_dashboard_restart(self):
        for platform, headless in (("macos", True), ("ubuntu", False)):
            with self.subTest(platform=platform):
                machine = self.api.machine({"name": platform, "os": platform})
                self.api.build(machine, self.report)
                commands = []
                def launch(args, **kwargs):
                    commands.append(args)
                    self.backend.vms[platform]["Running"] = True
                    return Mock(poll=Mock(return_value=None))
                with patch("appletart.lifecycle.subprocess.Popen", side_effect=launch):
                    self.api.start(platform, self.report, headless=headless)
                    self.api.stop(platform, self.report)
                    reopened = Lifecycle(self.api.store.root, lambda report=print: self.backend)
                    reopened.start(platform, self.report)
                    with patch.object(reopened, "shutdown", side_effect=lambda *args: self.backend.vms[platform].update(Running=False)):
                        reopened.restart(platform, self.report)
                self.assertEqual(["--no-graphics" in args for args in commands], [headless] * 3)


class UDPStartupRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_initial_udp_burst_is_forwarded_in_order_after_endpoint_creation(self):
        tasks = []
        worker = Mock(target="192.0.2.10")
        worker.task.side_effect = lambda awaitable: tasks.append(asyncio.create_task(awaitable))
        listener = UDPListener(worker, PortForward.from_dict({"host_port": 8443, "guest_port": 443, "protocol": "udp"}))
        listener.connection_made(Mock())
        transport = Mock()
        endpoint_ready = asyncio.Event()
        async def endpoint(*args, **kwargs):
            await endpoint_ready.wait()
            return transport, Mock()
        with patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=endpoint):
            for packet in (b"one", b"two", b"three"):
                listener.datagram_received(packet, ("192.0.2.1", 1234))
            endpoint_ready.set()
            await asyncio.gather(*tasks)
            listener.datagram_received(b"four", ("192.0.2.1", 1234))
        self.assertEqual([call.args[0] for call in transport.sendto.call_args_list], [b"one", b"two", b"three", b"four"])

    async def test_pending_udp_burst_is_bounded_by_packet_count_and_bytes(self):
        for packet_size, count in ((1, 64), (4096, 16)):
            with self.subTest(packet_size=packet_size):
                tasks = []
                worker = Mock(target="192.0.2.10")
                worker.task.side_effect = lambda awaitable: tasks.append(asyncio.create_task(awaitable))
                listener = UDPListener(worker, PortForward.from_dict({"host_port": 8443, "guest_port": 443, "protocol": "udp"}))
                transport = Mock()
                ready = asyncio.Event()
                async def endpoint(*args, **kwargs):
                    await ready.wait()
                    return transport, Mock()
                with patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=endpoint):
                    for _ in range(100):
                        listener.datagram_received(b"x" * packet_size, ("192.0.2.1", 1234))
                    ready.set()
                    await asyncio.gather(*tasks)
                self.assertEqual(transport.sendto.call_count, count)
                self.assertEqual(len(tasks), 1)

    async def test_closing_listener_during_udp_setup_closes_the_late_endpoint(self):
        tasks = []
        worker = Mock(target="192.0.2.10")
        worker.task.side_effect = lambda awaitable: tasks.append(asyncio.create_task(awaitable))
        listener = UDPListener(worker, PortForward.from_dict({"host_port": 8443, "guest_port": 443, "protocol": "udp"}))
        transport = Mock()
        ready = asyncio.Event()
        async def endpoint(*args, **kwargs):
            await ready.wait()
            return transport, Mock()
        with patch.object(asyncio.get_running_loop(), "create_datagram_endpoint", side_effect=endpoint):
            listener.datagram_received(b"queued", ("192.0.2.1", 1234))
            listener.expire(all_clients=True)
            ready.set()
            await asyncio.gather(*tasks)
        transport.close.assert_called_once()
        transport.sendto.assert_not_called()
        self.assertEqual(listener.clients, {})
