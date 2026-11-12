import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import build_source, host, native_build
from publisher.models import DeploymentInput
from pydantic import ValidationError


ACCOUNT, PROJECT, JOB, OTHER = (c * 24 for c in 'abcd')
POLICY = {'native_build_projects': {PROJECT: ACCOUNT}}
SPEC = {'project_id': PROJECT, 'account_id': ACCOUNT, 'runtime': 'node'}


class NativeBuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.job = self.root / 'builds' / JOB
        self.job.mkdir(parents=True)
        self.patch_root = patch.object(host, 'ROOT', self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def submit(self, spec):
        handler = object.__new__(host.Handler)
        handler.authorized = Mock(return_value=True)
        handler.path = '/v1/builds/' + JOB
        handler.headers = {'X-Paas-Build-Spec': base64.urlsafe_b64encode(json.dumps(spec).encode()).decode(),
                           'Content-Length': '3', 'Content-Type': 'application/gzip'}
        handler.rfile = io.BytesIO(b'abc')
        handler.connection = Mock()
        handler.response = Mock()
        with patch.object(host, 'admit_space'), patch.object(host, 'systemctl') as service, \
             patch.object(host, 'unit_state', return_value='active'):
            handler.do_POST()
        return handler.response.call_args.args, service

    def test_private_submission_pins_only_operator_authorized_project(self):
        (self.root / 'config.json').write_text(json.dumps(POLICY))
        (code, receipt), service = self.submit(SPEC)
        self.assertEqual((202, 'native'), (code, receipt['driver']))
        service.assert_called_once_with('start', f'paas-build@{JOB}.service')
        # Losing authorization cannot silently change an already submitted job's driver.
        (self.root / 'config.json').write_text('{}')
        (code, receipt), service = self.submit(SPEC)
        self.assertEqual((202, 'native'), (code, receipt['driver']))
        service.assert_not_called()
        with patch.object(native_build, 'build') as native, patch.object(host, 'build_in_vm') as vm:
            with self.assertRaisesRegex(host.ExecutionError, 'no longer authorized'):
                host.run_build(JOB)
            native.assert_not_called()
            vm.assert_not_called()

    def test_legacy_retry_with_project_identity_stays_in_vm(self):
        previous = {k: v for k, v in SPEC.items() if k != 'project_id'}
        self.assertEqual('vm', self.submit(previous)[0][1]['driver'])
        (self.root / 'config.json').write_text(json.dumps(POLICY))
        (code, receipt), service = self.submit(SPEC)
        self.assertEqual((202, 'vm'), (code, receipt['driver']))
        service.assert_not_called()

    def test_no_public_switch_and_no_import_or_cross_account_native_build(self):
        with self.assertRaises(ValidationError):
            DeploymentInput(runtime='node', driver='native')
        for spec in ({'runtime': 'node', 'account_id': ACCOUNT},
                     SPEC | {'account_id': OTHER}, SPEC | {'project_id': OTHER},
                     SPEC | {'runtime': 'image-import'}, SPEC | {'project_id': '../escape'}):
            self.assertFalse(native_build.allowed(POLICY, spec))
        self.assertFalse(native_build.allowed({}, SPEC))

    def test_native_finishes_without_calling_vm_driver(self):
        (self.root / 'config.json').write_text(json.dumps(POLICY))
        (self.job / 'spec.json').write_text(json.dumps(SPEC))
        (self.job / 'status.json').write_text('{"driver":"native"}')
        image = {'image': 'paas-release:' + JOB, 'image_bytes': 99, 'image_disk_bytes': 99}
        with patch.object(host, 'admit_space'), patch.object(native_build, 'validate_builder'), \
             patch.object(native_build, 'build', return_value=image) as native, \
             patch.object(native_build, 'finish') as finish, patch.object(host, 'build_in_vm') as vm:
            host.run_build(JOB)
        native.assert_called_once()
        finish.assert_called_once_with(self.root, self.job, PROJECT)
        vm.assert_not_called()
        self.assertEqual('succeeded', host.status(JOB)['state'])
        self.assertFalse((self.root / 'active-native-build').exists())

    def test_cleanup_only_stops_the_owned_native_builder(self):
        (self.job / 'spec.json').write_text(json.dumps(SPEC))
        (self.root / 'active-native-build').write_text(JOB)
        with patch.object(native_build, 'finish') as finish, patch.object(host, 'systemctl') as vm:
            host.finish_build(OTHER)
            finish.assert_not_called()
            host.finish_build(JOB)
            finish.assert_called_once_with(self.root, self.job, PROJECT)
            vm.assert_not_called()

    def test_local_builder_and_cache_configuration(self):
        valid = 'Driver: docker-container\nEndpoint: unix:///var/run/docker.sock\n'
        with patch.object(native_build.subprocess, 'run', return_value=Mock(stdout=valid)):
            native_build.validate_builder(self.root, PROJECT)
        for info in [valid + 'Endpoint: tcp://elsewhere:2376\n', valid.replace('docker-container', 'docker'),
                     valid.replace('unix:///var/run/docker.sock', 'tcp://elsewhere:2376')]:
            with patch.object(native_build.subprocess, 'run', return_value=Mock(stdout=info)):
                with self.assertRaises(ValueError):
                    native_build.validate_builder(self.root, PROJECT)
        self.assertIn('--mount=type=cache,target=/root/.npm', build_source.dockerfile(SPEC, cached=True))
        self.assertNotIn('--mount', build_source.dockerfile(SPEC))

    def test_archive_bytes_and_live_log_use_different_channels(self):
        path = self.job / 'image.tar.next'
        code = 'import sys; sys.stdout.buffer.write(b"\\0image"); sys.stderr.write("exporting\\n")'
        size = host.collect_build_output([sys.executable, '-c', code], self.job / 'build.log', 5, archive=path)
        self.assertEqual(len(b'\0image'), size)
        self.assertEqual(b'\0image', path.read_bytes())
        self.assertEqual(b'exporting\n', (self.job / 'build.log').read_bytes())
