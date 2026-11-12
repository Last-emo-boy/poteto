from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from execution import sandbox
from publisher.api import create_app
from publisher.backend import Dokploy
from publisher.config import Settings
from publisher.login_contract import ISSUER
from publisher.models import DeploymentInput
from publisher.store import PREVIEW_SCOPE, Problem, Store


class PersonalPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.settings=Settings(self.root,personal_previews=True,sandbox_memory_limit_mib=14336,sandbox_cpu_limit=5)
        self.app=create_app(self.settings);self.store=self.app.state.store
        self.admin=self.store.authenticate(self.store.create_account('existing')['token'])
        self.legacy=self.store.create_project(self.admin,'legacy')['id']
        with self.store.connection() as db:
            self.project=self.store.enroll_cli_identity(db,ISSUER,'test-subject')
            self.account=db.execute('SELECT account_id FROM projects WHERE id=?',(self.project,)).fetchone()[0]
            grant=self.store.mint_token(db,{'id':None,'account_id':self.account},name='personal',scopes=['deployments:preview'],project_ids=[self.project],expires=9999999999)
        self.actor=self.store.authenticate(grant['token'])
        self.client=TestClient(self.app,headers={'Authorization':'Bearer '+grant['token']});self.addCleanup(self.client.close)
        self.base='/v1/projects/'+self.project

    def test_enrollment_is_opt_in_and_existing_projects_are_never_migrated_implicitly(self):
        with self.store.connection() as db:
            self.assertEqual('isolated',db.execute('SELECT preview_mode FROM projects WHERE id=?',(self.project,)).fetchone()[0])
            self.assertEqual('legacy',db.execute('SELECT preview_mode FROM projects WHERE id=?',(self.legacy,)).fetchone()[0])
            self.assertEqual(self.project,self.store.enroll_cli_identity(db,ISSUER,'test-subject'))
        old=Store(replace(self.settings,personal_previews=False))
        with old.connection() as db:
            project=old.enroll_cli_identity(db,ISSUER,'older-subject')
        enabled=Store(self.settings)
        with enabled.connection() as db:
            self.assertEqual(project,enabled.enroll_cli_identity(db,ISSUER,'older-subject'))
            self.assertEqual('legacy',db.execute('SELECT preview_mode FROM projects WHERE id=?',(project,)).fetchone()[0])

    def test_static_and_prebuilt_defaults_are_vm_free_without_per_project_registration(self):
        for runtime,driver in [('static','static-artifact'),('image','gvisor')]:
            config={'runtime':runtime,'target':'preview'}
            response=self.client.post(self.base+'/preflight',json={'configuration':config,'archive_bytes':100,'unpacked_bytes':100,'files':1})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual(driver,response.json()['runtime_driver'])
            if runtime=='image':
                self.assertEqual(.1,response.json()['configuration']['cpus'])
                self.assertTrue(response.json()['configuration']['sleep'])
        release=self.client.post(self.base+'/deployments',json={'runtime':'image','target':'preview'}).json()
        self.assertEqual(('gvisor',.1,True),(release['runtime_driver'],release['config']['cpus'],release['config']['sleep']))
        capability=self.client.get(self.base+'/capabilities').json()
        self.assertTrue(capability['static_vm_free']);self.assertTrue(capability['prebuilt_images'])
        self.assertEqual({'memory_mib':14336,'vcpus':5},capability['sandbox_pool'])
        self.assertEqual(404,self.client.get('/v1/projects/'+self.legacy+'/capabilities').status_code)

    def test_explicit_resources_are_not_rewritten_and_legacy_defaults_remain(self):
        result=self.client.post(self.base+'/deployments',json={'runtime':'image','target':'preview','cpus':.4,'sleep':False,'memory_mb':512})
        self.assertEqual(201,result.status_code,result.text)
        self.assertEqual((.4,False,512),tuple(result.json()['config'][k] for k in ['cpus','sleep','memory_mb']))
        config=self.store.configuration(self.admin,self.legacy,DeploymentInput())
        self.assertEqual((.5,False),(config['cpus'],config['sleep']))

    def test_source_data_and_unavailable_runtime_do_not_fall_back_to_vms(self):
        for config in [{'runtime':'node'},{'runtime':'dockerfile'},{'runtime':'static','build_command':'npm run build'},
                       {'runtime':'image','database_id':'a'*24},{'runtime':'image','memory_mb':128}]:
            result=self.client.post(self.base+'/deployments',json=config|{'target':'preview'})
            self.assertEqual(422,result.status_code,result.text)
        disabled=TestClient(create_app(replace(self.settings,personal_previews=False)),headers=self.client.headers)
        self.addCleanup(disabled.close)
        self.assertEqual(503,disabled.post(self.base+'/deployments',json={'target':'preview'}).status_code)
        with self.store.connection() as db:
            owner=self.store.mint_token(db,{'id':None,'account_id':self.account},name='owner',scopes=['*'],project_ids=None,expires=9999999999)['token']
        with TestClient(self.app,headers={'Authorization':'Bearer '+owner}) as privileged:
            self.assertEqual(422,privileged.post(self.base+'/databases',json={'stage':'preview'}).status_code)
        with self.store.connection() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM deployments WHERE project_id=?',(self.project,)).fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM data_resources WHERE project_id=?',(self.project,)).fetchone()[0])

    def test_fifty_minimum_previews_fit_but_the_next_reservation_is_rejected(self):
        for index in range(51):
            with self.store.connection() as db:
                project=self.store.enroll_cli_identity(db,ISSUER,'capacity-'+str(index))
                account=db.execute('SELECT account_id FROM projects WHERE id=?',(project,)).fetchone()[0]
            actor={'id':None,'account_id':account,'scopes':[PREVIEW_SCOPE],'projects':[project]}
            config=self.store.configuration(actor,project,DeploymentInput(runtime='image',target='preview'))
            if index==50:
                with self.assertRaisesRegex(Problem,'Sandbox preview pool'):
                    self.store.create_deployment(actor,project,config,None)
            else:
                self.assertEqual('gvisor',self.store.create_deployment(actor,project,config,None)['runtime_driver'])

    def test_executor_receives_bound_personal_marker_and_never_a_vm_build(self):
        response=self.client.post(self.base+'/deployments',json={'runtime':'image','target':'preview'}).json()
        with self.store.connection() as db:release=self.store.deployment(db,self.actor,response['id'],allow_preview=True)
        backend=Dokploy({'routes':str(self.root),'releases':str(self.root/'releases')},self.settings);backend.store=self.store
        client=Mock()
        from publisher.executor import ExecutionFailure
        absent=ExecutionFailure('missing');absent.status=404;client.build_status.side_effect=absent
        client.start_build.return_value={'state':'succeeded','driver':'prebuilt','image':'paas-release:'+release['id'],'image_bytes':100}
        client.build_logs.return_value='';client.sandbox.return_value={'http_port':23100}
        with patch.object(backend,'execution_client',return_value=client),patch('publisher.runtime.RuntimeRegistry') as vm:
            backend.publish_sandbox(release,self.root/'image.tar.gz',Mock(),Mock())
        vm.assert_not_called()
        self.assertTrue(client.start_build.call_args.args[1]['personal_preview'])
        self.assertTrue(client.sandbox.call_args.kwargs['personal_preview'])
        self.assertNotIn('require_native',client.start_build.call_args.args[1])


class PoolAdmissionTests(unittest.TestCase):
    def test_effective_pool_and_stopped_receipts_are_both_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);records=root/'sandboxes';records.mkdir()
            (records/('a'*24+'.json')).write_text(json.dumps({'memory_mb':512,'cpus':.5}))
            config={'sandbox_memory_limit_mib':1024,'sandbox_cpu_limit':1}
            effective=(1024*1024**2,(100000,100000),0)
            with patch.object(sandbox,'pool_state',return_value=effective),patch.object(sandbox.subprocess,'run'):
                sandbox.check_pool(root,config,'b'*24,512,.5)
                with self.assertRaisesRegex(ValueError,'capacity is reserved'):sandbox.check_pool(root,config,'b'*24,768,.5)
                with self.assertRaisesRegex(ValueError,'capacity is reserved'):sandbox.check_pool(root,config,'b'*24,512,.6)
            with patch.object(sandbox,'pool_state',return_value=(2048*1024**2,(100000,100000),0)),patch.object(sandbox.subprocess,'run'):
                with self.assertRaisesRegex(ValueError,'effective cgroup'):sandbox.check_pool(root,config,'b'*24,256,.1)

    def test_public_flag_cannot_authorize_source_builds_or_a_disabled_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);release='a'*24;project='b'*24;account='c'*24
            build=root/'builds'/release;build.mkdir(parents=True)
            (build/'spec.json').write_text(json.dumps({'runtime':'node','project_id':project,'account_id':account,'personal_preview':True}))
            body={'action':'start','account_id':account,'project_id':project,'personal_preview':True,'port':3000,'memory_mb':256,'cpus':.1,'environment':{}}
            with self.assertRaisesRegex(ValueError,'not enabled'):sandbox.operation(root,{},release,body,Mock())
            with self.assertRaisesRegex(ValueError,'prebuilt image receipt'):
                sandbox.operation(root,{'personal_previews':True},release,body,Mock(return_value={'driver':'native'}))
