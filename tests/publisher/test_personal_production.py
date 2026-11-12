from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.backend import BackendError, Dokploy
from publisher.cli_login import login
from publisher.config import Settings
from publisher.login_contract import ISSUER, LOGIN_PATH
from publisher.oidc_login import CliLogin, PRODUCTION_SCOPES, PROJECT_MANAGEMENT_SCOPES
from publisher.store import PREVIEW_SCOPE
from publisher.worker import Worker


class PersonalProductionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root, personal_previews=True, personal_production=True,
                                 native_databases=True, sandbox_memory_limit_mib=4096, sandbox_cpu_limit=2)
        self.app = create_app(self.settings); self.store = self.app.state.store
        with self.store.connection() as db:
            self.project = self.store.enroll_cli_identity(db, ISSUER, 'production-user')
            self.account = db.execute('SELECT account_id FROM projects WHERE id=?', (self.project,)).fetchone()[0]
        self.base = '/v1/projects/' + self.project
        self.publisher = self.client(PRODUCTION_SCOPES)
        self.manager = self.client(PRODUCTION_SCOPES + PROJECT_MANAGEMENT_SCOPES)
        self.preview = self.client([PREVIEW_SCOPE])

    def client(self, scopes):
        with self.store.connection() as db:
            token = self.store.mint_token(db, {'id':None, 'account_id':self.account}, name='test', scopes=scopes,
                                          project_ids=[self.project], expires=9999999999)['token']
        client = TestClient(self.app, headers={'Authorization':'Bearer ' + token})
        self.addCleanup(client.close)
        return client

    def ready(self, config, client=None):
        result = (client or self.publisher).post(self.base + '/deployments', json=config)
        self.assertEqual(201, result.status_code, result.text)
        release = result.json()
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready' WHERE id=?", (release['id'],))
        return release

    def test_explicit_production_uses_existing_scoped_permissions_not_admin_access(self):
        denied = self.preview.post(self.base + '/deployments', json={'runtime':'image', 'target':'production'})
        self.assertEqual(403, denied.status_code)
        for runtime, driver in [('static','static-artifact'), ('image','gvisor')]:
            result = self.publisher.post(self.base + '/preflight', json={
                'configuration':{'runtime':runtime, 'target':'production'}, 'archive_bytes':1, 'unpacked_bytes':1, 'files':1})
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual(driver, result.json()['runtime_driver'])
        self.assertEqual(403, self.publisher.get(self.base + '/env').status_code)
        self.assertEqual(403, self.publisher.post(self.base + '/databases', json={}).status_code)
        self.assertEqual(403, self.publisher.post('/v1/tokens', json={'name':'bad','scopes':['*']}).status_code)
        self.assertEqual(403, self.manager.post('/v1/projects', json={'name':'other'}).status_code)
        capability = self.publisher.get(self.base + '/capabilities').json()['production']
        self.assertEqual({'enabled':True, 'authorized':True, 'environment':False, 'data':False}, capability)

    def test_disabled_production_keeps_old_static_contract_but_never_falls_back_for_images(self):
        disabled = TestClient(create_app(replace(self.settings, personal_production=False)), headers=self.publisher.headers)
        self.addCleanup(disabled.close)
        self.assertEqual(422, disabled.post(self.base + '/deployments', json={'runtime':'image','target':'production'}).status_code)
        self.assertEqual(201, disabled.post(self.base + '/deployments', json={'runtime':'static','target':'production'}).status_code)

    def test_production_and_preview_databases_are_distinct_and_binding_cannot_cross_stages(self):
        resources = {}
        for stage in ('preview','production'):
            result = self.manager.post(self.base + '/databases', json={'stage':stage})
            self.assertEqual(202, result.status_code, result.text)
            resources[stage] = result.json()['id']
            with self.store.connection() as db:
                db.execute("UPDATE data_resources SET state='ready' WHERE id=?", (resources[stage],))
                db.execute("UPDATE jobs SET state='completed' WHERE resource_id=?", (resources[stage],))
        self.assertNotEqual(resources['preview'], resources['production'])
        for target, wrong in [('preview','production'), ('production','preview')]:
            result = self.publisher.post(self.base + '/preflight', json={'configuration':{
                'runtime':'image','target':target,'database_id':resources[wrong]}, 'archive_bytes':1,'unpacked_bytes':1,'files':1})
            self.assertEqual(409, result.status_code, result.text)
        release = self.ready({'runtime':'image','target':'production','database_id':resources['production']})
        self.assertEqual('gvisor', release['runtime_driver'])
        self.assertEqual(404, self.preview.get('/v1/deployments/' + release['id']).status_code)
        self.assertEqual(409, self.manager.delete('/v1/data/' + resources['production'] + '?destroy_data=true').status_code)
        with self.store.connection() as db:
            self.assertEqual({'native-postgres'}, {row[0] for row in db.execute('SELECT runtime_driver FROM data_resources')})
            self.assertEqual(0, db.execute('SELECT count(*) FROM execution_targets').fetchone()[0])

    def test_sandbox_production_switch_keeps_data_and_environment_checks(self):
        preview = self.ready({'runtime':'image','target':'preview'}, self.preview)
        result = self.publisher.post(self.base + '/promote', json={'deployment_id':preview['id']})
        self.assertEqual(202, result.status_code, result.text)
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET state='completed'")
        self.manager.patch(self.base + '/env?target=production', json={'values':{'MODE':'production'}})
        result = self.publisher.post(self.base + '/promote', json={'deployment_id':preview['id']})
        self.assertEqual(409, result.status_code)
        production = self.ready({'runtime':'image','target':'production'})
        result = self.publisher.post(self.base + '/rollback', json={'deployment_id':production['id']})
        self.assertEqual(202, result.status_code, result.text)

    def test_route_switch_uses_immutable_host_and_restores_previous_route_on_failure(self):
        release = self.ready({'runtime':'image','target':'production'})
        with self.store.connection() as db:
            row = dict(db.execute('SELECT d.*,p.preview_mode,p.app_domain FROM deployments d JOIN projects p ON p.id=d.project_id WHERE d.id=?', (release['id'],)).fetchone())
        backend = Dokploy({'routes':str(self.root), 'releases':str(self.root/'releases')}, self.settings)
        backend.store = self.store
        backend.health = Mock(); backend.target = Mock(return_value=None); backend.public_health = Mock()
        backend.sandbox_action = Mock(return_value={'http_port':23100})
        connection = Mock(); connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.getheader.return_value = release['id']
        with patch('publisher.backend.http.client.HTTPConnection', return_value=connection):
            backend.promote(row)
        route = self.root / ('publisher-' + self.project + '.yml')
        value = json.loads(route.read_text()); key = 'publisher-host-' + self.project
        self.assertEqual(backend.hostname(row), value['http']['middlewares'][key]['headers']['customRequestHeaders']['Host'])
        self.assertEqual(backend.hostname(row, production=True), value['http']['middlewares'][key]['headers']['customRequestHeaders']['X-Forwarded-Host'])
        self.assertEqual('http://10.201.0.2:23100', value['http']['services']['publisher-' + self.project]['loadBalancer']['servers'][0]['url'])
        previous = route.read_bytes(); backend.public_health.side_effect = BackendError('origin unavailable')
        with patch('publisher.backend.http.client.HTTPConnection', return_value=connection), self.assertRaises(BackendError):
            backend.promote(row)
        self.assertEqual(previous, route.read_bytes())

    def test_production_import_keeps_native_driver_and_skips_restored_migration(self):
        from test_preview_archives import bundle
        content = bundle(target='production', production_env={'MODE':'production'})
        result = self.manager.post(self.base + '/imports', json={
            'bundle_id':'a'*24, 'size':len(content), 'kinds':['database']})
        self.assertEqual(201, result.status_code, result.text)
        operation = result.json()['id']; path = '/v1/imports/' + operation
        self.assertEqual(200, self.manager.put(path + '/part?offset=0', content=content,
            headers={'Content-Type':'application/octet-stream'}).status_code)
        self.assertEqual(202, self.manager.post(path + '/submit').status_code)
        backend = Mock(); backend.config = {'execution_enabled':True}
        worker = Worker(self.store, backend)
        def deploy(job, release, *, imported):
            self.assertEqual('gvisor', release['runtime_driver'])
            self.assertEqual('production', json.loads(release['config'])['target'])
            self.assertTrue(imported['skip_migration'])
            self.assertFalse(release['preview_access'])
            with self.store.connection() as db:
                db.execute("UPDATE deployments SET state='ready' WHERE id=?", (operation,))
        with patch('publisher.project_imports.DataBackend') as factory, patch.object(worker, 'deploy', side_effect=deploy):
            factory.return_value.create.return_value = {'driver':'native-postgres'}
            self.assertTrue(worker.run_once())
            factory.return_value.client.restore_native_database.assert_called_once()
        self.assertEqual('completed', self.manager.get(path).json()['state'])
        with self.store.connection() as db:
            resource = db.execute('SELECT stage,runtime_driver FROM data_resources').fetchone()
            self.assertEqual(('production','native-postgres'), tuple(resource))
            project = db.execute('SELECT * FROM projects WHERE id=?', (self.project,)).fetchone()
            self.assertEqual({'MODE':'production'}, self.store.environment(project, 'production'))
        self.assertEqual(403, self.preview.get(path).status_code)


class ProductionLoginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = CliLogin('test-client', {}, auto_provision=True)
        with patch('publisher.oidc_login.load_login', return_value=self.service):
            self.app = create_app(Settings(self.root, personal_previews=True, personal_production=True))
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.body = {'code':'test', 'verifier':'v'*43, 'nonce':'n'*43}

    def exchange(self, **fields):
        with patch.object(self.service, 'identity', return_value='new-user'):
            return self.client.post(LOGIN_PATH, json=self.body | fields)

    def test_default_stays_preview_and_production_requires_explicit_personal_consent(self):
        for fields, scopes in [({}, [PREVIEW_SCOPE]), ({'production':True}, PRODUCTION_SCOPES),
                               ({'production':True,'manage_project':True}, PRODUCTION_SCOPES + PROJECT_MANAGEMENT_SCOPES)]:
            response = self.exchange(**fields); self.assertEqual(201, response.status_code, response.text)
            actor = self.app.state.store.authenticate(response.json()['token'])
            self.assertEqual(set(scopes), set(actor['scopes']))
            self.assertEqual([response.json()['project_id']], actor['projects'])
            self.assertNotIn('tokens:write', actor['scopes'])
        self.service.subjects['new-user'] = self.app.state.store.create_project(
            self.app.state.store.authenticate(self.app.state.store.create_account('legacy')['token']), 'legacy')['id']
        self.assertEqual(403, self.exchange(production=True).status_code)

    def test_conflicting_or_unavailable_permissions_do_not_issue_credentials(self):
        for fields in ({'manage_project':True}, {'production':True,'manage_env':True}, {'production':True,'manage_data':True}):
            self.assertEqual(422, self.exchange(**fields).status_code)
        self.app.state.store.settings = replace(self.app.state.store.settings, personal_production=False)
        self.assertEqual(503, self.exchange(production=True).status_code)
        with self.app.state.store.connection() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM tokens').fetchone()[0])

    def test_portable_client_checks_mode_before_browser_and_sends_explicit_flags(self):
        client = Mock(url='https://deploy.iflink.tech')
        output = self.root/'token.json'
        for fields in ({'manage_project':True}, {'production':True,'manage_env':True}):
            with patch('publisher.cli_login.authorize') as browser, self.assertRaises(ValueError):
                login(client, output, **fields)
            browser.assert_not_called(); client.request.assert_not_called()
        client.request.side_effect = [{'issuer':ISSUER,'client_id':'test','production_publishing':True},
                                      {'token':'synthetic-private-value','project_id':'a'*24}]
        with patch('publisher.cli_login.authorize', return_value=self.body):
            login(client, output, production=True, manage_project=True)
        self.assertEqual(True, client.request.call_args.args[2]['production'])
        self.assertEqual(True, client.request.call_args.args[2]['manage_project'])
