from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.config import Settings
from publisher.login_contract import ISSUER
from publisher.models import DeploymentInput
from publisher.store import PREVIEW_ENV_SCOPE, PREVIEW_SCOPE, PREVIEW_WEB_SCOPE, Problem


class PreviewPermissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(self.root, personal_previews=True, sandbox_web=True)
        self.app = create_app(self.settings); self.store = self.app.state.store
        with self.store.connection() as db:
            self.project = self.store.enroll_cli_identity(db, ISSUER, 'web-owner')
            self.other = self.store.enroll_cli_identity(db, ISSUER, 'other-owner')
            self.account = db.execute('SELECT account_id FROM projects WHERE id=?', (self.project,)).fetchone()[0]
        self.base = '/v1/projects/' + self.project

    def client(self, scopes, projects=None):
        with self.store.connection() as db:
            token = self.store.mint_token(db, {'account_id':self.account,'id':None}, name='permission test',
                scopes=scopes, project_ids=projects or [self.project], expires=9999999999)['token']
        client = TestClient(self.app, headers={'Authorization':'Bearer '+token})
        self.addCleanup(client.close)
        return client

    def test_existing_preview_tokens_remain_deploy_only_and_none_is_wire_compatible(self):
        client = self.client([PREVIEW_SCOPE])
        self.assertNotIn('outbound_network', DeploymentInput().configuration())
        self.assertEqual(403, client.get(self.base+'/env?target=preview').status_code)
        response = client.post(self.base+'/preflight', json={'configuration':{'runtime':'image','target':'preview','outbound_network':'web'},
            'archive_bytes':100,'unpacked_bytes':100,'files':1})
        self.assertEqual(403, response.status_code, response.text)
        capabilities = client.get(self.base+'/capabilities').json()
        self.assertFalse(capabilities['preview_environment'])
        self.assertFalse(capabilities['web_egress']['authorized'])
        self.assertEqual(201, client.post(self.base+'/deployments', json={'target':'preview'}).status_code)

    def test_preview_environment_permission_never_grants_production_or_cross_project_access(self):
        client = self.client([PREVIEW_SCOPE, PREVIEW_ENV_SCOPE])
        secret = 'private-value-for-env-test'
        response = client.patch(self.base+'/env?target=preview', json={'values':{'API_KEY':secret}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn(secret, response.text)
        self.assertEqual(['API_KEY'], client.get(self.base+'/env?target=preview').json()['keys'])
        for method, body in [('get',None),('patch',{'values':{'FORBIDDEN':'value'}}),('put',{'values':{}})]:
            call = getattr(client,method)
            response = call(self.base+'/env', **({'json':body} if body is not None else {}))
            self.assertEqual(403, response.status_code, response.text)
        self.assertEqual(404, client.get('/v1/projects/'+self.other+'/env?target=preview').status_code)
        release = client.post(self.base+'/deployments', json={'runtime':'image','target':'preview'}).json()
        self.assertNotIn(secret, json.dumps(release))
        with self.store.connection() as db:
            row = db.execute('SELECT environment FROM deployments WHERE id=?',(release['id'],)).fetchone()
            self.assertNotIn(secret, row['environment'])
            self.assertEqual(secret, self.store.unseal(row['environment'])['API_KEY'])
        self.assertEqual([], client.patch(self.base+'/env?target=preview',json={'remove':['API_KEY']}).json()['keys'])

    def test_web_permission_and_operator_switch_are_both_required(self):
        client = self.client([PREVIEW_SCOPE, PREVIEW_WEB_SCOPE])
        config = {'runtime':'image','target':'preview','outbound_network':'web'}
        response = client.post(self.base+'/preflight', json={'configuration':config,'archive_bytes':100,'unpacked_bytes':100,'files':1})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('gvisor',response.json()['runtime_driver'])
        self.assertEqual('web',response.json()['configuration']['outbound_network'])
        actor = self.store.authenticate(client.headers['Authorization'].removeprefix('Bearer '))
        with self.assertRaisesRegex(Problem, 'not enabled'):
            from publisher.store import Store
            Store(replace(self.settings,sandbox_web=False)).create_deployment(actor,self.project,
                self.store.configuration(actor,self.project,DeploymentInput(**config)),None)
        self.assertEqual(422,client.post(self.base+'/deployments',json={'runtime':'static','target':'preview','outbound_network':'web'}).status_code)
        self.assertEqual(403,client.get(self.base+'/env?target=preview').status_code)
        self.assertEqual(404,client.post('/v1/projects/'+self.other+'/deployments',json=config).status_code)

    def test_manual_tokens_require_preview_companion_and_explicit_projects(self):
        owner = self.client(['*'])
        for scopes in [[PREVIEW_ENV_SCOPE],[PREVIEW_WEB_SCOPE],[PREVIEW_SCOPE,PREVIEW_ENV_SCOPE,'env:write']]:
            response = owner.post('/v1/tokens',json={'name':'bad','scopes':scopes,'project_ids':[self.project]})
            self.assertEqual(422,response.status_code,response.text)
        response = owner.post('/v1/tokens',json={'name':'limited','scopes':[PREVIEW_SCOPE,PREVIEW_ENV_SCOPE,PREVIEW_WEB_SCOPE],
            'project_ids':[self.project]})
        self.assertEqual(201,response.status_code,response.text)
        limited = self.client([PREVIEW_SCOPE])
        self.assertEqual(403,limited.post('/v1/tokens',json={'name':'escalate','scopes':[PREVIEW_SCOPE,PREVIEW_WEB_SCOPE],
            'project_ids':[self.project]}).status_code)

    def test_environment_write_preserves_export_barrier(self):
        client = self.client([PREVIEW_SCOPE,PREVIEW_ENV_SCOPE])
        with self.store.connection() as db:
            db.execute("INSERT INTO jobs(id,project_id,kind,state,created,updated) VALUES(?,?,'project-export','queued',0,0)",('f'*24,self.project))
        self.assertEqual(409,client.patch(self.base+'/env?target=preview',json={'values':{'API_KEY':'value'}}).status_code)
