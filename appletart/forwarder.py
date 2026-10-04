"""Managed TCP/UDP listeners: Mac IP:port -> guest NAT IP:port."""

import asyncio
import errno
from dataclasses import replace
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

from .connectivity import PortForward
from .deployment import DeploymentError, vm_name
from .host_network import interface_address


def resolve_rule(rule):
    if not rule.listen_interface:
        return rule
    try:
        return replace(rule, listen_address=interface_address(rule.listen_interface), listen_interface="")
    except DeploymentError as error:
        raise DeploymentError(f"Cannot listen on {rule.protocol.upper()} {rule.listen_interface}:{rule.host_port}: {error}") from error


def bind_listener(rule):
    """Bind explicitly so asyncio cannot discard the kernel's failure reason."""
    listener = None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM if rule.protocol == "tcp" else socket.SOCK_DGRAM)
        if rule.protocol == "tcp":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((rule.listen_address, rule.host_port))
        if rule.protocol == "tcp":
            listener.listen(100)
        listener.setblocking(False)
        return listener
    except OSError as error:
        if listener:
            listener.close()
        if error.errno == errno.EADDRNOTAVAIL:
            reason = ("this IP is not assigned to the Mac. Open Configure and select a current Mac listen IP; "
                      "the VM does not need rebuilding.")
        elif error.errno == errno.EADDRINUSE:
            reason = "this Mac port is already in use. Choose another host port or stop the service using it."
        elif error.errno in (errno.EACCES, errno.EPERM):
            reason = "permission denied. Choose a higher Mac host port or check listener permissions."
        else:
            reason = str(error)
        raise DeploymentError(f"Cannot listen on {rule.protocol.upper()} {rule.listen_address}:{rule.host_port}: {reason}") from error


def check_listeners(rules):
    """Check every listener before booting, releasing all sockets on any failure."""
    listeners = []
    try:
        for rule in rules:
            listeners.append(bind_listener(resolve_rule(rule)))
    finally:
        for listener in listeners:
            listener.close()


def runtime_path(root, name):
    return root / "forwarders" / f"{vm_name(name)}.json"


def control_path(root, name):
    identifier = hashlib.sha256(str(runtime_path(root.resolve(), name)).encode()).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"appletart-{os.getuid()}-{identifier}.sock"


def control(config, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(config["control"])
        connection.sendall(json.dumps({"token": config["token"], "command": command}).encode() + b"\n")
        return connection.recv(1024).decode().strip()


def status(root, name):
    path = runtime_path(root, name)
    if not path.exists():
        return False
    try:
        return control(json.loads(path.read_text()), "status") == "ready"
    except (OSError, ValueError, KeyError):
        return False


def stop(root, name):
    path = runtime_path(root, name)
    if not path.exists():
        return
    try:
        config = json.loads(path.read_text())
        control(config, "stop")
        for _ in range(40):
            try:
                control(config, "status")
            except OSError:
                break
            time.sleep(0.05)
        else:
            raise DeploymentError("The VM's forwarding service did not stop. Check its log before retrying.")
    except (OSError, ValueError, KeyError):
        # A stale record never authorizes sending signals to an unrelated PID.
        pass
    path.unlink(missing_ok=True)


def start(root, machine, backend, identity, report=print):
    if not machine.port_forwards:
        return
    name = machine.vm.name
    stop(root, name)
    address = backend.run(["ip", name, "--wait", "60", "--resolver", "dhcp"], capture=True).strip()
    try:
        ipaddress.IPv4Address(address)
    except ValueError as error:
        raise DeploymentError("Cannot resolve the VM's NAT IPv4 address for port forwarding.") from error
    path = runtime_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = {"name": name, "target": address, "rules": [rule.config() for rule in machine.port_forwards],
              "binary": backend.binary, "identity": identity, "token": secrets.token_urlsafe(32),
              "control": str(control_path(root, name))}
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    log_path = root / "logs" / f"{name}-forwarding.log"
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open("a") as log:
        log_offset = log.tell()
        process = subprocess.Popen([sys.executable, "-m", "appletart.forwarder", str(path)],
                                   stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        if process.poll() is not None:
            with log_path.open("rb") as log:
                log.seek(log_offset)
                detail = log.read()[-1500:].decode(errors="replace").strip()
            detail = detail or "The forwarding process exited without a diagnostic. Check its log."
            path.unlink(missing_ok=True)
            raise DeploymentError("Cannot start port forwarding: " + detail)
        if status(root, name):
            for rule in machine.port_forwards:
                actual = resolve_rule(rule)
                report(f"Forwarding {rule.protocol.upper()} {actual.listen_address}:{rule.host_port} → {address}:{rule.guest_port}" +
                       (f" · follows {rule.listen_interface}" if rule.listen_interface else ""))
            return
        time.sleep(0.05)
    stop(root, name)
    raise DeploymentError("Port forwarding did not confirm startup. Check the forwarding log.")


class UDPReply(asyncio.DatagramProtocol):
    def __init__(self, listener, client):
        self.listener, self.client = listener, client

    def datagram_received(self, data, address):
        session = self.listener.clients.get(self.client)
        if session:
            session[1] = time.monotonic()
            self.listener.transport.sendto(data, self.client)


class UDPListener(asyncio.DatagramProtocol):
    MAX_PENDING_PACKETS = 64
    MAX_PENDING_BYTES = 65536

    def __init__(self, worker, rule):
        self.worker, self.rule = worker, rule
        self.clients, self.pending = {}, {}
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, client):
        session = self.clients.get(client)
        if session:
            session[1] = time.monotonic()
            session[0].sendto(data)
        else:
            queue = self.pending.get(client)
            if queue is None:
                if len(self.clients) + len(self.pending) >= 1024:
                    return
                queue = {"packets": [], "bytes": 0}
                self.pending[client] = queue
                self.worker.task(self.connect(client))
            if len(queue["packets"]) < self.MAX_PENDING_PACKETS and queue["bytes"] + len(data) <= self.MAX_PENDING_BYTES:
                queue["packets"].append(data)
                queue["bytes"] += len(data)

    async def connect(self, client):
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: UDPReply(self, client), remote_addr=(self.worker.target, self.rule.guest_port))
            if client not in self.pending:
                transport.close()
                return
            self.clients[client] = [transport, time.monotonic()]
            for data in self.pending[client]["packets"]:
                transport.sendto(data)
        except OSError as error:
            print(f"UDP destination unavailable: {error}", flush=True)
        finally:
            self.pending.pop(client, None)

    def expire(self, all_clients=False):
        if all_clients:
            self.pending.clear()
        for client, (transport, touched) in list(self.clients.items()):
            if all_clients or time.monotonic() - touched > 60:
                transport.close()
                del self.clients[client]


class Worker:
    def __init__(self, config):
        self.config = config
        self.target = config["target"]
        ipaddress.IPv4Address(self.target)
        self.rules = [PortForward.from_dict(rule) for rule in config["rules"]]
        self.done = asyncio.Event()
        self.tasks, self.listeners, self.udp = set(), [], []
        self.ready = False
        self.active_listeners, self.bound_addresses = {}, {}

    async def open_listener(self, index, rule):
        sock = bind_listener(rule)
        udp = None
        try:
            if rule.protocol == "tcp":
                handle = await asyncio.start_server(lambda reader, writer, rule=rule: self.tcp(reader, writer, rule), sock=sock)
            else:
                udp = UDPListener(self, rule)
                handle, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: udp, sock=sock)
                self.udp.append(udp)
        except BaseException:
            sock.close()
            raise
        self.listeners.append(handle)
        self.active_listeners[index] = (handle, udp)
        self.bound_addresses[index] = rule.listen_address

    async def refresh_interfaces(self):
        addresses = {}
        for index, rule in enumerate(self.rules):
            if not rule.listen_interface:
                continue
            if rule.listen_interface not in addresses:
                try:
                    addresses[rule.listen_interface] = interface_address(rule.listen_interface)
                except DeploymentError:
                    addresses[rule.listen_interface] = None
            address = addresses[rule.listen_interface]
            if address == self.bound_addresses.get(index) and index in self.active_listeners:
                continue
            if index in self.active_listeners:
                handle, udp = self.active_listeners.pop(index)
                handle.close()
                self.listeners.remove(handle)
                if udp:
                    udp.expire(all_clients=True)
                    self.udp.remove(udp)
                self.bound_addresses.pop(index, None)
                print(f"Mac interface {rule.listen_interface} changed; refreshing its forwarding listener.", flush=True)
            if address:
                try:
                    await self.open_listener(index, replace(rule, listen_address=address, listen_interface=""))
                    print(f"Forwarding {rule.protocol.upper()} {address}:{rule.host_port} via {rule.listen_interface}.", flush=True)
                except DeploymentError as error:
                    print(str(error), flush=True)
        self.ready = len(self.active_listeners) == len(self.rules)

    def task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def tcp(self, reader, writer, rule):
        if len(self.tasks) >= 4096:
            writer.close()
            return
        async def relay():
            guest = None
            try:
                incoming, guest = await asyncio.wait_for(asyncio.open_connection(self.target, rule.guest_port), 10)
                async def copy(source, destination):
                    while data := await source.read(65536):
                        destination.write(data)
                        await destination.drain()
                    if destination.can_write_eof():
                        destination.write_eof()
                await asyncio.gather(copy(reader, guest), copy(incoming, writer))
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                if guest:
                    guest.close()
                writer.close()
        await self.task(relay())

    async def handle_control(self, reader, writer):
        try:
            data = json.loads(await asyncio.wait_for(reader.readline(), 2))
            if not secrets.compare_digest(data.get("token", ""), self.config["token"]):
                return
            if data.get("command") == "stop":
                self.done.set()
            writer.write(b"ready\n" if self.ready else b"starting\n")
            await writer.drain()
        except (ValueError, TypeError, OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()

    async def watch(self):
        while not self.done.is_set():
            await asyncio.sleep(3)
            await self.refresh_interfaces()
            for listener in self.udp:
                listener.expire()
            try:
                identity = self.config["identity"]
                stat = (Path(identity["home"]) / "vms" / self.config["name"]).stat()
                if (stat.st_dev, stat.st_ino, getattr(stat, "st_birthtime", None)) != (identity["device"], identity["inode"], identity["birth"]):
                    self.done.set()
                    return
                process = await asyncio.create_subprocess_exec(self.config["binary"], "list", "--source", "local", "--format", "json",
                                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    output, _ = await asyncio.wait_for(process.communicate(), 10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    self.done.set()
                    return
                if process.returncode or not any(item["Name"] == self.config["name"] and item["Running"] for item in json.loads(output)):
                    self.done.set()
            except (OSError, ValueError, KeyError):
                self.done.set()

    async def run(self, *, watch=True):
        control_file = Path(self.config["control"])
        control_file.unlink(missing_ok=True)
        try:
            for index, rule in enumerate(self.rules):
                await self.open_listener(index, resolve_rule(rule))
            server = await asyncio.start_unix_server(self.handle_control, path=control_file, limit=2048)
            self.listeners.append(server)
            control_file.chmod(0o600)
            self.ready = True
            if watch:
                self.task(self.watch())
            await self.done.wait()
        finally:
            self.ready = False
            for listener in self.listeners:
                listener.close()
            for listener in self.udp:
                listener.expire(all_clients=True)
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            for listener in self.listeners:
                if isinstance(listener, asyncio.Server):
                    await listener.wait_closed()
            control_file.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        asyncio.run(Worker(json.loads(Path(sys.argv[1]).read_text())).run())
    except (DeploymentError, OSError, ValueError) as error:
        print(str(error), flush=True)
        sys.exit(1)
