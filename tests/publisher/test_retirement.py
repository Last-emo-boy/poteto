import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from execution import host
from publisher.backend import BackendError
from publisher.config import Settings
from publisher.runtime import RuntimeRegistry
from publisher.store import Problem, Store

A='a'*24
B='b'*24


class AccountRetirementTests(unittest.TestCase):
    def test_requires_empty_project_inventory_and_revokes_stale_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Settings(Path(tmp)))
            account=store.create_account('temporary')
            aid=account['account_id'];actor=store.authenticate(account['token'])
            with self.assertRaisesRegex(Problem,'do not match'):
                store.begin_account_retirement('temporary',B)
            project=store.create_project(actor,'website')
            with self.assertRaisesRegex(Problem,'Delete every owned project'):
                store.begin_account_retirement('temporary',aid)
            self.assertEqual(aid,store.authenticate(account['token'])['account_id'])
            with store.connection() as db:
                db.execute("UPDATE projects SET state='deleted' WHERE id=?",(project['id'],))
                db.execute("INSERT INTO jobs(id,project_id,kind,state,created,updated) VALUES(?,?,?,'running',0,0)",
                           (A,project['id'],'project-delete'))
            with self.assertRaisesRegex(Problem,'active operation'):
                store.begin_account_retirement('temporary',aid)
            with store.connection() as db:
                db.execute("UPDATE jobs SET state='completed' WHERE id=?",(A,))
            self.assertIsNone(store.begin_account_retirement('temporary',aid))
            self.assertIsNone(store.begin_account_retirement('temporary',aid))
            with self.assertRaises(Problem):store.authenticate(account['token'])
            with self.assertRaises(Problem):store.create_project(actor,'stale-token')
            with self.assertRaises(ValueError):store.issue_owner_token('temporary')
            expected={'account_id':aid,'slot':1,'state':'completed'}
            self.assertEqual(expected,store.complete_account_retirement(aid,expected))
            self.assertEqual(expected,store.begin_account_retirement('temporary',aid))


def placement(account,slot):
    return {'account_id':account,'slot':slot,'state':'ready','ssh_port':22100+slot,
            'http_port':22200+slot,'alias':'runtime-'+account}


class ExecutorRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.index=self.root/'placements.json'
        self.index.write_text(json.dumps({A:placement(A,0),B:placement(B,1)}))
        self.disk=self.root/'vm/runtime0/root.qcow2'
        self.disk.parent.mkdir(parents=True);self.disk.write_bytes(b'test-tenant')
        other=self.root/'vm/runtime1/root.qcow2'
        other.parent.mkdir();other.write_bytes(b'keep-tenant')
        root=patch.object(host,'ROOT',self.root);root.start();self.addCleanup(root.stop)

    def test_blocks_unknown_workload_without_touching_disk_or_services(self):
        with patch.object(host,'runtime',return_value=placement(A,0)),patch.object(host,'systemctl') as service,patch.object(
                host,'ssh',side_effect=[Mock(stdout=b'pub-owned-app\n'),Mock(stdout=b'')]):
            with self.assertRaisesRegex(host.ExecutionError,'containers or volumes'):
                host.retire_runtime(A,'prepare')
            self.assertFalse(host.retirement_path(A).exists())
            service.assert_not_called()
            self.assertEqual(b'test-tenant',self.disk.read_bytes())

    def test_native_database_keeps_its_account_from_retiring(self):
        path = self.root/'native-databases'/('c'*24);path.mkdir(parents=True)
        (path/'database.json').write_text(json.dumps({'resource_id':'c'*24,'account_id':A}))
        with patch.object(host,'systemctl') as service, self.assertRaisesRegex(host.ExecutionError,'owns a database'):
            host.retire_runtime(A,'prepare')
        service.assert_not_called();self.assertTrue(self.disk.is_file())

    def test_interrupted_stop_keeps_placement_then_slot_can_be_reused(self):
        with patch.object(host,'runtime',return_value=placement(A,0)),patch.object(host,'confirm_empty_runtime'):
            self.assertEqual('prepared',host.retire_runtime(A,'prepare')['state'])
        with self.assertRaisesRegex(host.ExecutionError,'reserved for retirement'):
            with host.active_account(A):self.fail('retired request entered')
        with patch.object(host,'unit_state',return_value='active'),patch.object(host,'systemctl'):
            with self.assertRaisesRegex(host.ExecutionError,'not stopped'):
                host.retire_runtime(A,'commit')
        self.assertTrue(self.disk.is_file())
        self.assertIn(A,json.loads(self.index.read_text()))
        with patch.object(host,'unit_state',return_value='inactive'),patch.object(host,'systemctl') as control:
            result=host.retire_runtime(A,'commit')
            self.assertEqual({'account_id':A,'slot':0,'state':'completed'},result)
            control.assert_called_once_with('disable','--now','paas-runtime@0.service')
        self.assertFalse(self.disk.exists())
        self.assertEqual(b'keep-tenant',(self.root/'vm/runtime1/root.qcow2').read_bytes())
        self.assertEqual({B:placement(B,1)},json.loads(self.index.read_text()))
        self.index.write_text(json.dumps({B:placement(B,1),'c'*24:placement('c'*24,0)}))
        with patch.object(host,'systemctl') as control:
            self.assertEqual(result,host.retire_runtime(A,'commit'))
            control.assert_not_called()
        with self.assertRaisesRegex(host.ExecutionError,'reserved for retirement'):
            host.runtime(A,'ssh-key')

    def test_no_runtime_retirement_does_not_stop_another_account(self):
        orphan='d'*24
        with patch.object(host,'systemctl') as service:
            self.assertIsNone(host.retire_runtime(orphan,'prepare')['slot'])
            self.assertEqual('completed',host.retire_runtime(orphan,'commit')['state'])
            service.assert_not_called()


class RegistryRetirementTests(unittest.TestCase):
    def test_detaches_only_owned_target_then_commits_and_removes_registry_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'runtime-registry.json'
            path.write_text(json.dumps({'tenants':{A:placement(A,0)|{'server_id':'one'},B:placement(B,1)|{'server_id':'other'}}}))
            (root/'executor.json').write_text(json.dumps({'url':'http://10.201.0.2:21999'}))
            backend=Mock();backend.config={'project_id':'project'}
            registry=RuntimeRegistry(backend,root)
            registry.executor=Mock()
            prepare={'account_id':A,'slot':0,'state':'prepared'}
            server={'name':'publisher-'+A,'serverId':'one','port':22100,'ipAddress':'10.201.0.2',
                    'username':'root','serverType':'deploy','organizationId':'other-org'}
            other={'name':'publisher-'+B,'serverId':'other','port':22101}
            project={'organizationId':'owned-org','environments':[{'compose':[]}]}
            registry.executor.retire_runtime.side_effect=lambda account,commit=False: prepare|{'state':'completed' if commit else 'prepared'}
            servers=[server,other]
            def dokploy(name,body=None,query=None):
                if name=='server.all':return list(servers)
                if name=='project.one':return project
                self.assertEqual(('server.remove',{'serverId':'one'}),(name,body))
                servers.remove(server)
            backend.api.side_effect=dokploy
            with self.assertRaisesRegex(BackendError,'unconfirmed tenant ownership'):registry.retire(A)
            self.assertFalse(any(call.kwargs.get('commit') for call in registry.executor.retire_runtime.call_args_list))
            server['organizationId']='owned-org'
            project['environments'][0]['compose']=[{'serverId':'one'}]
            with self.assertRaisesRegex(BackendError,'still has an attached'):registry.retire(A)
            project['environments'][0]['compose']=[]
            self.assertEqual('completed',registry.retire(A)['state'])
            self.assertEqual([other],servers)
            self.assertEqual({B},set(json.loads(path.read_text())['tenants']))


if __name__=='__main__':unittest.main()
