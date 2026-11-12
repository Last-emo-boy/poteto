import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.config import Settings
from publisher.login_contract import ISSUER
from publisher import project_exports as exports, project_imports as imports
from publisher.store import PREVIEW_SCOPE, PREVIEW_ENV_SCOPE, PREVIEW_DATA_SCOPE, PREVIEW_MANAGE_SCOPE
from publisher.worker import Worker
from test_control import archive


def bundle(target='preview', production_env=None, web=False):
    resource='c'*24;release='b'*24
    manifest={'format':'ovh-paas-project/1','bundle_id':'a'*24,'name':'native-backup','source_release':release,
              'image':'paas-release:'+release,'configuration':{'runtime':'image','target':target,
                  'database_id':resource,'migration_command':'node migrate.js'},
              'project_environment':production_env or {},'preview_environment':{'MODE':'future'},
              'release_environment':{'MODE':'snapshot'},'resources':[{'id':resource,'kind':'database','stage':target}]}
    if web:manifest['configuration']['outbound_network']='web'
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as out:out.writestr('databases/'+resource+'.dump',b'PGDMP123')
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w') as out:
        out.writestr('manifest.json',json.dumps(manifest));out.writestr('source.tar.gz',archive())
        out.writestr('image.tar',b'image');out.writestr('data.zip',data.getvalue())
    return result.getvalue()


class PreviewArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.app=create_app(Settings(self.root,personal_previews=True,native_databases=True));self.store=self.app.state.store
        with self.store.connection() as db:
            self.project=self.store.enroll_cli_identity(db,ISSUER,'preview-archive-user')
            self.account=db.execute('SELECT account_id FROM projects WHERE id=?',(self.project,)).fetchone()[0]
        self.client=self.client_for([PREVIEW_SCOPE,PREVIEW_ENV_SCOPE,PREVIEW_DATA_SCOPE])
        self.base='/v1/projects/'+self.project
        self.age=self.root/'test-age'
        self.age.write_text('#!'+sys.executable+'\nimport sys\nif len(sys.argv)==3: sys.stdout.buffer.write(b"test-only\\0"+sys.stdin.buffer.read())\n')
        self.age.chmod(0o700)
        for name,value in [('AGE',self.age)]:
            item=patch.object(exports,name,value);item.start();self.addCleanup(item.stop)
        disk=patch.object(exports.shutil,'disk_usage',return_value=Mock(free=100*1024**3));disk.start();self.addCleanup(disk.stop)

    def client_for(self,scopes):
        with self.store.connection() as db:
            token=self.store.mint_token(db,{'id':None,'account_id':self.account},name='preview archive',scopes=scopes,
                                       project_ids=[self.project],expires=9999999999)['token']
        client=TestClient(self.app,headers={'Authorization':'Bearer '+token});self.addCleanup(client.close)
        return client

    def ready_release(self):
        resource=self.client.post(self.base+'/databases',json={'stage':'preview'}).json()['id']
        with self.store.connection() as db:
            db.execute("UPDATE data_resources SET state='ready' WHERE id=?",(resource,))
            db.execute("UPDATE jobs SET state='completed' WHERE resource_id=?",(resource,))
            db.execute('UPDATE projects SET environment=?,preview_environment=? WHERE id=?',
                       (self.store.seal({'PRODUCTION_SECRET':'never-export'}),self.store.seal({'PREVIEW_KEY':'preview-value'}),self.project))
        release=self.client.post(self.base+'/deployments',json={'runtime':'image','target':'preview','database_id':resource}).json()['id']
        with self.store.connection() as db:db.execute("UPDATE deployments SET state='ready' WHERE id=?",(release,))
        (self.root/'artifacts'/(release+'.tar.gz')).write_bytes(archive())
        return release,resource

    def test_preview_export_uses_native_stream_and_excludes_production_secrets(self):
        release,resource=self.ready_release()
        request={'recipient':'age1'+'a'*58,'deployment_id':release}
        response=self.client.post(self.base+'/exports',json=request)
        self.assertEqual(202,response.status_code,response.text);operation=response.json()['id']
        backend=Mock();backend.config={'execution_enabled':True}
        def stream(method,path,output,limit,check,**kwargs):
            if method=='GET':output.write(b'image')
            else:
                with zipfile.ZipFile(output,'w') as data:data.writestr('databases/'+resource+'.dump',b'PGDMP123')
        backend.execution_client.return_value.stream_export.side_effect=stream
        self.assertTrue(Worker(self.store,backend).run_once())
        self.assertEqual('completed',self.client.get('/v1/jobs/'+operation).json()['state'])
        contents=exports.artifact(self.store,operation).read_bytes()[10:]
        with zipfile.ZipFile(io.BytesIO(contents)) as zipped:
            manifest=json.loads(zipped.read('manifest.json'))
            self.assertEqual({},manifest['project_environment'])
            self.assertEqual({'PREVIEW_KEY':'preview-value'},manifest['preview_environment'])
            self.assertNotIn('never-export',zipped.read('manifest.json').decode())
            self.assertNotIn('DATABASE_URL',manifest['release_environment'])
        client=backend.execution_client.return_value
        self.assertEqual('/v1/databases/'+resource+'/snapshot',client.stream_export.call_args.args[1])
        client.bootstrap.assert_not_called();backend.target.assert_not_called();backend.find_container.assert_not_called()

    def test_preview_export_requires_environment_and_data_scopes_but_never_grants_production(self):
        release,_=self.ready_release();request={'recipient':'age1'+'a'*58,'deployment_id':release}
        for scopes in ([PREVIEW_SCOPE],[PREVIEW_SCOPE,PREVIEW_ENV_SCOPE],[PREVIEW_SCOPE,PREVIEW_DATA_SCOPE]):
            response=self.client_for(scopes).post(self.base+'/exports',json=request)
            self.assertEqual(403,response.status_code,response.text)
        self.assertEqual(403,self.client.post(self.base+'/exports',json={'recipient':'age1'+'a'*58}).status_code)

    def upload(self,content):
        response=self.client.post(self.base+'/imports',json={'bundle_id':'a'*24,'size':len(content),'kinds':['database'],'target':'preview'})
        self.assertEqual(201,response.status_code,response.text)
        path='/v1/imports/'+response.json()['id']
        response=self.client.put(path+'/part?offset=0',content=content,headers={'Content-Type':'application/octet-stream'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(202,self.client.post(path+'/submit').status_code)
        return path

    def test_preview_restore_keeps_target_rebinds_database_and_does_not_repeat_migration(self):
        path=self.upload(bundle());operation=path.split('/')[-1]
        backend=Mock();backend.config={'execution_enabled':True};worker=Worker(self.store,backend)
        def deploy(job,release,*,imported):
            config=json.loads(release['config'])
            self.assertEqual(('preview','gvisor','isolated'),(config['target'],release['runtime_driver'],release['preview_mode']))
            self.assertNotEqual('c'*24,config['database_id']);self.assertTrue(imported['skip_migration'])
            self.assertEqual('snapshot',self.store.unseal(release['environment'])['MODE'])
            with self.store.connection() as db:db.execute("UPDATE deployments SET state='ready' WHERE id=?",(operation,))
        with patch('publisher.project_imports.DataBackend') as factory,patch.object(worker,'deploy',side_effect=deploy):
            factory.return_value.create.return_value={'driver':'native-postgres'}
            worker.run_once()
            factory.return_value.client.restore_native_database.assert_called_once()
            factory.return_value.client.restore_data.assert_not_called()
        result=self.client.get(path).json();self.assertEqual('completed',result['state'],result)
        self.assertEqual('preview',result['target']);self.assertIn('preview-'+operation,result['url'])
        with self.store.connection() as db:
            project=db.execute('SELECT * FROM projects WHERE id=?',(self.project,)).fetchone()
            self.assertIsNone(project['current_deployment']);self.assertEqual({},self.store.unseal(project['environment']))
            self.assertEqual({'MODE':'future'},self.store.unseal(project['preview_environment']))

    def test_preview_import_rejects_production_bundle_before_allocating_data(self):
        path=self.upload(bundle(target='production',production_env={'SECRET':'production'}))
        backend=Mock();backend.config={'execution_enabled':True};Worker(self.store,backend).run_once()
        result=self.client.get(path).json();self.assertEqual('failed',result['state'],result)
        with self.store.connection() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM data_resources').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM deployments').fetchone()[0])

    def test_import_cannot_restore_web_access_without_the_original_permission(self):
        path=self.upload(bundle(web=True));backend=Mock();backend.config={'execution_enabled':True}
        Worker(self.store,backend).run_once()
        result=self.client.get(path).json();self.assertEqual('failed',result['state'],result)
        self.assertIn('explicit web permission',result['error'])
        with self.store.connection() as db:self.assertEqual(0,db.execute('SELECT count(*) FROM data_resources').fetchone()[0])

    def test_explicit_preview_cleanup_preserves_its_database_and_bulk_prune_protection(self):
        release,resource=self.ready_release()
        self.assertEqual(403,self.client.delete('/v1/deployments/'+release).status_code)
        manager=self.client_for([PREVIEW_SCOPE,PREVIEW_MANAGE_SCOPE])
        self.assertEqual(403,manager.delete('/v1/deployments/'+release).status_code)
        manager=self.client_for([PREVIEW_SCOPE,PREVIEW_MANAGE_SCOPE,PREVIEW_DATA_SCOPE])
        protected=manager.get(self.base+'/previews/prune?older_than_days=0&keep=0').json()['protected']
        self.assertEqual('persistent_data_binding',next(row['reason'] for row in protected if row['id']==release))
        deletion=manager.delete('/v1/deployments/'+release)
        self.assertEqual(202,deletion.status_code,deletion.text)
        backend=Mock();Worker(self.store,backend).run_once()
        self.assertEqual('completed',manager.get('/v1/jobs/'+deletion.json()['job_id']).json()['state'])
        self.assertEqual('ready',manager.get('/v1/data/'+resource).json()['state'])
        backend.execution_client.assert_not_called()
