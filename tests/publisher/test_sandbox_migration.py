import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import sandbox_migration as migration
from execution.database_storage import atomic_json


class SandboxMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.release='a'*24;self.account='b'*24;self.project='c'*24;self.database='d'*24
        self.record={'release_id':self.release,'account_id':self.account,'project_id':self.project,
                     'database_id':self.database,'image':'paas-release:'+self.release,'image_ids':['sha256:'+'e'*64],
                     'database':{'network':'poteto-data-'+self.database,'address':'172.30.0.2'},
                     'memory_mb':256,'cpus':.1,'command':'node migrate.js',
                     'environment':{'DATABASE_URL':'postgresql://app:private@db-'+self.database+':5432/app'},
                     'phase':'prepared'}
        migration.path(self.root,self.release).parent.mkdir()

    def save(self, **fields):
        record=self.record|fields;atomic_json(migration.path(self.root,self.release),record);return record

    def request(self, **fields):
        return {key:self.record[key] for key in ('account_id','project_id','database_id','memory_mb','cpus','environment','command')}|{'action':'migrate'}|fields

    def test_hardened_container_has_only_the_database_network_and_no_host_mounts_or_ports(self):
        socket=Mock();socket.getresponse.return_value=Mock(status=201)
        with patch.object(migration.sandbox,'DockerSocket',return_value=socket):migration.create(self.record)
        body=json.loads(socket.request.call_args.args[2]);host=body['HostConfig']
        self.assertEqual('10001:10001',body['User']);self.assertEqual(['sh','-c'],body['Entrypoint'])
        self.assertEqual([self.record['command']],body['Cmd']);self.assertTrue(body['Tty'])
        self.assertEqual(migration.sandbox.RUNTIME,host['Runtime']);self.assertTrue(host['ReadonlyRootfs'])
        self.assertEqual(['ALL'],host['CapDrop']);self.assertEqual(128,host['PidsLimit'])
        self.assertEqual({'Name':'no'},host['RestartPolicy'])
        self.assertNotIn('PortBindings',host);self.assertNotIn('Mounts',host)
        self.assertEqual({self.record['database']['network']},set(body['NetworkingConfig']['EndpointsConfig']))
        self.assertEqual(['127.0.0.1'],host['Dns'])
        self.assertEqual({'max-size':'1m','max-file':'2'},host['LogConfig']['Config'])

    def test_supervision_has_a_deadline_and_independent_container_cleanup(self):
        with patch.object(migration.subprocess,'run') as run:migration.start_unit(self.release)
        command=run.call_args.args[0]
        self.assertIn('--property=RuntimeMaxSec=315',command)
        self.assertIn('--property=KillMode=control-group',command)
        cleanup=next(value for value in command if value.startswith('--property=ExecStopPost='))
        self.assertIn('execution.sandbox_migration finish '+self.release,cleanup)
        self.assertFalse(any('private' in value for value in command))

    def test_successful_and_failed_commands_are_never_resubmitted(self):
        for phase in ('succeeded','failed'):
            self.save(phase=phase)
            with patch.object(migration,'container',return_value={'State':{'Status':'exited','ExitCode':0}}), \
                 patch.object(migration.sandbox,'docker',return_value='retained output'), \
                 patch.object(migration,'start_unit') as start,patch.object(migration,'create') as create:
                result=migration.operation(self.root,{},self.release,self.request(),Mock())
            self.assertEqual(phase,result['state']);start.assert_not_called();create.assert_not_called()

    def test_record_ownership_and_definition_are_immutable(self):
        self.save()
        for changed in ({'account_id':'f'*24},{'project_id':'f'*24},{'command':'different'},
                        {'database_id':'f'*24},{'environment':{'DATABASE_URL':'other'}}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                migration.operation(self.root,{},self.release,self.request(**changed),Mock())

    def test_only_an_owned_personal_prebuilt_image_can_run(self):
        source=self.root/'builds'/self.release;source.mkdir(parents=True)
        (source/'spec.json').write_text(json.dumps({'account_id':self.account,'project_id':self.project,'runtime':'node'}))
        config={'personal_previews':True,'native_databases':True}
        with patch.object(migration,'create') as create, self.assertRaisesRegex(ValueError,'prebuilt image'):
            migration.operation(self.root,config,self.release,self.request(),Mock(return_value={'driver':'native'}))
        create.assert_not_called()

    def test_timeout_marks_failure_and_stops_the_actual_container(self):
        self.save()
        states=[{'State':{'Running':False}}, {'State':{'Running':True}}]
        with patch.object(migration,'container',side_effect=states), \
             patch.object(migration.sandbox,'observe_limits'), \
             patch.object(migration.sandbox,'docker',side_effect=['',subprocess.TimeoutExpired('wait',300)]), \
             patch.object(migration,'finish') as finish:
            with self.assertRaises(subprocess.TimeoutExpired):migration.run(self.root,self.release)
        self.assertTrue(migration.read(self.root,self.release)['forced_failure'])
        finish.assert_called_once_with(self.root,self.release)
        states=[{'State':{'Running':True}}, {'State':{'Running':False,'Status':'exited','ExitCode':0}}]
        with patch.object(migration,'container',side_effect=states),patch.object(migration.sandbox,'docker') as docker:
            migration.finish(self.root,self.release)
        self.assertEqual('failed',migration.read(self.root,self.release)['phase'])
        self.assertEqual('stop',docker.call_args.args[0])

    def test_application_image_requires_successful_migration(self):
        self.save(phase='running')
        with self.assertRaisesRegex(ValueError,'must succeed'):
            migration.completed_image(self.root,self.release,self.account,self.project)
        self.save(phase='succeeded')
        self.assertEqual(self.record['image_ids'],migration.completed_image(self.root,self.release,self.account,self.project))

    def test_supervisor_does_not_restart_a_started_command(self):
        self.save(phase='running')
        with patch.object(migration.sandbox,'docker') as docker,self.assertRaisesRegex(ValueError,'already started'):
            migration.run(self.root,self.release)
        docker.assert_not_called()

    def test_deletion_stops_unit_before_removing_only_the_owned_container(self):
        self.save(phase='succeeded');events=[]
        with patch.object(migration,'stop_unit',side_effect=lambda *a,**k:events.append('unit')), \
             patch.object(migration,'container',return_value={'State':{'Running':False,'Status':'exited','ExitCode':0}}), \
             patch.object(migration.sandbox,'docker',side_effect=lambda *a,**k:events.append(a[0])):
            migration.remove(self.root,self.release,self.account,self.project)
        self.assertEqual(['unit','rm'],events);self.assertFalse(migration.path(self.root,self.release).exists())

    def test_confirmed_missing_transient_unit_does_not_block_cleanup(self):
        with patch.object(migration.subprocess,'run',return_value=Mock(returncode=1,stdout='not-found\n')) as run:
            migration.stop_unit(self.release)
        self.assertEqual(1,run.call_count)
        with patch.object(migration.subprocess,'run',return_value=Mock(returncode=1,stdout='')),self.assertRaisesRegex(ValueError,'unavailable'):
            migration.stop_unit(self.release)

    def test_start_failure_is_reported_without_requesting_uninitialized_logs(self):
        record=self.save(phase='failed')
        with patch.object(migration,'container',return_value={'State':{'Status':'created'}}), \
             patch.object(migration.sandbox,'docker') as docker:
            result=migration.result(self.root,record)
        self.assertEqual('failed',result['state']);self.assertIn('did not start',result['log'])
        docker.assert_not_called()
