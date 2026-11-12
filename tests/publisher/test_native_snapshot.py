import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from execution import native_snapshot as snapshot
from execution.database_storage import atomic_json


class NativeSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.source=self.root/'archive';self.source.mkdir()
        self.item={'resource_id':'a'*24,'account_id':'b'*24,'project_id':'c'*24,'running':True}
        self.record={key:value for key,value in self.item.items() if key!='running'}|{
            'phase':'ready','admin_password':'A'*48,'app_password':'B'*48,'slot':25000}
        self.operation='d'*24

    def payload(self):
        directory=self.source/self.item['resource_id'];directory.mkdir()
        atomic_json(directory/'database.json',self.record)
        with zipfile.ZipFile(directory/'data.zip','w') as archive:
            archive.writestr('databases/'+self.item['resource_id']+'.dump',b'PGDMP123')

    def test_portable_snapshot_requires_owned_metadata_and_one_postgresql_archive(self):
        self.payload();self.assertEqual([{key:value for key,value in self.item.items() if key!='running'}],
                                        snapshot.validate(self.source,[self.item]))
        with self.assertRaisesRegex(ValueError,'Duplicate'):snapshot.validate(self.source,[self.item,self.item])
        path=self.source/self.item['resource_id']/'database.json'
        atomic_json(path,self.record|{'account_id':'e'*24})
        with self.assertRaisesRegex(ValueError,'ownership'):snapshot.validate(self.source,[self.item])

    def test_raw_or_extra_data_cannot_be_confused_with_a_logical_database_backup(self):
        self.payload();path=self.source/self.item['resource_id']/'data.zip'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('databases/'+self.item['resource_id']+'.dump',b'not a PostgreSQL archive')
        with self.assertRaisesRegex(ValueError,'PostgreSQL'):snapshot.validate(self.source,[self.item])

    def test_capture_preserves_stopped_database_state_and_stores_no_raw_filesystem(self):
        database=Mock();database.read.return_value=self.record
        def dump(root,config,body,output):
            with zipfile.ZipFile(output,'w') as archive:archive.writestr('databases/'+'a'*24+'.dump',b'PGDMP123')
        with patch.object(snapshot,'provider',return_value=database),patch.object(snapshot.native_archive,'dump',side_effect=dump), \
             patch.object(snapshot.native_archive,'finish') as finish:
            snapshot.capture(self.root,{},[self.item|{'running':False}],self.operation,self.root/'out')
        database.stop.assert_called_once();finish.assert_called_once()
        self.assertEqual({'database.json','data.zip'},{p.name for p in (self.root/'out'/('a'*24)).iterdir()})

    def test_failed_capture_still_restores_the_original_stopped_state(self):
        database=Mock();database.read.return_value=self.record
        with patch.object(snapshot,'provider',return_value=database), \
             patch.object(snapshot.native_archive,'dump',side_effect=ValueError('dump failed')), \
             patch.object(snapshot.native_archive,'finish',side_effect=ValueError('cleanup failed')):
            with self.assertRaises(ValueError):snapshot.capture(self.root,{},[self.item|{'running':False}],self.operation,self.root/'out')
        database.stop.assert_called_once()

    def test_restore_refuses_existing_resources_before_allocating_or_writing(self):
        self.payload();database=Mock();database.path.exists.return_value=True
        with patch.object(snapshot,'provider',return_value=database),self.assertRaisesRegex(ValueError,'overwrite'):
            snapshot.restore(self.source,self.root,{},[self.item],self.operation,admit=Mock())
        database.ensure.assert_not_called()

    def test_restore_journal_resumes_without_overwriting_completed_database(self):
        self.payload();database=Mock();database.path=self.root/'database.json'
        database.read.side_effect=[None,self.record|{'import_id':self.operation}]
        admit=Mock()
        with patch.object(snapshot,'provider',return_value=database),patch.object(snapshot.native_archive,'restore') as restore:
            result=snapshot.restore(self.source,self.root,{},[self.item],self.operation,admit=admit)
        self.assertEqual(['a'*24],result);restore.assert_called_once()
        database.ensure.assert_called_once_with('A'*48,'B'*48,import_id=self.operation)
        self.assertEqual(self.operation,json.loads(database.path.read_text())['platform_restore'])
        completed=self.record|{'platform_restore':self.operation}
        database.read.side_effect=None;database.read.return_value=completed;database.ensure.reset_mock()
        with patch.object(snapshot,'provider',return_value=database),patch.object(snapshot.native_archive,'restore') as restore:
            self.assertEqual(['a'*24],snapshot.restore(self.source,self.root,{},[self.item],self.operation,admit=admit))
        restore.assert_not_called();database.ensure.assert_not_called()

    def test_only_originally_running_owned_apps_are_stopped_and_resumed(self):
        item={'release_id':'a'*24,'account_id':'b'*24,'project_id':'c'*24,'running':True}
        current={'Config':{'Labels':snapshot.sandbox.labels('a'*24,'b'*24,'c'*24)},'State':{'Running':True}}
        with patch.object(snapshot.sandbox,'inspect',return_value=current),patch.object(snapshot.sandbox,'docker') as docker:
            snapshot.set_applications([item,item|{'running':False}])
            docker.assert_called_once_with('stop','--time','30','poteto-sbx-'+'a'*24,timeout=45)
        current['State']['Running']=False
        with patch.object(snapshot.sandbox,'inspect',return_value=current),patch.object(snapshot.sandbox,'docker') as docker:
            snapshot.set_applications([item],resume=True)
            docker.assert_called_once_with('start','poteto-sbx-'+'a'*24)
