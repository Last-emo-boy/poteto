import io
import json
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.archive import ArchiveError, unpack
from publisher.config import Settings
from publisher.models import DeploymentInput
from publisher.store import Problem, Store


def archive(files=None, kind=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in (files or {"index.html": b"<h1>hello</h1>"}).items():
            entry = tarfile.TarInfo(name)
            if kind:
                entry.type = kind
                entry.linkname = "/etc/passwd"
                tar.addfile(entry)
            else:
                entry.size = len(content)
                tar.addfile(entry, io.BytesIO(content))
    return data.getvalue()


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root)
        self.app = create_app(self.settings)
        self.store = self.app.state.store
        self.owner = self.store.create_account("alice")
        self.other = self.store.create_account("bob")
        self.client = TestClient(self.app, headers={"Authorization": "Bearer " + self.owner["token"]})
        self.bob = TestClient(self.app, headers={"Authorization": "Bearer " + self.other["token"]})
        self.project = self.client.post("/v1/projects", json={"name": "test"}).json()["id"]

    def tearDown(self):
        self.client.close()
        self.bob.close()
        self.temp.cleanup()

    def release(self, config=None):
        r = self.client.post(f"/v1/projects/{self.project}/deployments", json=config or {})
        self.assertEqual(201, r.status_code, r.text)
        return r.json()

    def upload(self, deployment, data=None):
        return self.client.put(deployment["upload_url"], content=data or archive(), headers={"Content-Type": "application/gzip"})

    def test_auth_precedes_body_parsing_and_json_is_bounded(self):
        anonymous = TestClient(self.app)
        self.assertEqual(401, anonymous.post("/v1/projects", content=b"not json").status_code)
        self.assertEqual(413, self.client.post("/v1/projects", content=b"x" * 65537, headers={"Content-Type": "application/json"}).status_code)
        def chunks():
            yield b"x" * 40000
            yield b"x" * 40000
        self.assertEqual(413, self.client.post("/v1/projects", content=chunks(), headers={"Content-Type": "application/json"}).status_code)

    def test_maintenance_blocks_mutations_before_body_but_preserves_reads(self):
        (self.root/'maintenance.json').write_text('{"operation":"backup"}')
        response=self.client.post('/v1/projects',content=b'not JSON')
        self.assertEqual(503,response.status_code)
        self.assertEqual('30',response.headers['Retry-After'])
        self.assertEqual(200,self.client.get('/v1/me').status_code)
        self.assertEqual(401,TestClient(self.app).post('/v1/projects',content=b'not JSON').status_code)

    def test_private_restore_extraction_never_makes_keys_world_readable(self):
        source=self.root/'backup.tar.gz';source.write_bytes(archive({'keys/admin.key':b'secret'}))
        destination=self.root/'restore';destination.mkdir(mode=0o700)
        unpack(source,destination,max_bytes=1000,max_files=20,private=True)
        self.assertEqual(0o600,(destination/'keys/admin.key').stat().st_mode&0o777)
        self.assertEqual(0o700,(destination/'keys').stat().st_mode&0o777)
        self.assertEqual(0o700,destination.stat().st_mode&0o777)

    def test_extracted_files_are_readable_with_worker_umask(self):
        import os
        path = self.root / "input.tar.gz"
        path.write_bytes(archive({"assets/main.js": b"console.log(1)"}))
        destination = self.root / "unpack"
        destination.mkdir()
        old = os.umask(0o007)
        try:
            unpack(path, destination, max_bytes=1000, max_files=100)
        finally:
            os.umask(old)
        self.assertEqual(0o755, (destination / "assets").stat().st_mode & 0o777)
        self.assertEqual(0o644, (destination / "assets/main.js").stat().st_mode & 0o777)

    def test_authentication_expiration_revocation(self):
        self.assertEqual(401, TestClient(self.app).get("/v1/me").status_code)
        result = self.client.post("/v1/tokens", json={"name": "ci", "scopes": ["projects:read"]})
        self.assertEqual(201, result.status_code)
        value = result.json()
        ci = TestClient(self.app, headers={"Authorization": "Bearer " + value["token"]})
        self.assertEqual(200, ci.get("/v1/projects").status_code)
        self.assertEqual(403, ci.post("/v1/projects", json={"name": "no"}).status_code)
        self.assertEqual(204, self.client.delete("/v1/tokens/" + value["id"]).status_code)
        self.assertEqual(401, ci.get("/v1/projects").status_code)
        with self.store.connection() as db:
            db.execute("UPDATE tokens SET expires=? WHERE account_id=?", (time.time() - 1, self.owner["account_id"]))
        self.assertEqual(401, self.client.get("/v1/me").status_code)

    def test_tenant_boundary_on_every_release_path(self):
        deployment = self.release()
        self.assertEqual(202, self.upload(deployment).status_code)
        for path in [f"/v1/projects/{self.project}", f"/v1/projects/{self.project}/deployments", f"/v1/deployments/{deployment['id']}", f"/v1/deployments/{deployment['id']}/logs", f"/v1/deployments/{deployment['id']}/source"]:
            self.assertEqual(404, self.bob.get(path).status_code, path)
        self.assertEqual([], self.bob.get("/v1/projects").json())
        self.assertEqual(404, self.bob.put(f"/v1/projects/{self.project}/env", json={"values": {"KEY": "value"}}).status_code)
        self.assertEqual(404, self.bob.post(f"/v1/projects/{self.project}/deployments", json={}).status_code)

    def test_missing_master_key_requires_restore_not_silent_rekey(self):
        (self.root / "master.key").unlink()
        with self.assertRaisesRegex(RuntimeError, "restore"):
            Store(self.settings)
        self.assertFalse((self.root / "master.key").exists())

    def test_local_owner_recovery_preserves_account_and_projects(self):
        result = self.store.issue_owner_token("alice")
        recovered = TestClient(self.app, headers={"Authorization": "Bearer " + result["token"]})
        self.assertEqual(self.owner["account_id"], result["account_id"])
        self.assertEqual(self.project, recovered.get("/v1/projects").json()[0]["id"])

    def test_project_token_cannot_escalate(self):
        value = self.client.post("/v1/tokens", json={"name": "limited", "scopes": ["tokens:write", "deployments:write"], "project_ids": [self.project]}).json()
        limited = TestClient(self.app, headers={"Authorization": "Bearer " + value["token"]})
        self.assertEqual(403, limited.post("/v1/tokens", json={"name": "bad", "scopes": ["*"]}).status_code)
        self.assertEqual(403, limited.post("/v1/tokens", json={"name": "bad", "scopes": ["deployments:write"]}).status_code)
        self.assertEqual(404, limited.delete("/v1/tokens/" + self.store.authenticate(self.owner["token"])["id"]).status_code)
        self.assertEqual(201, limited.post("/v1/tokens", json={"name": "child", "scopes": ["deployments:write"], "project_ids": [self.project]}).status_code)

    def test_idempotent_and_durable_queue(self):
        url = f"/v1/projects/{self.project}/deployments"
        a = self.client.post(url, json={}, headers={"Idempotency-Key": "request-1"})
        b = self.client.post(url, json={}, headers={"Idempotency-Key": "request-1"})
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertEqual(409, self.client.post(url, json={"sleep": True}, headers={"Idempotency-Key": "request-1"}).status_code)
        self.assertEqual(202, self.upload(a.json()).status_code)
        restarted = Store(self.settings)
        claimed = restarted.claim("worker-1")
        self.assertEqual(a.json()["id"], claimed["deployment_id"])
        self.assertIsNone(restarted.claim("worker-2"))
        with restarted.connection() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (claimed["id"],))
        recovered = restarted.claim("worker-2")
        self.assertEqual(claimed["id"], recovered["id"])
        self.assertEqual(2, recovered["attempts"])
        with self.assertRaises(RuntimeError):
            restarted.heartbeat(claimed["id"], "worker-1")

    def test_upload_validation_limits_and_retry(self):
        deployment = self.release()
        self.assertEqual(422, self.upload(deployment, archive({"../escape": b"bad"})).status_code)
        self.assertEqual(422, self.upload(deployment, archive({"link": b""}, tarfile.SYMTYPE)).status_code)
        self.assertEqual(422, self.upload(deployment, b"not a tarball").status_code)
        self.assertEqual(422, self.upload(deployment, archive({".env": b"SECRET=x"})).status_code)
        self.assertEqual(202, self.upload(deployment).status_code)
        self.assertEqual(409, self.upload(deployment).status_code)
        self.assertEqual([], list((self.root / "uploads").iterdir()))
        self.assertEqual("queued", self.client.get("/v1/deployments/" + deployment["id"]).json()["state"])
        self.assertIsNone(self.client.get("/v1/deployments/" + deployment["id"]).json()["url"])

    def test_archive_expansion_and_count_limits(self):
        path = self.root / "bomb.tar.gz"
        path.write_bytes(archive({"index.html": b"x" * 100}))
        with self.assertRaises(ArchiveError):
            unpack(path, None, max_bytes=50, max_files=100)
        path.write_bytes(archive({"a": b"1", "b": b"2"}))
        with self.assertRaises(ArchiveError):
            unpack(path, None, max_bytes=100, max_files=1)

    def test_pax_metadata_is_bounded_before_tarfile_allocates_it(self):
        import gzip
        header = tarfile.TarInfo("pax")
        header.type = tarfile.XHDTYPE
        header.size = 8 * 1024 * 1024
        path = self.root / "large-pax.tar.gz"
        path.write_bytes(gzip.compress(header.tobuf() + b"x" * header.size))
        with self.assertRaisesRegex(ArchiveError, "metadata"):
            unpack(path, None, max_bytes=200 * 1024 * 1024, max_files=10000)

    def test_environment_encrypted_and_snapshotted(self):
        secret = "unique-secret-should-never-be-plaintext"
        self.assertEqual(200, self.client.put(f"/v1/projects/{self.project}/env", json={"values": {"API_SECRET": secret}}).status_code)
        deployment = self.release()
        self.client.put(f"/v1/projects/{self.project}/env", json={"values": {"API_SECRET": "new-value"}})
        with self.store.connection() as db:
            row = dict(db.execute("SELECT * FROM deployments WHERE id=?", (deployment["id"],)).fetchone())
        self.assertEqual(secret, self.store.unseal(row["environment"])["API_SECRET"])
        self.assertNotIn(secret, row["environment"])
        self.assertNotIn(secret, self.client.get(f"/v1/projects/{self.project}").text)
        self.assertEqual(["API_SECRET"], self.client.get(f"/v1/projects/{self.project}/env").json()["keys"])
        self.assertEqual(422, self.client.put(f"/v1/projects/{self.project}/env", json={"values": {"DATABASE_URL": "no"}}).status_code)

    def test_delegated_preview_cannot_publish_or_read_production(self):
        path = f"/v1/projects/{self.project}"
        prod = self.release()
        legacy = self.release({"target":"preview"})
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready' WHERE id=?", (prod['id'],))
            db.execute("UPDATE deployments SET preview_access=0 WHERE id=?", (legacy['id'],))
        body = {"name":"external-ai", "scopes":["deployments:preview"], "project_ids":[self.project]}
        for invalid in [body | {"project_ids":[]}, body | {"scopes":["deployments:preview","deployments:write"]}]:
            self.assertEqual(422,self.client.post('/v1/tokens',json=invalid).status_code)
        token = self.client.post('/v1/tokens',json=body).json()['token']
        with TestClient(self.app,headers={"Authorization":"Bearer "+token}) as preview:
            self.assertEqual(403,preview.post(path+'/deployments',json={}).status_code)
            self.assertEqual(403,preview.put(path+'/env?target=preview',json={"values":{}}).status_code)
            for action in ['promote','rollback']:
                self.assertEqual(403,preview.post(path+'/'+action,json={"deployment_id":prod['id']}).status_code)
            for release in (prod,legacy):
                url='/v1/deployments/'+release['id']
                for suffix in ['', '/logs','/runtime-logs']:
                    self.assertEqual(404,preview.get(url+suffix).status_code)
                self.assertEqual(403,preview.get(url+'/source').status_code)
                self.assertEqual(404,preview.put(url+'/source',content=archive(),headers={"Content-Type":"application/gzip"}).status_code)
            other=self.client.post('/v1/projects',json={"name":"other-project"}).json()['id']
            self.assertEqual(404,preview.post(f'/v1/projects/{other}/deployments',json={"target":"preview"}).status_code)
            new=preview.post(path+'/deployments',json={"target":"preview"}).json()
            self.assertEqual(202,preview.put(new['upload_url'],content=archive(),headers={"Content-Type":"application/gzip"}).status_code)
            url='/v1/deployments/'+new['id']
            self.assertEqual('queued',preview.get(url).json()['state'])
            self.assertEqual(200,preview.get(url+'/logs').status_code)
            self.assertEqual([new['id']],[release['id'] for release in preview.get(path+'/deployments').json()])

    def test_preview_environment_does_not_inherit_production_or_promote(self):
        path = f"/v1/projects/{self.project}"
        self.client.put(path+'/env',json={"values":{"PRODUCTION_ONLY":"secret"}})
        without_env = self.release({"target":"preview"})
        self.client.put(path+'/env?target=preview',json={"values":{"APP_STAGE":"preview"}})
        with_env = self.release({"target":"preview"})
        with self.store.connection() as db:
            rows = {r['id']:self.store.unseal(r['environment']) for r in db.execute("SELECT id,environment FROM deployments")}
            db.execute("UPDATE deployments SET state='ready' WHERE id=?", (with_env['id'],))
        self.assertEqual({},rows[without_env['id']])
        self.assertEqual({"APP_STAGE":"preview"},rows[with_env['id']])
        self.assertEqual(["PRODUCTION_ONLY"],self.client.get(path+'/env').json()['keys'])
        self.assertEqual(["APP_STAGE"],self.client.get(path+'/env?target=preview').json()['keys'])
        self.assertNotIn('preview_environment',self.client.get(path).json())
        self.assertEqual(409,self.client.post(path+'/promote',json={"deployment_id":with_env['id']}).status_code)

    def test_quotas_are_transactional(self):
        actor = self.store.authenticate(self.owner["token"])
        config = DeploymentInput().model_dump()
        def create(_):
            try:
                self.store.create_deployment(actor, self.project, config, None)
                return 201
            except Problem as error:
                return error.status
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(create, range(8)))
        self.assertEqual(2, codes.count(201))
        self.assertEqual(6, codes.count(429))

    def test_cpu_reservations_enforce_account_and_platform_caps(self):
        with self.store.connection() as db:
            db.execute("UPDATE accounts SET max_concurrent=20")
        alice = self.store.authenticate(self.owner["token"])
        bob = self.store.authenticate(self.other["token"])
        other_project = self.store.create_project(bob, "other")["id"]
        config = DeploymentInput(memory_mb=64, cpus=1).model_dump()
        for _ in range(4):
            self.store.create_deployment(alice, self.project, config, None)
        with self.assertRaisesRegex(Problem, "Account runtime CPU"):
            self.store.create_deployment(alice, self.project, config, None)
        for _ in range(2):
            self.store.create_deployment(bob, other_project, config, None)
        with self.assertRaisesRegex(Problem, "Platform runtime capacity"):
            self.store.create_deployment(bob, other_project, config, None)

    def test_failed_submitted_release_keeps_its_runtime_reservation(self):
        with self.store.connection() as db:
            db.execute("UPDATE accounts SET max_concurrent=20")
        actor = self.store.authenticate(self.owner["token"])
        config = DeploymentInput(cpus=1).model_dump()
        for _ in range(4):
            release = self.store.create_deployment(actor, self.project, config, None)
            with self.store.connection() as db:
                db.execute("UPDATE deployments SET state='failed',backend_id='still-running' WHERE id=?", (release["id"],))
        with self.assertRaisesRegex(Problem, "Account runtime CPU"):
            self.store.create_deployment(actor, self.project, config, None)

    def test_upload_refuses_to_consume_platform_disk_reserve(self):
        from unittest.mock import patch, Mock
        deployment = self.release()
        with patch("publisher.store.shutil.disk_usage", return_value=Mock(free=1024)):
            self.assertEqual(507, self.upload(deployment).status_code)
        self.assertEqual([], list((self.root / "uploads").iterdir()))

    def test_upload_lease_blocks_duplicate_streams(self):
        deployment = self.release()
        actor = self.store.authenticate(self.owner["token"])
        self.store.begin_upload(actor, deployment["id"], "stream-1")
        self.assertEqual(409, self.upload(deployment).status_code)

    def test_unsubmitted_release_can_be_deleted(self):
        deployment = self.release()
        self.assertEqual(204, self.client.delete("/v1/deployments/" + deployment["id"]).status_code)
        self.assertEqual(404, self.client.get("/v1/deployments/" + deployment["id"]).status_code)

    def test_failed_retry_preserves_release_and_has_only_one_active_job(self):
        deployment = self.release()
        self.assertEqual(202, self.upload(deployment).status_code)
        path = "/v1/deployments/" + deployment["id"] + "/retry"
        self.assertEqual(409, self.client.post(path).status_code)
        self.assertEqual(404, self.bob.post(path).status_code)
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='failed',backend_id='existing-compose',error='old' WHERE id=?", (deployment["id"],))
            db.execute("UPDATE jobs SET state='failed' WHERE deployment_id=?", (deployment["id"],))
        self.assertEqual(202, self.client.post(path).status_code)
        self.assertEqual(409, self.client.post(path).status_code)
        with self.store.connection() as db:
            release = db.execute("SELECT * FROM deployments WHERE id=?", (deployment["id"],)).fetchone()
            self.assertEqual("existing-compose", release["backend_id"])
            self.assertIsNone(release["error"])
            self.assertEqual(1, db.execute("SELECT count(*) FROM jobs WHERE deployment_id=? AND state='queued'", (deployment["id"],)).fetchone()[0])

    def test_retry_requires_source_and_available_concurrency(self):
        deployment = self.release()
        self.assertEqual(202, self.upload(deployment).status_code)
        path = "/v1/deployments/" + deployment["id"] + "/retry"
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='failed' WHERE id=?", (deployment["id"],))
            db.execute("UPDATE jobs SET state='failed' WHERE deployment_id=?", (deployment["id"],))
        self.release()
        self.release()
        self.assertEqual(429, self.client.post(path).status_code)
        (self.root / "artifacts" / (deployment["id"] + ".tar.gz")).unlink()
        self.assertEqual(409, self.client.post(path).status_code)

    def test_cancellation_and_deletion_respect_production_and_project_operations(self):
        release = self.release()
        path = '/v1/deployments/' + release['id']
        self.assertEqual(404, self.bob.post(path + '/cancel').status_code)
        self.assertEqual('cancelled', self.client.post(path + '/cancel').json()['state'])
        self.assertEqual(409, self.upload(release).status_code)
        result = self.client.delete(path)
        self.assertEqual(202, result.status_code)
        self.assertEqual(result.json()['job_id'], self.client.delete(path).json()['job_id'])
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET state='completed' WHERE deployment_id=?", (release['id'],))
            db.execute("UPDATE deployments SET state='ready' WHERE id=?", (release['id'],))
            db.execute('UPDATE projects SET current_deployment=? WHERE id=?', (release['id'],self.project))
        self.assertEqual(409, self.client.delete(path).status_code)
        self.assertEqual(409, self.client.post(path + '/cancel').status_code)

    def test_active_candidate_can_cancel_but_activation_is_atomic(self):
        release = self.release()
        self.upload(release)
        path = '/v1/deployments/' + release['id']
        self.assertEqual('cancelling', self.client.post(path + '/cancel').json()['state'])
        self.assertEqual(409, self.client.delete(path).status_code)
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='activating' WHERE id=?", (release['id'],))
        self.assertEqual(409, self.client.post(path + '/cancel').status_code)

    def test_expiry_preserves_active_uploads_and_removes_only_old_temporary_files(self):
        import os
        release = self.release()
        actor = self.store.authenticate(self.owner['token'])
        self.store.begin_upload(actor, release['id'], 'live')
        now = time.time()
        with self.store.connection() as db:
            db.execute('UPDATE deployments SET created=? WHERE id=?', (now-90000,release['id']))
        old = self.root/'uploads/old.part'; old.write_bytes(b'orphan')
        os.utime(old, (now-7200,now-7200))
        new = self.root/'uploads/new.part'; new.write_bytes(b'upload')
        self.store.expire_uploads(now)
        self.assertEqual('uploading',self.client.get('/v1/deployments/'+release['id']).json()['state'])
        self.assertFalse(old.exists());self.assertTrue(new.exists())
        self.store.expire_uploads(now+181)
        self.assertEqual('cancelled',self.client.get('/v1/deployments/'+release['id']).json()['state'])

    def test_promote_rejects_unhealthy_or_cross_project_release(self):
        deployment = self.release()
        self.assertEqual(409, self.client.post(f"/v1/projects/{self.project}/promote", json={"deployment_id": deployment["id"]}).status_code)
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='ready' WHERE id=?", (deployment["id"],))
        response = self.client.post(f"/v1/projects/{self.project}/promote", json={"deployment_id": deployment["id"]})
        self.assertEqual(202, response.status_code)
        self.assertEqual(404, self.bob.get("/v1/jobs/" + response.json()["job_id"]).status_code)
        self.assertEqual(409, self.client.post(f"/v1/projects/{self.project}/rollback", json={"deployment_id": deployment["id"]}).status_code)

    def test_config_cannot_control_compose_or_health_destination(self):
        for config in [{"privileged": True}, {"output_directory": "../../etc"}, {"health_check": "http://metadata/"}, {"health_check": "//metadata/"}, {"memory_mb": 9000}, {"cpus": 9}]:
            self.assertEqual(422, self.client.post(f"/v1/projects/{self.project}/deployments", json=config).status_code)


if __name__ == "__main__":
    unittest.main()
