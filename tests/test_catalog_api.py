import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from appletart.lifecycle import Lifecycle
from appletart import image_catalog
from appletart.web import AppServer
from test_lifecycle import FakeBackend


class CatalogAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.api = Lifecycle(Path(self.temp.name), lambda report=print: FakeBackend())
        self.api._preflight = Mock(return_value=[])
        self.server = AppServer(('127.0.0.1', 0), self.api)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def request(self, method, path, data=None, token=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        headers = {'X-Appletart-Token': self.server.token if token is None else token}
        if data is not None: headers['Content-Type'] = 'application/json'
        connection.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse(); body = response.read(); connection.close()
        return response.status, body

    def test_bundle_round_trip_preview_and_compatibility_use_workspace_data(self):
        self.assertEqual(self.request('GET', '/api/bundles', token='wrong')[0], 403)
        value = {'id': 'development', 'name': 'Development', 'platforms': ['linux'], 'packages': ['git', 'jq']}
        status, body = self.request('POST', '/api/bundle/save', {'bundle': value})
        self.assertEqual(status, 200); self.assertEqual(json.loads(body), value)
        self.assertEqual(self.request('POST', '/api/bundle/delete', {'id': value['id']}, token='wrong')[0], 403)
        status, body = self.request('POST', '/api/preview', {'config': {'name': 'dev', 'software_bundles': ['development']}})
        self.assertEqual(status, 200); self.assertEqual(json.loads(body)['config']['bundle_packages'], ['git', 'jq'])
        status, body = self.request('POST', '/api/preview', {'config': {'name': 'mac', 'os': 'macos', 'software_bundles': ['development']}})
        self.assertEqual(status, 400); self.assertIn('not compatible', json.loads(body)['error'])
        self.assertEqual(self.request('POST', '/api/bundle/delete', {'id': 'development'})[0], 200)
        self.assertEqual(self.request('POST', '/api/preview', {'config': {'name': 'dev', 'software_bundles': ['development']}})[0], 400)

    def test_custom_catalog_overrides_choices_preview_and_is_authenticated(self):
        catalog = {'custom': {'label': 'Custom template', 'family': 'linux', 'source_kind': 'tart', 'source': 'local-template', 'ssh_user': 'ops', 'icon': 'other'}}
        self.assertEqual(self.request('POST', '/api/catalog', {'images': catalog}, token='wrong')[0], 403)
        self.assertEqual(self.request('POST', '/api/catalog', {'images': catalog})[0], 200)
        status, body = self.request('GET', '/api/choices')
        self.assertEqual(status, 200); self.assertEqual(json.loads(body)['images'], catalog)
        status, body = self.request('POST', '/api/preview', {'config': {'name': 'dev', 'os': 'custom'}})
        self.assertEqual(status, 200); self.assertEqual(json.loads(body)['config']['source'], 'local-template')
        self.assertEqual(self.request('POST', '/api/catalog', {'images': []})[0], 400)
        self.assertEqual(self.request('GET', '/software.js')[0], 200)
        for icon in ('rhel', 'fedora', 'debian', 'rocky', 'macos', 'other'):
            status, body = self.request('GET', '/icons/' + icon + '.svg')
            self.assertEqual(status, 200); self.assertIn(b'<svg', body)

    def test_kali_version_round_trip_preserves_default_and_preview_checksum(self):
        images = image_catalog.load()
        default_source = images['kali']['source']
        version = {'label': 'Additional release', 'source_kind': 'cloud', 'source': 'https://example.org/older-arm64.img', 'sha512': 'c' * 128}
        images['kali']['versions'].append(version)
        self.assertEqual(self.request('POST', '/api/catalog', {'images': images})[0], 200)
        status, body = self.request('GET', '/api/choices')
        self.assertEqual(status, 200)
        kali = json.loads(body)['images']['kali']
        self.assertEqual(kali['source'], default_source)
        self.assertIn(version, kali['versions'])
        status, body = self.request('POST', '/api/preview', {'config': {'name': 'older', 'os': 'kali', 'source': version['source'], 'ssh_public_keys': ['/tmp/key.pub']}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['config']['sha512'], version['sha512'])
        self.assertEqual(json.loads(body)['config']['sha256'], '')


if __name__ == '__main__':
    unittest.main()
