import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch
from execution import host, vm
from execution.guest_build import dockerfile
from execution import runtime_reconcile
from execution import runtime_data, runtime_rpc


class ExecutionTests(unittest.TestCase):
    def test_runtime_bootstrap_creates_only_missing_network_and_propagates_failure(self):
        import os
        import subprocess
        script = (Path(__file__).resolve().parents[2] / 'execution/runtime_bootstrap.sh').read_text().split('PAAS_PUBLISHER_ALIASES=')[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'install').write_text('#!/bin/sh\nexit 0\n')
            (root / 'docker').write_text('#!/bin/sh\necho "$*" >> "$CALLS"\nif [ "$2" = inspect ]; then exit "$EXISTS"; fi\nexit "$CREATE"\n')
            for name in ['install', 'docker']:
                (root / name).chmod(0o755)
            for exists, create, expected in [('0', '0', 0), ('1', '0', 0), ('1', '1', 1)]:
                calls = root / 'calls'
                calls.unlink(missing_ok=True)
                result = subprocess.run(['bash', '-c', script], env=os.environ | {'PATH': str(root) + ':' + os.environ['PATH'], 'CALLS': str(calls), 'EXISTS': exists, 'CREATE': create}, capture_output=True)
                self.assertEqual(expected, result.returncode)
                lines = calls.read_text().splitlines()
                self.assertEqual(['network inspect dokploy-network'] + (['network create --driver bridge dokploy-network'] if exists == '1' else []), lines)

    def test_vm_ports_are_host_only_and_tenant_resources_bounded(self):
        args = vm.arguments('runtime2')
        net = args[args.index('-netdev') + 1]
        self.assertIn('127.0.0.1:22102', net)
        self.assertIn('127.0.0.1:22202', net)
        self.assertNotIn('0.0.0.0', net)
        self.assertEqual('6144', args[args.index('-m') + 1])
        self.assertEqual('2', args[args.index('-smp') + 1])
        for invalid in ['../builder', 'runtime4', 'runtime0;touch /tmp/x']:
            with self.assertRaises(ValueError):
                vm.arguments(invalid)

    def test_invalid_identifiers_cannot_be_paths_or_unit_names(self):
        for value in ['../../etc/shadow', 'a'*25, 'x'*24, 'a'*24 + '\n']:
            with self.assertRaises(ValueError):
                host.checked_id(value)

    def test_commands_are_json_encoded_not_dockerfile_instructions(self):
        value = 'echo hello\nFROM evil\nRUN echo injected'
        content = dockerfile({'runtime': 'node', 'build_command': value, 'start_command': value})
        self.assertEqual(2, sum(line.startswith('FROM ') for line in content.splitlines()))
        run = next(line[4:] for line in content.splitlines() if line.startswith('RUN ['))
        self.assertEqual(['sh', '-c', value], json.loads(run))
        command = next(line[4:] for line in content.splitlines() if line.startswith('CMD '))
        self.assertEqual(['sh', '-c', value], json.loads(command))

    def test_prebuilt_static_does_not_require_node_or_execute_source(self):
        content = dockerfile({'runtime': 'static'})
        self.assertNotIn('RUN', content)
        self.assertNotIn('npm', content)
        self.assertIn('COPY . /usr/share/nginx/html/', content)

    def test_active_builder_disk_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'ROOT', Path(temp)), patch.object(host, 'unit_state', return_value='active'), patch.object(host.shutil, 'disk_usage', return_value=Mock(free=200*1024**3)):
            path = Path(temp)/'vm/builder/root.qcow2'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'live-disk')
            with self.assertRaisesRegex(host.ExecutionError, 'active'):
                host.reset_disk('builder')
            self.assertEqual(b'live-disk', path.read_bytes())

    def test_persistent_runtime_disk_is_never_reset(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'ROOT', Path(temp)), patch.object(host.shutil, 'disk_usage', return_value=Mock(free=200*1024**3)):
            path = Path(temp)/'vm/runtime0/root.qcow2'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'user-data')
            with self.assertRaisesRegex(host.ExecutionError, 'persistent'):
                host.reset_disk('runtime0')
            self.assertEqual(b'user-data', path.read_bytes())

    def test_live_job_observation_never_restarts_it(self):
        build = 'a'*24
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'ROOT', Path(temp)), patch.object(host, 'unit_state', return_value='active'), patch.object(host, 'systemctl') as control:
            path = Path(temp)/'builds'/build/'status.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'id':build,'state':'running'}))
            self.assertEqual('running', host.status(build)['state'])
            control.assert_not_called()

    def test_failed_supervisor_is_terminal_without_resubmission(self):
        build = 'a'*24
        with tempfile.TemporaryDirectory() as temp, patch.object(host, 'ROOT', Path(temp)), patch.object(host, 'unit_state', return_value='failed'), patch.object(host, 'systemctl') as control:
            path = Path(temp)/'builds'/build/'status.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'id':build,'state':'running'}))
            self.assertEqual('failed', host.status(build)['state'])
            control.assert_not_called()

    def test_empty_tenant_route_inventory_emits_no_empty_traefik_elements(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def local_path(value):
                return root / str(value).lstrip('/')
            wake = local_path('/etc/dokploy/paas/wake')
            routes = local_path('/etc/dokploy/traefik/dynamic')
            routes.mkdir(parents=True)
            with patch('pathlib.Path', side_effect=local_path), patch('subprocess.check_output', return_value=''), patch('subprocess.run'), patch('sys.argv', ['configure-wakeup.py']):
                runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/configure-wakeup.py'), run_name='__main__')
            config = json.loads((routes / 'paas-wake.yml').read_text())['http']
            self.assertNotIn('routers', config)
            self.assertNotIn('middlewares', config)
            self.assertTrue(config['services'])
            self.assertIn('respond 404', (wake / 'Caddyfile').read_text())
            awake = routes / 'publisher-awake.yml'
            awake.write_text('{"http":{"middlewares":{}}}')
            with patch.object(runtime_reconcile, 'Path', side_effect=local_path), patch('subprocess.check_output', return_value=''), patch('subprocess.run'):
                runtime_reconcile.main()
            self.assertFalse(awake.exists())

    def test_build_cleanup_checks_ownership_and_live_unit_before_deleting(self):
        build, account = 'a'*24, 'b'*24
        with tempfile.TemporaryDirectory() as temp, patch.object(host,'ROOT',Path(temp)), patch.object(host,'unit_state',return_value='active'), patch.object(host,'systemctl') as control:
            job=Path(temp)/'builds'/build;job.mkdir(parents=True)
            (job/'spec.json').write_text(json.dumps({'account_id':account}))
            with self.assertRaisesRegex(host.ExecutionError,'belong'):
                host.build_operation(build,'c'*24,'cancel')
            control.assert_not_called()
            with self.assertRaisesRegex(host.ExecutionError,'supervised'):
                host.build_operation(build,account,'delete')
            self.assertTrue(job.exists())
            with patch.object(host,'unit_state',return_value='inactive'):
                self.assertTrue(host.build_operation(build,account,'delete')['deleted'])
            self.assertFalse(job.exists())

    def test_terminated_driver_shutdown_never_stops_another_build(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(host,'ROOT',Path(temp)), patch.object(host,'systemctl') as control:
            active=Path(temp)/'active-build';active.write_text('b'*24)
            host.finish_build('a'*24)
            control.assert_not_called()
            self.assertTrue(active.exists())
            host.finish_build('b'*24)
            control.assert_called_once_with('stop','paas-builder.service')
            self.assertFalse(active.exists())

    def test_deleted_build_reclaims_only_its_own_stopped_overlay(self):
        build, account = 'a'*24, 'b'*24
        for latest in [build, 'c'*24]:
            with self.subTest(latest=latest), tempfile.TemporaryDirectory() as temp, patch.object(host,'ROOT',Path(temp)), patch.object(host,'unit_state',return_value='inactive'):
                root=Path(temp);job=root/'builds'/build;job.mkdir(parents=True)
                (job/'spec.json').write_text(json.dumps({'account_id':account}))
                disk=root/'vm/builder/root.qcow2';disk.parent.mkdir(parents=True);disk.write_bytes(b'overlay')
                (root/'last-build').write_text(latest)
                host.build_operation(build,account,'delete')
                self.assertEqual(latest != build,disk.exists())

    def test_migration_observation_never_restarts_a_finished_command(self):
        request={'release_id':'a'*24,'project_id':'b'*24,'resource_id':'c'*24,'command':'exit 0','environment':{},'memory_mb':128,'cpus':0.25}
        item={'State':{'Status':'exited','Running':False,'ExitCode':0},'Config':{'Labels':{'paas.publisher.project':request['project_id'],'paas.publisher.database':request['resource_id']}}}
        result=Mock(returncode=0,stdout=json.dumps([item]).encode())
        log=Mock(returncode=0,stdout=b'migrated',stderr=b'')
        with patch.object(runtime_data,'docker',side_effect=[result,log]) as docker:
            self.assertEqual('succeeded',runtime_data.migration(request)['state'])
        self.assertEqual([('container','inspect','paas-migration-'+request['release_id']),('logs','--tail','200','paas-migration-'+request['release_id'])],[c.args for c in docker.call_args_list])

    def test_database_application_role_is_not_an_initialization_superuser(self):
        current={'Id':'container','State':{'Running':True,'Health':{'Status':'healthy'}}}
        with patch.object(runtime_data,'database',return_value=current),patch.object(runtime_data,'docker') as docker:
            self.assertTrue(runtime_data.database_role({'app_password':'a'*48})['ready'])
        sql=docker.call_args.kwargs['data'].decode()
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',sql)
        self.assertIn('ALTER DATABASE app OWNER TO app',sql)

    def test_migration_overrides_inherited_image_routing_and_data_labels(self):
        request={'release_id':'a'*24,'project_id':'b'*24,'resource_id':'c'*24,'command':'exit 0','environment':{},'memory_mb':128,'cpus':0.25}
        created={'State':{'Status':'created'},'Config':{'Labels':{'paas.publisher.project':request['project_id'],'paas.publisher.database':request['resource_id']}}}
        missing=Mock(returncode=1);result=Mock(returncode=0,stdout=json.dumps([created]).encode())
        with patch.object(runtime_data,'docker',side_effect=[missing,Mock(),result,Mock()]) as docker:
            runtime_data.migration(request)
        args=docker.call_args_list[1].args
        for label in ['paas.publisher.release=','paas.publisher.resource=','paas.publisher.kind=migration','sablier.enable=false','traefik.enable=false']:
            self.assertIn(label,args)

    def test_rejected_disk_admission_does_not_consume_a_tenant_slot(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(host,'ROOT',Path(temp)),patch.object(host,'admit_space',side_effect=host.ExecutionError('disk reserved')):
            with self.assertRaisesRegex(host.ExecutionError,'reserved'):
                host.runtime('a'*24,'public-key')
            self.assertFalse((Path(temp)/'placements.json').exists())

    def test_runtime_status_is_observation_only_even_when_the_vm_is_stopped(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(host,'ROOT',Path(temp)),patch.object(host,'unit_state',return_value='inactive'),patch.object(host,'systemctl') as control:
            account='a'*24
            (Path(temp)/'placements.json').write_text(json.dumps({account:{'slot':0,'state':'ready'}}))
            self.assertEqual('inactive',host.runtime_status(account)['unit_state'])
            control.assert_not_called()

    def test_image_admission_preserves_a_tenant_data_disk_reserve(self):
        with patch.object(host,'ssh',return_value=Mock(stdout=str(1024**3).encode())):
            with self.assertRaisesRegex(host.ExecutionError,'running applications and data'):
                host.admit_runtime_write({'ssh_port':22100,'alias':'tenant'},100*1024**2)
        with patch.object(host,'ssh',return_value=Mock(stdout=str(2*1024**3).encode())):
            host.admit_runtime_write({'ssh_port':22100,'alias':'tenant'},100*1024**2)

    def test_image_declared_volumes_are_reclaimed_but_managed_data_is_not(self):
        release='a'*24;anonymous='b'*64
        with tempfile.TemporaryDirectory() as temp:
            def local(value):return Path(temp)/str(value).lstrip('/')
            mounts={'Mounts':[{'Type':'volume','Name':anonymous},{'Type':'volume','Name':'paas-data-'+'c'*24},{'Type':'bind','Name':'/important'}]}
            with patch.object(runtime_rpc,'Path',side_effect=local):
                runtime_rpc.remember_volumes(release,mounts)
                with patch('subprocess.run',return_value=Mock(returncode=0)),patch.object(runtime_rpc,'docker') as docker:
                    runtime_rpc.cleanup_volumes(release)
                    docker.assert_called_once_with('volume','rm',anonymous)


if __name__ == '__main__':
    unittest.main()
