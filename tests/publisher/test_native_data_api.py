from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from publisher.api import create_app
from publisher import capacity
from publisher.config import Settings
from publisher.login_contract import ISSUER
from publisher.store import NATIVE_DATABASE_DRIVER, PREVIEW_DATA_SCOPE, PREVIEW_SCOPE, Store
from publisher.worker import Worker


class NativeDataApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.settings = Settings(Path(temporary.name), personal_previews=True, native_databases=True)
        self.app = create_app(self.settings); self.store = self.app.state.store
        with self.store.connection() as db:
            self.project = self.store.enroll_cli_identity(db, ISSUER, 'native-data-owner')
            self.other = self.store.enroll_cli_identity(db, ISSUER, 'other-data-owner')
            self.account = db.execute('SELECT account_id FROM projects WHERE id=?', (self.project,)).fetchone()[0]
        self.client = self.client_for([PREVIEW_SCOPE, PREVIEW_DATA_SCOPE])
        self.base = '/v1/projects/' + self.project

    def client_for(self, scopes):
        with self.store.connection() as db:
            token = self.store.mint_token(db, {'account_id': self.account, 'id': None}, name='native data test',
                scopes=scopes, project_ids=[self.project], expires=9999999999)['token']
        client = TestClient(self.app, headers={'Authorization': 'Bearer '+token})
        self.addCleanup(client.close)
        return client

    def create_database(self, *, ready=False):
        response = self.client.post(self.base+'/databases', json={'stage':'preview'})
        self.assertEqual(202, response.status_code, response.text)
        resource = response.json()
        if ready:
            with self.store.connection() as db:
                db.execute("UPDATE data_resources SET state='ready' WHERE id=?", (resource['id'],))
                db.execute("UPDATE jobs SET state='completed' WHERE resource_id=?", (resource['id'],))
        return resource

    def test_data_management_is_explicit_preview_only_and_project_scoped(self):
        deploy_only = self.client_for([PREVIEW_SCOPE])
        for path in ('/databases', '/volumes'):
            self.assertEqual(403, deploy_only.post(self.base+path, json={'stage':'preview'}).status_code)
        self.assertEqual(403, deploy_only.get(self.base+'/data').status_code)
        self.assertEqual(404, self.client.post(self.base+'/databases', json={}).status_code)
        self.assertEqual(422, self.client.post(self.base+'/volumes', json={'stage':'preview'}).status_code)
        self.assertEqual(404, self.client.post('/v1/projects/'+self.other+'/databases', json={'stage':'preview'}).status_code)
        resource = self.create_database()
        self.assertEqual(403, deploy_only.get('/v1/data/'+resource['id']).status_code)
        view = self.client.get('/v1/data/'+resource['id']).json()
        self.assertEqual(NATIVE_DATABASE_DRIVER, view['runtime_driver'])
        self.assertEqual({'scope':'dedicated database','gib':2}, view['disk_limit'])
        self.assertNotIn('password', json.dumps(view))
        self.assertEqual(200, self.client.get('/v1/jobs/'+resource['job_id']).status_code)
        self.assertEqual(403, deploy_only.get('/v1/jobs/'+resource['job_id']).status_code)

    def test_native_backend_never_provisions_a_vm_or_uses_dokploy(self):
        resource = self.create_database()
        backend = Mock(); backend.store = self.store
        backend.execution_client.return_value.native_database.return_value = {'ready':True,'driver':NATIVE_DATABASE_DRIVER}
        with patch('publisher.data_backend.RuntimeRegistry') as vm:
            self.assertTrue(Worker(self.store, backend).run_once())
        vm.assert_not_called(); backend.api.assert_not_called()
        client = backend.execution_client.return_value
        call = client.native_database.call_args
        self.assertEqual((self.account, resource['id'], self.project, 'ensure'), call.args)
        self.assertEqual({'admin_password','app_password'}, set(call.kwargs))
        view = self.client.get('/v1/data/'+resource['id'])
        self.assertEqual('ready', view.json()['state'])
        self.assertNotIn(call.kwargs['app_password'], view.text)

    def test_image_binding_injects_only_app_credentials_and_reserves_both_resources(self):
        resource = self.create_database(ready=True)
        response = self.client.post(self.base+'/deployments', json={
            'runtime':'image','target':'preview','database_id':resource['id']})
        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual('gvisor', response.json()['runtime_driver'])
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM deployments WHERE id=?', (response.json()['id'],)).fetchone()
            credentials = self.store.unseal(db.execute('SELECT definition FROM data_resources WHERE id=?', (resource['id'],)).fetchone()[0])
            environment = self.store.unseal(row['environment'])
            reservations = capacity.reservations(db)
        self.assertIn(credentials['app_password'], environment['DATABASE_URL'])
        self.assertNotIn(credentials['admin_password'], environment['DATABASE_URL'])
        self.assertNotIn(credentials['app_password'], response.text)
        self.assertEqual({'memory_mib':512,'vcpus':.35}, reservations['sandbox'])
        deletion = self.client.delete('/v1/data/'+resource['id']+'?destroy_data=true')
        self.assertEqual(409, deletion.status_code)

    def test_database_reservation_prevents_pool_overcommit_and_is_released_only_after_delete(self):
        small = create_app(replace(self.settings, sandbox_memory_limit_mib=256, sandbox_cpu_limit=.25))
        with TestClient(small, headers=self.client.headers) as client:
            resource = client.post(self.base+'/databases', json={'stage':'preview'}).json()
            with self.store.connection() as db:
                totals = capacity.reservations(db)
                self.assertEqual(capacity.DATABASE, totals['sandbox'])
                db.execute("UPDATE data_resources SET state='failed' WHERE id=?", (resource['id'],))
                db.execute("UPDATE jobs SET state='failed' WHERE resource_id=?", (resource['id'],))
            response = client.post(self.base+'/deployments', json={'runtime':'image','target':'preview'})
            self.assertEqual(429, response.status_code, response.text)
            deletion = client.delete('/v1/data/'+resource['id']+'?destroy_data=true')
            self.assertEqual(202, deletion.status_code, deletion.text)
            backend = Mock(); backend.store = self.store
            with patch('publisher.data_backend.RuntimeRegistry') as vm:
                self.assertTrue(Worker(self.store, backend).run_once())
            vm.assert_not_called(); backend.api.assert_not_called()
            backend.execution_client.return_value.native_database.assert_called_once_with(
                self.account, resource['id'], self.project, 'delete')
            receipt = client.get('/v1/jobs/'+deletion.json()['job_id'])
            self.assertEqual('completed', receipt.json()['state'])
            with self.store.connection() as db:
                self.assertEqual(capacity.empty(), capacity.reservations(db)['sandbox'])

    def test_native_flag_is_off_by_default_and_does_not_change_legacy_data(self):
        disabled = create_app(replace(self.settings, native_databases=False))
        with TestClient(disabled, headers=self.client.headers) as client:
            self.assertEqual(503, client.post(self.base+'/databases', json={'stage':'preview'}).status_code)
        owner = self.store.authenticate(self.store.create_account('legacy-data-owner')['token'])
        project = self.store.create_project(owner, 'legacy')['id']
        from publisher.data_store import DataStore
        resource = DataStore(self.store).create(owner, project, 'database', 'production')
        with self.store.connection() as db:
            row = dict(db.execute('SELECT * FROM data_resources WHERE id=?', (resource['id'],)).fetchone())
        self.assertIsNone(row['runtime_driver'])
        self.assertEqual({'scope':'shared tenant VM','gib':12}, DataStore.view(row)['disk_limit'])

    def test_schema_upgrade_keeps_legacy_data_and_credentials(self):
        owner = self.store.authenticate(self.store.create_account('schema-owner')['token'])
        project = self.store.create_project(owner, 'legacy')['id']
        from publisher.data_store import DataStore
        resource = DataStore(self.store).create(owner, project, 'database', 'production')
        with self.store.connection() as db:
            definition = db.execute('SELECT definition FROM data_resources WHERE id=?', (resource['id'],)).fetchone()[0]
            db.execute('ALTER TABLE data_resources DROP COLUMN runtime_driver')
            db.execute('ALTER TABLE jobs DROP COLUMN preview_access')
        upgraded = Store(self.settings)
        with upgraded.connection() as db:
            row = db.execute('SELECT * FROM data_resources WHERE id=?', (resource['id'],)).fetchone()
            self.assertEqual(definition, row['definition']); self.assertIsNone(row['runtime_driver'])
            self.assertEqual(0, db.execute('SELECT preview_access FROM jobs WHERE id=?', (resource['job_id'],)).fetchone()[0])

    def test_manual_preview_data_scope_cannot_mix_with_production_permissions(self):
        owner = self.client_for(['*'])
        for scopes in ([PREVIEW_DATA_SCOPE], [PREVIEW_SCOPE, PREVIEW_DATA_SCOPE, 'databases:write']):
            response = owner.post('/v1/tokens', json={'name':'bad','project_ids':[self.project],'scopes':scopes})
            self.assertEqual(422, response.status_code, response.text)

    def test_production_resources_and_jobs_remain_hidden_from_preview_tokens(self):
        owner = self.client_for(['*'])
        with self.store.connection() as db:
            db.execute("UPDATE projects SET preview_mode='legacy' WHERE id=?", (self.project,))
        response = owner.post(self.base+'/databases', json={'stage':'production'})
        self.assertEqual(202, response.status_code, response.text)
        resource = response.json()
        self.assertEqual([], self.client.get(self.base+'/data').json())
        self.assertEqual(404, self.client.get('/v1/data/'+resource['id']).status_code)
        self.assertEqual(404, self.client.get('/v1/jobs/'+resource['job_id']).status_code)
        self.assertEqual(404, self.client.delete('/v1/data/'+resource['id']+'?destroy_data=true').status_code)
