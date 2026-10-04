import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from appletart import capabilities
from appletart.cloud import GUEST_BUILD_HEALTH


class CloudNetworkTests(unittest.TestCase):
    mac = '02:00:00:00:00:01'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = {**os.environ, 'PATH': str(self.bin) + ':/usr/bin:/bin', 'FIXTURE': str(self.root)}
        self.interfaces = self.root / 'etc/network/interfaces.d/50-cloud-init'
        self.interfaces.parent.mkdir(parents=True)
        self.original = ('# This file is generated from information provided by the datasource.\n'
                         'auto lo\niface lo inet loopback\n\n'
                         'auto appletart\niface appletart inet dhcp\n    mtu 1500\n\n'
                         'auto extra0\niface extra0 inet static\n    address 10.0.0.2\n')
        self.interfaces.write_text(self.original)
        self.interfaces.chmod(0o640)
        self.rules = self.root / 'etc/udev/rules.d/70-persistent-net.rules'
        self.rules.parent.mkdir(parents=True)
        self.owned_rule = f'SUBSYSTEM=="net", ATTR{{address}}=="{self.mac}", NAME="appletart"\n'
        self.other_rule = 'SUBSYSTEM=="net", ATTR{address}=="02:00:00:00:00:ff", NAME="extra0"\n'
        self.rules.write_text(self.owned_rule + self.other_rule)
        self.command('python3', 'exec "' + sys.executable + '" "$@"')
        self.command('timeout', 'shift; exec "$@"')
        self.command('systemctl', 'printf "%s\\n" "$*" >> "$FIXTURE/service"')
        self.command('ip', 'if [ "$1" = -o ]; then echo "2: eth0 inet 192.168.64.2/24 scope global eth0"; else echo "default via 192.168.64.1 dev eth0"; fi')
        self.command('sleep', ':')

    def command(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\nset -eu\n' + body + '\n')
        path.chmod(0o755)

    def nic(self, name, mac=None):
        address = self.root / 'sys/class/net' / name / 'address'
        address.parent.mkdir(parents=True, exist_ok=True)
        address.write_text((mac or self.mac) + '\n')

    def execute(self, script):
        script = script.replace('/etc/network', str(self.root / 'etc/network'))
        script = script.replace('/etc/udev', str(self.root / 'etc/udev'))
        script = script.replace('/sys/class/net', str(self.root / 'sys/class/net'))
        return subprocess.run(['sh', '-s'], input=script, env=self.env, capture_output=True, text=True, timeout=5)

    def test_resolves_actual_names_preserves_other_interfaces_and_is_idempotent(self):
        for name in ('eth0', 'enp0s1'):
            with self.subTest(name=name):
                self.interfaces.write_text(self.original)
                self.rules.write_text(self.owned_rule + self.other_rule)
                self.nic(name)
                self.nic('extra0', '02:00:00:00:00:ff')
                result = self.execute(capabilities.cloud_network_setup(self.mac.upper()))
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = self.original.replace('auto appletart\niface appletart', f'auto {name}\niface {name}')
                self.assertEqual(self.interfaces.read_text(), expected)
                self.assertEqual(self.rules.read_text(), self.other_rule)
                self.assertEqual(self.interfaces.stat().st_mode & 0o777, 0o640)
                self.assertEqual((self.root / 'service').read_text(), 'restart networking.service\n')
                result = self.execute(capabilities.cloud_network_setup(self.mac))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / 'service').read_text(), 'restart networking.service\n')
                (self.root / 'service').unlink()
                (self.root / 'sys/class/net' / name / 'address').unlink()

    def test_configures_all_interfaces_with_the_requested_mac(self):
        for name in ('enp0s1', 'enp0s2'):
            self.nic(name)
        result = self.execute(capabilities.cloud_network_setup(self.mac))
        self.assertEqual(result.returncode, 0, result.stderr)
        contents = self.interfaces.read_text()
        for name in ('enp0s1', 'enp0s2'):
            self.assertIn(f'auto {name}\niface {name} inet dhcp\n    mtu 1500\n', contents)
        self.assertNotIn('appletart', contents)

    def test_skips_other_renderers_and_unowned_network_configuration(self):
        for contents in ('', self.original.replace('appletart', 'eth0'),
                         self.original.replace('# This file is generated from information provided by the datasource.', '# Custom configuration')):
            with self.subTest(contents=contents):
                self.interfaces.write_text(contents)
                result = self.execute(capabilities.cloud_network_setup(self.mac))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.interfaces.read_text(), contents)
                self.assertEqual(self.rules.read_text(), self.owned_rule + self.other_rule)
                self.assertFalse((self.root / 'service').exists())
        self.interfaces.unlink()
        self.assertEqual(self.execute(capabilities.cloud_network_setup(self.mac)).returncode, 0)

    def test_missing_mac_match_fails_explicitly_before_changing_configuration(self):
        self.nic('eth0', '02:00:00:00:00:ff')
        result = self.execute(capabilities.cloud_network_setup(self.mac))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('APPLETART_NETWORK_ERROR: No usable guest network interface matches', result.stderr)
        self.assertEqual(self.interfaces.read_text(), self.original)
        self.assertEqual(self.rules.read_text(), self.owned_rule + self.other_rule)
        self.assertFalse((self.root / 'service').exists())
        log = self.root / 'boot.log'
        log.write_text(result.stderr)
        self.assertIn('No usable guest network interface', capabilities.boot_failure(log))

    def test_activation_failure_is_reported_and_invalid_mac_is_rejected(self):
        self.nic('eth0')
        self.command('systemctl', 'exit 42')
        result = self.execute(capabilities.cloud_network_setup(self.mac))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not activate the resolved ENI', result.stderr)
        with self.assertRaises(ValueError):
            capabilities.cloud_network_setup('not-a-mac')

    def test_eni_activation_waits_for_ipv4_dhcp_and_reports_a_bounded_timeout(self):
        self.nic('eth0')
        self.command('ip', '''if [ "$1" = -o ]; then
count=0
test ! -f "$FIXTURE/dhcp" || count=$(cat "$FIXTURE/dhcp")
count=$((count + 1))
echo "$count" > "$FIXTURE/dhcp"
if [ "$count" -ge 3 ]; then echo "2: eth0 inet 192.168.64.2/24 scope global eth0"; fi
else echo "default via 192.168.64.1 dev eth0"; fi''')
        result = self.execute(capabilities.cloud_network_setup(self.mac))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'dhcp').exists(), 'ENI activation must check DHCP readiness')
        self.assertEqual((self.root / 'dhcp').read_text().strip(), '3')
        self.interfaces.write_text(self.original)
        self.command('ip', ':')
        result = self.execute(capabilities.cloud_network_setup(self.mac))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('IPv4 address and default route were not ready within 30 seconds', result.stderr)

    def test_health_reports_missing_address_route_ssh_and_sudo(self):
        self.command('ip', 'if [ "$1" = -o ]; then cat "$FIXTURE/address"; else cat "$FIXTURE/route"; fi')
        self.command('id', 'echo 1000')
        self.command('sudo', 'test ! -f "$FIXTURE/no-sudo"')
        self.command('systemctl', 'test "$1" = cat || test ! -f "$FIXTURE/no-ssh"')
        address = self.root / 'address'
        route = self.root / 'route'
        address.write_text('')
        route.write_text('default via 192.168.64.1 dev eth0\n')
        script = "expected=''\n" + GUEST_BUILD_HEALTH
        result = self.execute(script)
        self.assertIn('No usable guest IPv4 address', result.stderr)
        address.write_text('2: eth0 inet 192.168.64.2/24 scope global eth0\n')
        route.write_text('')
        self.assertIn('no IPv4 default route', self.execute(script).stderr)
        route.write_text('default via 192.168.64.1 dev eth0\n')
        (self.root / 'no-sudo').touch()
        self.assertIn('lacks passwordless sudo', self.execute(script).stderr)
        (self.root / 'no-sudo').unlink()
        (self.root / 'no-ssh').touch()
        self.assertIn('SSH service (ssh.service) is not active', self.execute(script).stderr)
        (self.root / 'no-ssh').unlink()
        self.assertEqual(self.execute(script).returncode, 0)
        self.assertIn('expected 192.168.64.99', self.execute("expected='192.168.64.99'\n" + GUEST_BUILD_HEALTH).stderr)


if __name__ == '__main__':
    unittest.main()
