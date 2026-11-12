import json
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock,patch
import zipfile
from execution import platform_backup as backup
from execution import platform_restore as restore
from execution import host


class PlatformBackupTests(unittest.TestCase):
    def test_recipients_cannot_invoke_plugins_or_be_command_arguments(self):
        for invalid in ['ssh-ed25519 AAAA','age1example1'+'a'*51,'--output=/etc/shadow','age1'+'a'*59]:
            with self.assertRaises(ValueError):backup.recipient(invalid)

    def test_failed_snapshot_resumes_only_the_inventory_owned_by_the_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);op='a'*24
            path=root/'backups'/op;path.mkdir(parents=True)
            execution=root/'execution';execution.mkdir();(execution/'builds').mkdir()
            for name in ['template.qcow2','base.img']:(execution/name).write_bytes(b'image')
            disk=root/'disk.qcow2';disk.write_bytes(b'image')
            request={'vms':[{'role':'controller','unit':'ovh-paas-vm.service','disk':str(disk),'seed':'seed'}],
                     'resume_units':['ovh-paas-vm.service']}
            (path/'request.json').write_text(json.dumps(request));(path/'status.json').write_text(json.dumps({'id':op,'state':'prepared'}))
            def command(*args,**kwargs):
                if 'qemu-img' in args:raise subprocess.CalledProcessError(1,args)
                return Mock(stdout=b'success')
            with patch.object(backup,'ROOT',root),patch.object(backup,'EXECUTION',execution),patch.object(backup,'admit_space'),patch.object(backup,'build_units_active',return_value=False),patch.object(backup,'unit_state',return_value='inactive'),patch.object(backup,'run',side_effect=command),patch.object(backup,'inventory',return_value=request['vms']),patch.object(backup,'resume') as recover:
                with self.assertRaises(subprocess.CalledProcessError):backup.snapshot(op)
                recover.assert_called_once_with(op,allow_active=True)

    def test_recovery_does_not_start_a_slot_reused_by_another_tenant(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'backups'/('a'*24);path.mkdir(parents=True)
            unit='paas-runtime@1.service'
            (path/'request.json').write_text(json.dumps({'resume_units':[unit,'paas-execution-api.service'],
                'vms':[{'unit':unit,'role':'runtime1','slot':1,'account_id':'b'*24}]}))
            (root/'placements.json').write_text(json.dumps({'c'*24:{'slot':1}}))
            with patch.object(backup,'ROOT',root),patch.object(backup,'EXECUTION',root),patch.object(backup,'unit_state',return_value='inactive'),patch.object(backup,'run',return_value=Mock(stdout=b'0')) as command:
                backup.resume('a'*24)
                self.assertEqual([('systemctl','start','paas-execution-api.service')],
                                 [call.args for call in command.call_args_list if call.args[1]=='start'])

    def test_recovery_refuses_to_restart_vms_while_backup_main_is_alive(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(backup,'ROOT',Path(temp)),patch.object(backup,'unit_state',return_value='active'),patch.object(backup,'run',return_value=Mock(stdout=b'123')) as command:
            path=Path(temp)/'backups'/('a'*24);path.mkdir(parents=True)
            (path/'request.json').write_text(json.dumps({'resume_units':['ovh-paas-vm.service']}))
            with self.assertRaisesRegex(ValueError,'still alive'):backup.resume('a'*24)
            self.assertFalse(any('start' in call.args for call in command.call_args_list))

    def test_restore_roles_use_disjoint_private_ports_and_disks(self):
        self.assertEqual((22522,22580),restore.ports('controller'))
        self.assertEqual((22600,22700),restore.ports('runtime0'))
        args=restore.qemu('a'*24,'runtime0')
        network=args[args.index('-netdev')+1]
        self.assertIn('127.0.0.1:22600',network)
        self.assertNotIn('22100',network)
        self.assertNotIn('0.0.0.0:',network)
        self.assertIn('/restore/'+'a'*24+'/',args[args.index('-drive')+1])
        self.assertIn('readonly=on',args[args.index('-drive',args.index('-drive')+1)+1])
        with self.assertRaises(ValueError):restore.qemu('a'*24,'runtime4')

    def test_restore_reserves_received_payload_and_overlay_growth_before_writing(self):
        request={'archive_bytes':100,'payload_bytes':200,'vm_count':2}
        with tempfile.TemporaryDirectory() as temp,patch.object(restore,'ROOT',Path(temp)/'restore'),patch.object(restore,'admit_space') as capacity:
            restore.prepare('a'*24,request)
            capacity.assert_called_once_with(incoming=100+200+2*4*1024**3)
            self.assertEqual(0o700,(Path(temp)/'restore'/('a'*24)).stat().st_mode&0o777)

    def test_restore_limits_cover_full_inventory_and_real_backing_device(self):
        with patch.object(restore.Path,'read_text',return_value='MemAvailable: 67108864 kB\n'),patch.object(restore.Path,'is_block_device',return_value=True),patch.object(restore.Path,'write_text') as write,patch.object(restore,'run',return_value=Mock(stdout=b'/dev/md3\n')):
            restore.configure_resources([{}]*5)
            config=write.call_args.args[0]
            self.assertIn('MemoryMax=38G',config)
            self.assertIn('IOReadBandwidthMax=/dev/md3 50M',config)
            self.assertIn('IOWriteBandwidthMax=/dev/md3 25M',config)

    def test_restore_refuses_to_take_host_memory_reserve(self):
        with patch.object(restore.Path,'read_text',return_value='MemAvailable: 10485760 kB\n'),patch.object(restore,'run') as command:
            with self.assertRaisesRegex(ValueError,'memory reserve'):restore.configure_resources([{}]*2)
            command.assert_not_called()

    def test_later_builds_reserve_unwritten_restore_capacity(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(host,'ROOT',Path(temp)),patch.object(host,'RESTORES',Path(temp)/'restores'),patch.object(host.shutil,'disk_usage',return_value=Mock(free=100*1024**3)):
            host.admit_space() # 18 GiB builder + 70 GiB host reserve fit.
            path=host.RESTORES/('a'*24);path.mkdir(parents=True)
            (path/'status.json').write_text(json.dumps({'archive_bytes':4*1024**3,'payload_bytes':8*1024**3,'vm_count':2,'state':'prepared'}))
            with self.assertRaises(host.ExecutionError):host.admit_space()

    def test_restore_hardlinks_do_not_double_count_allocated_capacity(self):
        import os
        with tempfile.TemporaryDirectory() as temp,patch.object(host,'RESTORES',Path(temp)):
            path=Path(temp)/('a'*24);path.mkdir()
            (path/'status.json').write_text(json.dumps({'archive_bytes':1024**2,'payload_bytes':2*1024**2,'vm_count':1,'state':'prepared'}))
            disk=path/'base';disk.write_bytes(b'x'*1024**2)
            before=host.restore_reservations();os.link(disk,path/'alias')
            self.assertEqual(before,host.restore_reservations())
            state=json.loads((path/'status.json').read_text());state['state']='extracted'
            (path/'status.json').write_text(json.dumps(state))
            self.assertEqual(before-1024**2,host.restore_reservations())


class NativePlatformBackupTests(unittest.TestCase):
    def test_busy_native_archive_or_migration_prevents_snapshot_admission(self):
        with patch.object(backup,'run',return_value=Mock(stdout=b'active')) as run:
            self.assertTrue(backup.build_units_active())
        args=run.call_args.args
        for pattern in ('paas-build@*.service','paas-migration-*.service','paas-native-dump-*.service','paas-native-restore-*.service'):
            self.assertIn(pattern,args)

    def test_native_data_and_applications_recover_before_wake_proxy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);operation='a'*24;path=root/'backups'/operation;path.mkdir(parents=True)
            request={'resume_units':['paas-execution-api.service','paas-sandbox-wake.service'],
                     'vms':[],'native_databases':[{'resource_id':'b'*24}],
                     'native_applications':[{'release_id':'c'*24}]}
            (path/'request.json').write_text(json.dumps(request));events=[]
            def command(*args,**kwargs):
                if args[:2]==('systemctl','start'):events.append(args[2])
                return Mock(stdout=b'0')
            with patch.object(backup,'ROOT',root),patch.object(backup,'EXECUTION',root), \
                 patch.object(backup,'unit_state',return_value='inactive'),patch.object(backup,'run',side_effect=command), \
                 patch.object(backup.native_snapshot,'restore_running',side_effect=lambda *a:events.append('data') or []), \
                 patch.object(backup.native_snapshot,'set_applications',side_effect=lambda *a,**k:events.append('apps')):
                backup.resume(operation)
            self.assertEqual(['data','paas-execution-api.service','apps','paas-sandbox-wake.service'],events)

    def fixture(self, root, version):
        operation='a'*24;path=root/operation;path.mkdir();source=root/'source';source.mkdir()
        vm=source/'vms/controller';vm.mkdir(parents=True)
        (vm/'disk.qcow2').write_bytes(b'disk');(vm/'seed.iso').write_bytes(b'seed')
        item={'resource_id':'b'*24,'account_id':'c'*24,'project_id':'d'*24,'running':True}
        data=source/'executor/native-databases'/item['resource_id'];data.mkdir(parents=True)
        (data/'database.json').write_text(json.dumps(item|{'phase':'ready','admin_password':'A'*48,'app_password':'B'*48}))
        with zipfile.ZipFile(data/'data.zip','w') as archive:archive.writestr('databases/'+item['resource_id']+'.dump',b'PGDMP123')
        manifest={'format':version,'vms':[{'role':'controller'}],'native_databases':[item],
                  'native_database_format':'poteto-native-postgres/1'}
        (source/'manifest.json').write_text(json.dumps(manifest))
        with tarfile.open(path/'incoming.tar.gz','w:gz') as archive:archive.add(source,arcname='platform')
        (path/'status.json').write_text(json.dumps({'state':'received','vm_count':1,'payload_bytes':1024**2}))
        return operation,path

    def test_extract_validates_portable_native_databases_and_keeps_legacy_format_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);operation,path=self.fixture(root,'ovh-paas-platform/3')
            with patch.object(restore,'ROOT',root):restore.extract(operation)
            state=json.loads((path/'status.json').read_text())
            self.assertEqual('extracted',state['state']);self.assertEqual(1,len(state['native_databases']))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);operation,path=self.fixture(root,'ovh-paas-platform/2')
            with patch.object(restore,'ROOT',root),self.assertRaisesRegex(ValueError,'Unsupported native'):
                restore.extract(operation)
            self.assertTrue((path/'incoming.tar.gz').exists())

    def test_native_restore_requires_disabled_execution_admission(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/('a'*24);path.mkdir()
            (path/'status.json').write_text(json.dumps({'state':'extracted'}))
            with patch.object(restore,'ROOT',root),patch.object(restore,'unit_state',return_value='active'), \
                 patch.object(restore.native_snapshot,'restore') as native,self.assertRaisesRegex(ValueError,'Stop execution'):
                restore.restore_native('a'*24)
            native.assert_not_called()


if __name__=='__main__':unittest.main()
