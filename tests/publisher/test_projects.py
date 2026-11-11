import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.backend import BackendError,Dokploy
from publisher.config import Settings
from publisher.store import Store
from publisher.worker import Worker


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(Settings(self.root));self.store=self.app.state.store
        self.owner=self.store.create_account('owner');self.other=self.store.create_account('other')
        self.client=TestClient(self.app,headers={'Authorization':'Bearer '+self.owner['token']})
        self.bob=TestClient(self.app,headers={'Authorization':'Bearer '+self.other['token']})
        self.project=self.client.post('/v1/projects',json={'name':'disposable'}).json()['id']
        self.path='/v1/projects/'+self.project;self.backend=Mock()

    def tearDown(self):
        self.client.close();self.bob.close();self.temp.cleanup()

    def release(self):
        release=self.client.post(self.path+'/deployments',json={}).json()['id']
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready',backend_id='compose' WHERE id=?",(release,))
        (self.root/'artifacts'/(release+'.tar.gz')).write_bytes(b'owned-source')
        return release

    def volume(self):
        result=self.client.post(self.path+'/volumes',json={}).json()
        with self.store.connection() as db:
            db.execute("UPDATE data_resources SET state='ready' WHERE id=?",(result['id'],))
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(result['job_id'],))
        return result['id']

    def test_existing_project_database_is_migrated_without_losing_identity(self):
        with self.store.connection() as db:db.execute('ALTER TABLE projects DROP COLUMN state')
        restored=Store(self.store.settings)
        with restored.connection() as db:
            row=db.execute('SELECT * FROM projects WHERE id=?',(self.project,)).fetchone()
            self.assertEqual('active',row['state']);self.assertEqual('disposable',row['name'])
        self.assertEqual(self.owner['account_id'],restored.authenticate(self.owner['token'])['account_id'])

    def test_rename_preserves_urls_and_enforces_ownership_uniqueness(self):
        before=self.client.get(self.path).json()
        self.assertEqual(404,self.bob.patch(self.path,json={'name':'stolen'}).status_code)
        self.assertEqual(404,self.bob.delete(self.path).status_code)
        result=self.client.patch(self.path,json={'name':'renamed'})
        self.assertEqual(200,result.status_code);self.assertEqual(before['url'],result.json()['url'])
        self.client.post('/v1/projects',json={'name':'taken'})
        self.assertEqual(409,self.client.patch(self.path,json={'name':'taken'}).status_code)
        self.assertEqual(422,self.client.patch(self.path,json={'name':'../unsafe'}).status_code)

    def test_active_jobs_and_uploads_must_finish_before_project_deletion(self):
        release=self.client.post(self.path+'/deployments',json={}).json()['id']
        actor=self.store.authenticate(self.owner['token'])
        self.store.begin_upload(actor,release,'a'*24)
        self.assertEqual(409,self.client.delete(self.path).status_code)
        with self.store.connection() as db:
            db.execute('DELETE FROM uploads')
            now=time.time()
            db.execute('INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)',('b'*24,self.project,'deploy',release,now,now))
        self.assertEqual(409,self.client.delete(self.path).status_code)
        self.assertEqual('active',self.client.get(self.path).json()['state'])

    def test_persistent_data_requires_consent_and_each_resource_scope(self):
        self.volume()
        self.assertEqual(409,self.client.delete(self.path).status_code)
        token=self.client.post('/v1/tokens',json={'name':'no-data-delete','scopes':['projects:write','deployments:write'],'project_ids':[self.project]}).json()['token']
        self.assertEqual(403,self.client.delete(self.path+'?destroy_data=true',headers={'Authorization':'Bearer '+token}).status_code)
        self.assertEqual('active',self.client.get(self.path).json()['state'])
        self.assertEqual(202,self.client.delete(self.path+'?destroy_data=true').status_code)

    def test_deleting_project_blocks_writes_and_log_refresh_but_not_job_reads(self):
        release=self.release()
        first=self.client.delete(self.path).json()
        self.assertEqual(first['job_id'],self.client.delete(self.path).json()['job_id'])
        for method,path,body in [('post',self.path+'/deployments',{}),('put',self.path+'/env',{'values':{'KEY':'value'}}),('post',self.path+'/volumes',{}),('patch',self.path,{'name':'renamed'})]:
            self.assertEqual(409,getattr(self.client,method)(path,json=body).status_code,path)
        self.assertEqual('paused',self.client.get('/v1/deployments/'+release+'/runtime-logs').json()['state'])
        self.assertEqual(200,self.client.get('/v1/jobs/'+first['job_id']).status_code)

    def test_deleting_project_is_removed_from_edge_inventory_before_cleanup(self):
        release=self.release();token=self.root/'edge.token';token.write_text('operator-edge-key')
        with patch.dict(os.environ,{'PAAS_EDGE_TOKEN_FILE':str(token)}):
            before=self.client.get('/internal/routes',headers={'X-Paas-Edge-Key':'operator-edge-key'}).json()['hosts']
            self.assertTrue(before)
            self.client.delete(self.path)
            after=self.client.get('/internal/routes',headers={'X-Paas-Edge-Key':'operator-edge-key'}).json()['hosts']
        self.assertEqual([],after)

    def test_cleanup_orders_releases_before_data_and_releases_name_quota_last(self):
        release=self.release();resource=self.volume()
        with self.store.connection() as db:
            db.execute('UPDATE projects SET current_deployment=?,environment=? WHERE id=?',(release,self.store.seal({'SECRET':'erase-me'}),self.project))
            db.execute('UPDATE accounts SET max_projects=1 WHERE id=?',(self.owner['account_id'],))
        result=self.client.delete(self.path+'?destroy_data=true').json()
        self.assertEqual(429,self.client.post('/v1/projects',json={'name':'new'}).status_code)
        order=[];self.backend.remove_project_route.side_effect=lambda p:order.append('route')
        self.backend.remove.side_effect=lambda r:order.append('release')
        def remove_data(row,check):
            check();order.append('data')
            with self.store.connection() as db:self.assertEqual(0,db.execute('SELECT count(*) FROM deployments WHERE project_id=?',(self.project,)).fetchone()[0])
            self.assertFalse((self.root/'artifacts'/(release+'.tar.gz')).exists())
        with patch('publisher.data_backend.DataBackend') as data:
            data.return_value.remove.side_effect=remove_data
            self.assertTrue(Worker(self.store,self.backend).run_once())
        self.assertEqual(['route','release','data'],order)
        self.assertEqual(404,self.client.get(self.path).status_code)
        self.assertEqual([],self.client.get('/v1/projects').json())
        self.assertEqual('completed',self.client.get('/v1/jobs/'+result['job_id']).json()['state'])
        self.assertEqual(result['job_id'],self.client.delete(self.path).json()['job_id'])
        self.assertEqual(404,self.bob.get('/v1/jobs/'+result['job_id']).status_code)
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM projects WHERE id=?',(self.project,)).fetchone()
            self.assertEqual({},self.store.unseal(row['environment']));self.assertIsNone(row['current_deployment'])
            self.assertEqual(0,db.execute('SELECT count(*) FROM data_resources').fetchone()[0])
        new=self.client.post('/v1/projects',json={'name':'disposable'})
        self.assertEqual(201,new.status_code,new.text);self.assertNotEqual(self.project,new.json()['id'])

    def test_partial_failure_preserves_remaining_resources_and_retry_resumes_them(self):
        first=self.release();second=self.release();resource=self.volume()
        result=self.client.delete(self.path+'?destroy_data=true').json()
        self.backend.remove.side_effect=[None,BackendError('Deletion interrupted')]
        with patch('publisher.data_backend.DataBackend') as data:
            Worker(self.store,self.backend).run_once();data.return_value.remove.assert_not_called()
            self.assertEqual('failed',self.client.get('/v1/jobs/'+result['job_id']).json()['state'])
            self.assertEqual('deleting',self.client.get(self.path).json()['state'])
            with self.store.connection() as db:
                self.assertEqual([second],[r['id'] for r in db.execute('SELECT id FROM deployments')])
                self.assertEqual(resource,db.execute('SELECT id FROM data_resources').fetchone()['id'])
            retry=self.client.delete(self.path+'?destroy_data=true').json()
            self.backend.remove.side_effect=None
            Worker(self.store,self.backend).run_once()
        self.assertEqual([first,second,second],[call.args[0]['id'] for call in self.backend.remove.call_args_list])
        self.assertEqual('completed',self.client.get('/v1/jobs/'+retry['job_id']).json()['state'])

    def test_transferred_lease_cannot_commit_old_cleanup_results(self):
        release=self.release();result=self.client.delete(self.path).json()
        def transfer(row):
            with self.store.connection() as db:
                db.execute('UPDATE jobs SET owner=?,lease_until=? WHERE id=?',('replacement-worker',time.time()+90,result['job_id']))
        self.backend.remove.side_effect=transfer
        Worker(self.store,self.backend).run_once()
        with self.store.connection() as db:
            self.assertEqual(release,db.execute('SELECT id FROM deployments').fetchone()[0])
            self.assertEqual('replacement-worker',db.execute('SELECT owner FROM jobs WHERE id=?',(result['job_id'],)).fetchone()[0])

    def test_project_route_cleanup_never_removes_other_projects(self):
        routes=self.root/'routes';routes.mkdir()
        own=routes/('publisher-'+self.project+'.yml');own.write_text('route');own.with_suffix('.next').write_text('pending')
        other=routes/('publisher-'+'a'*24+'.yml');other.write_text('keep')
        backend=Dokploy({'routes':str(routes),'releases':str(self.root/'releases')},self.store.settings)
        backend.remove_project_route(self.project)
        self.assertFalse(own.exists());self.assertFalse(own.with_suffix('.next').exists());self.assertEqual('keep',other.read_text())


if __name__=='__main__':unittest.main()
