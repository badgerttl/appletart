import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from appletart.catalog import Machine
from appletart.cloud import cloud_config, provision_cloud


class GoldenIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ('etc', 'var/lib/dbus', 'home', 'root', 'bin'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.identity = self.root / 'etc/machine-id'
        self.identity.write_text('a' * 32 + '\n')
        (self.root / 'etc/ssh').mkdir()
        (self.root / 'etc/ssh/sshd_config').write_text('PasswordAuthentication no\n')
        (self.root / 'var/lib/dbus/machine-id').write_text('a' * 32 + '\n')
        self.command('sudo', 'if [ "$1" = "-n" ]; then shift; fi\nexec "$@"')
        # Debian 12's cloud-init 22.4 unlinks the file during --machine-id clean.
        self.command('cloud-init', 'if [ "$*" = "clean --help" ]; then echo "--logs --machine-id --seed"; else rm -f "$APPLETART_FIXTURE_ROOT/etc/machine-id"; fi')
        self.command('getent', 'printf "root:x:0:0::%s/root:/bin/bash\\n" "$APPLETART_FIXTURE_ROOT"')
        self.command('usermod', 'test "$*" = "--password ! -- root"')
        self.command('systemd-machine-id-setup', 'printf "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n" > "$APPLETART_FIXTURE_ROOT/etc/machine-id"')
        self.command('netplan', 'test -s "$APPLETART_FIXTURE_ROOT/etc/machine-id"\nprintf "%s\\n" "$*" >> "$APPLETART_FIXTURE_ROOT/network-retries"')
        self.machine = Machine.from_dict({'name': 'debian-clone', 'os': 'other', 'source_kind': 'golden',
                                         'source': 'debian-golden', 'ssh_user': 'admin', 'ssh_public_keys': ['/tmp/user.pub']})

    def command(self, name, body):
        path = self.root / 'bin' / name
        path.write_text('#!/bin/sh\nset -eu\n' + body + '\n')
        path.chmod(0o755)

    def execute(self, script):
        for prefix in ('/etc', '/var/lib/dbus', '/home', '/root'):
            script = script.replace(prefix, str(self.root) + prefix)
        result = subprocess.run(['sh', '-s'], input=script, text=True, capture_output=True,
                                env={**os.environ, 'PATH': str(self.root / 'bin') + ':' + os.environ['PATH'],
                                     'APPLETART_FIXTURE_ROOT': str(self.root)}, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_golden_cleanup_keeps_a_machine_id_file_for_read_only_first_boot(self):
        scripts = []
        def ssh(*args, **kwargs):
            if kwargs.get('input'):
                scripts.append(kwargs['input'])
            return Mock(returncode=0, stdout='status: done', stderr='')
        with patch('appletart.cloud.subprocess.Popen', return_value=Mock(poll=Mock(return_value=None))), \
             patch('appletart.cloud.wait_for_ssh', return_value='192.0.2.10'), \
             patch('appletart.cloud.subprocess.run', side_effect=ssh), \
             patch('appletart.cloud.install_agent'), \
             patch('appletart.cloud.wait_for_build_agent', return_value=False), \
             patch('appletart.cloud.verify_management'), \
             patch('appletart.cloud.read_public_keys', return_value=[]):
            provision_cloud(Mock(binary='fake-tart', run=Mock(return_value='192.0.2.10')), self.machine,
                            self.root / 'seed.iso', self.root / 'bootstrap', 'ssh-ed25519 build',
                            self.root / 'logs/vm.log', Mock(), prepare_image=True)
        self.execute(scripts[-1])
        self.assertTrue(self.identity.is_file(), 'Missing machine-id prevents DHCP when /etc starts read-only')
        self.assertEqual(self.identity.read_text(), 'uninitialized\n')
        self.assertEqual((self.root / 'var/lib/dbus/machine-id').read_text(), 'uninitialized\n')

    def test_existing_golden_with_missing_machine_id_repairs_before_network_retry(self):
        self.identity.unlink()
        commands = cloud_config(self.machine, [])['bootcmd']
        for command in commands:
            self.assertEqual(command[:2], ['sh', '-c'])
            if "APPLETART_PREFLIGHT_ERROR" not in command[2]:
                self.execute(command[2])
        self.assertEqual(self.identity.read_text(), 'b' * 32 + '\n')
        self.assertEqual((self.root / 'network-retries').read_text(), 'apply\n')

    def test_existing_golden_with_valid_machine_id_keeps_identity_and_network(self):
        for command in cloud_config(self.machine, [])['bootcmd']:
            if "APPLETART_PREFLIGHT_ERROR" not in command[2]:
                self.execute(command[2])
        self.assertEqual(self.identity.read_text(), 'a' * 32 + '\n')
        self.assertFalse((self.root / 'network-retries').exists())


if __name__ == '__main__':
    unittest.main()
