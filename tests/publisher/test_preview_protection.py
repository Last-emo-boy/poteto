from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.backend import BackendError, Dokploy
from publisher.config import Settings
from publisher.models import DeploymentInput
from publisher.preview_protection import COOKIE, CHECK_PATH, LOGIN_PATH, decorate, probe
from publisher.store import PREVIEW_SCOPE, ident
from publisher.worker import Worker


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        settings=Settings(self.root,new_app_domain='page.example.test',personal_previews=True,personal_production=True,preview_protection=True)
        self.app=create_app(settings);self.store=self.app.state.store
        with self.store.connection() as db:
            self.project=self.store.enroll_cli_identity(db,'issuer','alice')
            account=db.execute('SELECT account_id FROM projects WHERE id=?',(self.project,)).fetchone()[0]
            actor={'id':None,'account_id':account}
            self.owner=self.store.mint_token(db,actor,name='owner',scopes=['*'],project_ids=None,expires=time.time()+3600)
        self.actor=self.store.authenticate(self.owner['token'])
        self.api=TestClient(self.app,base_url='https://testserver');self.addCleanup(self.api.close)
        self.headers={'Authorization':'Bearer '+self.owner['token']}
        self.base='/v1/projects/'+self.project+'/protection'
        self.backend=Mock();self.worker=Worker(self.store,self.backend)
        self.release=self.ready();self.second=self.ready()

    def ready(self):
        row=self.store.create_deployment(self.actor,self.project,DeploymentInput(target='preview').configuration(),None)
        with self.store.connection() as db:db.execute("UPDATE deployments SET state='ready' WHERE id=?",(row['id'],))
        return row['id']

    def enable(self):
        r=self.api.post(self.base,headers=self.headers);self.assertEqual(202,r.status_code,r.text)
        self.worker.run_once();return r.json()

    def check(self,release=None,**headers):
        return self.api.get(CHECK_PATH,headers={'X-Poteto-Release':release or self.release}|headers)

    def login(self,key,**extra):
        r=self.api.post('/internal/preview-access/login',headers={'X-Poteto-Release':self.release}|extra,
                        data={'access_key':key,'next':'/dashboard?tab=one'},follow_redirects=False)
        self.api.cookies.clear();return r

    def test_owner_consent_scoped_permissions_and_no_key_disclosure(self):
        with self.store.connection() as db:
            limited=self.store.mint_token(db,self.actor,name='agent',scopes=[PREVIEW_SCOPE],project_ids=[self.project],expires=time.time()+60)
        h={'Authorization':'Bearer '+limited['token']}
        self.assertEqual(403,self.api.post(self.base,headers=h).status_code)
        granted=self.api.post(self.base,headers=self.headers);self.assertEqual(202,granted.status_code)
        with self.store.connection() as db:
            stored=dict(db.execute('SELECT * FROM preview_protection').fetchone())
        self.assertNotIn(granted.json()['access_key'],json.dumps(stored))
        self.assertEqual(409,self.api.post('/v1/projects/'+self.project+'/deployments',headers=self.headers,json={}).status_code)
        self.worker.run_once()
        view=self.api.get(self.base,headers=self.headers).json()
        self.assertTrue(view['protected']);self.assertNotIn('key_hash',view);self.assertNotIn('access_key',view)
        self.assertEqual(409,self.api.post(self.base,headers=self.headers).status_code)

    def test_key_cookie_and_probe_are_separate_from_application_credentials(self):
        grant=self.enable();self.assertEqual(401,self.check(Authorization='Bearer '+self.owner['token']).status_code)
        self.assertEqual(204,self.check(**{'X-Poteto-Access-Key':grant['access_key']}).status_code)
        wrong=self.login('wrong');self.assertEqual(401,wrong.status_code);self.assertIn('did not match',wrong.text)
        logged=self.login(grant['access_key']);self.assertEqual(303,logged.status_code)
        self.assertIn('Secure',logged.headers['set-cookie']);self.assertIn('HttpOnly',logged.headers['set-cookie'])
        value=logged.cookies.get(COOKIE);self.assertTrue(value)
        response=self.check(Cookie='app_session=abc; '+COOKIE+'='+value+'; csrf=xyz')
        self.assertEqual(204,response.status_code);self.assertEqual('app_session=abc; csrf=xyz',response.headers['cookie'])
        self.assertEqual(401,self.check(self.second,Cookie=COOKIE+'='+value).status_code)
        self.assertEqual(403,self.login(grant['access_key'],Origin='https://other.test').status_code)
        self.assertEqual(204,self.check(**{'X-Poteto-Probe':probe(self.store,self.release,'/healthz'),'X-Forwarded-Uri':'/healthz','X-Forwarded-Method':'GET'}).status_code)
        self.assertEqual(401,self.check(**{'X-Poteto-Probe':probe(self.store,self.release,'/healthz'),'X-Forwarded-Uri':'/private','X-Forwarded-Method':'GET'}).status_code)
        self.assertEqual(401,self.check(**{'X-Poteto-Probe':probe(self.store,self.release,'/healthz'),'X-Forwarded-Uri':'/healthz','X-Forwarded-Method':'POST'}).status_code)

    def test_rotation_invalidates_old_key_and_browser_session_and_disable_is_explicit(self):
        original=self.enable();cookie=self.login(original['access_key']).cookies.get(COOKIE)
        rotated=self.api.post(self.base+'/rotate',headers=self.headers).json()
        self.assertNotEqual(original['access_key'],rotated['access_key'])
        self.assertEqual(401,self.check(**{'X-Poteto-Access-Key':original['access_key']}).status_code)
        self.assertEqual(401,self.check(Cookie=COOKIE+'='+cookie).status_code)
        self.assertEqual(204,self.check(**{'X-Poteto-Access-Key':rotated['access_key']}).status_code)
        self.store.settings=replace(self.store.settings,preview_protection=False)
        self.assertEqual(401,self.check().status_code)
        r=self.api.post(self.base+'/disable',headers=self.headers);self.assertEqual(202,r.status_code)
        self.worker.run_once();self.assertEqual(204,self.check().status_code)
        self.assertFalse(self.api.get(self.base,headers=self.headers).json()['protected'])

    def test_retry_and_cleanup_keep_the_operation_and_project_boundaries(self):
        grant=self.api.post(self.base,headers=self.headers).json()
        self.backend.protect_project.side_effect=BackendError('temporary gateway failure');self.worker.run_once()
        self.assertEqual('failed',self.api.get(self.base,headers=self.headers).json()['state'])
        retried=self.api.post(self.base+'/retry',headers=self.headers).json();self.assertEqual(grant['job_id'],retried['job_id'])
        self.backend.protect_project.side_effect=None;self.worker.run_once()
        self.assertTrue(self.api.get(self.base,headers=self.headers).json()['protected'])
        self.api.delete('/v1/projects/'+self.project,headers=self.headers);self.worker.run_once()
        with self.store.connection() as db:self.assertIsNone(db.execute('SELECT 1 FROM preview_protection').fetchone())

    def test_login_body_and_return_location_are_bounded(self):
        grant=self.enable()
        r=self.api.post('/internal/preview-access/login',headers={'X-Poteto-Release':self.release,'Content-Type':'application/x-www-form-urlencoded'},content=b'x'*4097)
        self.assertEqual(413,r.status_code)
        r=self.api.post('/internal/preview-access/login',headers={'X-Poteto-Release':self.release},data={'access_key':grant['access_key'],'next':'//evil.test/path'},follow_redirects=False)
        self.assertTrue(r.headers['location'].startswith('https://preview-'+self.release+'.page.example.test/'))
        self.api.cookies.clear()
        self.assertIn('Access key',self.check().text)

    def test_route_decoration_is_private_only_idempotent_and_strips_credentials(self):
        self.enable()
        backend=Dokploy({'releases':str(self.root/'runtime'),'routes':str(self.root/'routes')},self.store.settings);backend.store=self.store
        (self.root/'routes').mkdir()
        with self.store.connection() as db:
            release=dict(db.execute('SELECT * FROM deployments WHERE id=?',(self.release,)).fetchone())
            release.update({'app_domain':'page.example.test','preview_mode':'isolated'})
        path,_=backend.write_route(release,'http://upstream:8080')
        document=json.loads(path.read_text());self.assertEqual(document,decorate(backend,release,document))
        name='publisher-preview-'+self.release
        self.assertEqual('false',str(document['http']['middlewares'][name+'-guard']['forwardAuth']['trustForwardHeader']).lower())
        self.assertEqual(['Cookie'],document['http']['middlewares'][name+'-guard']['forwardAuth']['authResponseHeaders'])
        self.assertEqual('',document['http']['middlewares'][name+'-clean']['headers']['customRequestHeaders']['X-Poteto-Access-Key'])
        prod,_=backend.write_route(release,'http://upstream:8080',production=True)
        self.assertNotIn('forwardAuth',prod.read_text());self.assertNotIn(LOGIN_PATH,prod.read_text())

    def test_gateway_update_keeps_static_production_out_of_the_private_route(self):
        self.enable();root=self.root/'gateway';root.mkdir()
        backend=Dokploy({'releases':str(self.root/'runtime'),'routes':str(root)},self.store.settings);backend.store=self.store
        with self.store.connection() as db:
            db.execute('UPDATE projects SET current_deployment=? WHERE id=?',(self.release,self.project))
            release=dict(db.execute('SELECT * FROM deployments WHERE id=?',(self.release,)).fetchone())
            release.update({'app_domain':'page.example.test','preview_mode':'isolated'})
        backend.write_route(release,'http://poteto-static:8080')
        production=root/('publisher-'+self.project+'.yml')
        production.write_text(json.dumps({'http':{'services':{'publisher-'+self.project:{'loadBalancer':{'servers':[{'url':'http://127.0.0.1:80'}]}}}}}))
        connection=Mock();connection.getresponse.return_value.status=401
        connection.getresponse.return_value.getheader.return_value='enabled'
        with patch('publisher.backend.http.client.HTTPConnection',return_value=connection):backend.protect_project(self.project,lambda:None)
        self.assertEqual([{'url':'http://poteto-static:8080'}],json.loads(production.read_text())['http']['services']['publisher-'+self.project]['loadBalancer']['servers'])

    def test_expired_and_wrong_purpose_tokens_cannot_authenticate(self):
        self.enable()
        with self.store.connection() as db:revision=db.execute('SELECT revision FROM preview_protection').fetchone()[0]
        stale=self.store.cipher.encrypt_at_time(json.dumps({'purpose':'preview-session','release':self.release,'revision':revision}).encode(),int(time.time())-13*3600).decode()
        self.assertEqual(401,self.check(Cookie=COOKIE+'='+stale).status_code)
        other=self.store.cipher.encrypt(json.dumps({'purpose':'environment','release':self.release,'revision':revision}).encode()).decode()
        self.assertEqual(401,self.check(Cookie=COOKIE+'='+other).status_code)

    def test_asset_authorization_does_not_take_a_writer_lock_and_disabled_accounts_fail_closed(self):
        key=self.enable()['access_key']
        with self.store.connection() as db:
            self.assertEqual(204,self.check(**{'X-Poteto-Access-Key':key}).status_code)
            db.execute('UPDATE accounts SET enabled=0 WHERE id=?',(self.actor['account_id'],))
        self.assertEqual(404,self.check(**{'X-Poteto-Access-Key':key}).status_code)
