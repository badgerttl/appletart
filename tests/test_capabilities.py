import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import json
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart import capabilities, guest_agent, guest_access, software
from appletart.catalog import Machine
from appletart.cloud import completed_cloud_status
from appletart.deployment import DeploymentError
from appletart.ssh import wait_for_ssh, provision_keys


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commands = self.root / 'bin'
        self.commands.mkdir()
        self.env = {**os.environ, 'PATH': str(self.commands) + ':/usr/bin:/bin', 'APPLETART_FIXTURE_ROOT': str(self.root)}

    def command(self, name, body):
        path = self.commands / name
        path.write_text('#!/bin/sh\nset -eu\n' + body + '\n')
        path.chmod(0o755)
        return path

    def execute(self, script):
        return subprocess.run(['sh', '-s'], input=script, env=self.env, capture_output=True, text=True, timeout=5)

    def test_native_package_manager_selection_is_independent_of_platform_name(self):
        self.command('sudo', 'if [ "$1" = "-n" ]; then shift; fi\nexec "$@"')
        for provider, expected in [('apt-get', 'install -y --'), ('dnf', 'install -y --'), ('yum', 'install -y --'), ('zypper', '--non-interactive install --'), ('apk', 'add --'), ('pacman', '--noconfirm -S --')]:
            with self.subTest(provider=provider):
                log = self.root / 'packages'
                log.unlink(missing_ok=True)
                path = self.command(provider, 'printf "%s\\n" "$*" >> "$APPLETART_FIXTURE_ROOT/packages"')
                try:
                    result = self.execute(software.install_script(['git', 'libX11-devel'], 'linux'))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(expected + ' git libX11-devel', log.read_text())
                finally:
                    path.unlink()
        result = self.execute(software.install_script(['git'], 'linux'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No supported native package manager', result.stderr)

    def test_homebrew_installs_as_login_user_and_refuses_root(self):
        self.command('brew', 'printf "%s\\n" "$*" > "$APPLETART_FIXTURE_ROOT/packages"')
        self.command('id', 'printf "501\\n"')
        result = self.execute(software.install_script(['jq', 'git'], 'macos').replace('export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH', 'export PATH=' + str(self.commands) + ':/usr/bin:/bin'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'packages').read_text().strip(), 'install -- jq git')
        self.command('id', 'printf "0\\n"')
        result = self.execute(software.install_script(['jq'], 'macos').replace('export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH', 'export PATH=' + str(self.commands) + ':/usr/bin:/bin'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('configured login account', result.stderr)

    def test_initial_native_upgrade_runs_before_selected_packages_and_with_no_selection(self):
        self.command('sudo', 'if [ "$1" = "-n" ]; then shift; fi\nexec "$@"')
        providers = [('apt-get', ['update', 'upgrade -y']), ('dnf', ['makecache --refresh', 'upgrade -y']),
                     ('yum', ['makecache', 'update -y']), ('zypper', ['--non-interactive refresh', '--non-interactive update']),
                     ('apk', ['update', 'upgrade']), ('pacman', ['--noconfirm -Syu'])]
        for provider, expected in providers:
            with self.subTest(provider=provider):
                path = self.command(provider, 'printf "%s\\n" "$*" >> "$APPLETART_FIXTURE_ROOT/packages"')
                try:
                    for selection in ([], ['git']):
                        log = self.root / 'packages'
                        log.unlink(missing_ok=True)
                        result = self.execute(software.install_script(selection, 'linux', upgrade=True))
                        self.assertEqual(result.returncode, 0, result.stderr)
                        commands = log.read_text().splitlines()
                        self.assertEqual(len(commands), len(expected) + bool(selection))
                        for command, prefix in zip(commands, expected):
                            self.assertTrue(command.startswith(prefix), commands)
                        if selection:
                            self.assertTrue(commands[-1].endswith('-- git'), commands)
                        if provider == 'apt-get':
                            self.assertIn('--force-confold', commands[1])
                        self.assertIn('System package upgrade completed.', result.stdout)
                finally:
                    path.unlink()
        self.assertEqual(software.install_script([], 'macos', upgrade=True), '')

    def test_failed_package_update_or_upgrade_aborts_before_installation(self):
        self.command('sudo', 'if [ "$1" = "-n" ]; then shift; fi\nexec "$@"')
        for failed_step in ('update', 'upgrade'):
            with self.subTest(failed_step=failed_step):
                self.command('apt-get', 'printf "%s\\n" "$*" >> "$APPLETART_FIXTURE_ROOT/packages"\n'
                             + f'test "$1" != "{failed_step}" || {{ echo "Repository failure" >&2; exit 42; }}')
                log = self.root / 'packages'
                log.unlink(missing_ok=True)
                result = self.execute(software.install_script(['git'], 'linux', upgrade=True))
                self.assertEqual(result.returncode, 42)
                self.assertNotIn('install', log.read_text())
                self.assertNotIn('upgrade completed', result.stdout)

    def test_cloud_preflight_checks_architecture_services_and_handles_group_collision(self):
        self.command('uname', 'if [ "$1" = -s ]; then echo Linux; else echo aarch64; fi')
        for tool in ('cloud-init', 'ip', 'sudo'):
            self.command(tool, ':')
        self.command('systemctl', 'test "$*" = "cat sshd.service"')
        self.command('getent', 'test "$*" = "group admin"')
        self.command('useradd', 'printf "%s\\n" "$*" > "$APPLETART_FIXTURE_ROOT/account"')
        result = self.execute(capabilities.cloud_preflight('admin'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--gid admin', (self.root / 'account').read_text())
        self.command('uname', 'if [ "$1" = -s ]; then echo Linux; else echo x86_64; fi')
        result = self.execute(capabilities.cloud_preflight('admin'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not ARM64', result.stderr)
        self.command('uname', 'if [ "$1" = -s ]; then echo Linux; else echo aarch64; fi')
        self.command('systemctl', 'exit 1')
        result = self.execute(capabilities.cloud_preflight('admin'))
        self.assertIn('must include OpenSSH', result.stderr)

    def test_explicit_preflight_failure_aborts_ssh_wait_without_using_old_log_errors(self):
        log = self.root / 'boot.log'
        log.write_text('APPLETART_PREFLIGHT_ERROR: previous boot\n')
        start = log.stat().st_size
        self.assertEqual(capabilities.boot_failure(log, start), '')
        with log.open('a') as output:
            output.write('APPLETART_PREFLIGHT_ERROR: Required guest capability is missing: sudo\n')
        backend = Mock()
        with self.assertRaisesRegex(DeploymentError, 'missing: sudo'):
            wait_for_ssh(backend, Machine.from_dict({'name': 'dev'}).vm, Mock(), boot_failure=lambda: capabilities.boot_failure(log, start))
        backend.run.assert_not_called()

    def test_tart_template_checks_nocloud_and_required_modules_before_changes(self):
        self.command('uname', 'if [ "$1" = -s ]; then echo Linux; else echo aarch64; fi')
        self.command('id', 'echo 0')
        for tool in ('cloud-init', 'ip', 'sudo', 'useradd'):
            self.command(tool, ':')
        self.command('systemctl', 'test "$*" = "cat sshd.service"')
        self.command('getent', 'exit 1')
        modules = self.root / 'modules/cloudinit'
        modules.mkdir(parents=True)
        (modules / '__init__.py').touch()
        (modules / 'sources.py').write_text('class DataSourceNoCloud: pass\n')
        self.env['PYTHONPATH'] = str(modules.parent)
        self.command('python3', 'exec "' + sys.executable + '" "$@"')
        etc = self.root / 'etc/cloud/cloud.cfg.d'
        etc.mkdir(parents=True)
        cmdline = self.root / 'cmdline'
        cmdline.write_text('console=ttyAMA0')
        config = {'cloud_init_modules': ['bootcmd', 'users_groups', 'ssh', 'set_hostname', 'growpart', 'resizefs'],
                  'cloud_config_modules': ['runcmd'], 'cloud_final_modules': ['scripts_user']}
        path = self.root / 'etc/cloud/cloud.cfg'
        path.write_text(json.dumps(config))
        script = capabilities.linux_golden_preparation('admin').replace('/etc/cloud', str(self.root / 'etc/cloud')).replace('/proc/cmdline', str(cmdline))
        # The capability boundary precedes all service/configuration cleanup.
        preflight = script.split("units=''", 1)[0]
        result = self.execute(preflight)
        self.assertEqual(result.returncode, 0, result.stderr)
        config['cloud_init_modules'] = [['bootcmd', 'always'], 'users-groups', 'ssh', 'set-hostname', 'growpart', 'resizefs']
        config['cloud_final_modules'] = ['scripts-user']
        path.write_text(json.dumps(config))
        result = self.execute(preflight)
        self.assertEqual(result.returncode, 0, result.stderr)
        (etc / '99-vendor.cfg').write_text('cloud_init_modules: [growpart, resizefs]\n')
        result = self.execute(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing required modules', result.stderr)
        self.assertFalse((etc / 'zzzz-appletart-nocloud.cfg').exists())
        (etc / '99-vendor.cfg').unlink()
        (modules / 'sources.py').write_text('# No NoCloud datasource\n')
        result = self.execute(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DataSourceNoCloud', result.stderr)
        self.assertFalse((etc / 'zzzz-appletart-nocloud.cfg').exists())

    def test_old_cloud_init_status_file_confirms_all_stages_and_rejects_errors(self):
        stages = {name: {'finished': 8, 'errors': []} for name in ('init-local', 'init', 'modules-config', 'modules-final')}
        result = subprocess.CompletedProcess([], 0, json.dumps({'v1': stages}), '')
        self.assertTrue(completed_cloud_status(result))
        stages['modules-final']['errors'] = ['package install failed']
        result.stdout = json.dumps({'v1': stages})
        self.assertFalse(completed_cloud_status(result))
        del stages['modules-final']
        result.stdout = json.dumps({'v1': stages})
        self.assertFalse(completed_cloud_status(result))

    def test_golden_cleanup_uses_supported_flags_and_account_homes(self):
        for directory in ('etc', 'etc/ssh', 'var/lib/dbus', 'srv/developer/.ssh', 'root/.ssh'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        keys = self.root / 'srv/developer/.ssh/authorized_keys'
        keys.write_text('ssh-ed25519 old-user\n')
        root_keys = self.root / 'root/.ssh/authorized_keys'
        root_keys.write_text('ssh-ed25519 root-key\n')
        host_key = self.root / 'etc/ssh/ssh_host_ed25519_key'
        host_key.write_text('host-key')
        (self.root / 'etc/ssh/sshd_config').write_text('PasswordAuthentication no\n')
        self.command('usermod', 'exit 0')
        self.command('sudo', 'exec "$@"')
        self.command('cloud-init', 'if [ "$*" = "clean --help" ]; then echo "--logs"; else printf "%s\\n" "$*" > "$APPLETART_FIXTURE_ROOT/cleanup"; fi')
        self.command('getent', 'printf "developer:x:1000:1000::%s/srv/developer:/bin/bash\\nroot:x:0:0::%s/root:/bin/bash\\n" "$APPLETART_FIXTURE_ROOT" "$APPLETART_FIXTURE_ROOT"')
        script = capabilities.GOLDEN_CLEANUP
        for prefix in ('/etc', '/var/lib/dbus'):
            script = script.replace(prefix, str(self.root) + prefix)
        # BSD truncate is provided on this Mac; use a portable fixture when absent.
        self.command('truncate', 'test "$1" = -s; test "$2" = 0; shift 2; for path in "$@"; do : > "$path"; done')
        result = self.execute('set -eu\n' + script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'cleanup').read_text().strip(), 'clean --logs')
        self.assertEqual(keys.read_text(), '')
        self.assertEqual(root_keys.read_text(), '')
        self.assertFalse(host_key.exists())
        self.assertEqual((self.root / 'etc/machine-id').read_text(), 'uninitialized\n')

    def test_software_setup_gets_thirty_minutes_and_stops_its_boot_process(self):
        process = Mock(poll=Mock(return_value=None))
        with patch('appletart.ssh.subprocess.Popen', return_value=process), patch('appletart.ssh.wait_for_ssh', return_value='192.0.2.10'), patch('appletart.ssh.ssh_install') as install, patch('appletart.ssh.stop_build') as stop:
            provision_keys(Mock(binary='fake-tart'), Machine.from_dict({'name': 'dev'}).vm, [], extra_script=software.install_script(['git'], 'linux'), known_hosts=self.root / 'known_hosts')
        self.assertEqual(install.call_args.kwargs['timeout'], 1800)
        stop.assert_called_once_with(process, 'dev')

    def test_macos_interface_and_mount_details_use_native_output(self):
        interfaces, mounts = guest_access.macos_details('en0: flags=8863<UP,BROADCAST,RUNNING,MULTICAST> mtu 1500\n\tinet 192.168.64.5 netmask 0xffffff00 broadcast 192.168.64.255\nlo0: flags=8049<UP,LOOPBACK> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n', 'appletart-share0 on /mnt/My Documents (virtiofs, local, read-only)\n/dev/disk3s1 on / (apfs, local)\n')
        self.assertEqual(interfaces[0], {'name': 'en0', 'state': 'UP', 'addresses': ['192.168.64.5']})
        self.assertEqual(list(mounts), ['/mnt/My Documents'])

    def test_macos_agent_is_verified_and_launchd_root_rpc_keeps_clipboard_service(self):
        binary = b'\xca\xfe\xba\xbe' + bytes(60)
        archive = self.root / 'agent.tar.gz'
        with tarfile.open(archive, 'w:gz') as target:
            entry = tarfile.TarInfo('tart-guest-agent'); entry.size = len(binary)
            target.addfile(entry, io.BytesIO(binary))
        with patch('appletart.guest_agent.fetch_image', return_value=archive) as fetch:
            self.assertEqual(guest_agent.prepare(self.root, Mock(), family='macos'), binary)
            self.assertIn('darwin-all', fetch.call_args.args[0])
            self.assertEqual(len(fetch.call_args.args[1]), 64)
        (self.root / 'Library/LaunchAgents').mkdir(parents=True)
        (self.root / 'Library/LaunchDaemons').mkdir(parents=True)
        vendor = self.root / 'Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist'
        vendor.write_bytes(plistlib.dumps({'Label': 'org.cirruslabs.tart-guest-agent', 'ProgramArguments': ['/opt/homebrew/bin/tart-guest-agent', '--run-agent']}))
        self.command('sudo', 'if [ "$1" = -n ]; then shift; fi\nexec "$@"')
        self.command('launchctl', 'printf "%s\\n" "$*" >> "$APPLETART_FIXTURE_ROOT/launchd"')
        self.command('chown', ':')
        buddy = self.commands / 'PlistBuddy'
        buddy.write_text(f'#!{sys.executable}\nimport plistlib,sys\npath=sys.argv[-1]\nwith open(path,"rb") as f: value=plistlib.load(f)\nparts=sys.argv[2].split(" ",2)\nvalue["ProgramArguments"][int(parts[1].split(":")[-1])]=parts[2]\nwith open(path,"wb") as f: plistlib.dump(value,f)\n')
        buddy.chmod(0o755)
        script = guest_agent.install_script(binary).replace('/usr/libexec/PlistBuddy', str(buddy))
        for prefix in ('/usr/local', '/Library'):
            script = script.replace(prefix, str(self.root) + prefix)
        result = self.execute(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'usr/local/bin/appletart-guest-agent').read_bytes(), binary)
        service = plistlib.loads((self.root / 'Library/LaunchDaemons/org.appletart.guest-management.plist').read_bytes())
        self.assertEqual(service['ProgramArguments'][-1], '--run-rpc')
        self.assertNotIn('UserName', service, 'A system LaunchDaemon runs as root by default')
        self.assertEqual(plistlib.loads(vendor.read_bytes())['ProgramArguments'][-1], '--run-vdagent')
        self.assertIn('bootstrap system ', (self.root / 'launchd').read_text())


if __name__ == '__main__':
    unittest.main()
