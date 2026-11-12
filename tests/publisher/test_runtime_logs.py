import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.backend import BackendError,Dokploy
from publisher.config import Settings
from publisher.container_logs import capture
from publisher.worker import Worker
from execution import runtime_rpc
from execution import sandbox
from publisher.runtime_logs import save


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(Settings(self.root));self.store=self.app.state.store
        self.owner=self.store.create_account('owner');self.other=self.store.create_account('other')
        self.client=TestClient(self.app,headers={'Authorization':'Bearer '+self.owner['token']})
        self.bob=TestClient(self.app,headers={'Authorization':'Bearer '+self.other['token']})
        self.project=self.client.post('/v1/projects',json={'name':'logs'}).json()['id']
        self.release=self.client.post(f'/v1/projects/{self.project}/deployments',json={}).json()['id']
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready',backend_id='compose' WHERE id=?",(self.release,))
        self.path='/v1/deployments/'+self.release+'/runtime-logs'

    def tearDown(self):
        self.client.close();self.bob.close();self.temp.cleanup()

    def test_logs_require_release_ownership_scope_and_project_permission(self):
        self.assertEqual(401,TestClient(self.app).get(self.path).status_code)
        self.assertEqual(404,self.bob.get(self.path).status_code)
        denied=self.client.post('/v1/tokens',json={'name':'no-log-read','scopes':['projects:read']}).json()
        self.assertEqual(403,self.client.get(self.path,headers={'Authorization':'Bearer '+denied['token']}).status_code)
        other_project=self.client.post('/v1/projects',json={'name':'other'}).json()['id']
        limited=self.client.post('/v1/tokens',json={'name':'different-project','scopes':['deployments:read'],'project_ids':[other_project]}).json()
        self.assertEqual(404,self.client.get(self.path,headers={'Authorization':'Bearer '+limited['token']}).status_code)
        with self.store.connection() as db:
            self.assertEqual(0,db.execute("SELECT count(*) FROM jobs WHERE kind='runtime-logs'").fetchone()[0])

    def test_refresh_deduplicates_and_worker_lane_does_not_wait_for_builds(self):
        result=self.client.get(self.path).json()
        for _ in range(5):self.assertEqual(result['job_id'],self.client.get(self.path).json()['job_id'])
        backend=Mock();backend.runtime_logs.return_value='2026-11-10T00:00:00Z hello\n'
        self.assertFalse(Worker(self.store,backend).run_once())
        active=self.client.post(f'/v1/projects/{self.project}/deployments',json={}).json()['id']
        with self.store.connection() as db:
            now=time.time()
            db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,state,owner,lease_until,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",('a'*24,self.project,'deploy',active,'running','busy-builder',now+90,now,now))
        self.assertTrue(Worker(self.store,backend,logs_only=True).run_once())
        snapshot=self.client.get(self.path).json()
        self.assertEqual('completed',snapshot['state'])
        self.assertIn('hello',snapshot['log'])
        backend.runtime_logs.assert_called_once()
        backend.promote.assert_not_called()
        with self.store.connection() as db:
            self.assertEqual('busy-builder',db.execute("SELECT owner FROM jobs WHERE id=?",('a'*24,)).fetchone()[0])
            self.assertEqual('ready',db.execute('SELECT state FROM deployments WHERE id=?',(self.release,)).fetchone()[0])

    def test_snapshot_is_bounded_and_redacted_before_storage(self):
        self.client.get(self.path)
        with self.store.connection() as db:
            db.execute('UPDATE deployments SET environment=? WHERE id=?',(self.store.seal({'APP_SECRET':'runtime-private-value','DATABASE_URL':'postgresql://app:database-secret@db/app'}),self.release))
        backend=Mock();backend.runtime_logs.return_value='x'*90000+' runtime-private-value database-secret'
        Worker(self.store,backend,logs_only=True).run_once()
        result=self.client.get(self.path).json()
        self.assertNotIn('runtime-private-value',result['log']);self.assertNotIn('database-secret',result['log'])
        self.assertIn('[REDACTED]',result['log']);self.assertLessEqual(len(result['log'].encode()),65536)
        with self.store.connection() as db:
            self.assertNotIn('database-secret',db.execute('SELECT content FROM runtime_logs').fetchone()[0])

    def test_refresh_failure_preserves_release_and_previous_snapshot(self):
        self.client.get(self.path)
        with self.store.connection() as db:
            db.execute('INSERT INTO runtime_logs VALUES(?,?,?)',(self.release,'previous log',time.time()-20))
        backend=Mock();backend.runtime_logs.side_effect=BackendError('Release container not found')
        Worker(self.store,backend,logs_only=True).run_once()
        response=self.client.get(self.path).json()
        self.assertEqual('failed',response['state']);self.assertEqual('previous log',response['log'])
        self.assertEqual('ready',self.client.get('/v1/deployments/'+self.release).json()['state'])

    def test_maintenance_reads_never_enqueue_new_background_work(self):
        (self.root/'maintenance.json').write_text('{}')
        self.assertEqual('paused',self.client.get(self.path).json()['state'])
        with self.store.connection() as db:self.assertEqual(0,db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_log_refresh_concurrency_is_bounded_per_account(self):
        self.client.get(self.path)
        for index in range(5):
            release=self.client.post(f'/v1/projects/{self.project}/deployments',json={}).json()['id']
            with self.store.connection() as db:
                db.execute("UPDATE deployments SET state='ready',backend_id='compose' WHERE id=?",(release,))
            response=self.client.get('/v1/deployments/'+release+'/runtime-logs')
            self.assertEqual(200 if index<4 else 429,response.status_code)

    def test_refresh_receipts_and_release_cleanup_are_bounded(self):
        backend=Mock();backend.runtime_logs.return_value='log'
        for _ in range(8):
            self.client.get(self.path);Worker(self.store,backend,logs_only=True).run_once()
            with self.store.connection() as db:db.execute('UPDATE jobs SET updated=0')
        with self.store.connection() as db:
            self.assertEqual(3,db.execute("SELECT count(*) FROM jobs WHERE kind='runtime-logs'").fetchone()[0])
            self.store.purge_release(db,self.release)
            self.assertEqual(0,db.execute('SELECT count(*) FROM runtime_logs').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM runtime_log_entries').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM runtime_log_heads').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def snapshot(self, text):
        with self.store.connection() as db:save(db,self.release,text,time.time())

    def test_cursor_deduplicates_overlap_and_preserves_repeated_lines(self):
        self.snapshot('first\nsecond\n')
        first=self.client.get(self.path).json()
        self.assertEqual(2,first['next_cursor'])
        self.snapshot('first\nsecond\n')
        self.assertEqual([],self.client.get(self.path+'?after=2').json()['entries'])
        self.snapshot('second\nthird\nthird\n')
        delta=self.client.get(self.path+'?after=2').json()
        self.assertEqual(['third\n','third\n'],[r['message'] for r in delta['entries']])
        self.assertEqual('third\nthird\n',delta['log']);self.assertEqual(4,delta['next_cursor'])
        self.assertFalse(delta['truncated'])

    def test_tail_zero_and_cursor_survive_restart_without_replay(self):
        self.snapshot('one\ntwo\nthree\n')
        self.assertEqual('three\n',self.client.get(self.path+'?tail=1').json()['log'])
        start=self.client.get(self.path+'?tail=0').json()
        self.assertEqual([],start['entries']);self.assertEqual('',start['log']);self.assertEqual(3,start['next_cursor'])
        app=create_app(Settings(self.root))
        with TestClient(app,headers={'Authorization':'Bearer '+self.owner['token']}) as client:
            self.assertEqual([],client.get(self.path+'?after=3').json()['entries'])

    def test_rotated_log_emits_gap_and_evicted_cursor_is_explicit(self):
        self.snapshot('old\n');self.snapshot('new\n')
        result=self.client.get(self.path+'?after=1').json()
        self.assertEqual(['gap','line'],[r['kind'] for r in result['entries']])
        for index in range(210):self.snapshot(f'new\n{index}\n')
        result=self.client.get(self.path+'?after=1').json()
        self.assertTrue(result['truncated']);self.assertLessEqual(len(result['entries']),200)
        with self.store.connection() as db:
            size=db.execute('SELECT sum(length(CAST(message AS BLOB))) FROM runtime_log_entries').fetchone()[0]
            self.assertLessEqual(size,65536)

    def test_cursor_validation_does_not_queue_work(self):
        for query in ['after=-1','after=9223372036854775808','tail=-1','tail=201','tail=abc']:
            self.assertEqual(422,self.client.get(self.path+'?'+query).status_code)
        self.assertEqual(409,self.client.get(self.path+'?after=1').status_code)
        with self.store.connection() as db:self.assertEqual(0,db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_stream_is_redacted_and_stays_bounded_for_multibyte_output(self):
        self.client.get(self.path)
        with self.store.connection() as db:
            db.execute('UPDATE deployments SET environment=? WHERE id=?',(self.store.seal({'SECRET':'never-show'}),self.release))
        backend=Mock();backend.runtime_logs.return_value='中'*30000+' never-show\n'
        Worker(self.store,backend,logs_only=True).run_once()
        result=self.client.get(self.path+'?after=0').json()
        self.assertNotIn('never-show',json.dumps(result));self.assertIn('[REDACTED]',result['log'])
        self.assertLessEqual(len(result['log'].encode()),65536)

    def test_legacy_cached_snapshot_gets_cursor_on_first_refresh(self):
        self.client.get(self.path)
        backend=Mock();backend.runtime_logs.return_value='legacy\n'
        Worker(self.store,backend,logs_only=True).run_once()
        with self.store.connection() as db:
            db.execute('DELETE FROM runtime_log_entries');db.execute('DELETE FROM runtime_log_heads')
        response=self.client.get(self.path).json();self.assertEqual('queued',response['state'])
        Worker(self.store,backend,logs_only=True).run_once()
        self.assertEqual('legacy\n',self.client.get(self.path+'?after=0').json()['log'])

    def test_runtime_rpc_reads_stopped_container_without_starting_it(self):
        current={'Id':'a'*64,'State':{'Running':False}}
        request=json.dumps({'release_id':self.release,'action':'logs'})
        with patch.object(runtime_rpc.sys,'stdin',__import__('io').StringIO(request)),patch.object(runtime_rpc,'container',return_value=current),patch.object(runtime_rpc,'capture',return_value='sleeping log') as read,patch.object(runtime_rpc,'docker') as docker:
            self.assertEqual({'log':'sleeping log'},runtime_rpc.main())
            read.assert_called_once_with(current['Id']);docker.assert_not_called()

    def test_legacy_adapter_uses_owned_container_and_bounded_capture(self):
        backend=Dokploy({'releases':str(self.root/'releases')},self.store.settings)
        with patch.object(backend,'find_container',return_value={'Id':'a'*64}) as find,patch('publisher.backend.capture',return_value='legacy log') as read:
            self.assertEqual('legacy log',backend.runtime_logs({'id':self.release}))
            find.assert_called_once_with(self.release);read.assert_called_once_with('a'*64)

    def test_sandbox_logs_use_timestamped_bounded_capture_without_starting(self):
        account,project='a'*24,'b'*24
        sandbox.save(self.root/'sandboxes'/(self.release+'.json'),{'account_id':account,'project_id':project,'host_port':23000})
        body={'action':'logs','account_id':account,'project_id':project}
        current={'Config':{'Labels':sandbox.labels(self.release,account,project)},'State':{'Running':False}}
        with patch.object(sandbox,'inspect',return_value=current),patch('publisher.container_logs.capture',return_value='timestamp stderr') as read,patch.object(sandbox,'docker') as docker:
            self.assertEqual({'log':'timestamp stderr'},sandbox.operation(self.root,{},self.release,body,Mock()))
            read.assert_called_once_with('poteto-sbx-'+self.release,command=('/usr/bin/docker','--host','unix:///var/run/docker.sock'))
            docker.assert_not_called()
        with patch.object(sandbox,'inspect',return_value=current),patch('publisher.container_logs.capture',side_effect=RuntimeError('failed')):
            with self.assertRaisesRegex(ValueError,'log collection failed'):sandbox.operation(self.root,{},self.release,body,Mock())

    def test_sandbox_refuses_foreign_ownership_before_log_capture(self):
        sandbox.save(self.root/'sandboxes'/(self.release+'.json'),{'account_id':'a'*24,'project_id':'b'*24,'host_port':23000})
        with patch('publisher.container_logs.capture') as read,self.assertRaisesRegex(ValueError,'does not belong'):
            sandbox.operation(self.root,{},self.release,{'action':'logs','account_id':'c'*24,'project_id':'b'*24},Mock())
        read.assert_not_called()

    def test_real_pipe_bounds_large_stdout_and_stderr(self):
        command=self.root/'docker'
        command.write_text('#!'+sys.executable+'\nimport sys\nsys.stdout.write("x"*2000000)\nsys.stdout.flush()\nsys.stderr.write("stderr-marker\\n")\n')
        command.chmod(0o700)
        with patch.dict(os.environ,{'PATH':str(self.root)+os.pathsep+os.environ['PATH']}):
            text=capture('a'*64)
        self.assertEqual(65536,len(text.encode()));self.assertTrue(text.endswith('stderr-marker\n'))


if __name__=='__main__':unittest.main()
