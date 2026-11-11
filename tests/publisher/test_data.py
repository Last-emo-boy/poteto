import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.config import Settings
from publisher.data_backend import DataBackend
from publisher.models import DeploymentInput
from publisher.store import Store
from publisher.worker import Worker
from publisher.executor import ExecutionFailure


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(Settings(self.root));self.store=self.app.state.store
        self.owner=self.store.create_account('owner');self.other=self.store.create_account('other')
        self.client=TestClient(self.app,headers={'Authorization':'Bearer '+self.owner['token']})
        self.bob=TestClient(self.app,headers={'Authorization':'Bearer '+self.other['token']})
        self.project=self.client.post('/v1/projects',json={'name':'data'}).json()['id']

    def tearDown(self):
        self.client.close();self.bob.close();self.temp.cleanup()

    def resource(self,kind='databases',stage='production',ready=False):
        response=self.client.post('/v1/projects/'+self.project+'/'+kind,json={'stage':stage})
        self.assertEqual(202,response.status_code,response.text)
        result=response.json()
        if ready:
            with self.store.connection() as db:
                db.execute("UPDATE data_resources SET state='ready' WHERE id=?",(result['id'],))
                db.execute("UPDATE jobs SET state='completed' WHERE resource_id=?",(result['id'],))
        return result

    def test_credentials_are_encrypted_and_never_returned_by_data_endpoints(self):
        resource=self.resource()
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM data_resources WHERE id=?',(resource['id'],)).fetchone()
        password=self.store.unseal(row['definition'])['app_password']
        self.assertNotIn(password,row['definition'])
        for path in ['/v1/data/'+resource['id'],'/v1/projects/'+self.project+'/data','/v1/jobs/'+resource['job_id']]:
            response=self.client.get(path)
            self.assertEqual(200,response.status_code,response.text)
            self.assertNotIn(password,response.text)
            self.assertNotIn('admin_password',response.text)
            self.assertEqual(404,self.bob.get(path).status_code)
        self.assertEqual(404,self.bob.post('/v1/projects/'+self.project+'/databases',json={}).status_code)

    def test_binding_is_explicit_same_project_ready_and_same_stage(self):
        resource=self.resource(ready=True)
        config={'runtime':'node','database_id':resource['id']}
        response=self.client.post('/v1/projects/'+self.project+'/deployments',json=config)
        self.assertEqual(201,response.status_code,response.text)
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM deployments WHERE id=?',(response.json()['id'],)).fetchone()
        environment=self.store.unseal(row['environment'])
        self.assertIn('postgresql://app:',environment['DATABASE_URL'])
        self.assertNotIn('paas_admin',environment['DATABASE_URL'])
        self.assertNotIn(environment['DATABASE_URL'],response.text)
        self.assertEqual(409,self.client.post('/v1/projects/'+self.project+'/deployments',json=config|{'target':'preview'}).status_code)
        other=self.client.post('/v1/projects',json={'name':'different'}).json()['id']
        self.assertEqual(404,self.client.post('/v1/projects/'+other+'/deployments',json=config).status_code)
        with self.store.connection() as db:
            db.execute("UPDATE data_resources SET state='failed' WHERE id=?",(resource['id'],))
        self.assertEqual(409,self.client.post('/v1/projects/'+self.project+'/deployments',json=config).status_code)

    def test_data_quota_and_database_runtime_reservation(self):
        first=self.resource(ready=True)
        self.resource(stage='preview',ready=True)
        other=self.client.post('/v1/projects',json={'name':'more'}).json()['id']
        self.assertEqual(429,self.client.post('/v1/projects/'+other+'/databases',json={}).status_code)
        with self.store.connection() as db:
            db.execute('UPDATE accounts SET max_concurrent=20')
        for _ in range(3):
            response=self.client.post('/v1/projects/'+self.project+'/deployments',json={'runtime':'node','memory_mb':1024,'cpus':0.1})
            self.assertEqual(201,response.status_code,response.text)
        self.assertEqual(429,self.client.post('/v1/projects/'+self.project+'/deployments',json={'runtime':'node','memory_mb':1024,'cpus':0.1}).status_code)

    def test_data_delete_requires_explicit_intent_and_no_retained_bindings(self):
        resource=self.resource('volumes',ready=True);path='/v1/data/'+resource['id']
        self.assertEqual(409,self.client.delete(path).status_code)
        release=self.client.post('/v1/projects/'+self.project+'/deployments',json={'runtime':'node','volume_id':resource['id']}).json()
        self.assertEqual(409,self.client.delete(path+'?destroy_data=true').status_code)
        self.assertEqual(204,self.client.delete('/v1/deployments/'+release['id']).status_code)
        self.assertEqual(202,self.client.delete(path+'?destroy_data=true').status_code)

    def test_failed_resource_retry_uses_its_own_kind_and_preserves_credentials(self):
        for kind in ['databases','volumes']:
            resource=self.resource(kind,ready=True)
            with self.store.connection() as db:
                original=db.execute('SELECT definition FROM data_resources WHERE id=?',(resource['id'],)).fetchone()[0]
                db.execute("UPDATE data_resources SET state='failed' WHERE id=?",(resource['id'],))
            retry=self.client.post('/v1/data/'+resource['id']+'/retry')
            self.assertEqual(202,retry.status_code,retry.text)
            job=self.client.get('/v1/jobs/'+retry.json()['job_id']).json()
            self.assertEqual(('database' if kind=='databases' else 'volume')+'-create',job['kind'])
            with self.store.connection() as db:
                self.assertEqual(original,db.execute('SELECT definition FROM data_resources WHERE id=?',(resource['id'],)).fetchone()[0])

    def test_data_only_token_can_observe_its_delete_receipt(self):
        resource=self.resource('volumes',ready=True)
        token=self.client.post('/v1/tokens',json={'name':'storage-only','scopes':['storage:write'],'project_ids':[self.project]}).json()['token']
        headers={'Authorization':'Bearer '+token}
        response=self.client.delete('/v1/data/'+resource['id']+'?destroy_data=true',headers=headers)
        self.assertEqual(202,response.status_code,response.text)
        job=self.client.get('/v1/jobs/'+response.json()['job_id'],headers=headers)
        self.assertEqual(200,job.status_code,job.text)
        self.assertEqual('volume-delete',job.json()['kind'])

    def test_preview_data_cannot_be_promoted_into_production(self):
        resource=self.resource(stage='preview',ready=True)
        release=self.client.post('/v1/projects/'+self.project+'/deployments',json={'runtime':'node','target':'preview','database_id':resource['id']}).json()
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready' WHERE id=?",(release['id'],))
        self.assertEqual(409,self.client.post('/v1/projects/'+self.project+'/promote',json={'deployment_id':release['id']}).status_code)

    def test_resource_worker_and_receipts_survive_deletion(self):
        resource=self.resource('volumes');worker=Worker(self.store,Mock())
        data=Mock();data.create.return_value={'server_id':'server'}
        with patch('publisher.data_backend.DataBackend',return_value=data):
            self.assertTrue(worker.run_once())
            self.assertEqual('ready',self.client.get('/v1/data/'+resource['id']).json()['state'])
            deletion=self.client.delete('/v1/data/'+resource['id']+'?destroy_data=true').json()
            self.assertTrue(worker.run_once())
        self.assertEqual(404,self.client.get('/v1/data/'+resource['id']).status_code)
        receipt=self.client.get('/v1/jobs/'+deletion['job_id'])
        self.assertEqual(200,receipt.status_code,receipt.text)
        self.assertEqual('completed',receipt.json()['state'])
        data.remove.assert_called_once()

    def test_old_job_schema_migrates_without_changing_accounts_or_keys(self):
        with self.store.connection() as db:
            db.execute('ALTER TABLE jobs DROP COLUMN resource_id')
        restored=Store(Settings(self.root))
        self.assertEqual(self.owner['account_id'],restored.authenticate(self.owner['token'])['account_id'])
        with restored.connection() as db:
            self.assertIn('resource_id',{r['name'] for r in db.execute('PRAGMA table_info(jobs)')})
        self.assertNotIn('database_id',DeploymentInput().configuration())

    def test_rendering_uses_private_network_and_external_managed_volume(self):
        deployment={'config':json.dumps({'database_id':'a'*24,'volume_id':'b'*24})}
        definition={'services':{'app':{'networks':['edge'],'volumes':[]}},'networks':{'edge':{}}}
        backend=Mock();backend.store=self.store
        DataBackend(backend).attach(deployment,definition)
        self.assertEqual('paas-data-'+'a'*24,definition['networks']['data-'+'a'*24]['name'])
        self.assertTrue(definition['volumes']['persistent']['external'])
        self.assertIn('persistent:/data',definition['services']['app']['volumes'])

    def test_failed_creation_without_a_vm_can_be_deleted_without_bootstrapping_one(self):
        value=self.resource()
        with self.store.connection() as db:
            resource=dict(db.execute('SELECT * FROM data_resources WHERE id=?',(value['id'],)).fetchone())
        backend=Mock();backend.store=self.store;backend.config={'project_id':'project'}
        backend.api.return_value={'environments':[]}
        missing=ExecutionFailure('Runtime not provisioned');missing.status=404
        client=backend.execution_client.return_value;client.runtime_status.side_effect=missing
        DataBackend(backend).remove(resource,lambda:None)
        client.data.assert_not_called()


if __name__=='__main__':
    unittest.main()
