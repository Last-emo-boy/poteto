import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock,patch
import zipfile
from fastapi.testclient import TestClient
from execution import runtime_data
from publisher import project_imports as imports
from publisher.api import create_app
from publisher.config import Settings
from publisher.worker import Worker
from test_control import archive


def fixture_bundle(bundle='a'*24,with_data=True):
    result=io.BytesIO();data=io.BytesIO();db='c'*24;volume='d'*24
    resources=[{'id':db,'kind':'database','stage':'production'},{'id':volume,'kind':'volume','stage':'production'}] if with_data else []
    config={'runtime':'node'}|({'database_id':db,'volume_id':volume,'migration_command':'do not replay'} if with_data else {})
    manifest={'format':'ovh-paas-project/1','bundle_id':bundle,'name':'portable','source_release':'b'*24,'image':'paas-release:'+'b'*24,
        'configuration':config,'project_environment':{'APP_VERSION':'future'},'preview_environment':{'APP_VERSION':'preview'},
        'release_environment':{'APP_VERSION':'published'},'resources':resources}
    with zipfile.ZipFile(result,'w') as output:
        output.writestr('manifest.json',json.dumps(manifest));output.writestr('source.tar.gz',archive());output.writestr('image.tar',b'owned image')
        if with_data:
            with zipfile.ZipFile(data,'w') as content:
                content.writestr('databases/'+db+'.dump',b'PGDMP'+b'x'*20)
                content.writestr('volumes/'+volume+'/',b'');content.writestr('volumes/'+volume+'/proof.txt',b'file from snapshot')
            output.writestr('data.zip',data.getvalue())
    return result.getvalue()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(Settings(self.root,new_app_domain='tenant-apps.test'));self.store=self.app.state.store
        self.owner=self.store.create_account('owner');other=self.store.create_account('other')
        self.client=TestClient(self.app,headers={'Authorization':'Bearer '+self.owner['token']})
        self.bob=TestClient(self.app,headers={'Authorization':'Bearer '+other['token']})
        self.project=self.client.post('/v1/projects',json={'name':'destination'}).json()['id'];self.path='/v1/projects/'+self.project
        self.disk=patch.object(imports.shutil,'disk_usage',return_value=Mock(free=100*1024**3));self.disk.start()
        self.chunks=patch.object(imports,'CHUNK',512);self.chunks.start()

    def tearDown(self):self.chunks.stop();self.disk.stop();self.client.close();self.bob.close();self.temp.cleanup()

    def create(self,payload,kinds=()):
        response=self.client.post(self.path+'/imports',json={'bundle_id':'a'*24,'size':len(payload),'kinds':list(kinds)})
        self.assertEqual(201,response.status_code,response.text)
        return '/v1/imports/'+response.json()['id']

    def upload(self,path,payload,offset):
        return self.client.put(path+'/part',params={'offset':offset},content=payload[offset:offset+imports.CHUNK],headers={'Content-Type':'application/octet-stream'})

    def upload_all(self,path,payload):
        for offset in range(0,len(payload),imports.CHUNK):
            response=self.upload(path,payload,offset);self.assertEqual(200,response.status_code,response.text)
        response=self.client.post(path+'/submit');self.assertEqual(202,response.status_code,response.text)

    def test_offset_lease_encrypted_parts_and_explicit_discard(self):
        data=b'synthetic transfer secret'*80;path=self.create(data);op=path.split('/')[-1]
        self.assertEqual(404,self.bob.get(path).status_code)
        self.assertEqual(404,self.bob.put(path+'/part?offset=0',content=data[:512],headers={'Content-Type':'application/octet-stream'}).status_code)
        self.assertEqual(409,self.client.post(path+'/submit').status_code)
        self.assertEqual(409,self.client.post(self.path+'/deployments',json={}).status_code)
        self.assertEqual(409,self.client.delete(self.path).status_code)
        self.assertEqual(409,self.upload(path,data,512).status_code)
        self.assertEqual(512,self.upload(path,data,0).json()['received'])
        stored=imports.part_path(self.store,op,0).read_bytes();self.assertNotIn(data[:32],stored)
        self.assertEqual(imports.part_binding(op,0)+data[:512],self.store.cipher.decrypt(stored))
        self.assertEqual(409,self.upload(path,data,0).status_code)
        self.assertEqual(204,self.client.delete(path).status_code)
        self.assertFalse(imports.payload_dir(self.store,op).exists())
        self.assertEqual(201,self.client.post(self.path+'/deployments',json={}).status_code)

    def test_rejects_existing_environment_and_missing_data_scope(self):
        token=self.client.post('/v1/tokens',json={'name':'ci','scopes':sorted(imports.BASE_SCOPES),'project_ids':[self.project]}).json()['token']
        with TestClient(self.app,headers={'Authorization':'Bearer '+token}) as scoped:
            self.assertEqual(403,scoped.post(self.path+'/imports',json={'bundle_id':'a'*24,'size':100,'kinds':['database']}).status_code)
        self.client.put(self.path+'/env',json={'values':{'EXISTING':'keep'}})
        self.assertEqual(409,self.client.post(self.path+'/imports',json={'bundle_id':'a'*24,'size':100}).status_code)

    def test_tampered_part_fails_before_creating_any_destination_resource(self):
        data=fixture_bundle(with_data=False);path=self.create(data);self.upload_all(path,data);op=path.split('/')[-1]
        imports.part_path(self.store,op,0).write_bytes(b'corrupted ciphertext')
        backend=Mock();self.assertTrue(Worker(self.store,backend).run_once())
        result=self.client.get(path).json();self.assertEqual('failed',result['state']);self.assertIsNone(result['deployment_id'])
        self.assertIn('authentication',result['error']);backend.prepare_remote.assert_not_called()
        self.assertEqual([],list(imports.work_dir(self.store,op).glob('stage')))
        self.assertEqual(204,self.client.delete(path).status_code)

    def test_import_materializes_owned_resources_and_two_environment_snapshots(self):
        payload=fixture_bundle();path=self.create(payload,['database','volume']);self.upload_all(path,payload);op=path.split('/')[-1]
        backend=Mock();backend.config={'execution_enabled':True};worker=Worker(self.store,backend)
        def deploy(job,release,*,imported):
            config=json.loads(release['config']);env=self.store.unseal(release['environment'])
            self.assertEqual('tenant-apps.test',release['app_domain'])
            self.assertNotEqual('c'*24,config['database_id']);self.assertNotEqual('d'*24,config['volume_id'])
            self.assertEqual('do not replay',config['migration_command']);self.assertIn('db-'+config['database_id'],env['DATABASE_URL'])
            self.assertEqual('published',env['APP_VERSION']);self.assertEqual('/data',env['PAAS_DATA_DIR'])
            self.assertEqual(b'owned image',imported['image_path'].read_bytes())
            with self.store.connection() as db:
                worker.guard(db);db.execute("UPDATE deployments SET state='ready' WHERE id=?",(op,));db.execute('UPDATE projects SET current_deployment=? WHERE id=?',(op,self.project))
        with patch('publisher.project_imports.DataBackend') as backend_factory,patch.object(worker,'deploy',side_effect=deploy):
            data=backend_factory.return_value;data.create.return_value={'owned':True}
            self.assertTrue(worker.run_once());self.assertEqual(2,data.client.restore_data.call_count)
        result=self.client.get(path).json();self.assertEqual('completed',result['state']);self.assertEqual(op,result['deployment_id'])
        self.assertEqual('https://app-'+self.project+'.tenant-apps.test',result['url'])
        self.assertFalse(imports.payload_dir(self.store,op).exists())
        with self.store.connection() as db:
            row=db.execute('SELECT environment,preview_environment FROM projects WHERE id=?',(self.project,)).fetchone()
            self.assertEqual({'APP_VERSION':'future'},self.store.unseal(row['environment']))
            self.assertEqual({'APP_VERSION':'preview'},self.store.environment(row,'preview'))


class RuntimeImportTests(unittest.TestCase):
    def test_database_receipt_prevents_replaying_a_committed_restore(self):
        op='a'*24;request={'operation':op,'project_id':'b'*24,'resource_id':'c'*24}
        owned={'Id':'owned','State':{'Running':True},'Config':{'Labels':{'paas.publisher.import':op}}}
        with patch.object(runtime_data,'database',return_value=owned),patch.object(runtime_data,'pg_admin',side_effect=['',op,'']) as admin,patch.object(runtime_data.subprocess,'Popen') as process:
            runtime_data.restore_database(request,runtime_data.RestoreInput(io.BytesIO(b'PGDMP'),5))
            process.assert_not_called();self.assertIn('DROP DATABASE IF EXISTS paas_restore_old',admin.call_args.args[1])

    def test_volume_receipt_keeps_later_writes_when_response_is_replayed(self):
        op='a'*24;rid='c'*24;pid='b'*24;data=b'snapshot';entry=json.dumps({'name':'proof.txt','size':len(data),'directory':False,'executable':False}).encode()
        payload=struct.pack('!I',len(entry))+entry+data+struct.pack('!I',0)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base=root/('paas-data-'+rid)/'_data';base.mkdir(parents=True);receipt=root/'receipt.json'
            labels=runtime_data.object_labels(rid,pid,'volume')|{'paas.publisher.import':op}
            volume=Mock(stdout=json.dumps([{'Labels':labels,'Mountpoint':str(base)}]).encode());empty=Mock(stdout=b'')
            with patch.object(runtime_data,'VOLUME_ROOT',root),patch.object(runtime_data,'docker',side_effect=[volume,empty,empty,volume]),patch.object(runtime_data.os,'chown'),patch.object(runtime_data.shutil,'disk_usage',return_value=Mock(free=10*1024**3)):
                spec={'operation':op,'resource_id':rid,'project_id':pid}
                runtime_data.restore_volume(spec,runtime_data.RestoreInput(io.BytesIO(payload),len(payload)),receipt)
                self.assertEqual(data,(base/'proof.txt').read_bytes())
                (base/'proof.txt').write_bytes(b'later application write')
                runtime_data.restore_volume(spec,runtime_data.RestoreInput(io.BytesIO(payload),len(payload)),receipt)
                self.assertEqual(b'later application write',(base/'proof.txt').read_bytes())

    def test_incomplete_restore_stream_never_reads_beyond_declared_bounds(self):
        with self.assertRaisesRegex(ValueError,'ended early'):
            runtime_data.RestoreInput(io.BytesIO(b'x'),2).copy(None,2)
        with self.assertRaisesRegex(ValueError,'declared length'):
            runtime_data.RestoreInput(io.BytesIO(b'abc'),1).read(2)
