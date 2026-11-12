import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from publisher import source_uploads as uploads
from publisher.api import create_app
from publisher.config import Settings
from publisher.store import Store
from test_control import archive


class SourceUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name))
        self.app = create_app(self.settings)
        self.store = self.app.state.store
        self.owner = self.store.create_account('owner')
        other = self.store.create_account('other')
        self.client = TestClient(self.app, headers={'Authorization': 'Bearer ' + self.owner['token']})
        self.bob = TestClient(self.app, headers={'Authorization': 'Bearer ' + other['token']})
        self.addCleanup(self.client.close)
        self.addCleanup(self.bob.close)
        self.project = self.client.post('/v1/projects', json={'name': 'upload'}).json()['id']
        self.payload = archive()
        self.chunk = patch.object(uploads, 'CHUNK', 64)
        self.chunk.start()
        self.addCleanup(self.chunk.stop)

    def release(self, client=None, config=None):
        r = (client or self.client).post('/v1/projects/' + self.project + '/deployments', json=config or {'target': 'preview'})
        self.assertEqual(201, r.status_code, r.text)
        return r.json()

    def start(self, release, payload=None, client=None):
        data = self.payload if payload is None else payload
        response = (client or self.client).post(release['resumable_upload_url'], json={
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        self.assertEqual(200, response.status_code, response.text)
        return release['resumable_upload_url']

    def part(self, base, data, offset=0, client=None):
        return (client or self.client).put(base + '/part?offset=' + str(offset), content=data,
                                          headers={'Content-Type': 'application/octet-stream'})

    def all_parts(self, base, data=None, client=None):
        data = self.payload if data is None else data
        for offset in range(0, len(data), uploads.CHUNK):
            response = self.part(base, data[offset:offset + uploads.CHUNK], offset, client)
            self.assertEqual(200, response.status_code, response.text)
        return (client or self.client).post(base + '/complete')

    def test_resume_survives_restart_and_completion_is_idempotent(self):
        release = self.release();base = self.start(release)
        self.assertEqual(64, self.part(base, self.payload[:64]).json()['received'])
        restarted = TestClient(create_app(self.settings), headers=self.client.headers)
        self.addCleanup(restarted.close)
        self.assertEqual(64, restarted.get(base).json()['received'])
        self.assertEqual(200, self.part(base, self.payload[64:], 64, restarted).status_code)
        for _ in range(2):
            result = restarted.post(base + '/complete')
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual('queued', result.json()['state'])
        with self.store.connection() as db:
            self.assertEqual(1, db.execute('SELECT count(*) FROM jobs').fetchone()[0])
            self.assertEqual(len(self.payload) + len(b'<h1>hello</h1>'), self.store.storage_used(db, self.owner['account_id']))
        self.assertEqual(self.payload, (self.settings.root/'artifacts'/(release['id']+'.tar.gz')).read_bytes())
        self.assertFalse(uploads.path(self.store, release['id']).exists())
        self.assertEqual(self.payload, self.client.get(release['upload_url']).content)

    def test_tenant_boundary_precedes_body_and_preview_token_is_sufficient(self):
        token = self.client.post('/v1/tokens', json={'name': 'agent', 'scopes': ['deployments:preview'],
                                                    'project_ids': [self.project]}).json()['token']
        limited = TestClient(self.app, headers={'Authorization': 'Bearer ' + token})
        self.addCleanup(limited.close)
        release = self.release(limited);base = self.start(release, client=limited)
        with TestClient(self.app) as anonymous:
            self.assertEqual(401, anonymous.post(base, content=b'invalid').status_code)
        for method, url, kwargs in [
            ('get', base, {}),
            ('post', base, {'json': {'size': 1, 'sha256': 'a'*64}}),
            ('put', base+'/part?offset=0', {'content': b'x', 'headers': {'Content-Type': 'application/octet-stream'}}),
            ('post', base+'/complete', {}),
        ]:
            response = getattr(self.bob, method)(url, **kwargs)
            self.assertEqual(404, response.status_code, response.text)
        self.assertEqual(200, self.all_parts(base, client=limited).status_code)
        production = self.release(config={'target': 'production'})
        self.assertEqual(404, limited.get(production['resumable_upload_url']).status_code)

    def test_original_put_is_preserved_but_cannot_overwrite_resumable_data(self):
        original = self.release()
        self.assertEqual(202, self.client.put(original['upload_url'], content=self.payload,
                                            headers={'Content-Type': 'application/gzip'}).status_code)
        release = self.release();base = self.start(release)
        self.assertEqual(409, self.client.put(release['upload_url'], content=self.payload,
                                            headers={'Content-Type': 'application/gzip'}).status_code)
        self.assertEqual(200, self.all_parts(base).status_code)

    def test_offsets_lengths_lease_and_content_type_are_bounded(self):
        release = self.release();base = self.start(release)
        self.assertEqual(409, self.part(base, b'x'*64, -1).status_code)
        self.assertEqual(400, self.part(base, b'x'*65).status_code)
        self.assertEqual(400, self.part(base, b'').status_code)
        self.assertEqual(415, self.client.put(base+'/part?offset=0', content=b'x').status_code)
        actor = self.store.authenticate(self.owner['token'])
        lease = uploads.lease(self.store, actor, release['id'])
        self.assertEqual(409, self.part(base, self.payload[:64]).status_code)
        uploads.release_lease(self.store, release['id'], lease)
        self.assertEqual(200, self.part(base, self.payload[:64]).status_code)
        self.assertEqual(409, self.part(base, self.payload[:64]).status_code)
        self.assertEqual(409, self.client.post(base+'/complete').status_code)

    def test_crash_tail_is_rewritten_at_the_committed_offset(self):
        release = self.release();base = self.start(release)
        self.part(base, self.payload[:64])
        with uploads.path(self.store, release['id']).open('ab') as f:
            f.write(b'uncommitted bytes from an interrupted write')
        self.assertEqual(200, self.part(base, self.payload[64:], 64).status_code)
        self.assertEqual(200, self.client.post(base+'/complete').status_code)

    def test_completion_recovers_archive_rename_before_sql_commit(self):
        release = self.release();base = self.start(release)
        for offset in range(0, len(self.payload), uploads.CHUNK):
            self.part(base, self.payload[offset:offset+uploads.CHUNK], offset)
        uploads.path(self.store, release['id']).replace(self.settings.root/'artifacts'/(release['id']+'.tar.gz'))
        self.assertEqual(200, self.client.post(base+'/complete').status_code)

    def test_artifact_identity_archive_validation_and_expansion_limit(self):
        release = self.release();base = self.start(release)
        self.assertEqual(409, self.client.post(base, json={'size':len(self.payload),'sha256':'f'*64}).status_code)
        for offset in range(0, len(self.payload), uploads.CHUNK):
            self.part(base, self.payload[offset:offset+uploads.CHUNK], offset)
        uploads.path(self.store, release['id']).write_bytes(b'x'*len(self.payload))
        self.assertEqual(422, self.client.post(base+'/complete').status_code)
        second = self.release();bad = archive({'../escape': b'unsafe'});other = self.start(second,bad)
        self.assertEqual(422, self.all_parts(other,bad).status_code)
        with self.store.connection() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_reservation_expiry_and_deletion_free_abandoned_parts(self):
        release = self.release();base = self.start(release)
        self.part(base,self.payload[:64])
        with self.store.connection() as db:
            self.assertEqual(len(self.payload),self.store.storage_used(db,self.owner['account_id']))
            db.execute('UPDATE accounts SET max_storage=? WHERE id=?',(len(self.payload)+1,self.owner['account_id']))
        second = self.release()
        self.assertEqual(429, self.client.post(second['resumable_upload_url'],json={'size':2,'sha256':'a'*64}).status_code)
        self.store.expire_uploads(time.time()+86401)
        self.assertFalse(uploads.path(self.store,release['id']).exists())
        self.assertEqual(409,self.part(base,b'x').status_code)
        with self.store.connection() as db:
            self.assertEqual(0,self.store.storage_used(db,self.owner['account_id']))

    def test_stream_part_is_not_limited_as_json(self):
        release = self.release();data = b'x'*80000
        with patch.object(uploads,'CHUNK',80000):
            base = self.start(release,data)
            self.assertEqual(200,self.part(base,data).status_code)
        self.assertEqual(413,self.client.post(base,content=b'x'*65537).status_code)

    def test_disconnected_stream_commits_nothing_and_releases_lease(self):
        release = self.release();base = self.start(release)
        def disconnected():
            yield self.payload[:10]
            raise ClientDisconnect()
        response = self.client.put(base+'/part?offset=0',content=disconnected(),headers={
            'Content-Type':'application/octet-stream','Content-Length':'64'})
        self.assertEqual(408,response.status_code,response.text)
        self.assertEqual(0,self.client.get(base).json()['received'])
        self.assertFalse(uploads.path(self.store,release['id']).exists())
        self.assertEqual(200,self.part(base,self.payload[:64]).status_code)

    def test_size_and_archive_expansion_are_enforced(self):
        release = self.release();base = release['resumable_upload_url']
        self.assertEqual(413,self.client.post(base,json={'size':51*1024**2,'sha256':'a'*64}).status_code)
        self.assertEqual(422,self.client.post(base,json={'size':-1,'sha256':'a'*64}).status_code)
        self.start(release)
        for offset in range(0,len(self.payload),uploads.CHUNK):
            self.part(base,self.payload[offset:offset+uploads.CHUNK],offset)
        with TestClient(create_app(Settings(self.settings.root,unpack_limit=8)),headers=self.client.headers) as small:
            self.assertEqual(422,small.post(base+'/complete').status_code)
