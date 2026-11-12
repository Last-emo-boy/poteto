import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from execution.database_storage import DatabaseStorage, atomic_json


class DatabaseStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.disk = DatabaseStorage(self.root, 'a'*24, 'b'*24, 'c'*24, limit_mib=64)

    def owned_image(self):
        self.disk.directory.mkdir(parents=True)
        record = self.disk.owner | {'uuid':'11111111-2222-4333-8444-555555555555','phase':'prepared'}
        atomic_json(self.disk.metadata, record)
        with self.disk.image.open('wb') as image: image.truncate(record['bytes'])
        return record

    def test_identifiers_quota_and_existing_ownership_are_required(self):
        for identifier in ['../escape', 'z'*24, '', None]:
            with self.assertRaises(ValueError):DatabaseStorage(self.root,identifier,'b'*24,'c'*24)
        self.owned_image()
        other = DatabaseStorage(self.root,'a'*24,'d'*24,'c'*24,limit_mib=64)
        with patch('execution.database_storage.run') as run, self.assertRaisesRegex(ValueError,'ownership'):
            other.prepare()
        run.assert_not_called()

    def test_missing_prepared_image_is_never_silently_recreated(self):
        self.owned_image(); self.disk.image.unlink()
        with patch('execution.database_storage.run') as run, self.assertRaisesRegex(ValueError,'restore it'):
            self.disk.prepare()
        run.assert_not_called()
        self.assertFalse(self.disk.image.exists())

    def test_image_links_and_wrong_uuid_are_rejected_before_mounting(self):
        record=self.owned_image()
        with patch('execution.database_storage.run',return_value='different'),self.assertRaisesRegex(ValueError,'UUID'):
            self.disk.validate_image(record)
        self.disk.image.unlink(); self.disk.image.symlink_to(self.disk.metadata)
        with self.assertRaisesRegex(ValueError,'type'):
            self.disk.validate_image(record)

    def test_an_unowned_directory_is_not_adopted_or_erased(self):
        self.disk.directory.mkdir(parents=True); marker=self.disk.directory/'existing';marker.write_text('keep')
        with self.assertRaisesRegex(ValueError,'unowned'):self.disk.prepare()
        self.assertEqual('keep',marker.read_text())

    def test_mount_checks_exact_backing_file_uuid_and_restrictive_options(self):
        record=self.owned_image()
        filesystem={'source':'/dev/loop9','fstype':'ext4','uuid':record['uuid'],'options':'rw,nodev,nosuid,noexec'}
        def state(value):return SimpleNamespace(returncode=0,stdout=json.dumps({'filesystems':[value]}))
        with patch('execution.database_storage.subprocess.run',return_value=state(filesystem)), \
             patch('execution.database_storage.run',return_value=str(self.disk.image)):
            self.assertTrue(self.disk.mounted(record))
        for changed in [filesystem|{'uuid':'other'},filesystem|{'options':'rw'},filesystem|{'fstype':'xfs'}]:
            with patch('execution.database_storage.subprocess.run',return_value=state(changed)),self.assertRaises(ValueError):
                self.disk.mounted(record)
        with patch('execution.database_storage.subprocess.run',return_value=state(filesystem)), \
             patch('execution.database_storage.run',return_value='/someone/else.img'),self.assertRaisesRegex(ValueError,'another image'):
            self.disk.mounted(record)

    def test_destroy_refuses_to_hide_unmounted_files(self):
        self.owned_image();self.disk.mountpoint.mkdir();marker=self.disk.mountpoint/'unexpected';marker.write_text('keep')
        with patch.object(self.disk,'validate_image'),patch.object(self.disk,'mounted',return_value=False),self.assertRaises(OSError):
            self.disk.destroy()
        self.assertTrue(self.disk.image.exists());self.assertEqual('keep',marker.read_text())

    def test_recovery_of_formatted_image_does_not_format_it_again(self):
        record=self.owned_image();record['phase']='creating';atomic_json(self.disk.metadata,record)
        with patch('execution.database_storage.run',return_value=record['uuid']) as run, \
             patch.object(self.disk,'mounted',return_value=True):
            self.assertEqual(self.disk.mountpoint,self.disk.prepare())
        self.assertEqual('prepared',self.disk.record()['phase'])
        self.assertEqual(['blkid'],[call.args[0] for call in run.call_args_list])

    def test_prepare_will_not_mount_over_unexpected_host_files(self):
        record=self.owned_image();self.disk.mountpoint.mkdir();(self.disk.mountpoint/'keep').write_text('data')
        with patch('execution.database_storage.run',return_value=record['uuid']) as run, \
             patch.object(self.disk,'mounted',return_value=False),self.assertRaisesRegex(ValueError,'hide files'):
            self.disk.prepare()
        self.assertFalse(any(call.args[0]=='mount' for call in run.call_args_list))

    def test_corrupt_receipt_never_becomes_new_storage(self):
        self.owned_image();self.disk.metadata.write_text('[]')
        with self.assertRaisesRegex(ValueError,'ownership'):self.disk.prepare()
        self.assertTrue(self.disk.image.exists())

    def test_explicit_destroy_resumes_after_image_removal_but_never_while_mounted(self):
        self.owned_image();self.disk.image.unlink();self.disk.mountpoint.mkdir()
        with patch.object(self.disk,'mounted',return_value=True),self.assertRaisesRegex(ValueError,'still mounted'):
            self.disk.destroy()
        self.assertTrue(self.disk.metadata.exists())
        with patch.object(self.disk,'mounted',return_value=False):
            self.disk.destroy()
        self.assertFalse(self.disk.metadata.exists());self.assertFalse(self.disk.mountpoint.exists())

    def test_creation_cleanup_removes_only_its_owned_pending_image(self):
        record=self.owned_image();self.disk.image.unlink();record['phase']='creating'
        atomic_json(self.disk.metadata,record)
        pending=self.disk.directory/'data.pending';pending.write_bytes(b'partial filesystem')
        with patch.object(self.disk,'mounted',return_value=False):self.disk.destroy()
        self.assertFalse(pending.exists());self.assertFalse(self.disk.metadata.exists())

    def test_ready_database_never_claims_an_unexpected_pending_image(self):
        self.owned_image();pending=self.disk.directory/'data.pending';pending.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError,'staging image'):self.disk.destroy()
        self.assertTrue(self.disk.image.exists());self.assertEqual(b'keep',pending.read_bytes())
