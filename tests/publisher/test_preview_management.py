import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.backend import BackendError
from publisher.config import Settings
from publisher.models import DeploymentInput
from publisher.store import PREVIEW_MANAGE_SCOPE, PREVIEW_SCOPE
from publisher.worker import Worker


class PreviewManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = create_app(Settings(self.root,static_artifacts=True));self.store=self.app.state.store
        owner=self.store.create_account('owner');self.account=owner['account_id'];self.actor=self.store.authenticate(owner['token'])
        self.client=TestClient(self.app,headers={'Authorization':'Bearer '+owner['token']});self.addCleanup(self.client.close)
        self.project=self.store.create_project(self.actor,'cleanup')['id']
        self.other=self.store.create_project(self.actor,'hidden-project')['id']
        self.base=f'/v1/projects/{self.project}'
        self.preview=self.token([PREVIEW_SCOPE]);self.manager=self.token([PREVIEW_SCOPE,PREVIEW_MANAGE_SCOPE])

    def token(self,scopes):
        response=self.client.post('/v1/tokens',json={'name':'scoped','scopes':scopes,'project_ids':[self.project]})
        self.assertEqual(201,response.status_code,response.text)
        client=TestClient(self.app,headers={'Authorization':'Bearer '+response.json()['token']})
        self.addCleanup(client.close);return client

    def release(self,*,state='ready',days=10,config=None,backend=None,project=None):
        row=self.store.create_deployment(self.actor,project or self.project,
            DeploymentInput(**({'target':'preview'} | (config or {}))).configuration(),None)
        with self.store.connection() as db:
            db.execute('UPDATE deployments SET state=?,created=?,artifact_bytes=10,unpacked_bytes=20,built_bytes=30,backend_id=? WHERE id=?',
                       (state,time.time()-days*86400,backend,row['id']))
        (self.root/'artifacts'/(row['id']+'.tar.gz')).write_bytes(b'owned test artifact')
        return row['id']

    def plan(self,client=None,keep=0,days=7):
        response=(client or self.client).get(self.base+'/previews/prune',params={'keep':keep,'older_than_days':days})
        self.assertEqual(200,response.status_code,response.text)
        return response.json()

    def apply(self,ids,client=None,keep=0,days=7):
        return (client or self.client).post(self.base+'/previews/prune',json={'ids':ids,'keep':keep,'older_than_days':days})

    def test_usage_matches_admission_reservations_and_restricts_details(self):
        static=self.release()
        dynamic=self.release(config={'runtime':'node','memory_mb':512,'cpus':0.5})
        failed=self.release(state='failed',config={'runtime':'node','memory_mb':512,'cpus':0.5},backend='owned')
        self.release(state='failed',config={'runtime':'node','memory_mb':512,'cpus':0.5})
        hidden=self.release(project=self.other)
        prod=self.release(config={'target':'production'})
        with self.store.connection() as db:
            db.execute('UPDATE projects SET environment=? WHERE id=?',(self.store.seal({'PRIVATE':'never-return-this-secret'}),self.project))
        response=self.preview.get(self.base+'/usage')
        self.assertEqual(200,response.status_code,response.text)
        result=response.json();ids={r['id'] for r in result['releases']}
        self.assertIn(static,ids);self.assertIn(dynamic,ids);self.assertNotIn(hidden,ids);self.assertNotIn(prod,ids)
        self.assertNotIn('hidden-project',response.text);self.assertNotIn('never-return-this-secret',response.text)
        self.assertEqual({'memory_mib':1024,'vcpus':1.0},result['account_runtime']['reserved'])
        self.assertEqual(360,result['artifact_storage']['reserved_bytes'])
        reserved={r['id']:r['runtime_reserved'] for r in result['releases']}
        self.assertEqual(512,reserved[failed]['memory_mib']);self.assertEqual(0,reserved[static]['memory_mib'])
        self.assertEqual(404,self.preview.get(f'/v1/projects/{self.other}/usage').status_code)
        self.assertEqual([self.project],[p['id'] for p in result['projects']])
        owner_usage=self.client.get(self.base+'/usage').json()
        self.assertEqual({self.project,self.other},{p['id'] for p in owner_usage['projects']})
        self.assertEqual(360,sum(p['artifact_reserved_bytes'] for p in owner_usage['projects']))

    def test_dry_run_protects_published_history_data_recent_and_latest_ready(self):
        oldest=self.release(days=30);kept=self.release(days=20);recent=self.release(days=1)
        prod=self.release(days=30,config={'target':'production'})
        published=self.release(days=30)
        data=self.release(days=30)
        active=self.release(state='building',days=30)
        with self.store.connection() as db:
            row=db.execute('SELECT config FROM deployments WHERE id=?',(data,)).fetchone()
            db.execute('UPDATE deployments SET config=? WHERE id=?',(json.dumps(json.loads(row['config'])|{'database_id':'a'*24}),data))
            db.execute('UPDATE projects SET current_deployment=? WHERE id=?',(prod,self.project))
            db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,state,created,updated) VALUES(?,?,?,?,'completed',0,0)",('f'*24,self.project,'promote',published))
            before={t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in ['deployments','jobs','audit']}
        plan=self.plan(keep=1)
        # The newest ready preview is recent; both older stateless previews are removable.
        self.assertEqual({oldest,kept},{r['id'] for r in plan['candidates']})
        reasons={r['id']:r['reason'] for r in plan['protected']}
        self.assertEqual('too_recent',reasons[recent]);self.assertEqual('published_or_rollback_release',reasons[prod])
        self.assertEqual('published_or_rollback_release',reasons[published]);self.assertEqual('persistent_data_binding',reasons[data])
        self.assertEqual('active_deployment',reasons[active])
        with self.store.connection() as db:
            self.assertEqual(before,{t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in before})

    def test_batch_is_explicit_atomic_and_replays_existing_jobs(self):
        first=self.release();second=self.release();prod=self.release(config={'target':'production'})
        result=self.apply([first,prod]);self.assertEqual(409,result.status_code)
        self.assertEqual('ready',self.client.get('/v1/deployments/'+first).json()['state'])
        queued=self.apply([first,second],self.manager);self.assertEqual(202,queued.status_code,queued.text)
        job=queued.json()['releases'][0]['job_id']
        self.assertEqual(200,self.manager.get('/v1/jobs/'+job).status_code)
        self.assertEqual(403,self.preview.get('/v1/jobs/'+job).status_code)
        again=self.apply([first,second],self.manager);self.assertEqual(queued.json(),again.json())
        new=self.release(days=30)
        backend=Mock();worker=Worker(self.store,backend)
        self.assertTrue(worker.run_once());self.assertTrue(worker.run_once())
        self.assertFalse((self.root/'artifacts'/(first+'.tar.gz')).exists())
        self.assertFalse((self.root/'artifacts'/(second+'.tar.gz')).exists())
        self.assertEqual('ready',self.client.get('/v1/deployments/'+new).json()['state'])
        absent=self.apply([first,second],self.manager)
        self.assertEqual(['absent','absent'],[r['state'] for r in absent.json()['releases']])
        self.assertEqual(2,backend.remove.call_count)
        self.assertEqual('completed',self.manager.get('/v1/jobs/'+job).json()['state'])

    def test_default_preview_token_cannot_delete_and_manager_cannot_escalate(self):
        preview=self.release();prod=self.release(config={'target':'production'})
        self.assertFalse(self.plan(self.preview)['can_apply'])
        self.assertEqual(403,self.apply([preview],self.preview).status_code)
        self.assertEqual(409,self.apply([prod],self.manager).status_code)
        self.assertEqual(403,self.manager.delete('/v1/deployments/'+prod).status_code)
        self.assertEqual(403,self.manager.patch(self.base+'/env',json={'values':{'A':'b'}}).status_code)
        self.assertEqual(403,self.manager.post('/v1/tokens',json={'name':'escalate','scopes':['*']}).status_code)
        self.assertEqual(404,self.manager.post(f'/v1/projects/{self.other}/previews/prune',json={'ids':[preview]}).status_code)
        for scopes in [[PREVIEW_MANAGE_SCOPE],[PREVIEW_SCOPE,PREVIEW_MANAGE_SCOPE,'deployments:write']]:
            self.assertEqual(422,self.client.post('/v1/tokens',json={'name':'invalid','scopes':scopes,'project_ids':[self.project]}).status_code)

    def test_rechecks_publication_after_planning_and_blocks_other_operations(self):
        candidate=self.release();self.assertEqual([candidate],[r['id'] for r in self.plan()['candidates']])
        with self.store.connection() as db:db.execute('UPDATE projects SET current_deployment=? WHERE id=?',(candidate,self.project))
        self.assertEqual(409,self.apply([candidate]).status_code)
        with self.store.connection() as db:
            db.execute('UPDATE projects SET current_deployment=NULL WHERE id=?',(self.project,))
            db.execute("INSERT INTO jobs(id,project_id,kind,state,created,updated) VALUES(?,?,'project-export','queued',0,0)",('e'*24,self.project))
        self.assertEqual(409,self.apply([candidate]).status_code)
        self.assertEqual('ready',self.client.get('/v1/deployments/'+candidate).json()['state'])

    def test_failed_cleanup_can_resume_by_repeating_the_same_ids(self):
        candidate=self.release();self.assertEqual(202,self.apply([candidate]).status_code)
        backend=Mock();backend.remove.side_effect=BackendError('temporary removal failure');worker=Worker(self.store,backend)
        self.assertTrue(worker.run_once())
        self.assertEqual(candidate,self.plan()['candidates'][0]['id'])
        retry=self.apply([candidate]);self.assertEqual(202,retry.status_code,retry.text)
        backend.remove.side_effect=None
        self.assertTrue(worker.run_once())
        self.assertEqual('absent',self.apply([candidate]).json()['releases'][0]['state'])
