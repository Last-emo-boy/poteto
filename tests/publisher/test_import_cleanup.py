import json
from unittest.mock import Mock, patch
import unittest

from publisher.backend import BackendError
from publisher.worker import Worker
import test_preview_archives as fixtures


class ImportCleanupTests(unittest.TestCase):
    setUp=fixtures.PreviewArchiveTests.setUp
    client_for=fixtures.PreviewArchiveTests.client_for
    upload=fixtures.PreviewArchiveTests.upload

    def failed_import(self):
        path=self.upload(fixtures.bundle());backend=Mock();backend.config={'execution_enabled':True}
        with patch('publisher.project_imports.DataBackend') as factory:
            factory.return_value.create.return_value={'driver':'native-postgres'}
            factory.return_value.client.restore_native_database.side_effect=BackendError('Simulated restore error')
            Worker(self.store,backend).run_once()
        self.assertEqual('failed',self.client.get(path).json()['state'])
        with self.store.connection() as db:
            resource=db.execute('SELECT id FROM data_resources WHERE project_id=?',(self.project,)).fetchone()[0]
        return path,resource,backend

    def test_explicit_cleanup_removes_only_failed_import_resources_and_reopens_project(self):
        path,resource,backend=self.failed_import()
        self.assertEqual(409,self.client.delete(path).status_code)
        response=self.client.delete(path+'?destroy_data=true')
        self.assertEqual(202,response.status_code,response.text);job=response.json()['job_id']
        self.assertEqual(job,self.client.delete(path+'?destroy_data=true').json()['job_id'])
        with patch('publisher.project_imports.DataBackend') as factory:
            Worker(self.store,backend).run_once()
            self.assertEqual(resource,factory.return_value.remove.call_args.args[0]['id'])
        self.assertEqual('completed',self.client.get('/v1/jobs/'+job).json()['state'])
        self.assertEqual('cancelled',self.client.get(path).json()['state'])
        with self.store.connection() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM data_resources WHERE project_id=?',(self.project,)).fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM deployments WHERE project_id=?',(self.project,)).fetchone()[0])
            self.assertEqual('active',db.execute('SELECT state FROM projects WHERE id=?',(self.project,)).fetchone()[0])
        self.assertEqual(201,self.client.post(self.base+'/deployments',json={'target':'preview'}).status_code)

    def test_failed_cleanup_is_retryable_and_keeps_project_writes_blocked(self):
        path,_,backend=self.failed_import();job=self.client.delete(path+'?destroy_data=true').json()['job_id']
        with patch('publisher.project_imports.DataBackend') as factory:
            factory.return_value.remove.side_effect=BackendError('Simulated remove error')
            Worker(self.store,backend).run_once()
        self.assertEqual('failed',self.client.get('/v1/jobs/'+job).json()['state'])
        self.assertEqual(409,self.client.post(self.base+'/deployments',json={'target':'preview'}).status_code)
        retry=self.client.delete(path+'?destroy_data=true')
        self.assertEqual(job,retry.json()['job_id'])
        with patch('publisher.project_imports.DataBackend'):
            Worker(self.store,backend).run_once()
        self.assertEqual('completed',self.client.get('/v1/jobs/'+job).json()['state'])
        self.assertEqual(1,backend.remove.call_count)

    def test_published_import_is_protected_even_when_its_job_failed(self):
        path,_,_=self.failed_import();operation=path.split('/')[-1]
        with self.store.connection() as db:db.execute('UPDATE projects SET current_deployment=? WHERE id=?',(operation,self.project))
        response=self.client.delete(path+'?destroy_data=true')
        self.assertEqual(409,response.status_code,response.text)
        with self.store.connection() as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM data_resources WHERE project_id=?',(self.project,)).fetchone()[0])
            self.assertEqual(0,db.execute("SELECT count(*) FROM jobs WHERE kind='project-import-cleanup'").fetchone()[0])

    def test_wrong_import_ownership_is_rejected_before_any_runtime_deletion(self):
        path,resource,backend=self.failed_import()
        with self.store.connection() as db:
            db.execute('UPDATE data_resources SET definition=? WHERE id=?',
                       (self.store.seal({'import_id':'f'*24}),resource))
        job=self.client.delete(path+'?destroy_data=true').json()['job_id']
        with patch('publisher.project_imports.DataBackend') as factory:
            Worker(self.store,backend).run_once();factory.return_value.remove.assert_not_called()
        backend.remove.assert_not_called()
        self.assertEqual('failed',self.client.get('/v1/jobs/'+job).json()['state'])
        with self.store.connection() as db:self.assertEqual(1,db.execute('SELECT count(*) FROM data_resources').fetchone()[0])
