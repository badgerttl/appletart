import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from appletart import guest_agent
from appletart.deployment import DeploymentError
from appletart.ip_resolver import address
from appletart.operations import JobCancelled


class GuestAgentTests(unittest.TestCase):
    def test_installer_starts_agent_with_an_executable_selinux_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = root / "commands"
            commands.mkdir()
            (root / "etc/systemd/system").mkdir(parents=True)
            def command(name, body):
                path = commands / name
                path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
                path.chmod(0o755)
            command("sudo", 'if [ "$1" = "-n" ]; then shift; fi\nexec "$@"')
            command("sha256sum", 'exec shasum -a 256 "$@"')
            command("restorecon", '''for path in "$@"; do
case "$path" in */usr/local/bin/*) echo bin_t > "$path.label" ;; esac
done''')
            # Model RHEL's denial when systemd executes a lib_t file as init_t.
            # File installation and checksum verification use the real script.
            command("systemctl", r'''case "$1" in
restart)
    binary=$(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' "$APPLETART_FIXTURE_ROOT/etc/systemd/system/appletart-guest-agent.service")
    if [ ! -f "$binary.label" ] || [ "$(cat "$binary.label")" != bin_t ]; then
        echo 'AF_VSOCK permission denied: executable has a library label' >&2
        exit 1
    fi ;;
esac''')
            script = guest_agent.install_script(b"verified agent fixture")
            for prefix in ("/usr/local", "/etc/systemd/system"):
                script = script.replace(prefix, str(root) + prefix)
            result = subprocess.run(["sh", "-s"], input=script, text=True, capture_output=True, timeout=5,
                                    env={**os.environ, "PATH": str(commands) + ":" + os.environ["PATH"],
                                         "APPLETART_FIXTURE_ROOT": str(root)})
            self.assertEqual(result.returncode, 0, result.stderr)
            service = (root / "etc/systemd/system/appletart-guest-agent.service").read_text()
            self.assertIn("User=root\n", service)
            self.assertIn("--run-rpc\n", service)
            self.assertNotIn("--exec-wrapper", service)
            self.assertNotIn("ProtectHome=true", service)
            self.assertNotIn("NoNewPrivileges=true", service)

    def test_root_probe_requires_success_and_uid_zero(self):
        backend = Mock(binary="fake-tart")
        for code, uid in ((0, "0\n"), (0, "65534"), (1, "0")):
            with self.subTest(code=code, uid=uid), patch("appletart.guest_agent.run", return_value=subprocess.CompletedProcess([], code, uid, "")) as run:
                self.assertEqual(guest_agent.root_available(backend, "dev"), code == 0 and uid.strip() == "0")
                self.assertEqual(run.call_args.args[0], ["fake-tart", "exec", "dev", "/usr/bin/id", "-u"])
                self.assertEqual(run.call_args.kwargs["timeout"], 8)

    def test_unavailable_probe_falls_back_but_cancellation_propagates(self):
        backend = Mock(binary="fake-tart")
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("tart", 8)):
            with patch("appletart.guest_agent.run", side_effect=error):
                self.assertFalse(guest_agent.root_available(backend, "dev"))
        with patch("appletart.guest_agent.run", side_effect=JobCancelled("cancelled")), self.assertRaises(JobCancelled):
            guest_agent.root_available(backend, "dev")

    def test_archive_accepts_only_a_regular_linux_arm64_executable(self):
        binary = b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00" + bytes(20)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "agent.tar.gz"
            for kind, data in ((tarfile.REGTYPE, binary), (tarfile.SYMTYPE, binary), (tarfile.REGTYPE, b"invalid executable")):
                with tarfile.open(archive, "w:gz") as target:
                    info = tarfile.TarInfo("tart-guest-agent")
                    info.type = kind; info.size = len(data) if kind == tarfile.REGTYPE else 0
                    target.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
                with patch("appletart.guest_agent.fetch_image", return_value=archive) as fetch:
                    if kind == tarfile.REGTYPE and data == binary:
                        self.assertEqual(guest_agent.prepare(Path(directory), Mock()), binary)
                    else:
                        with self.assertRaises(DeploymentError):
                            guest_agent.prepare(Path(directory), Mock())
                self.assertEqual(fetch.call_args.args[1], guest_agent.SHA256)

    def test_resolver_rejects_unusable_and_invalid_addresses(self):
        for value in ("", "garbage", "0.0.0.0", "127.0.0.1", "169.254.0.1", "224.0.0.1", "255.255.255.255", "::1", "192.0.2.10\n192.0.2.11"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                address(value)
        self.assertEqual(address("192.0.2.10\n"), "192.0.2.10")


if __name__ == "__main__":
    unittest.main()

class ExistingTemplateAgentTests(unittest.TestCase):
    def test_installer_hands_rpc_from_unprivileged_vendor_service_to_root_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); commands = root / 'commands'; commands.mkdir()
            (root / 'etc/systemd/system').mkdir(parents=True)
            (root / 'vendor-listening').touch()
            def command(name, body):
                path = commands / name; path.write_text('#!/bin/sh\nset -eu\n' + body + '\n'); path.chmod(0o755)
            command('sudo', 'if [ "$1" = -n ]; then shift; fi\nexec "$@"')
            command('sha256sum', 'exec shasum -a 256 "$@"')
            command('systemctl', '''case "$1" in
cat) test "$2" = tart-guest-agent.service; echo 'ExecStart=tart-guest-agent --run-rpc' ;;
stop) test "$2" = tart-guest-agent.service; rm -f "$APPLETART_FIXTURE_ROOT/vendor-listening" ;;
disable) test "$2" = tart-guest-agent.service; touch "$APPLETART_FIXTURE_ROOT/vendor-disabled" ;;
restart) if test -e "$APPLETART_FIXTURE_ROOT/vendor-listening"; then echo 'AF_VSOCK port 8080: address already in use' >&2; exit 1; fi ;;
esac''')
            script = guest_agent.install_script(b'verified guest-agent fixture')
            for prefix in ('/usr/local', '/etc/systemd/system'):
                script = script.replace(prefix, str(root) + prefix)
            result = subprocess.run(['sh','-s'], input=script, text=True, capture_output=True, timeout=5,
                                    env={**os.environ,'PATH':str(commands)+':/usr/bin:/bin','APPLETART_FIXTURE_ROOT':str(root)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'vendor-disabled').exists())
            self.assertFalse((root / 'vendor-listening').exists())
            service = (root / 'etc/systemd/system/appletart-guest-agent.service').read_text()
            self.assertIn('User=root', service)
