import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.backend import BackendError
from publisher.config import Settings
from publisher.controlflow import LeaseLost
from publisher.models import DeploymentInput
from publisher.preview_retention import enqueue_due
from publisher.store import PREVIEW_SCOPE, PREVIEW_MANAGE_SCOPE
from publisher.worker import Worker


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = create_app(Settings(self.root, static_artifacts=True)); self.store = self.app.state.store
        owner = self.store.create_account('retention-owner'); self.actor = self.store.authenticate(owner['token'])
        self.client = TestClient(self.app, headers={'Authorization': 'Bearer '+owner['token']}); self.addCleanup(self.client.close)
        self.project = self.store.create_project(self.actor, 'retention')['id']
        self.other = self.store.create_project(self.actor, 'other')['id']
        self.base = '/v1/projects/'+self.project+'/previews/retention'
        self.backend = Mock(); self.worker = Worker(self.store, self.backend)

    def release(self, *, days=10, state='ready', config=None, project=None):
        release = self.store.create_deployment(self.actor, project or self.project,
            DeploymentInput(**({'target':'preview'} | (config or {}))).configuration(), None)
        with self.store.connection() as db:
            db.execute('UPDATE deployments SET state=?,created=? WHERE id=?', (state,time.time()-days*86400,release['id']))
        (self.root/'artifacts'/(release['id']+'.tar.gz')).write_bytes(b'test')
        return release['id']

    def enable(self, keep=1, days=7):
        result = self.client.put(self.base, json={'keep':keep,'older_than_days':days})
        self.assertEqual(200,result.status_code,result.text)
        return result.json()

    def state(self, release):
        with self.store.connection() as db:
            row = db.execute('SELECT state FROM deployments WHERE id=?',(release,)).fetchone()
            return row['state'] if row else None

    def reset_scan(self):
        with self.store.connection() as db:db.execute('UPDATE preview_retention SET checked_at=0')

    def scoped(self, scopes):
        with self.store.connection() as db:
            grant = self.store.mint_token(db,self.actor,name='scoped',scopes=scopes,project_ids=[self.project],expires=time.time()+300)
        client = TestClient(self.app, headers={'Authorization':'Bearer '+grant['token']});self.addCleanup(client.close)
        return client

    def test_default_is_off_and_only_project_management_can_enable_it(self):
        old = self.release();self.release(days=0)
        self.assertFalse(self.client.get(self.base).json()['enabled'])
        self.assertEqual([],enqueue_due(self.store));self.assertEqual('ready',self.state(old))
        for scopes in [[PREVIEW_SCOPE],[PREVIEW_SCOPE,PREVIEW_MANAGE_SCOPE],['deployments:read','deployments:write']]:
            client = self.scoped(scopes)
            view = client.get(self.base);self.assertEqual(200,view.status_code);self.assertFalse(view.json()['can_manage'])
            self.assertEqual(403,client.put(self.base,json={'keep':1,'older_than_days':7}).status_code)
            self.assertEqual(403,client.delete(self.base).status_code)
            self.assertEqual(404,client.get('/v1/projects/'+self.other+'/previews/retention').status_code)
        for body in [{'keep':0},{'older_than_days':0},{'keep':True},{'older_than_days':'7'}]:
            self.assertEqual(422,self.client.put(self.base,json=body).status_code)

    def test_one_job_at_a_time_reuses_the_normal_worker_and_preserves_latest(self):
        old = self.release();latest = self.release(days=0)
        self.enable();jobs = enqueue_due(self.store);self.assertEqual(1,len(jobs))
        self.assertEqual([],enqueue_due(self.store));self.assertEqual('ready',self.state(old))
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.state(old));self.assertEqual('ready',self.state(latest))
        self.assertFalse((self.root/'artifacts'/(old+'.tar.gz')).exists())
        self.backend.remove.assert_called_once()
        self.assertEqual('completed',self.client.get('/v1/jobs/'+jobs[0]).json()['state'])
        self.assertEqual(jobs[0],self.client.get(self.base).json()['recent_jobs'][0]['id'])
        self.assertEqual(old,self.client.get(self.base).json()['recent_jobs'][0]['deployment_id'])
        self.reset_scan();self.assertEqual([],enqueue_due(self.store))

    def test_first_worker_pass_schedules_even_on_a_freshly_booted_host(self):
        old=self.release();self.release(days=0);self.enable()
        with patch('publisher.worker.time.monotonic',return_value=10):
            self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.state(old))

    def test_protection_matches_prune_for_production_history_data_and_active_work(self):
        removable = self.release();latest = self.release(days=0)
        prod = self.release(config={'target':'production'});published = self.release()
        data = self.release();active = self.release(state='building')
        with self.store.connection() as db:
            cfg=json.loads(db.execute('SELECT config FROM deployments WHERE id=?',(data,)).fetchone()[0]);cfg['database_id']='d'*24
            db.execute('UPDATE deployments SET config=? WHERE id=?',(json.dumps(cfg),data))
            db.execute('UPDATE projects SET current_deployment=? WHERE id=?',(prod,self.project))
            db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,state,created,updated) VALUES(?,?,'promote',?,'completed',0,0)",('f'*24,self.project,published))
        self.enable();self.worker.run_once()
        self.assertIsNone(self.state(removable))
        for release in [latest,prod,published,data,active]:self.assertIsNotNone(self.state(release))
        self.reset_scan();self.assertEqual([],enqueue_due(self.store))

    def test_disabling_or_tightening_the_policy_rechecks_queued_not_started_jobs(self):
        old = self.release();self.release(days=0)
        self.enable();job = enqueue_due(self.store)[0]
        self.client.delete(self.base);self.worker.run_once()
        self.assertEqual('ready',self.state(old));self.backend.remove.assert_not_called()
        self.assertEqual('completed',self.client.get('/v1/jobs/'+job).json()['state'])
        self.enable();self.reset_scan();enqueue_due(self.store);self.enable(keep=2);self.worker.last_maintenance=time.monotonic();self.worker.run_once()
        self.assertEqual('ready',self.state(old));self.backend.remove.assert_not_called()

    def test_failed_removal_is_visible_and_needs_an_explicit_retry(self):
        old=self.release();self.release(days=0);self.enable()
        self.backend.remove.side_effect=BackendError('temporary removal failure')
        self.worker.run_once();self.assertEqual('deleting',self.state(old))
        self.reset_scan();self.assertEqual([],enqueue_due(self.store))
        status=self.client.get(self.base).json();self.assertEqual('failed',status['recent_jobs'][0]['state'])
        self.assertEqual('temporary removal failure',status['recent_jobs'][0]['error'])
        self.backend.remove.side_effect=None
        result=self.client.post('/v1/projects/'+self.project+'/previews/prune',json={'ids':[old],'keep':1,'older_than_days':7})
        self.assertEqual(202,result.status_code,result.text);self.worker.run_once();self.assertIsNone(self.state(old))

    def test_already_started_cleanup_recovers_after_policy_is_disabled(self):
        old=self.release();self.release(days=0);self.enable()
        self.backend.remove.side_effect=LeaseLost('test interruption after durable deletion intent')
        self.worker.run_once();self.assertEqual('deleting',self.state(old))
        self.client.delete(self.base)
        with self.store.connection() as db:db.execute("UPDATE jobs SET lease_until=0 WHERE kind='preview-retention' AND state='running'")
        self.backend.remove.side_effect=None
        self.assertTrue(Worker(self.store,self.backend).run_once());self.assertIsNone(self.state(old))

    def test_scheduler_respects_maintenance_other_jobs_and_disabled_accounts(self):
        old=self.release();self.release(days=0);self.enable()
        (self.root/'maintenance.json').write_text('{}');self.assertEqual([],enqueue_due(self.store))
        (self.root/'maintenance.json').unlink()
        with self.store.connection() as db:
            db.execute("INSERT INTO jobs(id,project_id,kind,state,created,updated) VALUES(?,?,'project-export','queued',0,0)",('e'*24,self.project))
        self.assertEqual([],enqueue_due(self.store))
        with self.store.connection() as db:
            db.execute("DELETE FROM jobs WHERE id=?",('e'*24,))
            db.execute('UPDATE accounts SET enabled=0 WHERE id=?',(self.actor['account_id'],))
        self.assertEqual([],enqueue_due(self.store));self.assertEqual('ready',self.state(old))

    def test_project_deletion_removes_its_policy(self):
        self.enable()
        result=self.client.delete('/v1/projects/'+self.project);self.assertEqual(202,result.status_code)
        self.worker.run_once()
        with self.store.connection() as db:self.assertIsNone(db.execute('SELECT 1 FROM preview_retention WHERE project_id=?',(self.project,)).fetchone())

    def test_queued_job_rechecks_publication_and_dedicated_workers_do_not_schedule(self):
        old=self.release();self.release(days=0);self.enable()
        self.assertFalse(Worker(self.store,self.backend,logs_only=True).run_once())
        self.assertFalse(Worker(self.store,self.backend,static_previews_only=True).run_once())
        queued=enqueue_due(self.store);self.assertEqual(1,len(queued))
        with self.store.connection() as db:db.execute('UPDATE projects SET current_deployment=? WHERE id=?',(old,self.project))
        self.assertTrue(self.worker.run_once());self.assertEqual('ready',self.state(old));self.backend.remove.assert_not_called()

    def test_busy_projects_do_not_starve_later_policies(self):
        self.release();self.release(days=0);self.enable()
        with self.store.connection() as db:
            for i in range(20):
                p=self.store.insert_project(db,self.actor,'busy-'+str(i))['id']
                db.execute('INSERT INTO preview_retention VALUES(?,1,7,1,0,?)',(p,-100+i))
                db.execute("INSERT INTO jobs(id,project_id,kind,state,created,updated) VALUES(?,?,'project-export','queued',0,0)",(f'{i:024x}',p))
        self.assertEqual([],enqueue_due(self.store))
        self.assertEqual(1,len(enqueue_due(self.store)))
