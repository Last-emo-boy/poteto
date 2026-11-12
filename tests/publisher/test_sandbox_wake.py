from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import sandbox, sandbox_wake
from publisher.config import Settings


class SandboxWakeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.release = 'a' * 24
        self.wake = {'hostname': 'preview-' + self.release + '.apps.example', 'health_check': '/healthz', 'port': 3000}
        self.record = {'host_port': 23000, 'wake': self.wake, 'wake_ready': True}

    def test_sleep_requires_separate_project_opt_in(self):
        config = {'runtime': 'image', 'target': 'preview', 'sleep': True, 'memory_mb': 256}
        settings = Settings(self.root, sandbox_projects=(self.release,))
        self.assertFalse(settings.sandbox_enabled(self.release, config))
        settings = replace(settings, sandbox_sleep_projects=(self.release,))
        self.assertTrue(settings.sandbox_enabled(self.release, config))
        self.assertFalse(settings.sandbox_enabled(self.release, config | {'database_id': 'b'*24}))
        self.assertFalse(settings.sandbox_enabled(self.release, config | {'target': 'production'}))

    def test_generator_routes_only_ready_owned_hosts_to_their_private_port(self):
        config = sandbox_wake.configuration([(self.release, self.record)], idle_seconds=10)
        self.assertIn('bind 127.0.0.1', config)
        self.assertIn('session_duration 10s', config)
        self.assertIn('group poteto-sbx-' + self.release, config)
        self.assertIn('reverse_proxy 127.0.0.1:23000', config)
        self.assertNotIn('reverse_proxy', sandbox_wake.configuration([(self.release, self.record | {'wake_ready': False})]))
        with self.assertRaisesRegex(ValueError, 'host'):
            sandbox_wake.configuration([(self.release, self.record | {'wake': self.wake | {'hostname': 'another.example'}})])
        with self.assertRaisesRegex(ValueError, 'health'):
            sandbox_wake.options(self.release, self.wake | {'sleep': True, 'health_check': '/\n respond 200'})

    def test_reload_failure_keeps_the_previous_active_config(self):
        records = self.root/'sandboxes';records.mkdir()
        (records/(self.release+'.json')).write_text(json.dumps(self.record))
        directory = self.root/'sandbox-wake';directory.mkdir()
        live = directory/'Caddyfile';live.write_text('previous configuration')
        docker = Mock(side_effect=['', ValueError('reload failed')])
        with self.assertRaisesRegex(ValueError, 'reload failed'):
            sandbox_wake.configure(self.root, {}, docker)
        self.assertEqual('previous configuration', live.read_text())
        sandbox_wake.configure(self.root, {}, Mock())
        self.assertIn('reverse_proxy', live.read_text())

    def test_production_host_is_restored_only_inside_the_selected_release_route(self):
        record = self.record | {'project_id':'b'*24}
        value = sandbox_wake.configuration([(self.release, record)])
        production = 'app-' + 'b'*24 + '.apps.example'
        self.assertIn('header X-Forwarded-Host ' + production, value)
        self.assertIn('request_header @p' + self.release + ' Host ' + production, value)
        self.assertLess(value.index('handle @r'), value.index('request_header @p'))
        self.assertIn('host ' + self.wake['hostname'], value)

    def test_inspection_reports_owned_ingress_port_even_when_asleep(self):
        records = self.root/'sandboxes'; records.mkdir()
        account, project = 'b'*24, 'c'*24
        ownership = sandbox.labels(self.release, account, project)
        container = {'Config':{'Labels':ownership}, 'State':{'Running':False}}
        body = {'action':'inspect','account_id':account,'project_id':project}
        for wake, port in [(self.wake,23100), (None,23000)]:
            record = self.record | {'account_id':account,'project_id':project,'wake':wake}
            (records/(self.release+'.json')).write_text(json.dumps(record))
            with patch.object(sandbox,'inspect',return_value=container):
                result = sandbox.operation(self.root, {}, self.release, body, Mock())
            self.assertEqual(port, result['http_port'])
            self.assertFalse(result['container']['State']['Running'])
        with patch.object(sandbox,'inspect',return_value=None):
            self.assertNotIn('http_port', sandbox.operation(self.root, {}, self.release, body, Mock()))

    def test_retired_port_is_held_until_old_blocking_requests_expire(self):
        with patch.object(sandbox_wake, 'boot_id', return_value='boot'), patch.object(sandbox_wake.time, 'monotonic', return_value=10):
            sandbox_wake.quarantine(self.root, 23000)
            self.assertEqual({23000}, sandbox_wake.unavailable(self.root))
        with patch.object(sandbox_wake, 'boot_id', return_value='boot'), patch.object(sandbox_wake.time, 'monotonic', return_value=81):
            self.assertEqual(set(), sandbox_wake.unavailable(self.root))

    def test_readiness_command_and_only_readonly_platform_probe_are_injected(self):
        socket = Mock();socket.getresponse.return_value = Mock(status=201)
        with patch.object(sandbox, 'DockerSocket', return_value=socket):
            sandbox.create('poteto-sbx-'+self.release, 'sha256:image', 3000, 23000, 256, .1, {}, {}, wake=self.wake)
        body = json.loads(socket.request.call_args.args[2])
        self.assertEqual(['CMD', '/poteto-healthcheck', 'http://127.0.0.1:3000/healthz'], body['Healthcheck']['Test'])
        self.assertEqual('true', body['Labels']['sablier.enable'])
        self.assertEqual('true', body['Labels']['sablierapp.dev/enable'])
        self.assertEqual([{'Type': 'bind', 'Source': '/opt/paas-execution/healthcheck',
                           'Target': '/poteto-healthcheck', 'ReadOnly': True}], body['HostConfig']['Mounts'])
        self.assertNotIn('Binds', body['HostConfig'])

    def test_image_cannot_inject_other_groups_or_compose_dependencies(self):
        job = self.root/'builds'/self.release;job.mkdir(parents=True)
        account, project = 'b'*24, 'c'*24
        (job/'spec.json').write_text(json.dumps({'runtime': 'image', 'account_id': account, 'project_id': project}))
        (job/'image.tar').write_bytes(b'fixture')
        body = self.wake | {'sleep': True, 'action': 'start', 'account_id': account, 'project_id': project,
                            'memory_mb': 256, 'cpus': .1, 'environment': {}}
        config = {'sandbox_projects': {project: account}, 'sandbox_sleep_projects': {project: account}}
        status = Mock(return_value={'driver': 'prebuilt', 'state': 'succeeded', 'image': 'paas-release:'+self.release})
        for label in ['sablier.group', 'sablierapp.dev/group', 'com.docker.compose.depends_on']:
            metadata = {'Id': 'sha256:fixture', 'Config': {'Labels': {label: 'another-app'}}}
            def docker(*args, **kwargs):
                return json.dumps([metadata]) if args[:2] == ('image', 'inspect') else ''
            with self.subTest(label=label), patch.object(sandbox, 'docker', side_effect=docker), \
                 patch.object(sandbox, 'inspect', return_value=None), patch.object(sandbox, 'has_tag', return_value=False), \
                 patch.object(sandbox, 'image_identities', return_value=['sha256:fixture']), \
                 patch.object(sandbox, 'create') as create, patch.object(sandbox.subprocess, 'run'), \
                 patch.object(sandbox_wake.HEALTHCHECK.__class__, 'is_file', return_value=True):
                with self.assertRaisesRegex(ValueError, 'lifecycle labels'):
                    sandbox.operation(self.root, config, self.release, body, status)
                create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
