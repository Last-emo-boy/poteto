import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from execution import native_archive as archive


class NativeArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.operation='a'*24
        self.request={'account_id':'b'*24,'project_id':'c'*24,'resource_id':'d'*24,
                      'operation':self.operation,'size':8}
        self.database=Mock(name='database');self.database.name='poteto-db-'+'d'*24
        self.database.resource='d'*24;self.database.storage.mountpoint=self.root/'data'
        self.database.read.return_value={'import_id':self.operation}
        bound=patch.object(archive,'database',return_value=(self.database,self.operation));bound.start();self.addCleanup(bound.stop)

    def process(self, payload=b'PGDMP123'):
        process=Mock();process.stdout=io.BytesIO(payload);process.poll.return_value=0;process.wait.return_value=0
        process.stdin=Mock(closed=False)
        return process

    def test_dump_is_a_compatible_bounded_zip_without_credentials_or_admin_role(self):
        process=self.process();output=io.BytesIO()
        with patch.object(archive.subprocess,'Popen',return_value=process) as popen:
            archive.dump(self.root,{},self.request,output)
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as result:
            self.assertEqual(['databases/'+'d'*24+'.dump'],result.namelist())
            self.assertEqual(b'PGDMP123',result.read(result.namelist()[0]))
        command=popen.call_args.args[0]
        self.assertIn('--username=app',command);self.assertNotIn('--username=paas_admin',command)
        self.assertIn('PGAPPNAME=poteto-dump-'+self.operation,command)
        self.assertIn('PGOPTIONS=-c statement_timeout=330000 -c lock_timeout=30000',command)

    def test_dump_overflow_does_not_publish_a_valid_completed_archive(self):
        process=self.process();output=io.BytesIO()
        with patch.object(archive.subprocess,'Popen',return_value=process),patch.object(archive,'LIMIT',4), \
             self.assertRaisesRegex(ValueError,'exceeds'):
            archive.dump(self.root,{},self.request,output)

    def test_restore_uses_app_role_and_commits_database_swap_with_receipt(self):
        self.database.sql.side_effect=[None,'',None,None,None,None]
        process=self.process()
        with patch.object(archive.shutil,'disk_usage',return_value=Mock(free=10**12)), \
             patch.object(archive.subprocess,'Popen',return_value=process) as popen:
            self.assertEqual({'restored':True},archive.restore(self.root,{},self.request,io.BytesIO(b'PGDMP123')))
        command=popen.call_args.args[0]
        self.assertIn('--username=app',command);self.assertIn('--single-transaction',command)
        self.assertIn('--no-security-labels',command);self.assertIn('--no-subscriptions',command)
        process.stdin.write.assert_called_once_with(b'PGDMP123')
        statements=[call.args[0] for call in self.database.sql.call_args_list]
        swap=next(sql for sql in statements if 'RENAME TO' in sql)
        self.assertTrue(swap.startswith('BEGIN;'));self.assertTrue(swap.endswith('COMMIT;'))
        self.assertIn(self.operation,swap);self.assertIn('paas_restore_receipts',swap)

    def test_lost_success_response_reconciles_without_overwriting_restored_data(self):
        self.database.sql.side_effect=[None,self.operation,None]
        with patch.object(archive.subprocess,'Popen') as popen:
            archive.restore(self.root,{},self.request,io.BytesIO(b'PGDMP123'))
        popen.assert_not_called()
        self.assertFalse(any('RENAME' in call.args[0] for call in self.database.sql.call_args_list))

    def test_restore_rejects_foreign_import_and_retained_application(self):
        self.database.read.return_value={'import_id':'e'*24}
        with self.assertRaisesRegex(ValueError,'owned by this import'):
            archive.restore(self.root,{},self.request,io.BytesIO(b'PGDMP123'))
        self.database.sql.assert_not_called()
        self.database.read.return_value={'import_id':self.operation};self.database.sql.side_effect=[None,'']
        (self.root/'sandboxes').mkdir();(self.root/'sandboxes'/('f'*24+'.json')).write_text(json.dumps({'database_id':'d'*24}))
        with self.assertRaisesRegex(ValueError,'retained application'):
            archive.restore(self.root,{},self.request,io.BytesIO(b'PGDMP123'))
        self.assertFalse(any('DROP DATABASE' in call.args[0] for call in self.database.sql.call_args_list))

    def test_failed_restore_never_swaps_the_original_database(self):
        self.database.sql.side_effect=[None,'',None,None];process=self.process();process.wait.return_value=1
        with patch.object(archive.shutil,'disk_usage',return_value=Mock(free=10**12)), \
             patch.object(archive.subprocess,'Popen',return_value=process),self.assertRaisesRegex(ValueError,'original database'):
            archive.restore(self.root,{},self.request,io.BytesIO(b'PGDMP123'))
        self.assertFalse(any('RENAME' in call.args[0] for call in self.database.sql.call_args_list))

    def test_cleanup_terminates_only_this_owned_operation(self):
        archive.finish(self.root,{},self.request,'dump')
        sql=self.database.sql.call_args.args[0]
        self.assertIn("application_name='poteto-dump-"+self.operation+"'",sql)
        self.assertIn('pid<>pg_backend_pid()',sql)
