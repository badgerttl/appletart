import argparse
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import sys

from .deployment import DeploymentError, load_manifest, vm_name
from .tart import Tart
from .ssh import read_public_keys
from .catalog import Machine
from .lifecycle import Lifecycle


def data_dir() -> Path:
    return Path(os.environ.get("APPLETART_HOME", str(Path.home() / "Library" / "Application Support" / "AppleTart"))).expanduser()


def name_arg(value: str) -> str:
    try:
        return vm_name(value)
    except DeploymentError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def wait_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("wait must be an integer") from error
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("wait must be between 0 and 65535 seconds")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Manage Linux and macOS VMs with the AppleTart browser wizard.")
    result.add_argument("--version", action="version", version="appletart 0.2.0")
    commands = result.add_subparsers(dest="command", required=True)
    for action in ("plan", "deploy"):
        command = commands.add_parser(action, help="Preview commands without changes" if action == "plan" else "Clone and configure all VMs")
        command.add_argument("manifest", type=Path)
    ui = commands.add_parser("ui", help="Open the local browser wizard and VM dashboard")
    ui.add_argument("--data-dir", type=Path, default=data_dir())
    ui.add_argument("--port", type=int, default=4991, help="Dashboard port (default: 4991; 0 selects an available port)")
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--foreground", action="store_true", help="Keep the dashboard attached to this terminal")
    ui.add_argument("--ready-file", type=Path, help=argparse.SUPPRESS)
    service = commands.add_parser("service", help="Start, stop, restart, or inspect the background dashboard")
    service.add_argument("action", choices=["start", "stop", "restart", "status"])
    service.add_argument("--data-dir", type=Path, default=data_dir())
    service.add_argument("--port", type=int, default=4991)
    service.add_argument("--no-browser", action="store_true")
    for action in ("download", "build", "create"):
        command = commands.add_parser(action, help={"download": "Download a source image", "build": "Build and prepare a VM", "create": "Download, build and start a VM"}[action])
        command.add_argument("name", type=name_arg)
        command.add_argument("--data-dir", type=Path, default=data_dir())
        command.add_argument("--os", default="ubuntu", help="Platform identifier from the editable image catalog")
        command.add_argument("--size", choices=["small", "medium", "large"], default="small")
        command.add_argument("--cpu", type=int)
        command.add_argument("--memory-mb", type=int)
        command.add_argument("--disk-gb", type=int)
        command.add_argument("--network", choices=["nat", "bridged"], default="nat")
        command.add_argument("--bridge")
        command.add_argument("--extra-bridge", action="append", default=[])
        command.add_argument("--forward", action="append", default=[], metavar="IP_OR_INTERFACE:HOST:VM[/tcp|udp]", help="Forward to the VM NAT port; a Mac interface such as en0 follows IP changes")
        command.add_argument("--share", action="append", default=[], metavar="HOST:VM[:ro|rw]", help="Share a local directory, read-only by default")
        command.add_argument("--source-kind", choices=["tart", "iso", "cloud", "golden"])
        command.add_argument("--source")
        command.add_argument("--sha256")
        command.add_argument("--sha512")
        command.add_argument("--ssh-user")
        command.add_argument("--ssh-public-key", action="append")
        command.add_argument("--hostname")
        command.add_argument("--package", action="append", dest="packages")
        command.add_argument("--bundle", action="append", dest="software_bundles", default=[], help="Named software bundle identifier")
        command.add_argument("--refresh-source", action="store_true", help="Download a remote cloud image or ISO again")
        command.add_argument("--no-ssh", action="store_true")
        command.add_argument("--ask-password", action="store_true")
        command.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None,
                             help="Headless startup (default for Linux); use --no-headless for a display")
    for action in ("start", "finish", "destroy", "machines", "agent", "shutdown", "restart", "force-stop", "health", "connect", "checkpoints", "checkpoint", "restore", "delete-checkpoint"):
        command = commands.add_parser(action, help="Manage a wizard-created VM" if action != "machines" else "List wizard records and Tart VM states")
        command.add_argument("--data-dir", type=Path, default=data_dir())
        if action != "machines":
            command.add_argument("name", type=name_arg)
        if action == "destroy":
            command.add_argument("--confirm-name", required=True)
        if action == "start":
            command.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None,
                                 help="Headless startup (default for Linux); macOS opens a display by default")
        if action == "finish":
            command.add_argument("--ask-password", action="store_true")
        if action == "health":
            command.add_argument("--details", action="store_true")
        if action == "connect":
            command.add_argument("--terminal", action="store_true")
        if action == "checkpoint":
            command.add_argument("--label", required=True)
            command.add_argument("--notes", default="")
        if action in ("restore", "delete-checkpoint"):
            command.add_argument("--checkpoint-id", type=name_arg, required=True)
            command.add_argument("--confirm", required=True)
    commands.add_parser("doctor", help="Check host requirements and Tart availability")
    golden = commands.add_parser("golden", help="Save a stopped cloud VM as a reusable golden image")
    golden.add_argument("name", type=name_arg)
    golden.add_argument("--image-name", type=name_arg, required=True)
    golden.add_argument("--data-dir", type=Path, default=data_dir())
    golden.add_argument("--image-version", default="1")
    golden.add_argument("--notes", default="")
    storage = commands.add_parser("storage", help="Inspect managed storage or remove selected unused items")
    storage.add_argument("--data-dir", type=Path, default=data_dir())
    storage.add_argument("--cleanup", action="append", metavar="ITEM_ID")
    storage.add_argument("--confirm", default="")
    profiles = commands.add_parser("profiles", help="List reusable deployment profiles")
    profiles.add_argument("--data-dir", type=Path, default=data_dir())
    settings = commands.add_parser("settings", help="Show settings or select the default SSH terminal")
    settings.add_argument("--data-dir", type=Path, default=data_dir())
    settings.add_argument("--terminal", choices=["terminal", "iterm2", "warp"])
    for action in ("profile-save", "profile-import", "profile-export", "profile-delete"):
        command = commands.add_parser(action)
        command.add_argument("--data-dir", type=Path, default=data_dir())
        if action != "profile-import":
            command.add_argument("name", type=name_arg)
        if action == "profile-save":
            command.add_argument("--from-vm", type=name_arg, required=True)
            command.add_argument("--notes", default="")
        if action in ("profile-import", "profile-export"):
            command.add_argument("path", type=Path)
        if action == "profile-delete":
            command.add_argument("--confirm-name", required=True)
    images = commands.add_parser("images", help="List saved golden images")
    images.add_argument("--data-dir", type=Path, default=data_dir())
    listing = commands.add_parser("list", help="List local Tart VMs")
    listing.add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="Run a VM in the foreground")
    run.add_argument("name", type=name_arg)
    run.add_argument("--headless", action="store_true", default=True)
    run.add_argument("--bridge", help="Bridge to a host interface; defaults to NAT")
    stop = commands.add_parser("stop", help="Stop a VM through Tart")
    stop.add_argument("name", type=name_arg)
    ip = commands.add_parser("ip", help="Resolve a VM's IP address")
    ip.add_argument("name", type=name_arg)
    ip.add_argument("--wait", type=wait_arg, default=60)
    ip.add_argument("--resolver", choices=["dhcp", "arp", "agent"], default="dhcp")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(arguments or ["ui"])
    try:
        if args.command == "ui":
            if args.foreground:
                from .web import serve
                serve(args.data_dir, args.port, open_browser=not args.no_browser, ready_file=args.ready_file)
            else:
                from .service import DashboardService
                DashboardService(args.data_dir).start(args.port, open_browser=not args.no_browser)
        elif args.command == "service":
            from .service import DashboardService
            service = DashboardService(args.data_dir)
            if args.action == "status":
                print(json.dumps(service.status(), indent=2))
            else:
                if args.action in ("stop", "restart"):
                    service.stop()
                if args.action in ("start", "restart"):
                    service.start(args.port, open_browser=not args.no_browser)
        elif args.command == "golden":
            Lifecycle(args.data_dir).create_golden(args.name, args.image_name, version=args.image_version, notes=args.notes)
        elif args.command == "storage":
            lifecycle = Lifecycle(args.data_dir)
            if args.cleanup:
                lifecycle.cleanup_storage(args.cleanup, args.confirm)
            else:
                print(json.dumps(lifecycle.storage_listing(), indent=2))
        elif args.command == "profiles":
            print(json.dumps(Lifecycle(args.data_dir).profiles.all(), indent=2))
        elif args.command == "settings":
            lifecycle = Lifecycle(args.data_dir)
            print(json.dumps(lifecycle.save_settings({"terminal": args.terminal}) if args.terminal else lifecycle.settings(), indent=2))
        elif args.command.startswith("profile-"):
            lifecycle = Lifecycle(args.data_dir)
            if args.command == "profile-save":
                record, _ = lifecycle._managed(args.from_vm)
                lifecycle.save_profile(args.name, record["config"], args.notes)
            elif args.command == "profile-import":
                if args.path.stat().st_size > 65536:
                    raise DeploymentError("Profile JSON must be under 64 KB.")
                try:
                    lifecycle.import_profile(json.loads(args.path.read_text()))
                except ValueError as error:
                    raise DeploymentError("Profile JSON is invalid.") from error
            elif args.command == "profile-export":
                profile = lifecycle.profiles.get(args.name)
                if not profile:
                    raise DeploymentError("This profile does not exist.")
                with args.path.open("x") as output:
                    output.write(json.dumps(profile, indent=2) + "\n")
            else:
                lifecycle.delete_profile(args.name, args.confirm_name)
        elif args.command == "images":
            print(json.dumps(Lifecycle(args.data_dir).image_listing(), indent=2))
        elif args.command in ("download", "build", "create"):
            fields = {key: getattr(args, key) for key in ("name", "os", "size", "cpu", "memory_mb", "disk_gb", "network", "bridge", "source_kind", "source", "sha256", "sha512", "ssh_user", "hostname", "packages") if getattr(args, key) is not None}
            lifecycle = Lifecycle(args.data_dir)
            preferences = lifecycle.settings()
            default_keys = [preferences["default_public_key"]] if preferences["install_ssh_key_by_default"] and preferences["default_public_key"] else []
            fields["ssh_public_keys"] = [] if args.no_ssh else (args.ssh_public_key or default_keys)
            if args.extra_bridge:
                fields["bridges"] = ([args.bridge] if args.bridge else []) + args.extra_bridge
            fields["software_bundles"] = args.software_bundles
            fields["refresh_source"] = args.refresh_source
            fields["port_forwards"] = []
            for value in args.forward:
                ports, _, protocol = value.partition("/")
                parts = ports.split(":")
                if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                    raise DeploymentError("--forward uses MAC_IP:HOST_PORT:VM_PORT[/tcp|udp].")
                listener = {"listen_interface": parts[0]} if re.fullmatch(r"(?:en|bridge)\d+", parts[0]) else {"listen_address": parts[0]}
                fields["port_forwards"].append({**listener, "host_port": int(parts[1]), "guest_port": int(parts[2]), "protocol": protocol or "tcp"})
            fields["directory_shares"] = []
            for value in args.share:
                parts = value.split(":")
                if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] not in ("ro", "rw")):
                    raise DeploymentError("--share uses LOCAL_PATH:GUEST_PATH[:ro|rw].")
                fields["directory_shares"].append({"host_path": parts[0], "guest_path": parts[1], "read_only": len(parts) == 2 or parts[2] == "ro"})
            machine = lifecycle.machine(fields)
            password = getpass.getpass("Existing guest password: ") if args.ask_password else ""
            if args.command == "download":
                lifecycle.download(machine)
            else:
                lifecycle.build(machine, password=password)
                if args.command == "create":
                    lifecycle.start(machine.vm.name, headless=args.headless)
        elif args.command in ("start", "finish", "destroy", "machines", "agent", "shutdown", "restart", "force-stop", "health", "connect", "checkpoints", "checkpoint", "restore", "delete-checkpoint"):
            lifecycle = Lifecycle(args.data_dir)
            if args.command == "start":
                lifecycle.start(args.name, headless=args.headless)
            elif args.command == "finish":
                password = getpass.getpass("Existing guest password: ") if args.ask_password else ""
                lifecycle.finish_installation(args.name, password=password)
            elif args.command == "destroy":
                lifecycle.destroy(args.name, args.confirm_name)
            elif args.command == "agent":
                lifecycle.install_guest_agent(args.name)
            elif args.command in ("shutdown", "restart", "force-stop"):
                getattr(lifecycle, args.command.replace("-", "_"))(args.name)
            elif args.command == "health":
                print(json.dumps(lifecycle.health(args.name, details=args.details), indent=2))
            elif args.command == "connect":
                print(lifecycle.connection(args.name, terminal=args.terminal)["command"])
            elif args.command == "checkpoints":
                print(json.dumps(lifecycle.checkpoints(args.name), indent=2))
            elif args.command == "checkpoint":
                lifecycle.create_checkpoint(args.name, args.label, args.notes)
            elif args.command == "restore":
                lifecycle.restore_checkpoint(args.name, args.checkpoint_id, args.confirm)
            elif args.command == "delete-checkpoint":
                lifecycle.delete_checkpoint(args.name, args.checkpoint_id, args.confirm)
            else:
                print(json.dumps(lifecycle.listing(), indent=2))
        elif args.command in ("plan", "deploy"):
            vms = load_manifest(args.manifest)
            if args.command == "plan":
                keys = {vm.name: read_public_keys(vm.ssh_public_keys) for vm in vms}
                for vm in vms:
                    for command in vm.commands():
                        print(shlex.join(["tart", *command]))
                    if keys[vm.name]:
                        print(f"# Boot {vm.name}, install {len(keys[vm.name])} public key(s) for {vm.ssh_user} via SSH, then stop the VM.")
                return 0
            Tart().deploy(vms)
        elif args.command == "doctor":
            tart = Tart()
            print(f"Host supported. Tart: {tart.binary}")
            print(tart.run(["--version"], capture=True).strip())
        elif args.command == "list":
            items = Tart().inventory()
            if args.json:
                print(json.dumps(items, indent=2))
            else:
                for item in items:
                    print(f"{item['Name']}\t{item.get('State', 'Running' if item['Running'] else 'Stopped')}")
        elif args.command == "run":
            Tart().run(["run", args.name, *(["--no-graphics"] if args.headless else []), *(["--net-bridged", args.bridge] if args.bridge else [])])
        elif args.command == "stop":
            Tart().run(["stop", args.name])
        elif args.command == "ip":
            print(Tart().run(["ip", args.name, "--wait", str(args.wait), "--resolver", args.resolver], capture=True).strip())
        return 0
    except DeploymentError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Inspect VM state with appletart list.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
