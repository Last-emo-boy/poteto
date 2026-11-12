from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from publisher import cli
from publisher.api import create_app
from publisher.cli_login import login
from publisher.config import Settings
from publisher.login_contract import ISSUER, LOGIN_PATH, PRODUCTION_SCOPES
from publisher.oidc_login import CliLogin
from publisher.store import PREVIEW_SCOPE


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.settings=Settings(self.root,personal_previews=True,personal_production=True,personal_workspaces=True)
        self.auth=CliLogin('workspace-test',{},auto_provision=True)
        with patch('publisher.oidc_login.load_login',return_value=self.auth):self.app=create_app(self.settings)
        self.api=TestClient(self.app);self.addCleanup(self.api.close);self.store=self.app.state.store
        self.alice,self.grant=self.workspace('alice');self.bob,self.other=self.workspace('bob')

    def exchange(self,subject,**options):
        with patch.object(self.auth,'identity',return_value=subject):
            return self.api.post(LOGIN_PATH,json={'code':'code','nonce':'n'*43,'verifier':'v'*43}|options)

    def workspace(self,subject):
        result=self.exchange(subject,production=True,manage_workspace=True)
        self.assertEqual(201,result.status_code,result.text)
        client=TestClient(self.app,headers={'Authorization':'Bearer '+result.json()['token']})
        self.addCleanup(client.close);return client,result.json()

    def project(self,name='second'):
        result=self.alice.post('/v1/projects',json={'name':name})
        self.assertEqual(201,result.status_code,result.text);return result.json()['id']

    def token(self,project,scopes):
        return self.alice.post('/v1/tokens',json={'name':'agent','scopes':scopes,'project_ids':[project],'expires_in_days':30})

    def test_workspace_consent_is_distinct_from_project_management(self):
        owner=self.store.authenticate(self.grant['token']);self.assertIsNone(owner['projects'])
        self.assertIn('tokens:write',owner['scopes']);self.assertNotIn('*',owner['scopes'])
        project=self.exchange('alice',production=True,manage_project=True).json()
        actor=self.store.authenticate(project['token'])
        self.assertEqual([self.grant['project_id']],actor['projects']);self.assertNotIn('tokens:write',actor['scopes'])
        for options in [{'manage_workspace':True},{'production':True,'manage_project':True,'manage_workspace':True},
                        {'production':True,'manage_workspace':True,'manage_env':True}]:
            self.assertEqual(422,self.exchange('alice',**options).status_code)
        self.store.settings=replace(self.settings,personal_workspaces=False)
        self.assertEqual(503,self.exchange('alice',production=True,manage_workspace=True).status_code)

    def test_new_projects_are_isolated_and_five_project_quota_is_account_wide(self):
        projects=[self.grant['project_id']]+[self.project('site-'+str(i)) for i in range(4)]
        self.assertEqual(429,self.alice.post('/v1/projects',json={'name':'sixth'}).status_code)
        self.assertEqual(set(projects),{p['id'] for p in self.alice.get('/v1/projects').json()})
        self.assertEqual(404,self.bob.get('/v1/projects/'+projects[1]).status_code)
        for project in projects:
            with self.store.connection() as db:
                self.assertEqual('isolated',db.execute('SELECT preview_mode FROM projects WHERE id=?',(project,)).fetchone()[0])
        plan=self.alice.post('/v1/projects/'+projects[1]+'/preflight',json={
            'configuration':{'runtime':'image','target':'production'},'archive_bytes':1,'unpacked_bytes':1,'files':1})
        self.assertEqual('gvisor',plan.json()['runtime_driver'])
        before=self.alice.get('/v1/projects/'+projects[1]).json()['url']
        renamed=self.alice.patch('/v1/projects/'+projects[1],json={'name':'renamed'})
        self.assertEqual(before,renamed.json()['url'])

    def test_selected_project_login_is_scoped_and_does_not_move_the_default_binding(self):
        project=self.project()
        result=self.exchange('alice',project_id=project)
        self.assertEqual(201,result.status_code,result.text)
        self.assertEqual([project],self.store.authenticate(result.json()['token'])['projects'])
        self.assertEqual(404,self.exchange('bob',project_id=project).status_code)
        self.assertEqual(self.grant['project_id'],self.exchange('alice').json()['project_id'])

    def test_deleted_default_can_be_managed_without_recreating_an_account_or_project(self):
        home=self.grant['project_id']
        with self.store.connection() as db:db.execute("UPDATE projects SET state='deleted' WHERE id=?",(home,))
        workspace,grant=self.workspace('alice');self.assertIsNone(grant['project_id'])
        result=workspace.post('/v1/projects',json={'name':'replacement'});self.assertEqual(201,result.status_code)
        self.assertEqual(201,self.exchange('alice',project_id=result.json()['id']).status_code)
        self.assertEqual(403,self.exchange('alice').status_code)
        with self.store.connection() as db:
            self.assertEqual(2,db.execute('SELECT count(*) FROM accounts').fetchone()[0])
            account=db.execute('SELECT account_id FROM projects WHERE id=?',(home,)).fetchone()[0]
            db.execute('UPDATE accounts SET enabled=0 WHERE id=?',(account,))
        self.assertEqual(403,self.exchange('alice',production=True,manage_workspace=True).status_code)

    def test_ci_profiles_cannot_escape_projects_environment_or_issuer_expiry(self):
        project=self.project()
        for scopes in [[PREVIEW_SCOPE],PRODUCTION_SCOPES]:
            result=self.token(project,scopes);self.assertEqual(201,result.status_code,result.text)
            grant=result.json();self.assertLessEqual(grant['expires'],self.grant['expires'])
            actor=self.store.authenticate(grant['token']);self.assertEqual([project],actor['projects'])
            headers={'Authorization':'Bearer '+grant['token']}
            self.assertEqual(403,self.api.post('/v1/tokens',headers=headers,json={'name':'bad','scopes':['*']}).status_code)
            self.assertEqual(403,self.api.get('/v1/projects/'+project+'/env',headers=headers).status_code)
            self.assertEqual(404,self.api.get('/v1/projects/'+self.other['project_id']+'/capabilities',headers=headers).status_code)
            if scopes==[PREVIEW_SCOPE]:
                self.assertEqual(403,self.api.post('/v1/projects/'+project+'/deployments',headers=headers,json={'target':'production'}).status_code)
            rows=self.alice.get('/v1/tokens').json();self.assertNotIn('token',rows[0]);self.assertNotIn('secret_hash',rows[0])
            self.assertEqual(404,self.bob.delete('/v1/tokens/'+grant['id']).status_code)
            self.assertEqual(204,self.alice.delete('/v1/tokens/'+grant['id']).status_code)
            self.assertEqual(401,self.api.get('/v1/me',headers=headers).status_code)

    def test_delegation_only_derives_permissions_already_held(self):
        project=self.project();actor=self.store.authenticate(self.grant['token'])
        with self.store.connection() as db:
            grant=self.store.mint_token(db,actor,name='limited issuer',scopes=['tokens:write','deployments:write'],
                                        project_ids=[project],expires=time.time()+100)
        headers={'Authorization':'Bearer '+grant['token']}
        good={'name':'narrow','scopes':[PREVIEW_SCOPE],'project_ids':[project]}
        self.assertEqual(201,self.api.post('/v1/tokens',headers=headers,json=good).status_code)
        self.assertEqual(403,self.api.post('/v1/tokens',headers=headers,json=good|{'scopes':[PREVIEW_SCOPE,'previews:env']}).status_code)
        self.assertEqual(403,self.token(project,['*']).status_code)

    def test_project_limit_is_operator_owned_and_cannot_drop_below_usage(self):
        from publisher.admin import set_project_limit
        self.project();actor=self.store.authenticate(self.grant['token']);account=actor['account_id']
        with self.assertRaisesRegex(ValueError,'below'):set_project_limit(self.store,'iflink-'+account,account,1)
        with self.assertRaisesRegex(ValueError,'do not match'):set_project_limit(self.store,'wrong',account,4)
        result=set_project_limit(self.store,'iflink-'+account,account,3);self.assertEqual(2,result['active_projects'])
        self.project('third');self.assertEqual(429,self.alice.post('/v1/projects',json={'name':'fourth'}).status_code)

    def test_existing_personal_limit_and_legacy_binding_do_not_gain_workspace_access(self):
        with self.store.connection() as db:
            db.execute('UPDATE accounts SET max_projects=1 WHERE id=(SELECT account_id FROM projects WHERE id=?)',
                       (self.grant['project_id'],))
        self.assertEqual(201,self.exchange('alice',production=True,manage_workspace=True).status_code)
        self.assertEqual(429,self.alice.post('/v1/projects',json={'name':'second'}).status_code)
        with self.store.connection() as db:
            db.execute("UPDATE projects SET preview_mode='legacy' WHERE id=?",(self.grant['project_id'],))
        self.assertEqual(403,self.exchange('alice',production=True,manage_workspace=True).status_code)
        self.assertEqual(403,self.exchange('alice',project_id=self.other['project_id']).status_code)

    def test_disabled_workspace_feature_keeps_the_single_project_enrollment_limit(self):
        self.store.settings=replace(self.settings,personal_workspaces=False)
        grant=self.exchange('charlie').json()
        with self.store.connection() as db:
            limit=db.execute('SELECT a.max_projects FROM accounts a JOIN projects p ON p.account_id=a.id WHERE p.id=?',
                             (grant['project_id'],)).fetchone()[0]
        self.assertEqual(1,limit)


class PortableWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)

    def test_workspace_login_requires_explicit_mode_and_sends_project_choice(self):
        client=Mock(url='https://deploy.iflink.tech');output=self.root/'owner.json'
        with self.assertRaises(ValueError):login(client,output,manage_workspace=True)
        client.request.assert_not_called()
        client.request.side_effect=[{'issuer':ISSUER,'client_id':'test','workspace_management':True,
                                     'production_publishing':True,'project_selection':True},
                                    {'token':'private-value','project_id':'a'*24}]
        with patch('publisher.cli_login.authorize',return_value={}):
            login(client,output,production=True,manage_workspace=True,project_id='a'*24)
        request=client.request.call_args.args[2]
        self.assertTrue(request['manage_workspace']);self.assertEqual('a'*24,request['project_id'])

    def command(self,args,client):
        out=io.StringIO()
        with patch.dict(os.environ,{'POTETO_TOKEN':'owner-secret','POTETO_URL':'https://deploy.iflink.tech'}), \
             patch.object(cli,'_credential_file',return_value=None),patch.object(cli,'find_link',return_value=None), \
             patch.object(cli,'Client',return_value=client),patch('sys.argv',['poteto',*map(str,args)]),redirect_stdout(out):
            cli.main()
        self.assertNotIn('private-value',out.getvalue());return json.loads(out.getvalue())

    def test_preview_and_production_files_bind_the_api_and_project_without_printing_secrets(self):
        for mode,scopes in [('create-preview',[PREVIEW_SCOPE]),('create-production',PRODUCTION_SCOPES)]:
            client=Mock(url='https://deploy.iflink.tech');client.request.return_value={'id':'b'*24,'token':'private-value'}
            output=self.root/mode/'token.json'
            result=self.command(['token',mode,'--project','a'*24,'--name','ci','--output',output],client)
            self.assertEqual(scopes,client.request.call_args.args[2]['scopes'])
            self.assertEqual({'token':'private-value','poteto_url':client.url,'project_id':'a'*24},json.loads(output.read_text()))
            self.assertEqual(str(output),result['output'])
            if os.name!='nt':self.assertEqual(0o600,output.stat().st_mode & 0o777)

    def test_token_list_reports_metadata_as_structured_arrays(self):
        client=Mock();client.request.return_value=[{'id':'a'*24,'scopes':'["deployments:preview"]','projects':'["'+'b'*24+'"]'}]
        result=self.command(['token','list'],client)
        self.assertEqual([PREVIEW_SCOPE],result[0]['scopes']);self.assertEqual(['b'*24],result[0]['projects'])

    def test_doctor_checks_the_linked_project_before_the_login_default(self):
        credential=self.root/'owner.json'
        credential.write_text(json.dumps({'token':'private-value','poteto_url':'https://deploy.iflink.tech','project_id':'a'*24}))
        client=Mock(url='https://deploy.iflink.tech')
        client.request.side_effect=[{'project_ids':None,'scopes':['projects:write'],'account':{'max_projects':5}}, {'project_id':'b'*24}]
        with patch.object(cli,'_credential_file',return_value=credential), \
             patch.object(cli,'find_link',return_value=Mock(url=client.url,project_id='b'*24)), \
             patch.object(cli,'Client',return_value=client), patch('sys.argv',['poteto','--url',client.url,'doctor']), \
             redirect_stdout(io.StringIO()):
            cli.main()
        self.assertEqual('/v1/projects/'+'b'*24+'/capabilities',client.request.call_args.args[1])
