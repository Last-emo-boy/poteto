from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import sandbox
from publisher.backend import Dokploy
from publisher.config import Settings
from publisher.models import DeploymentInput
from publisher.store import SANDBOX_DRIVER, Problem, Store


class SandboxPreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.account, self.project, self.release = ('a' * 24, 'b' * 24, 'c' * 24)
        self.start = {'action': 'start', 'account_id': self.account, 'project_id': self.project,
                      'port': 3000, 'cpus': .35, 'memory_mb': 512, 'environment': {'SAMPLE': 'a\nb'}}

    def test_container_create_sets_runtime_network_limits_and_keeps_environment_out_of_argv(self):
        socket = Mock()
        socket.getresponse.return_value = Mock(status=201)
        with patch.object(sandbox, 'DockerSocket', return_value=socket):
            sandbox.create('container', 'sha256:image', 3000, 23000, 512, .35,
                           self.start['environment'], sandbox.labels(self.release, self.account, self.project))
        method, path, payload, _ = socket.request.call_args.args
        self.assertEqual(('POST', '/containers/create?name=container'), (method, path))
        body = json.loads(payload)
        self.assertEqual(['SAMPLE=a\nb'], body['Env'])
        self.assertEqual('10001:10001', body['User'])
        self.assertTrue(body['Tty'])
        host = body['HostConfig']
        self.assertEqual(sandbox.RUNTIME, host['Runtime'])
        self.assertTrue(host['ReadonlyRootfs'])
        self.assertEqual(['ALL'], host['CapDrop'])
        self.assertEqual('paas-sandbox.slice', host['CgroupParent'])
        self.assertEqual(350000000, host['NanoCpus'])
        self.assertEqual([{'HostIp': '127.0.0.1', 'HostPort': '23000'}], host['PortBindings']['3000/tcp'])
        self.assertNotIn('Binds', host)

    def test_denies_untrusted_builds_and_cross_project_cleanup_before_docker(self):
        build = self.root / 'builds' / self.release
        build.mkdir(parents=True)
        (build / 'spec.json').write_text(json.dumps({'account_id': self.account, 'project_id': self.project}))
        status = Mock(return_value={'state': 'succeeded', 'driver': 'vm', 'image': 'paas-release:' + self.release})
        with patch.object(sandbox, 'docker') as docker:
            with self.assertRaisesRegex(ValueError, 'not authorized'):
                sandbox.operation(self.root, {}, self.release, self.start, status)
            config = {'sandbox_projects': {self.project: self.account}}
            with self.assertRaisesRegex(ValueError, 'trusted native build'):
                sandbox.operation(self.root, config, self.release, self.start, status)
            receipt = self.root / 'sandboxes' / (self.release + '.json')
            sandbox.save(receipt, {'account_id': self.account, 'project_id': self.project, 'host_port': 23000})
            with self.assertRaisesRegex(ValueError, 'does not belong'):
                sandbox.operation(self.root, config, self.release, self.start | {
                    'action': 'delete', 'account_id': 'd' * 24}, status)
            self.assertTrue(receipt.exists())
            docker.assert_not_called()

    def test_image_receipt_accepts_only_the_expected_native_export_tag(self):
        tag = 'paas-release:' + self.release
        config_data = b'{"os":"linux","architecture":"amd64"}'
        config = 'sha256:' + hashlib.sha256(config_data).hexdigest()
        descriptor_data = json.dumps({'config': {'digest': config}}).encode()
        digest = 'sha256:' + hashlib.sha256(descriptor_data).hexdigest()
        entries = {
            'manifest.json': json.dumps([{'RepoTags': [tag], 'Config': 'blobs/sha256/' + config.split(':')[1]}]).encode(),
            'index.json': json.dumps({'manifests': [{'digest': digest}]}).encode(),
            'blobs/sha256/' + config.split(':')[1]: config_data,
            'blobs/sha256/' + digest.split(':')[1]: descriptor_data,
        }
        archive = self.root / 'image.tar'
        with tarfile.open(archive, 'w') as tar:
            for name, payload in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        self.assertEqual([config, digest], sandbox.image_identities(archive, tag))
        with self.assertRaisesRegex(ValueError, 'owned image tag'):
            sandbox.image_identities(archive, 'paas-release:' + 'f'*24)
        entries['blobs/sha256/' + config.split(':')[1]] = b'{"unexpected":"content"}'
        with tarfile.open(archive, 'w') as tar:
            for name, payload in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        with self.assertRaisesRegex(ValueError, 'configuration digest'):
            sandbox.image_identities(archive, tag)

    def test_prebuilt_driver_requires_image_spec_and_matching_owner_before_docker(self):
        build = self.root / 'builds' / self.release
        build.mkdir(parents=True)
        status = Mock(return_value={'state': 'succeeded', 'driver': 'prebuilt', 'image': 'paas-release:' + self.release})
        config = {'sandbox_projects': {self.project: self.account}}
        for runtime, account in [('node', self.account), ('image', 'd'*24)]:
            (build / 'spec.json').write_text(json.dumps({'runtime': runtime, 'account_id': account, 'project_id': self.project}))
            with patch.object(sandbox, 'docker') as docker, self.assertRaisesRegex(ValueError, 'owned'):
                sandbox.operation(self.root, config, self.release, self.start, status)
            docker.assert_not_called()

    def test_prebuilt_preview_needs_both_opt_ins_and_never_starts_a_vm_builder(self):
        store = Store(Settings(self.root))
        actor = store.authenticate(store.create_account('prebuilt')['token'])
        project = store.create_project(actor, 'app')['id']
        settings = replace(store.settings, sandbox_projects=(project,), prebuilt_image_projects=(project,))
        store = Store(settings)
        backend = Dokploy({'routes': str(self.root), 'releases': str(self.root/'releases')}, settings)
        backend.store = store
        spec = DeploymentInput(runtime='image', target='preview', memory_mb=256, cpus=.1).configuration()
        release = store.create_deployment(actor, project, spec, None)
        self.assertEqual(SANDBOX_DRIVER, release['runtime_driver'])
        for change in ({'target': 'production'}, {'database_id': 'a'*24}, {'sleep': True}, {'memory_mb': 128}):
            self.assertFalse(settings.sandbox_enabled(project, spec | change))
        client = Mock()
        from publisher.executor import ExecutionFailure
        absent = ExecutionFailure('absent')
        absent.status = 404
        client.build_status.side_effect = absent
        client.start_build.return_value = {'state': 'succeeded', 'driver': 'prebuilt',
                                          'image': 'paas-release:' + release['id'], 'image_bytes': 20480}
        client.build_logs.return_value = ''
        client.sandbox.return_value = {'http_port': 23000}
        with patch.object(backend, 'execution_client', return_value=client), patch('publisher.runtime.RuntimeRegistry') as vm:
            backend.publish_sandbox(release, self.root/'source.tar.gz', Mock(), Mock())
        vm.assert_not_called()
        client.load_image.assert_not_called()
        self.assertEqual('image', client.start_build.call_args.args[1]['runtime'])
        self.assertNotIn('require_native', client.start_build.call_args.args[1])
        self.assertIsNone(backend.target(release['id']))
        self.assertEqual('start', client.sandbox.call_args.args[-1])
        disabled = Store(replace(settings, prebuilt_image_projects=()))
        with self.assertRaisesRegex(Problem, 'not enabled'):
            disabled.create_deployment(actor, project, spec, None)

    def test_sandbox_pool_reservations_match_the_host_slice(self):
        for memory, cpu, count in [(512, .5, 3), (256, .1, 8)]:
            with self.subTest(memory=memory, cpu=cpu), tempfile.TemporaryDirectory() as state:
                store = Store(Settings(Path(state)))
                actor = store.authenticate(store.create_account('pool')['token'])
                project = store.create_project(actor, 'app')['id']
                store = Store(replace(store.settings, sandbox_projects=(project,), prebuilt_image_projects=(project,)))
                spec = DeploymentInput(runtime='image', target='preview', memory_mb=memory, cpus=cpu).configuration()
                for _ in range(count):
                    release = store.create_deployment(actor, project, spec, None)
                    with store.connection() as db:
                        db.execute("UPDATE deployments SET state='ready' WHERE id=?", (release['id'],))
                with self.assertRaisesRegex(Problem, 'Sandbox preview pool') as error:
                    store.create_deployment(actor, project, spec, None)
                self.assertEqual(429, error.exception.status)

    def test_preview_keeps_native_build_and_vm_runtime_independent(self):
        store = Store(Settings(self.root))
        actor = store.authenticate(store.create_account('pilot')['token'])
        project = store.create_project(actor, 'app')['id']
        settings = replace(store.settings, sandbox_projects=(project,))
        store = Store(settings)
        backend = Dokploy({'routes': str(self.root), 'releases': str(self.root / 'releases')}, settings)
        backend.store = store
        spec = DeploymentInput(runtime='node', target='preview').configuration()
        release = store.create_deployment(actor, project, spec, None)
        self.assertEqual(SANDBOX_DRIVER, release['runtime_driver'])
        self.assertFalse(settings.sandbox_enabled(project, spec | {'target': 'production'}))
        self.assertFalse(settings.sandbox_enabled(project, spec | {'volume_id': 'a' * 24}))
        client = Mock()
        client.build_status.return_value = {'state': 'succeeded', 'driver': 'native', 'image': 'paas-release:' + release['id']}
        client.build_logs.return_value = ''
        client.sandbox.return_value = {'http_port': 23000}
        with (patch.object(backend, 'execution_client', return_value=client),
              patch('publisher.runtime.RuntimeRegistry') as vm):
            backend.publish_sandbox(release, self.root/'source.tar.gz', Mock(), Mock())
            vm.assert_not_called()
            client.load_image.assert_not_called()
        self.assertIsNone(backend.target(release['id']))
        args, fields = client.sandbox.call_args
        self.assertEqual((release['id'], actor['account_id'], project, 'start'), args)
        self.assertEqual('3000', fields['environment']['PORT'])
        route = self.root / ('publisher-preview-' + release['id'] + '.yml')
        self.assertIn('http://10.201.0.2:23000', route.read_text())

        lingering = Mock(status=200)
        lingering.getheader.return_value = release['id']
        withdrawn = Mock(status=404)
        withdrawn.getheader.return_value = None
        with (patch.object(backend, 'execution_client', return_value=client),
              patch('publisher.backend.time.sleep'),
              patch('publisher.backend.http.client.HTTPConnection') as http):
            http.return_value.getresponse.side_effect = [lingering, withdrawn]

            def after_withdrawal(*args):
                self.assertFalse(route.exists())
                self.assertEqual(2, http.return_value.getresponse.call_count)

            client.sandbox.side_effect = after_withdrawal
            backend.remove(release)
        self.assertEqual('delete', client.sandbox.call_args.args[-1])
