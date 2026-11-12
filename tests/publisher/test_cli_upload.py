import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from publisher import cli_upload
from publisher.cli import Client


class UploadCliTests(unittest.TestCase):
    def test_real_http_disconnect_resume_uses_same_artifact_and_release(self):
        release = 'a' * 24
        project = 'b' * 24
        route = '/v1/deployments/' + release
        source_route = route + '/source-upload'
        state = {'creates': 0, 'queued': 0, 'received': bytearray(), 'phase': 1, 'dropped': False}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def read(self):
                return self.rfile.read(int(self.headers.get('Content-Length', 0)))

            def respond(self, value, status=200):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def drop(self):
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

            def upload(self):
                return state['identity'] | {'id': release, 'received': len(state['received']),
                        'chunk_limit': 2*1024**2, 'state': 'queued' if state['queued'] else 'uploading'}

            def deployment(self):
                return {'id': release, 'project_id': project, 'config': state['config'],
                        'state': 'queued' if state['queued'] else 'uploading',
                        'upload_url': route+'/source', 'resumable_upload_url': source_route}

            def do_POST(self):
                body = self.read()
                if self.path == '/v1/projects/'+project+'/deployments':
                    state['creates'] += 1
                    state['config'] = json.loads(body)
                    return self.respond(self.deployment(), 201)
                if self.path == source_route:
                    value = json.loads(body)
                    if 'identity' in state:
                        assert value == state['identity']
                    state['identity'] = value
                    return self.respond(self.upload())
                if self.path == source_route+'/complete':
                    assert len(state['received']) == state['identity']['size']
                    assert hashlib.sha256(state['received']).hexdigest() == state['identity']['sha256']
                    state['queued'] += 1
                    if state['phase'] == 2:
                        return self.drop()
                    return self.respond(self.upload())
                self.respond({'error': 'unexpected'}, 404)

            def do_GET(self):
                if self.path == source_route:
                    return self.respond(self.upload())
                if self.path == route:
                    return self.respond(self.deployment())
                self.respond({'error': 'unexpected'}, 404)

            def do_PUT(self):
                data = self.read()
                parsed = urlsplit(self.path)
                if parsed.path != source_route+'/part':
                    return self.respond({'error': 'unexpected'}, 404)
                offset = int(parse_qs(parsed.query)['offset'][0])
                if state['phase'] == 1 and offset > 0:
                    return self.respond({'error': 'transient upstream failure'}, 502)
                assert offset == len(state['received'])
                state['received'].extend(data)
                if not state['dropped']:
                    state['dropped'] = True
                    return self.drop()
                self.respond(self.upload())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'index.html').write_text('original page')
            (root/'random.bin').write_bytes(os.urandom(4*1024**2+1000))
            repo = Path(__file__).resolve().parents[2]
            env = os.environ | {'POTETO_TOKEN': 'test-token', 'POTETO_TOKEN_FILE': '', 'PAAS_TOKEN': '',
                                'PAAS_TOKEN_FILE': '', 'PYTHONPATH': str(repo),
                                'POTETO_URL': 'http://127.0.0.1:'+str(server.server_port)}
            def command(*args):
                return subprocess.run([sys.executable, '-m', 'publisher.cli', 'deploy', '.', '--project', project,
                                       '--no-wait', *args], cwd=root, env=env, text=True, capture_output=True, timeout=30)
            failed = command('--preview')
            self.assertEqual(1, failed.returncode, failed.stderr)
            self.assertIn('--resume '+release, failed.stderr)
            self.assertEqual(2*1024**2, len(state['received']))
            checkpoint = root/'.poteto'/('upload-'+release+'.tar.gz')
            original = checkpoint.read_bytes()
            # A resume publishes the saved bytes, not changed working files or a new build.
            (root/'index.html').write_text('not part of this deployment')
            state['phase'] = 2
            lost_completion = command('--resume', release)
            self.assertEqual(1, lost_completion.returncode, lost_completion.stderr)
            self.assertEqual(1, state['queued'])
            self.assertTrue(checkpoint.is_file())
            state['phase'] = 3
            resumed = command('--resume', release)
            self.assertEqual(0, resumed.returncode, resumed.stderr)
            self.assertEqual('queued', json.loads(resumed.stdout)['state'])
            self.assertEqual(1, state['creates'])
            self.assertEqual(1, state['queued'])
            self.assertEqual(original, bytes(state['received']))
            with tarfile.open(fileobj=io.BytesIO(state['received']), mode='r:gz') as archive:
                self.assertEqual(b'original page', archive.extractfile('index.html').read())
            self.assertFalse(checkpoint.exists())
            self.assertFalse(checkpoint.with_suffix('').with_suffix('.json').exists())

    def test_checkpoint_preserves_existing_files_and_binds_api_and_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);release = {'id': 'a'*24, 'project_id': 'b'*24, 'config': {}}
            client = Client('https://one.example', 'secret')
            cli_upload.checkpoint(root, client, release, b'original')
            with self.assertRaises(FileExistsError):
                cli_upload.checkpoint(root, client, release, b'replacement')
            metadata, artifact = cli_upload.files(root, release['id'])
            self.assertEqual(b'original', artifact.read_bytes())
            self.assertNotIn('secret', metadata.read_text())
            for url, project in [('https://two.example', release['project_id']), (client.url, 'c'*24)]:
                other = Mock(url=url)
                with self.assertRaisesRegex(RuntimeError, 'different API or project'):
                    cli_upload.resume(root, other, release['id'], project)
                other.request.assert_not_called()

    def test_certificate_failure_is_not_retried(self):
        self.assertFalse(cli_upload.retryable(ssl.SSLCertVerificationError('untrusted')))
        self.assertFalse(cli_upload.retryable(cli_upload.RequestError(403, 'forbidden')))
        self.assertTrue(cli_upload.retryable(cli_upload.RequestError(502, 'upstream')))
