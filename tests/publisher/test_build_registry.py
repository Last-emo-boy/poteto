from contextlib import nullcontext
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from execution import guest_build, host


class BuildRegistryTests(unittest.TestCase):
    def test_registry_is_an_optional_npm_argument_not_a_dockerfile_instruction(self):
        self.assertEqual(['npm', 'ci'], guest_build.npm_command({}, 'ci'))
        spec = {'runtime': 'node', 'npm_registry': 'https://registry.npmmirror.com'}
        lines = guest_build.dockerfile(spec).splitlines()
        commands = [json.loads(line[4:]) for line in lines if line.startswith('RUN [')]
        self.assertIn(['npm', 'ci', '--registry', spec['npm_registry']], commands)
        self.assertIn(['npm', 'prune', '--omit=dev', '--registry', spec['npm_registry']], commands)
        for registry in ['http://registry.example', 'https://user:secret@example.org', 'https://example.org?token=secret']:
            with self.assertRaises(ValueError):
                guest_build.npm_command({'npm_registry': registry}, 'ci')

    def test_output_is_flushed_before_the_build_exits(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(guest_build, 'WORK', Path(temp)):
            process = Mock(stdout=io.BytesIO(b'build progress\n'))
            def completed():
                self.assertEqual(b'build progress\n', (Path(temp) / 'build.log').read_bytes())
                return 0
            process.wait.side_effect = completed
            with patch.object(guest_build.subprocess, 'Popen', return_value=process):
                self.assertTrue(guest_build.logged(['fixture']))

    def test_timeout_keeps_bounded_guest_output_and_stops_owned_builder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); job = root / 'builds' / ('a' * 24); job.mkdir(parents=True)
            (job / 'spec.json').write_text('{"account_id":"bbbbbbbbbbbbbbbbbbbbbbbb"}')
            failure = subprocess.TimeoutExpired('ready', 180)
            with patch.object(host, 'ROOT', root), patch.object(host, 'lock', return_value=nullcontext()), \
                 patch.object(host, 'reset_disk'), patch.object(host, 'systemctl') as control, \
                 patch.object(host, 'wait_ready', side_effect=failure), \
                 patch.object(host, 'ssh', return_value=Mock(stdout=b'last build output')) as ssh:
                with self.assertRaises(subprocess.TimeoutExpired): host.run_build('a' * 24)
            self.assertEqual(b'last build output', (job / 'build.log').read_bytes())
            self.assertEqual('failed', json.loads((job / 'status.json').read_text())['state'])
            self.assertEqual('sudo -n tail -c 2097152 /work/build.log', ssh.call_args.args[2])
            self.assertEqual(('stop', 'paas-builder.service'), control.call_args.args)
            self.assertFalse((root / 'active-build').exists())


if __name__ == '__main__': unittest.main()
