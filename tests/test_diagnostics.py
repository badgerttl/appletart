import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

from appletart import diagnostics
from appletart.catalog import Machine
from appletart.deployment import DeploymentError
from appletart.lifecycle import Lifecycle
from appletart.operations import Cancellation, run, scope
from appletart.web import Jobs


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.api = Lifecycle(self.root)
        from unittest.mock import patch
        for target in ("appletart.lifecycle.guest_agent.prepare", "appletart.lifecycle.provision_keys"):
            stub = patch(target)
            stub.start()
            self.addCleanup(stub.stop)
        self.machine = Machine.from_dict({'name': 'log-test'})
        self.api.store.put({'config': self.machine.config(), 'phase': 'created', 'owned': True})

    def test_operation_detail_survives_activity_limits_and_dashboard_restart(self):
        def download(machine, report):
            report('FIRST DIAGNOSTIC MARKER')
            report('x' * 5000 + ' END OF LONG ERROR')
            for n in range(150):
                report(f'progress {n}')
        self.api.download = download
        jobs = Jobs(self.api)
        jobs.submit('download', {'config': self.machine.config()})
        deadline = time.monotonic() + 2
        while jobs.snapshot()[0]['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        jobs.shutdown()
        self.assertNotIn('FIRST DIAGNOSTIC MARKER', '\n'.join(jobs.snapshot()[0]['lines']))
        restarted = Lifecycle(self.root)
        log = restarted.log('log-test')
        self.assertIn('FIRST DIAGNOSTIC MARKER', log)
        self.assertIn('END OF LONG ERROR', log)
        self.assertIn('complete', log)

    def test_command_failure_retains_full_output_without_stdin_or_environment_secrets(self):
        journal = diagnostics.Journal(self.root, 'log-test', 'a' * 16, 'build', secrets=('bootstrap-password',))
        with journal.scope(), scope(Cancellation()):
            result = run([sys.executable, '-c', 'import sys; print("x"*5000+"ERROR END"); sys.exit(7)'],
                         input='PRIVATE-STDIN', text=True, capture_output=True, timeout=2)
        journal.write('error', 'bootstrap-password')
        journal.finish('failed')
        text = journal.path.read_text()
        self.assertEqual(result.returncode, 7)
        self.assertIn('ERROR END', text)
        self.assertIn('code=7', text)
        self.assertNotIn('PRIVATE-STDIN', text)
        self.assertNotIn('bootstrap-password', text)
        self.assertEqual(journal.path.stat().st_mode & 0o777, 0o600)

    def test_preview_pages_and_download_path_retain_the_entire_transcript(self):
        journal = diagnostics.Journal(self.root, 'log-test', 'b' * 16, 'build')
        journal.write('stdout', 'START' + 'x' * (diagnostics.PREVIEW_BYTES * 2) + 'END')
        journal.finish('complete')
        latest = diagnostics.read(self.root, 'log-test')
        self.assertGreater(latest['start'], 0)
        self.assertIn('END', latest['log'])
        pieces = [latest['log']]
        while latest['start']:
            latest = diagnostics.read(self.root, 'log-test', 'b' * 16, latest['start'])
            pieces.insert(0, latest['log'])
        self.assertEqual(''.join(pieces), journal.path.read_text())

        for source in ('../../machines/log-test.json', None, 'c' * 15):
            with self.assertRaises(DeploymentError):
                diagnostics.source_path(self.root, 'log-test', source)

    def test_separate_retry_logs_and_console_capture(self):
        console = self.root / 'logs/log-test.log'
        console.parent.mkdir(exist_ok=True)
        console.write_text('old boot\n')
        first = diagnostics.Journal(self.root, 'log-test', 'c' * 16, 'build')
        first.console(console)
        with console.open('a') as file:
            file.write('new boot error\n')
        first.finish('failed')
        second = diagnostics.Journal(self.root, 'log-test', 'd' * 16, 'build')
        second.finish('complete')
        self.assertNotIn('old boot', first.path.read_text())
        self.assertIn('new boot error', first.path.read_text())
        self.assertEqual(len(diagnostics.sources(self.root, 'log-test')), 3)

    def test_cloud_failure_snapshot_is_saved_before_cloud_init_cleanup(self):
        from unittest.mock import patch
        from appletart.cloud import provision_cloud
        process = Mock(poll=Mock(return_value=None))
        journal = diagnostics.Journal(self.root, 'log-test', 'e' * 16, 'build')
        cloud = Machine.from_dict({'name': 'log-test', 'os': 'kali', 'ssh_public_keys': ['/tmp/key.pub']})
        events = []
        def ssh(args, **kwargs):
            if args[-1] == 'true':
                return subprocess.CompletedProcess(args, 0, '', '')
            if 'status --wait' in args[-1]:
                return subprocess.CompletedProcess(args, 1, 'status: error\n' + 'x' * 5000 + 'FULL STATUS ERROR', '')
            if '=== Guest system ===' in kwargs.get('input', ''):
                events.append('snapshot')
                return subprocess.CompletedProcess(args, 0, 'cloud-init.log\n' + 'x' * 6000 + 'PACKAGE MANAGER ROOT CAUSE', '')
            if args[-1] == 'sudo -n -- cloud-init clean':
                events.append('clean')
            return subprocess.CompletedProcess(args, 0, '', '')
        with journal.scope(), patch('appletart.cloud.subprocess.Popen', return_value=process), \
             patch('appletart.cloud.wait_for_ssh', return_value='192.0.2.10'), \
             patch('appletart.operations.subprocess.run', side_effect=ssh), \
             self.assertRaisesRegex(DeploymentError, 'Cloud-init reported'):
            provision_cloud(Mock(binary='fake-tart'), cloud, self.root / 'seed.iso', self.root / 'bootstrap',
                            'ssh-ed25519 public', self.root / 'logs/log-test.log', Mock())
        journal.finish('failed')
        self.assertEqual(events, ['snapshot', 'clean'])
        self.assertIn('PACKAGE MANAGER ROOT CAUSE', journal.path.read_text())
        self.assertIn('FULL STATUS ERROR', journal.path.read_text())

    def test_utf8_pages_and_incremental_updates_do_not_lose_text(self):
        journal = diagnostics.Journal(self.root, 'log-test', 'f' * 16, 'build')
        journal.write('stdout', '日本語…' * 60000)
        journal.finish('complete')
        latest = diagnostics.read(self.root, 'log-test')
        pieces = [latest['log']]
        while latest['start']:
            latest = diagnostics.read(self.root, 'log-test', 'f' * 16, latest['start'])
            pieces.insert(0, latest['log'])
        self.assertEqual(''.join(pieces), journal.path.read_text())
        pieces, cursor = [], 0
        while cursor < journal.path.stat().st_size:
            page = diagnostics.read(self.root, 'log-test', 'f' * 16, after=cursor)
            self.assertGreater(page['end'], cursor)
            cursor = page['end']
            pieces.append(page['log'])
        self.assertEqual(''.join(pieces), journal.path.read_text())

    def test_running_guest_log_collection_checks_ownership_and_uses_existing_ssh_identity(self):
        from unittest.mock import patch
        from test_lifecycle import FakeBackend
        backend = FakeBackend()
        api = Lifecycle(self.root, lambda report=print: backend)
        api.store.remove('log-test')
        api.build(self.machine, Mock())
        with patch('appletart.guest_agent.root_available', return_value=False), patch('appletart.lifecycle.capture_guest', return_value=True) as capture:
            with self.assertRaisesRegex(DeploymentError, 'Start the VM'):
                api.collect_logs('log-test', Mock())
            backend.vms['log-test']['Running'] = True
            api.collect_logs('log-test', Mock())
            command = capture.call_args.kwargs['command']
            self.assertEqual(command[0], 'ssh')
            self.assertIn('BatchMode=yes', command)
            self.assertEqual(command[-2:], ['admin@192.0.2.10', 'sudo -n /bin/sh -s'])
            self.assertTrue(backend.vms['log-test']['Running'])

    def test_agent_log_collection_requires_no_guest_network(self):
        from unittest.mock import patch
        from test_lifecycle import FakeBackend
        backend = FakeBackend()
        api = Lifecycle(self.root, lambda report=print: backend)
        api.store.remove('log-test')
        api.build(self.machine, Mock())
        backend.vms['log-test']['Running'] = True
        with patch('appletart.guest_agent.root_available', return_value=True), \
             patch('appletart.guest_access.resolve', side_effect=AssertionError('must not require IP')), \
             patch('appletart.lifecycle.capture_guest', return_value=True) as capture:
            api.collect_logs('log-test', Mock())
        self.assertEqual(capture.call_args.kwargs['command'], ['fake-tart', 'exec', '-i', 'log-test', '/bin/sh', '-s'])
        self.assertTrue(backend.vms['log-test']['Running'])

    def test_streamed_private_key_blocks_and_interrupted_attempts_are_handled(self):
        journal = diagnostics.Journal(self.root, 'log-test', '0' * 16, 'build')
        for line in ('before', '-----BEGIN OPENSSH PRIVATE KEY-----', 'secret-key-material', '-----END OPENSSH PRIVATE KEY-----', 'after'):
            journal.write('console', line)
        journal.file.close()  # Simulate process exit without a completion record.
        diagnostics.recover_interrupted(self.root)
        text = journal.path.read_text()
        self.assertNotIn('secret-key-material', text)
        self.assertIn('after', text)
        self.assertIn('interrupted', text)
        self.assertEqual(diagnostics.sources(self.root, 'log-test')[0]['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
