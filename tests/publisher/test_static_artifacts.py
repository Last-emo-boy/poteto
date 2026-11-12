from dataclasses import replace
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from publisher.backend import BackendError, Dokploy
from publisher.config import Settings
from publisher.models import DeploymentInput
from publisher.static_artifacts import built_archive
from publisher.store import ARTIFACT_DRIVER, Problem, Store
from publisher.worker import Worker


class StaticArtifactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(Settings(self.root, static_artifacts=True))
        self.actor = self.store.authenticate(self.store.create_account('static')['token'])
        self.project = self.store.create_project(self.actor, 'site')['id']
        self.backend = Dokploy({'routes': str(self.root), 'releases': str(self.root / 'releases'), 'execution_enabled': True,
                                'ingress_probe': 'public'}, self.store.settings)
        self.worker = Worker(self.store, self.backend)

    def deployment(self, **kwargs):
        config = DeploymentInput(target='preview', **kwargs).configuration()
        return self.store.create_deployment(self.actor, self.project, config, None)

    def archive(self, deployment, files):
        self.write_tar(self.root / 'artifacts' / (deployment['id'] + '.tar.gz'), files)

    @staticmethod
    def write_tar(path, files):
        with tarfile.open(path, 'w:gz') as tar:
            for name, content in files.items():
                item = tarfile.TarInfo(name)
                item.size = len(content)
                tar.addfile(item, io.BytesIO(content))

    def source_deployment(self):
        settings = replace(self.store.settings, static_source_builds=True)
        self.store = self.backend.store = Store(settings)
        self.backend.settings = settings
        return self.deployment(build_command='npm run build', output_directory='dist')

    def test_source_build_retains_only_validated_output_and_releases_builder_receipt(self):
        release = self.source_deployment()
        self.assertEqual(ARTIFACT_DRIVER, release['runtime_driver'])
        rid = release['id']
        archive = self.root/'generated.tar.gz'
        self.write_tar(archive, {'index.html': b'<p>generated</p>'})
        data = archive.read_bytes()
        client = Mock()
        client.build_status.return_value = {'state': 'succeeded', 'driver': 'vm',
                                            'output_kind': 'static', 'artifact_bytes': len(data)}
        client.build_logs.return_value = ''
        client.stream_export.side_effect = lambda method, path, out, limit, check, **kw: out.write(data)
        with (patch.object(self.backend, 'execution_client', return_value=client),
              patch('publisher.runtime.RuntimeRegistry') as vm):
            self.backend.publish_artifact(release, self.root/'input.tar.gz', Mock(), log=Mock())
            self.assertEqual(len(data), built_archive(self.store.settings, rid).stat().st_size)
            self.assertEqual(b'<p>generated</p>', (self.root/'static-releases'/rid/'index.html').read_bytes())
            with self.store.connection() as db:
                self.assertEqual(len(data)+16, db.execute("SELECT built_bytes FROM deployments WHERE id=?", (rid,)).fetchone()[0])
                self.assertEqual(0, db.execute("SELECT count(*) FROM execution_targets").fetchone()[0])
            self.assertEqual('/v1/builds/'+rid+'/static', client.stream_export.call_args.args[1])
            self.assertEqual(self.project, client.stream_export.call_args.kwargs['headers']['X-Paas-Project'])
            self.assertEqual(['cancel', 'delete'], [call.args[-1] for call in client.build_operation.call_args_list])
            vm.assert_not_called()
            client.reset_mock()
            self.backend.publish_artifact(release | {'backend_id': ARTIFACT_DRIVER},
                                          self.root/'input.tar.gz', Mock(), log=Mock())
            self.assertEqual([], client.mock_calls)

    def test_restored_build_needs_no_executor_and_quota_failure_never_creates_route(self):
        release = self.source_deployment()
        rid = release['id']
        archive = self.root/'restored.tar.gz'
        self.write_tar(archive, {'index.html': b'<p>generated</p>'})
        route = self.root/('publisher-preview-'+rid+'.yml')
        with self.store.connection() as db:
            db.execute("UPDATE accounts SET max_storage=1 WHERE id=?", (self.actor['account_id'],))
        with patch.object(self.backend, 'execution_client') as executor:
            with self.assertRaisesRegex(BackendError, 'storage quota'):
                self.backend.publish_artifact(release, self.root/'source', Mock(), restored=archive)
            self.assertFalse(route.exists())
            self.assertFalse(built_archive(self.store.settings, rid).exists())
            with self.store.connection() as db:
                db.execute("UPDATE accounts SET max_storage=4096 WHERE id=?", (self.actor['account_id'],))
            self.backend.publish_artifact(release, self.root/'source', Mock(), restored=archive)
            self.assertTrue(route.is_file())
            self.backend.remove(release | {'backend_id': ARTIFACT_DRIVER})
            self.assertFalse(built_archive(self.store.settings, rid).exists())
            executor.assert_not_called()

    def test_pins_driver_and_uses_no_vm_reservation(self):
        static = self.deployment(cpus=1)
        self.assertEqual(ARTIFACT_DRIVER, static['runtime_driver'])
        self.store = Store(Settings(self.root))
        first = self.store.create_deployment(self.actor, self.project, DeploymentInput().configuration(), 'old')
        self.assertIsNone(first['runtime_driver'])
        self.store = Store(Settings(self.root, static_artifacts=True))
        repeated = self.store.create_deployment(self.actor, self.project, DeploymentInput().configuration(), 'old')
        self.assertIsNone(repeated['runtime_driver'])
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready' WHERE id=?", (static['id'],))
            for _ in range(3):
                self.store.check_runtime_capacity(db, self.actor['account_id'], {'cpus': 1, 'memory_mb': 64})
                db.execute("INSERT INTO deployments(id,project_id,state,config,environment,created,updated) "
                           "VALUES(lower(hex(randomblob(12))),?,'ready',?,?,0,0)",
                           (self.project, json.dumps({'cpus': 1, 'memory_mb': 64}), self.store.seal({})))
            with self.assertRaises(Problem):
                self.store.check_runtime_capacity(db, self.actor['account_id'], {'cpus': 1, 'memory_mb': 64})
            self.store.check_runtime_capacity(db, self.actor['account_id'], {'cpus': 1, 'memory_mb': 64},
                                              driver=ARTIFACT_DRIVER)

    def test_worker_publishes_output_and_cleanup_never_contacts_executor(self):
        release = self.deployment(output_directory='dist', sleep=True)
        self.archive(release, {'dist/index.html': b'<p>prebuilt</p>', 'private.txt': b'not in output'})
        response = Mock(status=200)
        response.getheader.return_value = release['id']
        with (patch.object(self.backend, 'execution_client') as executor,
              patch.object(self.backend, 'api') as dokploy,
              patch('publisher.backend.http.client.HTTPConnection') as http,
              patch('publisher.backend.http.client.HTTPSConnection', new=http)):
            http.return_value.getresponse.return_value = response
            self.worker.deploy({}, release)
            with self.store.connection() as db:
                row = db.execute("SELECT state,backend_id FROM deployments WHERE id=?", (release['id'],)).fetchone()
                self.assertEqual(('ready', ARTIFACT_DRIVER), tuple(row))
            root = self.root / 'static-releases' / release['id']
            self.assertEqual(b'<p>prebuilt</p>', (root / 'index.html').read_bytes())
            self.assertFalse((root / 'private.txt').exists())
            route = self.root / ('publisher-preview-' + release['id'] + '.yml')
            self.assertIn('http://poteto-static:8080', route.read_text())
            self.backend.remove(release)
            self.backend.remove(release)
            self.assertFalse(root.exists())
            self.assertFalse(route.exists())
            executor.assert_not_called()
            dokploy.assert_not_called()

    def test_requires_index_and_preserves_build_command_path(self):
        built = self.deployment(build_command='npm run build')
        self.assertIsNone(built['runtime_driver'])
        release = self.deployment(output_directory='dist')
        self.archive(release, {'index.html': b'outside the selected output'})
        with self.assertRaisesRegex(ValueError, 'must contain index'):
            self.worker.deploy({}, release)
        self.assertFalse(any((self.root / 'static-releases').iterdir()))

    def test_fifty_tenants_publish_distinct_artifacts_without_runtime_slots(self):
        response = Mock(status=200)
        with (patch.object(self.backend, 'execution_client') as executor,
              patch.object(self.backend, 'api') as dokploy,
              patch('publisher.backend.http.client.HTTPConnection') as http,
              patch('publisher.backend.http.client.HTTPSConnection', new=http)):
            http.return_value.getresponse.return_value = response
            for index in range(50):
                actor = self.store.authenticate(self.store.create_account(f'tenant-{index}')['token'])
                project = self.store.create_project(actor, 'site')['id']
                spec = DeploymentInput(target='preview').configuration()
                release = self.store.create_deployment(actor, project, spec, None)
                self.archive(release, {'index.html': f'<p>{index}</p>'.encode()})
                response.getheader.return_value = release['id']
                self.worker.deploy({}, release)
            executor.assert_not_called()
            dokploy.assert_not_called()
        self.assertEqual(50, len(list((self.root / 'static-releases').iterdir())))
        with self.store.connection() as db:
            self.assertEqual(50, db.execute("SELECT count(*) FROM deployments WHERE state='ready'").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT count(*) FROM execution_targets").fetchone()[0])

    def test_prebuilt_preview_lane_runs_other_projects_while_source_builder_owns_its_project(self):
        built = self.source_deployment()
        same = self.deployment()
        other_actor = self.store.authenticate(self.store.create_account('another')['token'])
        other_project = self.store.create_project(other_actor, 'site')['id']
        preview = self.store.create_deployment(other_actor, other_project,
            DeploymentInput(target='preview').configuration(), None)
        production = self.store.create_deployment(other_actor, other_project,
            DeploymentInput(target='production').configuration(), None)
        self.archive(preview, {'index.html': b'<p>fast preview</p>'})
        with self.store.connection() as db:
            for index, item in enumerate((built, same, preview, production)):
                db.execute("UPDATE deployments SET state='queued' WHERE id=?", (item['id'],))
                db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,'deploy',?,?,?)",
                           (item['id'], item['project_id'], item['id'], index, index))
        main_job = self.store.claim('main-worker')
        self.assertEqual(built['id'], main_job['deployment_id'])
        response = Mock(status=200)
        response.getheader.return_value = preview['id']
        with (patch.object(self.backend, 'execution_client') as executor,
              patch('publisher.backend.http.client.HTTPConnection') as http,
              patch('publisher.backend.http.client.HTTPSConnection', new=http)):
            http.return_value.getresponse.return_value = response
            lane = Worker(self.store, self.backend, static_previews_only=True)
            self.assertTrue(lane.run_once())
            self.assertFalse(lane.run_once())
            executor.assert_not_called()
        with self.store.connection() as db:
            self.assertEqual({built['id']: 'running', same['id']: 'queued', preview['id']: 'completed',
                              production['id']: 'queued'},
                             dict(db.execute("SELECT deployment_id,state FROM jobs").fetchall()))
