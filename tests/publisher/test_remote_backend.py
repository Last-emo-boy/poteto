import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from publisher.backend import Dokploy
from publisher.config import Settings
from publisher.executor import ExecutionFailure
from publisher.models import DeploymentInput
from publisher.store import Store


class RemoteBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(Settings(self.root))
        account=self.store.create_account('owner');actor=self.store.authenticate(account['token'])
        self.account=account['account_id'];self.project=self.store.create_project(actor,'app')['id']
        with self.store.connection() as db:
            db.execute('UPDATE projects SET environment=? WHERE id=?',(self.store.seal({'TEST_SECRET':'secret$value'}),self.project))
        self.release=self.store.create_deployment(actor,self.project,DeploymentInput(runtime='node',health_check='/healthz').model_dump(),None)
        self.backend=Dokploy({'releases':str(self.root/'releases'),'routes':str(self.root),'url':'http://localhost:3000','api_key':'test','project_id':'project'},self.store.settings)
        self.backend.store=self.store

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_dokploy_mutation_response_is_valid(self):
        response=Mock();response.read.return_value=b'';response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch('publisher.backend.urllib.request.urlopen',return_value=response):
            self.assertIsNone(self.backend.api('sshKey.create',{'name':'test'}))

    def test_build_is_tenant_bound_and_receives_no_runtime_secrets(self):
        registry=Mock();registry.ensure.return_value={'server_id':'server','http_port':22200}
        client=Mock();missing=ExecutionFailure('not found');missing.status=404
        client.build_status.side_effect=missing
        client.start_build.return_value={'state':'succeeded','image':'paas-release:'+self.release['id']}
        client.build_logs.return_value='completed'
        with patch('publisher.runtime.RuntimeRegistry',return_value=registry),patch.object(self.backend,'execution_client',return_value=client):
            definition=self.backend.prepare_remote(self.release,self.root/'source.tar.gz',lambda _:None)
        args=client.start_build.call_args.args
        self.assertEqual(self.account,args[1]['account_id'])
        self.assertNotIn('secret$value',json.dumps(args[1]))
        self.assertNotIn('environment',args[1])
        self.assertEqual((self.account,self.release['id']),client.load_image.call_args.args)
        app=definition['services']['app']
        self.assertEqual('secret$$value',app['environment']['TEST_SECRET'])
        self.assertEqual('false',app['labels']['traefik.enable'])
        self.assertEqual(self.store.settings.hostname(self.project,self.release['id']),app['labels']['paas.publisher.host'])
        self.assertEqual('never',app['pull_policy'])
        self.assertEqual(['CMD','/paas-healthcheck','http://127.0.0.1:3000/healthz'],app['healthcheck']['test'])
        self.assertEqual('server',self.backend.target(self.release['id'])['server_id'])

    def test_observation_failure_never_resubmits_an_unknown_job(self):
        registry=Mock();registry.ensure.return_value={'server_id':'server','http_port':22200}
        client=Mock();client.build_status.side_effect=TimeoutError('observation lost')
        with patch('publisher.runtime.RuntimeRegistry',return_value=registry),patch.object(self.backend,'execution_client',return_value=client):
            with self.assertRaises(TimeoutError):
                self.backend.prepare_remote(self.release,self.root/'source.tar.gz',lambda _:None)
        client.start_build.assert_not_called()

    def test_migration_is_runtime_only_and_data_mounts_remain_external(self):
        config=json.loads(self.release['config'])|{'database_id':'a'*24,'volume_id':'b'*24,'migration_command':'python migrate.py'}
        self.release['config']=json.dumps(config)
        registry=Mock();registry.ensure.return_value={'server_id':'server','http_port':22200}
        client=Mock();missing=ExecutionFailure('not found');missing.status=404
        client.build_status.side_effect=missing
        client.start_build.return_value={'state':'succeeded','image':'paas-release:'+self.release['id']}
        client.build_logs.return_value='completed';client.data.return_value={'state':'succeeded'}
        before=Mock()
        with patch('publisher.runtime.RuntimeRegistry',return_value=registry),patch.object(self.backend,'execution_client',return_value=client):
            definition=self.backend.prepare_remote(self.release,self.root/'source.tar.gz',lambda _:None,before_migrate=before)
        before.assert_called_once()
        spec=client.start_build.call_args.args[1]
        self.assertNotIn('migration_command',spec)
        self.assertNotIn('database_id',spec)
        self.assertTrue(definition['volumes']['persistent']['external'])


if __name__=='__main__':unittest.main()
