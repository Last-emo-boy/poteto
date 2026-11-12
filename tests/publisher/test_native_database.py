import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import native_database as native
from execution.database_storage import atomic_json


class NativeDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.admit=Mock()
        self.database=native.NativeDatabase(self.root,{'host_ipv4':'8.8.4.4'},'a'*24,'b'*24,'c'*24,admit=self.admit)
        self.record={'resource_id':'a'*24,'account_id':'b'*24,'project_id':'c'*24,'phase':'starting','slot':25000,
                     'image_id':'sha256:'+'d'*64,'address':'172.25.0.2','admin_password':'A'*48,'app_password':'B'*48}

    def test_create_uses_fixed_hardened_database_options_and_no_host_port(self):
        connection=Mock();connection.getresponse.return_value=Mock(status=201)
        with patch.object(native.sandbox,'DockerSocket',return_value=connection):self.database.create_container(self.record)
        body=json.loads(connection.request.call_args.args[2]);host=body['HostConfig']
        self.assertEqual(native.sandbox.RUNTIME,host['Runtime'])
        self.assertEqual('70:70',body['User']);self.assertTrue(body['Tty'])
        self.assertTrue(host['ReadonlyRootfs']);self.assertEqual(['ALL'],host['CapDrop'])
        self.assertEqual(256*1024**2,host['Memory']);self.assertEqual(128,host['PidsLimit'])
        self.assertEqual({'Name':'no'},host['RestartPolicy'])
        self.assertNotIn('PortBindings',host)
        self.assertEqual(str(self.database.storage.mountpoint),host['Mounts'][0]['Source'])
        self.assertEqual('172.25.0.2',body['NetworkingConfig']['EndpointsConfig'][self.database.network]['IPAMConfig']['IPv4Address'])

    def test_sql_and_passwords_never_enter_process_arguments(self):
        process=Mock(returncode=0,stdout=b'ok\n')
        statement="ALTER ROLE app PASSWORD 'sensitive-value';"
        with patch.object(native.subprocess,'run',return_value=process) as run:
            self.assertEqual('ok',self.database.sql(statement))
        self.assertNotIn('sensitive-value',' '.join(run.call_args.args[0]))
        self.assertEqual(statement.encode(),run.call_args.kwargs['input'])

    def test_disabled_or_unpinned_provisioning_is_rejected_before_host_changes(self):
        with patch.object(native.sandbox,'docker') as docker:
            with self.assertRaisesRegex(ValueError,'disabled'):self.database.ensure('A'*48,'B'*48)
            self.database.config['native_databases']=True
            self.database.config['native_database_image']='postgres:latest'
            with self.assertRaisesRegex(ValueError,'Pin'):self.database.ensure('A'*48,'B'*48)
        docker.assert_not_called();self.admit.assert_not_called()

    def test_database_receipt_cannot_be_reused_by_another_account(self):
        self.database.path.parent.mkdir(parents=True);atomic_json(self.database.path,self.record)
        other=native.NativeDatabase(self.root,{},'a'*24,'f'*24,'c'*24,admit=Mock())
        with self.assertRaisesRegex(ValueError,'ownership'):other.read()

    def test_network_must_remain_private_internal_and_owned(self):
        network={'Internal':True,'Labels':self.database.labels,'Options':{'com.docker.network.bridge.name':'ptdb25000'},
                 'IPAM':{'Config':[{'Subnet':'172.25.0.0/16'}]}}
        self.database.path.parent.mkdir(parents=True)
        with patch.object(native.sandbox,'inspect',return_value=network):
            self.assertEqual('172.25.0.2',self.database.ensure_network(dict(self.record)))
        with patch.object(native.sandbox,'inspect',return_value=network|{'Internal':False}),self.assertRaises(ValueError):
            self.database.ensure_network(dict(self.record))
        network['IPAM']['Config']=[{'Subnet':'169.254.0.0/16'}]
        with patch.object(native.sandbox,'inspect',return_value=network),self.assertRaisesRegex(ValueError,'private IPv4'):
            self.database.ensure_network(dict(self.record))

    def test_delete_keeps_data_while_a_retained_app_is_bound(self):
        self.database.path.parent.mkdir(parents=True);atomic_json(self.database.path,self.record)
        (self.root/'sandboxes').mkdir();(self.root/'sandboxes'/('e'*24+'.json')).write_text(json.dumps({'database_id':'a'*24}))
        with patch.object(native.sandbox,'docker') as docker,self.assertRaisesRegex(ValueError,'retained application'):
            self.database.remove()
        docker.assert_not_called();self.assertTrue(self.database.path.exists())

    def test_application_receives_only_its_bound_database_network(self):
        connection=Mock();connection.getresponse.return_value=Mock(status=201)
        database={'id':'a'*24,'network':'poteto-data-'+'a'*24,'address':'172.25.0.2'}
        with patch.object(native.sandbox,'DockerSocket',return_value=connection):
            native.sandbox.create('application','image',3000,23000,256,.1,{}, {},database=database)
        body=json.loads(connection.request.call_args.args[2])
        self.assertEqual({'application',database['network']},set(body['NetworkingConfig']['EndpointsConfig']))
        self.assertEqual(['db-'+'a'*24+':172.25.0.2'],body['HostConfig']['ExtraHosts'])

    def test_foreign_network_attachment_stops_deletion_before_database_shutdown(self):
        self.database.path.parent.mkdir(parents=True);atomic_json(self.database.path,self.record)
        container={'Id':'database-container'}
        network={'Labels':self.database.labels,'Containers':{'foreign-container':{}}}
        with patch.object(self.database,'container',return_value=container), \
             patch.object(native.sandbox,'inspect',return_value=network), \
             patch.object(native.sandbox,'docker') as docker, self.assertRaisesRegex(ValueError,'attached applications'):
            self.database.remove()
        docker.assert_not_called()
        self.assertEqual('starting',self.database.read()['phase'])

    def test_private_endpoint_rejects_unknown_actions_and_foreign_receipts(self):
        self.database.path.parent.mkdir(parents=True);atomic_json(self.database.path,self.record)
        body={'account_id':'f'*24,'project_id':'c'*24,'action':'inspect'}
        with self.assertRaisesRegex(ValueError,'ownership'):
            native.operation(self.root,{},'a'*24,body,admit=self.admit)
        with self.assertRaisesRegex(ValueError,'Unsupported'):
            native.operation(self.root,{},'a'*24,body|{'account_id':'b'*24,'action':'sql'},admit=self.admit)

    def test_recovery_is_per_database_and_preserves_failed_and_deleting_resources(self):
        first=self.record|{'phase':'ready'}
        second=first|{'resource_id':'e'*24}
        deleting=first|{'resource_id':'f'*24,'phase':'deleting'}
        successful=Mock();failed=Mock();failed.ensure.side_effect=ValueError('Image missing')
        untouched=Mock()
        with patch.object(native,'records',return_value=[first,second,deleting]), \
             patch.object(native,'NativeDatabase',side_effect=[successful,failed,untouched]):
            result=native.reconcile(self.root,{},admit=self.admit)
        self.assertEqual(['a'*24],result['ready']);self.assertEqual(['e'*24],result['failed'])
        self.assertEqual(['f'*24],result['deleting']);untouched.ensure.assert_not_called()
        failed.remove.assert_not_called()

    def test_shutdown_stops_postgres_before_unmounting_its_data(self):
        self.database.path.parent.mkdir(parents=True);atomic_json(self.database.path,self.record)
        events=[]
        with patch.object(self.database,'container',return_value={'State':{'Running':True}}), \
             patch.object(native.sandbox,'docker',side_effect=lambda *a,**k:events.append('stop')), \
             patch.object(self.database.storage,'unmount',side_effect=lambda:events.append('unmount')):
            self.database.stop()
        self.assertEqual(['stop','unmount'],events)
        self.assertTrue(self.database.path.exists())

    def test_interrupted_provisioning_can_clean_owned_storage_without_a_runtime(self):
        self.database.storage.directory.mkdir(parents=True)
        self.database.storage.metadata.write_text('{}')
        def destroy():self.database.storage.metadata.unlink()
        with patch.object(self.database.storage,'record'),patch.object(self.database.storage,'destroy',side_effect=destroy) as cleanup, \
             patch.object(native.sandbox,'inspect',return_value=None),patch.object(native.sandbox_network,'configure'):
            self.assertEqual({'deleted':True},self.database.remove())
        cleanup.assert_called_once();self.assertFalse(self.database.storage.directory.exists())
