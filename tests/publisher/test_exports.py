import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
import zipfile
from fastapi.testclient import TestClient
from execution import runtime_data
from publisher import project_exports as exports
from publisher.api import create_app
from publisher.config import Settings
from publisher.controlflow import ObservationUnavailable
from publisher.worker import Worker
from test_control import archive


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(Settings(self.root));self.store=self.app.state.store
        self.owner=self.store.create_account('owner');self.other=self.store.create_account('other')
        self.client=TestClient(self.app,headers={'Authorization':'Bearer '+self.owner['token']})
        self.bob=TestClient(self.app,headers={'Authorization':'Bearer '+self.other['token']})
        self.project=self.client.post('/v1/projects',json={'name':'exported'}).json()['id']
        self.path='/v1/projects/'+self.project
        self.release=self.client.post(self.path+'/deployments',json={'runtime':'node'}).json()['id']
        self.source=archive({'index.html':b'owned source'})
        (self.root/'artifacts'/(self.release+'.tar.gz')).write_bytes(self.source)
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready',backend_id='compose',environment=? WHERE id=?",(self.store.seal({'APP_SECRET':'release-value','DATABASE_URL':'internal-database-password'}),self.release))
            db.execute('UPDATE projects SET current_deployment=?,environment=? WHERE id=?',(self.release,self.store.seal({'APP_SECRET':'future-value'}),self.project))
        self.age=self.root/'test-age'
        self.age.write_text('#!'+sys.executable+'\nimport sys\nif len(sys.argv)==3: sys.stdout.buffer.write(b"test-only\\0"+sys.stdin.buffer.read())\n')
        self.age.chmod(0o700)
        self.age_patch=patch.object(exports,'AGE',self.age);self.age_patch.start()
        self.disk_patch=patch.object(exports.shutil,'disk_usage',return_value=Mock(free=100*1024**3));self.disk_patch.start()

    def tearDown(self):
        self.age_patch.stop();self.disk_patch.stop();self.client.close();self.bob.close();self.temp.cleanup()

    def volume(self):
        result=self.client.post(self.path+'/volumes',json={}).json()
        with self.store.connection() as db:
            db.execute("UPDATE data_resources SET state='ready' WHERE id=?",(result['id'],))
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(result['job_id'],))
        return result['id']

    def create(self,quiesce=False,client=None):
        return (client or self.client).post(self.path+'/exports',json={'recipient':'age1'+'a'*58,'quiesce':quiesce})

    def backend(self):
        backend=Mock();backend.target.return_value={'account_id':self.owner['account_id']}
        def stream(method,path,output,limit,check,**kwargs):
            check();output.write(b'owned image' if method=='GET' else b'owned data zip');return 11
        backend.execution_client.return_value.stream_export.side_effect=stream
        return backend

    def test_export_requires_data_consent_and_original_scopes_on_download(self):
        resource=self.volume()
        self.assertEqual(409,self.create().status_code)
        self.assertEqual(404,self.create(True,self.bob).status_code)
        restricted=self.client.post('/v1/tokens',json={'name':'no-files','scopes':['exports:write','deployments:write','env:write'],'project_ids':[self.project]}).json()
        with TestClient(self.app,headers={'Authorization':'Bearer '+restricted['token']}) as token:
            self.assertEqual(403,self.create(True,token).status_code)
            response=self.create(True);self.assertEqual(202,response.status_code,response.text)
            operation=response.json()['id']
            with self.store.connection() as db:
                db.execute('UPDATE jobs SET resource_id=NULL WHERE resource_id=?',(resource,));db.execute('DELETE FROM data_resources WHERE id=?',(resource,))
            self.assertEqual(403,token.get('/v1/exports/'+operation+'/data').status_code)

    def test_export_fences_project_writes_and_streams_owned_snapshot(self):
        resource=self.volume();response=self.create(True);self.assertEqual(202,response.status_code,response.text)
        operation=response.json()['id'];path='/v1/exports/'+operation
        self.assertEqual(409,self.client.put(self.path+'/env',json={'values':{'NEW':'changed'}}).status_code)
        self.assertEqual(409,self.client.delete(self.path+'?destroy_data=true').status_code)
        self.assertEqual(409,self.client.get(path+'/data').status_code)
        self.assertEqual(404,self.bob.get(path).status_code)
        backend=self.backend();self.assertTrue(Worker(self.store,backend).run_once())
        status=self.client.get(path).json();self.assertEqual('completed',status['state'])
        body=exports.artifact(self.store,operation).read_bytes()
        self.assertEqual(status['size'],len(body));self.assertTrue(body.startswith(b'test-only\0'))
        with zipfile.ZipFile(io.BytesIO(body[10:])) as bundle:
            self.assertEqual(self.source,bundle.read('source.tar.gz'))
            self.assertEqual(b'owned image',bundle.read('image.tar'));self.assertEqual(b'owned data zip',bundle.read('data.zip'))
            manifest=json.loads(bundle.read('manifest.json'))
            self.assertEqual({'APP_SECRET':'release-value'},manifest['release_environment'])
            self.assertEqual({'APP_SECRET':'future-value'},manifest['project_environment'])
            self.assertEqual([{'id':resource,'kind':'volume','stage':'production'}],manifest['resources'])
            self.assertNotIn(b'internal-database-password',bundle.read('manifest.json'))
        chunk=self.client.get(path+'/data?offset=4&length=12')
        self.assertEqual(200,chunk.status_code);self.assertEqual(body[4:16],chunk.content)
        self.assertEqual('4',chunk.headers['X-Paas-Offset']);self.assertEqual('no-store',chunk.headers['Cache-Control'])
        self.assertEqual(416,self.client.get(path+'/data?length=8388609').status_code)
        self.assertEqual(204,self.client.delete(path).status_code);self.assertFalse(exports.artifact(self.store,operation).exists())

    def test_incomplete_snapshot_never_publishes_ciphertext(self):
        self.volume();operation=self.create(True).json()['id']
        backend=self.backend()
        def stream(method,path,output,limit,check,**kwargs):
            output.write(b'partial')
            if method=='POST':raise ObservationUnavailable('Snapshot stream interrupted')
        backend.execution_client.return_value.stream_export.side_effect=stream
        Worker(self.store,backend).run_once()
        self.assertEqual('queued',self.client.get('/v1/exports/'+operation).json()['state'])
        self.assertEqual([],list(exports.directory(self.store).iterdir()))

    def test_recovery_adopts_only_a_finalized_previous_output(self):
        operation=self.create().json()['id'];final=exports.artifact(self.store,operation);final.write_bytes(b'prior encrypted output')
        backend=self.backend();Worker(self.store,backend).run_once()
        backend.execution_client.assert_not_called()
        self.assertEqual('completed',self.client.get('/v1/exports/'+operation).json()['state'])


class SnapshotTests(unittest.TestCase):
    def capture(self,wake=False):
        project,operation,resource='1'*24,'2'*24,'3'*24
        state={'a':{'Running':True,'Paused':False,'StartedAt':'first'},'b':{'Running':False,'Paused':False,'StartedAt':'old'},'c':{'Running':True,'Paused':True,'StartedAt':'manual'}}
        commands=[]
        def docker(action,cid):
            commands.append((action,cid));state[cid]['Paused']=action=='pause'
        def volume(spec,archive,remaining,entries):
            self.assertTrue(state['a']['Paused']);archive.writestr('volumes/'+resource+'/file.txt','data')
            if wake:state['b']|={'Running':True,'StartedAt':'new'}
        with tempfile.TemporaryDirectory() as temp,patch.object(runtime_data,'EXPORT_ROOT',Path(temp)),patch.object(runtime_data,'project_apps',side_effect=lambda p:copy.deepcopy(state)),patch.object(runtime_data,'docker',side_effect=docker),patch.object(runtime_data,'export_volume',side_effect=volume):
            request={'project_id':project,'resources':[{'id':resource,'kind':'volume'}]};out=io.BytesIO()
            if wake:
                with self.assertRaisesRegex(ValueError,'changed during'):runtime_data.capture_export(operation,request,out)
            else:
                runtime_data.capture_export(operation,request,out)
                with zipfile.ZipFile(out) as archive:self.assertEqual(b'data',archive.read('volumes/'+resource+'/file.txt'))
            self.assertFalse((Path(temp)/(project+'.json')).exists())
        self.assertEqual([('pause','a'),('unpause','a')],commands)
        self.assertTrue(state['c']['Paused'])

    def test_snapshot_resumes_only_originally_running_unpaused_apps(self):self.capture()

    def test_late_wakeup_invalidates_snapshot_and_still_resumes(self):self.capture(wake=True)

    def test_data_copy_refuses_to_exceed_shared_byte_budget(self):
        with self.assertRaisesRegex(ValueError,'size limit'):
            runtime_data.export_copy(io.BytesIO(b'abcd'),io.BytesIO(),[3])
