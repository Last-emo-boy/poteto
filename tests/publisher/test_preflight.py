from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from publisher import cli
from publisher.api import create_app
from publisher.config import Settings
from publisher.store import Store


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(self.root / 'state', static_artifacts=True)
        self.store = Store(self.settings)
        owner = self.store.create_account('owner')
        self.owner_token = owner['token']
        actor = self.store.authenticate(owner['token'])
        self.project = self.store.create_project(actor, 'app')['id']
        stranger = self.store.create_account('stranger')
        self.other = self.store.create_project(self.store.authenticate(stranger['token']), 'other')['id']
        self.client = TestClient(create_app(self.settings), headers={'Authorization': 'Bearer ' + owner['token']})
        self.addCleanup(self.client.close)
        token = self.client.post('/v1/tokens', json={'name': 'preview', 'scopes': ['deployments:preview'],
                                                  'project_ids': [self.project]}).json()['token']
        self.preview_token = token
        self.preview = TestClient(create_app(self.settings), headers={'Authorization': 'Bearer ' + token})
        self.addCleanup(self.preview.close)
        self.path = f'/v1/projects/{self.project}/preflight'
        self.body = {'configuration': {'runtime': 'static', 'target': 'preview'},
                     'archive_bytes': 80, 'unpacked_bytes': 100, 'files': 1}

    def inventory(self):
        with self.store.connection() as db:
            return {table: list(map(tuple, db.execute('SELECT * FROM ' + table)))
                    for table in ['deployments', 'jobs', 'uploads', 'logs', 'audit']}

    def test_checks_are_read_only_and_do_not_expose_environment(self):
        with self.store.connection() as db:
            db.execute("UPDATE projects SET preview_environment=? WHERE id=?",
                       (self.store.seal({'KEY': 'private-runtime-value'}), self.project))
        before = self.inventory()
        response = self.preview.post(self.path, json=self.body)
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual((True, 'preview', 'static-artifact'), (result['ok'], result['target'], result['runtime_driver']))
        self.assertEqual(536870912 - 180, result['storage']['remaining_after_upload'])
        self.assertNotIn('private-runtime-value', response.text)
        self.assertEqual(before, self.inventory())
        self.assertEqual([], list((self.settings.root / 'artifacts').iterdir()))
        self.assertEqual([], list((self.settings.root / 'uploads').iterdir()))

    def test_scopes_sizes_and_data_bindings_match_deployment_admission(self):
        self.assertEqual(403, self.preview.post(self.path, json=self.body | {'configuration': {'target': 'production'}}).status_code)
        self.assertEqual(404, self.preview.post(f'/v1/projects/{self.other}/preflight', json=self.body).status_code)
        self.assertEqual(413, self.preview.post(self.path, json=self.body | {'archive_bytes': 51 * 1024**2}).status_code)
        self.assertEqual(413, self.preview.post(self.path, json=self.body | {'unpacked_bytes': 201 * 1024**2}).status_code)
        self.assertEqual(422, self.preview.post(self.path, json=self.body | {'files': 0}).status_code)
        resource = self.client.post(f'/v1/projects/{self.project}/databases', json={'stage': 'production'}).json()['id']
        with self.store.connection() as db:
            db.execute("UPDATE data_resources SET state='ready' WHERE id=?", (resource,))
        request = self.body | {'configuration': {'runtime': 'node', 'target': 'preview', 'database_id': resource}}
        planned = self.preview.post(self.path, json=request)
        deployed = self.preview.post(f'/v1/projects/{self.project}/deployments', json=request['configuration'])
        self.assertEqual(409, planned.status_code)
        self.assertEqual(planned.json(), deployed.json())

    def test_storage_and_concurrency_are_checked_without_reserving_a_release(self):
        with self.store.connection() as db:
            db.execute('UPDATE accounts SET max_storage=128 WHERE name=?', ('owner',))
        self.assertEqual(429, self.preview.post(self.path, json=self.body).status_code)
        with self.store.connection() as db:
            db.execute('UPDATE accounts SET max_storage=536870912,max_concurrent=1 WHERE name=?', ('owner',))
        release = self.preview.post(f'/v1/projects/{self.project}/deployments', json=self.body['configuration']).json()
        before = self.inventory()
        response = self.preview.post(self.path, json=self.body)
        self.assertEqual(429, response.status_code)
        self.assertIn('Concurrent deployment', response.text)
        self.assertEqual(before, self.inventory())
        self.assertEqual(release['id'], self.client.get('/v1/deployments/' + release['id']).json()['id'])

    def test_runtime_capacity_and_image_opt_in_match_real_creation(self):
        image = self.body | {'configuration': {'runtime':'image','target':'preview'}}
        self.assertEqual(403, self.preview.post(self.path,json=image).status_code)
        node = {'runtime':'node','target':'preview','memory_mb':1024,'cpus':1}
        for _ in range(4):
            created=self.preview.post(f'/v1/projects/{self.project}/deployments',json=node)
            self.assertEqual(201,created.status_code,created.text)
            with self.store.connection() as db:
                db.execute("UPDATE deployments SET state='ready' WHERE id=?", (created.json()['id'],))
        request=self.body | {'configuration':{'runtime':'node','target':'preview','memory_mb':64,'cpus':0.1}}
        planned=self.preview.post(self.path,json=request)
        deployed=self.preview.post(f'/v1/projects/{self.project}/deployments',json=request['configuration'])
        self.assertEqual(429,planned.status_code)
        self.assertEqual(planned.json(),deployed.json())

    def test_cli_keeps_offline_mode_and_remote_uses_the_same_preview_config(self):
        app = self.root / 'app'; app.mkdir()
        (app / 'index.html').write_text('private-source-text', encoding='utf-8')
        credential = self.root / 'credential.json'
        credential.write_text(json.dumps({'token': self.preview_token, 'project_id': self.project,
                                         'poteto_url': 'https://deploy.example.test'}))
        real = self.client
        seen = []
        class LocalClient(cli.Client):
            def request(inner, method, path, body=None, **kwargs):
                seen.append((method, path, body))
                response = real.request(method, path, json=body,
                                        headers={'Authorization': 'Bearer ' + inner.token})
                if response.status_code >= 400:
                    raise RuntimeError(f'HTTP {response.status_code}: {response.text}')
                return response.json()
        def command(*args, status=0):
            out, err = io.StringIO(), io.StringIO()
            with patch('sys.argv', ['poteto', *map(str, args)]), redirect_stdout(out), redirect_stderr(err):
                if status:
                    with self.assertRaises(SystemExit) as error: cli.main()
                    self.assertEqual(status, error.exception.code)
                else: cli.main()
            self.assertNotIn(self.preview_token, out.getvalue() + err.getvalue())
            return json.loads(out.getvalue())
        with patch.dict(os.environ, {'POTETO_TOKEN_FILE': str(credential), 'POTETO_URL': '',
                                     'PAAS_URL': '', 'POTETO_TOKEN': '', 'PAAS_TOKEN': '', 'PAAS_TOKEN_FILE': ''}),              patch.object(cli, 'Client', LocalClient):
            offline = command('plan', app, '--json')
            self.assertTrue(offline['ok'])
            self.assertEqual([], seen)
            before = self.inventory()
            remote = command('--url', 'https://deploy.example.test', 'plan', app, '--remote', '--json')
            self.assertEqual('preview', remote['target'])
            self.assertEqual('preview', remote['remote']['target'])
            self.assertEqual(('POST', self.path), seen[0][:2])
            self.assertNotIn('private-source-text', json.dumps(seen))
            self.assertEqual(len('private-source-text'), seen[0][2]['unpacked_bytes'])
            self.assertEqual(before, self.inventory())
            denied = command('--url', 'https://deploy.example.test', 'plan', app, '--remote', '--prod', '--json', status=1)
            self.assertEqual('remote_preflight_failed', denied['error']['code'])
