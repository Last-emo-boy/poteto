import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from publisher.backend import Dokploy
from publisher.config import Settings
from publisher.models import DeploymentInput
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
                                'ingress_probe': 'private'}, self.store.settings)
        self.worker = Worker(self.store, self.backend)

    def deployment(self, **kwargs):
        config = DeploymentInput(target='preview', **kwargs).configuration()
        return self.store.create_deployment(self.actor, self.project, config, None)

    def archive(self, deployment, files):
        with tarfile.open(self.root / 'artifacts' / (deployment['id'] + '.tar.gz'), 'w:gz') as tar:
            for name, content in files.items():
                item = tarfile.TarInfo(name)
                item.size = len(content)
                tar.addfile(item, io.BytesIO(content))

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
