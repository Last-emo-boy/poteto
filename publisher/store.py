from contextlib import contextmanager
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from cryptography.fernet import Fernet


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def ident():
    return secrets.token_hex(12)


def encode(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


PREVIEW_SCOPE = "deployments:preview"
PREVIEW_MANAGE_SCOPE = "previews:manage"
PREVIEW_ENV_SCOPE = "previews:env"
PREVIEW_WEB_SCOPE = "previews:web"
PREVIEW_DATA_SCOPE = "previews:data"
PREVIEW_SCOPES = {PREVIEW_SCOPE, PREVIEW_MANAGE_SCOPE, PREVIEW_ENV_SCOPE, PREVIEW_WEB_SCOPE, PREVIEW_DATA_SCOPE}
ARTIFACT_DRIVER = "static-artifact"
SANDBOX_DRIVER = "gvisor"
NATIVE_DATABASE_DRIVER = "native-postgres"
SCOPES = {"projects:read", "projects:write", "deployments:read", "deployments:write", "env:write", "databases:write", "storage:write", "tokens:write", "exports:write", "imports:write"} | PREVIEW_SCOPES
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 max_projects INTEGER NOT NULL DEFAULT 5, max_concurrent INTEGER NOT NULL DEFAULT 2,
 max_storage INTEGER NOT NULL DEFAULT 536870912, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), name TEXT NOT NULL,
 secret_hash TEXT UNIQUE NOT NULL, scopes TEXT NOT NULL, projects TEXT,
 expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS account_retirements (
 account_id TEXT PRIMARY KEY REFERENCES accounts(id), started REAL NOT NULL,
 completed REAL, receipt TEXT);
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), name TEXT NOT NULL,
 current_deployment TEXT, environment TEXT NOT NULL, created REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'active',
 UNIQUE(account_id, name));
CREATE TABLE IF NOT EXISTS cli_identities (
 issuer TEXT NOT NULL, subject TEXT NOT NULL,
 project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
 PRIMARY KEY (issuer, subject));
CREATE TABLE IF NOT EXISTS deployments (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), state TEXT NOT NULL,
 config TEXT NOT NULL, environment TEXT NOT NULL, artifact_bytes INTEGER NOT NULL DEFAULT 0,
 unpacked_bytes INTEGER NOT NULL DEFAULT 0,
 built_bytes INTEGER NOT NULL DEFAULT 0,
 error TEXT, backend_id TEXT, idempotency_key TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 runtime_driver TEXT,
 UNIQUE(project_id, idempotency_key));
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
 deployment_id TEXT REFERENCES deployments(id), state TEXT NOT NULL DEFAULT 'queued',
 owner TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS data_resources (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 kind TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
 definition TEXT NOT NULL, backend TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(project_id,kind,stage));
CREATE TABLE IF NOT EXISTS logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id TEXT NOT NULL REFERENCES deployments(id),
 message TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_logs (
 deployment_id TEXT PRIMARY KEY REFERENCES deployments(id), content TEXT NOT NULL,
 collected REAL NOT NULL);
CREATE TABLE IF NOT EXISTS project_exports (
 id TEXT PRIMARY KEY REFERENCES jobs(id), recipient TEXT NOT NULL, scopes TEXT NOT NULL,
 expires REAL NOT NULL, size INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_imports (
 id TEXT PRIMARY KEY REFERENCES jobs(id), bundle_id TEXT NOT NULL, kinds TEXT NOT NULL,
 size INTEGER NOT NULL, expires REAL NOT NULL, received INTEGER NOT NULL DEFAULT 0,
 upload_lease TEXT, upload_until REAL, resource_map TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, token_id TEXT,
 action TEXT NOT NULL, object_id TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS execution_targets (
 deployment_id TEXT PRIMARY KEY REFERENCES deployments(id), account_id TEXT NOT NULL REFERENCES accounts(id),
 server_id TEXT NOT NULL, http_port INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS uploads (
 deployment_id TEXT PRIMARY KEY REFERENCES deployments(id), upload_id TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_uploads (
 deployment_id TEXT PRIMARY KEY REFERENCES deployments(id) ON DELETE CASCADE,
 size INTEGER NOT NULL, sha256 TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS rate_limits (
 token_id TEXT PRIMARY KEY REFERENCES tokens(id), window INTEGER NOT NULL, requests INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS queue_state ON jobs(state, created);
CREATE INDEX IF NOT EXISTS deployment_project ON deployments(project_id, created);
CREATE INDEX IF NOT EXISTS logs_deployment ON logs(deployment_id, id);
"""


class Store:
    def __init__(self, settings):
        self.settings = settings
        settings.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        (settings.root / "artifacts").mkdir(exist_ok=True, mode=0o750)
        (settings.root / "uploads").mkdir(exist_ok=True, mode=0o750)
        (settings.root / 'exports').mkdir(exist_ok=True,mode=0o750)
        (settings.root / 'imports').mkdir(exist_ok=True,mode=0o750)
        key_path = settings.root / "master.key"
        if settings.db.exists() and not key_path.exists():
            raise RuntimeError("Encryption key is missing; restore the original master.key before starting")
        try:
            with open(key_path, "xb", opener=lambda p, f: os.open(p, f, 0o600)) as out:
                out.write(Fernet.generate_key())
        except FileExistsError:
            pass
        self.cipher = Fernet(key_path.read_bytes())
        with self.connection() as db:
            db.executescript(SCHEMA)
        with self.connection() as db:
            if 'resource_id' not in {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN resource_id TEXT REFERENCES data_resources(id)')
            if 'preview_access' not in {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN preview_access INTEGER NOT NULL DEFAULT 0')
            if 'skip_migration' not in {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN skip_migration INTEGER NOT NULL DEFAULT 0')
            if 'runtime_driver' not in {r['name'] for r in db.execute('PRAGMA table_info(data_resources)')}:
                db.execute('ALTER TABLE data_resources ADD COLUMN runtime_driver TEXT')
            if 'destination_target' not in {r['name'] for r in db.execute('PRAGMA table_info(project_imports)')}:
                db.execute("ALTER TABLE project_imports ADD COLUMN destination_target TEXT NOT NULL DEFAULT 'production'")
            if 'web_access' not in {r['name'] for r in db.execute('PRAGMA table_info(project_imports)')}:
                db.execute('ALTER TABLE project_imports ADD COLUMN web_access INTEGER NOT NULL DEFAULT 0')
            if 'cleanup_job' not in {r['name'] for r in db.execute('PRAGMA table_info(project_imports)')}:
                db.execute('ALTER TABLE project_imports ADD COLUMN cleanup_job TEXT REFERENCES jobs(id)')
            if 'state' not in {r['name'] for r in db.execute('PRAGMA table_info(projects)')}:
                db.execute("ALTER TABLE projects ADD COLUMN state TEXT NOT NULL DEFAULT 'active'")
            if 'preview_environment' not in {r['name'] for r in db.execute('PRAGMA table_info(projects)')}:
                db.execute("ALTER TABLE projects ADD COLUMN preview_environment TEXT")
            if 'preview_access' not in {r['name'] for r in db.execute('PRAGMA table_info(deployments)')}:
                db.execute("ALTER TABLE deployments ADD COLUMN preview_access INTEGER NOT NULL DEFAULT 0")
            if 'preview_mode' not in {r['name'] for r in db.execute('PRAGMA table_info(projects)')}:
                db.execute("ALTER TABLE projects ADD COLUMN preview_mode TEXT NOT NULL DEFAULT 'legacy'")
            if 'runtime_driver' not in {r['name'] for r in db.execute('PRAGMA table_info(deployments)')}:
                db.execute("ALTER TABLE deployments ADD COLUMN runtime_driver TEXT")
            if 'built_bytes' not in {r['name'] for r in db.execute('PRAGMA table_info(deployments)')}:
                db.execute("ALTER TABLE deployments ADD COLUMN built_bytes INTEGER NOT NULL DEFAULT 0")
            if 'app_domain' not in {r['name'] for r in db.execute('PRAGMA table_info(projects)')}:
                db.execute("ALTER TABLE projects ADD COLUMN app_domain TEXT")
            db.execute('UPDATE projects SET app_domain=? WHERE app_domain IS NULL',(settings.domain,))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.settings.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seal(self, value):
        return self.cipher.encrypt(encode(value).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def environment(self, project, target):
        value = project['preview_environment' if target == 'preview' else 'environment']
        return self.unseal(value) if value else {}

    def create_account(self, name):
        account_id = ident()
        token = "paas_" + secrets.token_urlsafe(36)
        now = time.time()
        with self.connection() as db:
            db.execute("INSERT INTO accounts(id,name,created) VALUES(?,?,?)", (account_id, name, now))
            db.execute("INSERT INTO tokens VALUES(?,?,?,?,?,?,?,?,?)", (ident(), account_id, "account-owner", self.token_hash(token), '["*"]', None, now + 365 * 86400, 0, now))
        return {"account_id": account_id, "token": token}

    def issue_owner_token(self, name):
        token, now = "paas_" + secrets.token_urlsafe(36), time.time()
        with self.connection() as db:
            account = db.execute("SELECT * FROM accounts WHERE name=? AND enabled=1", (name,)).fetchone()
            if not account:
                raise ValueError("Enabled account not found")
            db.execute("INSERT INTO tokens VALUES(?,?,?,?,?,?,?,?,?)", (ident(), account["id"], "account-owner-recovery", self.token_hash(token), '["*"]', None, now + 365 * 86400, 0, now))
        return {"account_id": account["id"], "token": token}

    def begin_account_retirement(self, name, account_id):
        with self.connection() as db:
            if (self.settings.root/'maintenance.json').exists():
                raise Problem(503,'Finish platform maintenance before retiring an account')
            account=db.execute('SELECT id FROM accounts WHERE name=? AND id=?',(name,account_id)).fetchone()
            if account is None:
                raise Problem(404,'Account name and confirmation ID do not match')
            row=db.execute('SELECT receipt FROM account_retirements WHERE account_id=?',(account_id,)).fetchone()
            if row and row['receipt']:
                return json.loads(row['receipt'])
            if db.execute("SELECT 1 FROM projects WHERE account_id=? AND (state!='deleted' OR current_deployment IS NOT NULL)",(account_id,)).fetchone():
                raise Problem(409,'Delete every owned project through the API before retiring its account')
            for table in ('deployments','data_resources','jobs'):
                active=" AND x.state IN ('uploading','queued','running')" if table=='jobs' else ''
                if db.execute(f'SELECT 1 FROM {table} x JOIN projects p ON p.id=x.project_id WHERE p.account_id=?'+active,(account_id,)).fetchone():
                    raise Problem(409,'Account still owns an active operation or resource')
            if db.execute('SELECT 1 FROM execution_targets WHERE account_id=?',(account_id,)).fetchone():
                raise Problem(409,'Account still owns a deployment target')
            db.execute('INSERT OR IGNORE INTO account_retirements(account_id,started) VALUES(?,?)',(account_id,time.time()))
            db.execute('UPDATE accounts SET enabled=0 WHERE id=?',(account_id,))
            db.execute('UPDATE tokens SET revoked=1 WHERE account_id=?',(account_id,))
            return None

    def complete_account_retirement(self, account_id, receipt):
        if receipt.get('account_id')!=account_id or receipt.get('state')!='completed':
            raise ValueError('Unexpected executor retirement receipt')
        with self.connection() as db:
            changed=db.execute('UPDATE account_retirements SET completed=?,receipt=? WHERE account_id=? AND completed IS NULL',
                               (time.time(),encode(receipt),account_id)).rowcount
            if changed:
                db.execute('INSERT INTO audit(account_id,token_id,action,object_id,created) VALUES(?,NULL,?,?,?)',
                           (account_id,'account.retire',account_id,time.time()))
            return json.loads(db.execute('SELECT receipt FROM account_retirements WHERE account_id=?',(account_id,)).fetchone()[0])

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def mint_token(self, db, actor, *, name, scopes, project_ids, expires, action="token.create"):
        token_id, token = ident(), "paas_" + secrets.token_urlsafe(36)
        db.execute("INSERT INTO tokens VALUES(?,?,?,?,?,?,?,?,?)", (
            token_id, actor["account_id"], name, self.token_hash(token), encode(scopes),
            encode(project_ids) if project_ids is not None else None, expires, 0, time.time(),
        ))
        self.audit(db, actor, action, token_id)
        return {"id": token_id, "token": token, "expires": expires, "scopes": scopes, "project_ids": project_ids}

    def authenticate(self, token):
        with self.connection() as db:
            row = db.execute("SELECT t.*, a.enabled FROM tokens t JOIN accounts a ON a.id=t.account_id WHERE secret_hash=?", (self.token_hash(token),)).fetchone()
        if not row or row["revoked"] or not row["enabled"] or row["expires"] <= time.time():
            raise Problem(401, "Invalid or expired platform token")
        with self.connection() as db:
            window = int(time.time() // 60)
            db.execute("INSERT INTO rate_limits VALUES(?,?,1) ON CONFLICT(token_id) DO UPDATE SET requests=CASE WHEN window=excluded.window THEN requests+1 ELSE 1 END,window=excluded.window", (row["id"], window))
            if db.execute("SELECT requests FROM rate_limits WHERE token_id=?", (row["id"],)).fetchone()[0] > 600:
                raise Problem(429, "Token request rate exceeded")
        result = dict(row)
        result["scopes"] = json.loads(row["scopes"])
        result["projects"] = json.loads(row["projects"]) if row["projects"] is not None else None
        return result

    @staticmethod
    def permit(actor, scope, project_id=None):
        if "*" not in actor["scopes"] and scope not in actor["scopes"]:
            raise Problem(403, "Token does not grant this operation")
        if project_id and actor["projects"] is not None and project_id not in actor["projects"]:
            raise Problem(404, "Project not found")

    @staticmethod
    def audit(db, actor, action, object_id):
        db.execute("INSERT INTO audit(account_id,token_id,action,object_id,created) VALUES(?,?,?,?,?)", (actor["account_id"], actor["id"], action, object_id, time.time()))

    def project(self, db, actor, project_id, scope="projects:read", *, allow_inactive=False, allow_preview=False):
        self.permit(actor, PREVIEW_SCOPE if allow_preview and PREVIEW_SCOPE in actor['scopes'] else scope, project_id)
        row = db.execute("SELECT * FROM projects WHERE id=? AND account_id=?", (project_id, actor["account_id"])).fetchone()
        if not row or (row['state']=='deleted' and not allow_inactive):
            raise Problem(404, "Project not found")
        if row['state']!='active' and scope.endswith(':write') and not allow_inactive:
            raise Problem(409,'Project deletion is in progress; observe its job or repeat DELETE to resume failed cleanup')
        if scope.endswith(':write') and not allow_inactive and db.execute("SELECT 1 FROM jobs WHERE project_id=? AND kind='project-export' AND state IN ('queued','running')",(project_id,)).fetchone():
            raise Problem(409,'A project snapshot is pending; observe its job before modifying the project')
        if scope.endswith(':write') and not allow_inactive and db.execute("SELECT 1 FROM jobs WHERE project_id=? AND kind='project-import' AND state IN ('uploading','queued','running','failed','cancelling')",(project_id,)).fetchone():
            raise Problem(409,'Destination is reserved for import; finish, retry or discard the import before modifying it')
        return dict(row)

    def deployment(self, db, actor, deployment_id, scope="deployments:read", *, allow_preview=False):
        row = db.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        if not row:
            raise Problem(404, "Deployment not found")
        project=self.project(db, actor, row["project_id"], scope, allow_preview=allow_preview)
        if PREVIEW_SCOPE in actor['scopes'] and not row['preview_access']:
            raise Problem(404, "Deployment not found")
        return dict(row)|{'app_domain':project['app_domain'], 'preview_mode':project['preview_mode']}

    def create_project(self, actor, name):
        self.permit(actor, "projects:write")
        if actor["projects"] is not None:
            raise Problem(403, "Project-scoped tokens cannot create projects")
        with self.connection() as db:
            account = db.execute("SELECT * FROM accounts WHERE id=?", (actor["account_id"],)).fetchone()
            if not account or not account["enabled"]:
                raise Problem(401,"Invalid or expired platform token")
            if db.execute("SELECT count(*) FROM projects WHERE account_id=? AND state!='deleted'", (actor["account_id"],)).fetchone()[0] >= account["max_projects"]:
                raise Problem(429, "Project quota reached")
            return self.insert_project(db, actor, name)

    def insert_project(self, db, actor, name):
        project_id = ident()
        domain = self.settings.new_app_domain or self.settings.domain
        try:
            db.execute("INSERT INTO projects(id,account_id,name,current_deployment,environment,created,app_domain) VALUES(?,?,?,?,?,?,?)",
                       (project_id, actor["account_id"], name, None, self.seal({}), time.time(), domain))
        except sqlite3.IntegrityError:
            raise Problem(409, "Project name already exists") from None
        self.audit(db, actor, "project.create", project_id)
        return {"id": project_id, "name": name, "url": "https://" + self.settings.hostname(project_id,domain=domain), "current_deployment": None}

    def enroll_cli_identity(self, db, issuer, subject):
        bound = db.execute("SELECT project_id FROM cli_identities WHERE issuer=? AND subject=?",
                           (issuer, subject)).fetchone()
        if bound is not None:
            return bound["project_id"]
        account_id = ident()
        db.execute("INSERT INTO accounts(id,name,max_projects,max_concurrent,created) VALUES(?,?,1,1,?)",
                   (account_id, "iflink-" + account_id, time.time()))
        actor = {"id": None, "account_id": account_id}
        self.audit(db, actor, "account.cli-enroll", account_id)
        project_id = self.insert_project(db, actor, "preview")["id"]
        if self.settings.personal_previews:
            db.execute("UPDATE projects SET preview_mode='isolated' WHERE id=?", (project_id,))
        db.execute("INSERT INTO cli_identities VALUES(?,?,?)", (issuer, subject, project_id))
        return project_id

    def rename_project(self, actor, project_id, name):
        with self.connection() as db:
            project=self.project(db,actor,project_id,'projects:write')
            try:db.execute('UPDATE projects SET name=? WHERE id=?',(name,project_id))
            except sqlite3.IntegrityError:raise Problem(409,'Project name already exists') from None
            self.audit(db,actor,'project.rename',project_id)
        return {'id':project_id,'name':name,'url':'https://'+self.settings.hostname(project_id,domain=project['app_domain'])}

    def delete_project(self, actor, project_id, destroy_data=False):
        with self.connection() as db:
            project=self.project(db,actor,project_id,'projects:write',allow_inactive=True)
            latest=db.execute("SELECT * FROM jobs WHERE project_id=? AND kind='project-delete' ORDER BY created DESC LIMIT 1",(project_id,)).fetchone()
            if project['state']=='deleted':
                return {'project_id':project_id,'job_id':latest['id'],'state':'completed' if latest['state']=='completed' else 'deleting'}
            self.permit(actor,'deployments:write',project_id)
            resources=db.execute('SELECT kind FROM data_resources WHERE project_id=?',(project_id,)).fetchall()
            if resources and not destroy_data:raise Problem(409,'Project contains persistent data; pass destroy_data=true explicitly to destroy it')
            for resource in resources:
                self.permit(actor,'databases:write' if resource['kind']=='database' else 'storage:write',project_id)
            if latest and latest['state'] in {'queued','running'}:
                return {'project_id':project_id,'job_id':latest['id'],'state':'deleting'}
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('uploading','queued','running')",(project_id,)).fetchone():
                raise Problem(409,'Project has an active operation; finish or cancel it before deleting the project')
            if db.execute('SELECT 1 FROM uploads u JOIN deployments d ON d.id=u.deployment_id WHERE d.project_id=?',(project_id,)).fetchone():
                raise Problem(409,'A project source upload is still in progress')
            job_id,now=ident(),time.time()
            db.execute("UPDATE projects SET state='deleting' WHERE id=?",(project_id,))
            db.execute("INSERT INTO jobs(id,project_id,kind,created,updated) VALUES(?,?,?,?,?)",(job_id,project_id,'project-delete',now,now))
            self.audit(db,actor,'project.delete',project_id)
        return {'project_id':project_id,'job_id':job_id,'state':'deleting'}

    def check_concurrency(self, db, actor):
        from publisher.capacity import active_count
        account = db.execute("SELECT * FROM accounts WHERE id=?", (actor["account_id"],)).fetchone()
        count = active_count(db, actor["account_id"])
        if count >= account["max_concurrent"]:
            raise Problem(429, "Concurrent deployment quota reached")
        return account

    @staticmethod
    def purge_release(db, deployment_id):
        # Completed job receipts remain queryable after their release is removed.
        db.execute("DELETE FROM jobs WHERE deployment_id=? AND kind='runtime-logs'",(deployment_id,))
        db.execute('UPDATE jobs SET deployment_id=NULL WHERE deployment_id=?', (deployment_id,))
        db.execute('DELETE FROM logs WHERE deployment_id=?', (deployment_id,))
        db.execute('DELETE FROM runtime_logs WHERE deployment_id=?', (deployment_id,))
        db.execute('DELETE FROM execution_targets WHERE deployment_id=?', (deployment_id,))
        db.execute('DELETE FROM deployments WHERE id=?', (deployment_id,))

    def check_runtime_capacity(self, db, account_id, config, excluding="", *, driver=None):
        from publisher import capacity
        if driver == ARTIFACT_DRIVER:
            return
        # A failed release can still own a running container. Its reservation is
        # released only after resource deletion, not when a health check fails.
        totals = capacity.reservations(db, excluding)
        total, sandbox = totals["platform"], totals["sandbox"]
        account = totals["accounts"].get(account_id, capacity.empty())
        # Match the sandbox slice envelope, rather than over-reserving its parent limits.
        if driver in {SANDBOX_DRIVER, NATIVE_DATABASE_DRIVER} and (sandbox["memory_mib"] + config['memory_mb'] > self.settings.sandbox_limits["memory_mib"]
                                         or sandbox["vcpus"] + config['cpus'] > self.settings.sandbox_limits["vcpus"]):
            raise Problem(429, "Sandbox preview pool capacity is reserved; remove an unused sandbox preview")
        if account["memory_mib"] + config["memory_mb"] > capacity.ACCOUNT["memory_mib"]:
            raise Problem(429, "Account runtime memory reservation exceeds 4096 MiB; delete unused releases")
        if account["vcpus"] + config["cpus"] > capacity.ACCOUNT["vcpus"]:
            raise Problem(429, "Account runtime CPU reservation exceeds 4 vCPU; delete unused releases")
        if total["memory_mib"] + config["memory_mb"] > capacity.PLATFORM["memory_mib"] or total["vcpus"] + config["cpus"] > capacity.PLATFORM["vcpus"]:
            raise Problem(429, "Platform runtime capacity is reserved; retry after releases are removed")

    def deployment_preconditions(self, db, actor, project, config):
        project_id = project['id']
        personal = project['preview_mode'] == 'isolated'
        if personal:
            if not self.settings.personal_previews:
                raise Problem(503, 'The personal preview runtime is temporarily disabled')
            if config['runtime'] not in {'static', 'image'} or config.get('build_command'):
                raise Problem(422, 'Build this personal project on your computer or CI with poteto deploy --local-build')
            if config['runtime'] == 'image' and not self.settings.sandbox_enabled(project_id, config, personal=True):
                raise Problem(422, 'Personal images need at least 256 MiB and an enabled target; only enabled native database bindings are supported')
        if config['runtime'] == 'image' and not personal and project_id not in self.settings.prebuilt_image_projects:
            raise Problem(403, "Prebuilt image deployment is not enabled for this project")
        account = self.check_concurrency(db, actor)
        driver = None
        if self.settings.artifact_enabled(project_id, config, personal=personal):
            driver = ARTIFACT_DRIVER
        elif self.settings.sandbox_enabled(project_id, config, personal=personal):
            driver = SANDBOX_DRIVER
        if config.get('outbound_network', 'none') != 'none':
            if config['outbound_network'] != 'web' or driver != SANDBOX_DRIVER:
                raise Problem(422, 'Web egress is supported only by sandbox previews')
            if not self.settings.sandbox_web:
                raise Problem(503, 'Web egress is not enabled on this platform')
            self.permit(actor, PREVIEW_WEB_SCOPE if PREVIEW_SCOPE in actor['scopes'] else 'deployments:write', project_id)
        self.check_runtime_capacity(db, actor["account_id"], config, driver=driver)
        from publisher.data_store import DataStore
        environment = DataStore(self).bind_resources(db, project_id, config, self.environment(project, config['target']))
        count = db.execute("SELECT count(*) FROM deployments WHERE project_id=?", (project_id,)).fetchone()[0]
        if count >= 100:
            raise Problem(429, "Release retention quota reached; delete unused releases")
        return driver, environment, account

    def preflight(self, actor, project_id, config, archive_bytes, unpacked_bytes, files):
        with self.connection() as db:
            project = self.project(db, actor, project_id, "deployments:write", allow_preview=config['target'] == 'preview')
            upload_limit, unpack_limit, file_limit = self.settings.upload_limits(config['runtime'])
            if archive_bytes > upload_limit or unpacked_bytes > unpack_limit or files > file_limit:
                raise Problem(413, "Artifact exceeds this runtime's upload, expansion or file limit")
            driver, _, account = self.deployment_preconditions(db, actor, project, config)
            self.check_upload_capacity(db, config['runtime'])
            used = self.storage_used(db, actor['account_id'])
            if used + archive_bytes + unpacked_bytes > account['max_storage']:
                raise Problem(429, "Artifact storage quota reached")
            return {"ok": True, "project_id": project_id, "target": config['target'], "runtime_driver": driver,
                    "preview_mode": project['preview_mode'], "configuration": config,
                    "upload": {"archive_bytes": archive_bytes, "unpacked_bytes": unpacked_bytes, "files": files,
                               "limit_bytes": upload_limit, "expansion_limit_bytes": unpack_limit, "file_limit": file_limit},
                    "storage": {"used_bytes": used, "limit_bytes": account['max_storage'],
                                "remaining_after_upload": account['max_storage'] - used - archive_bytes - unpacked_bytes},
                    "checked_at": time.time()}

    def create_deployment(self, actor, project_id, config, key):
        with self.connection() as db:
            preview = config['target'] == 'preview'
            project = self.project(db, actor, project_id, "deployments:write", allow_preview=preview)
            if key:
                old = db.execute("SELECT * FROM deployments WHERE project_id=? AND idempotency_key=?", (project_id, key)).fetchone()
                if old:
                    if old["config"] != encode(config):
                        raise Problem(409, "Idempotency key was used with different configuration")
                    if PREVIEW_SCOPE in actor['scopes'] and not old['preview_access']:
                        raise Problem(409, "Idempotency key is already in use")
                    return dict(old)|{'app_domain':project['app_domain'], 'preview_mode':project['preview_mode']}
            driver, environment, _ = self.deployment_preconditions(db, actor, project, config)
            deployment_id, now = ident(), time.time()
            db.execute("INSERT INTO deployments(id,project_id,state,config,environment,idempotency_key,created,updated,preview_access,runtime_driver) VALUES(?,?,?,?,?,?,?,?,?,?)", (deployment_id, project_id, "uploading", encode(config), self.seal(environment), key, now, now, preview, driver))
            self.audit(db, actor, "deployment.create", deployment_id)
            return dict(db.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone())|{'app_domain':project['app_domain'], 'preview_mode':project['preview_mode']}

    def configuration(self, actor, project_id, model):
        with self.connection() as db:
            project = self.project(db, actor, project_id, 'deployments:write', allow_preview=model.target == 'preview')
        defaults = {'cpus': 0.1, 'sleep': True} if project['preview_mode'] == 'isolated' and model.runtime == 'image' else {}
        return model.configuration(defaults=defaults)

    def environment_project(self, db, actor, project_id, target):
        if target == 'preview' and PREVIEW_SCOPE in actor['scopes']:
            self.permit(actor, PREVIEW_ENV_SCOPE, project_id)
            return self.project(db, actor, project_id, 'env:write', allow_preview=True)
        return self.project(db, actor, project_id, 'env:write')

    def patch_environment(self, actor, project_id, values, remove, target):
        from pydantic import ValidationError
        from publisher.models import EnvironmentInput
        with self.connection() as db:
            project = self.environment_project(db, actor, project_id, target)
            current = self.environment(project, target)
            current.update(values)
            for key in remove:
                current.pop(key, None)
            try:
                EnvironmentInput(values=current)
            except ValidationError:
                raise Problem(422, "Combined environment exceeds the variable or size limits") from None
            column = "preview_environment" if target == "preview" else "environment"
            db.execute(f"UPDATE projects SET {column}=? WHERE id=?", (self.seal(current), project_id))
            self.audit(db, actor, "environment.patch." + target, project_id)
        return {"keys": sorted(current), "applies_to": "new deployments"}

    def check_upload_capacity(self, db, runtime, *, reserve_new=True):
        upload_limit, _, _ = self.settings.upload_limits(runtime)
        reserved = db.execute("SELECT coalesce(sum(s.size-s.received),0) FROM source_uploads s "
                              "JOIN deployments d ON d.id=s.deployment_id WHERE d.state='uploading'").fetchone()[0]
        if shutil.disk_usage(self.settings.root).free < 1024**3 + reserved + (upload_limit if reserve_new else 0):
            raise Problem(507, "Upload refused to preserve the platform's free disk reserve")
        if db.execute("SELECT count(*) FROM uploads WHERE expires>=?", (time.time(),)).fetchone()[0] >= 4:
            raise Problem(429, "Upload concurrency limit reached")

    def begin_upload(self, actor, deployment_id, upload_id, *, resumable=False):
        with self.connection() as db:
            deployment = self.deployment(db, actor, deployment_id, "deployments:write", allow_preview=True)
            if deployment["state"] != "uploading":
                raise Problem(409, "Deployment no longer accepts an upload")
            session = db.execute("SELECT 1 FROM source_uploads WHERE deployment_id=?", (deployment_id,)).fetchone()
            if bool(session) != resumable:
                raise Problem(409, "Use this deployment's existing upload protocol")
            db.execute("DELETE FROM uploads WHERE expires<?", (time.time(),))
            self.check_upload_capacity(db, json.loads(deployment["config"])["runtime"], reserve_new=not resumable)
            try:
                db.execute("INSERT INTO uploads VALUES(?,?,?)", (deployment_id, upload_id, time.time() + 180))
            except sqlite3.IntegrityError:
                raise Problem(409, "An upload for this deployment is already in progress") from None

    @staticmethod
    def storage_used(db, account_id):
        stored = db.execute("SELECT coalesce(sum(d.artifact_bytes+d.unpacked_bytes+d.built_bytes),0) FROM deployments d "
                            "JOIN projects p ON p.id=d.project_id WHERE p.account_id=?", (account_id,)).fetchone()[0]
        reserved = db.execute("SELECT coalesce(sum(s.size),0) FROM source_uploads s JOIN deployments d ON d.id=s.deployment_id "
                              "JOIN projects p ON p.id=d.project_id WHERE p.account_id=? AND d.state='uploading'",
                              (account_id,)).fetchone()[0]
        return stored + reserved

    def reserve_built_artifact(self, deployment_id, size, *, image=False):
        with self.connection() as db:
            row = db.execute("SELECT p.account_id,a.max_storage,d.built_bytes,d.runtime_driver,d.config FROM deployments d "
                             "JOIN projects p ON p.id=d.project_id JOIN accounts a ON a.id=p.account_id "
                             "WHERE d.id=?", (deployment_id,)).fetchone()
            if row is None or not (json.loads(row['config'])['runtime'] == 'image' if image else
                                   row['runtime_driver'] == ARTIFACT_DRIVER):
                raise Problem(409, "Release no longer owns this artifact")
            if self.storage_used(db, row['account_id']) - row['built_bytes'] + size > row['max_storage']:
                raise Problem(429, "Built files exceed the account artifact storage quota")
            db.execute("UPDATE deployments SET built_bytes=? WHERE id=?", (size, deployment_id))

    def enqueue_upload(self, actor, deployment_id, path, size, unpacked_bytes, *, upload_id=None):
        with self.connection() as db:
            deployment = self.deployment(db, actor, deployment_id, "deployments:write", allow_preview=True)
            if deployment["state"] != "uploading":
                raise Problem(409, "Deployment no longer accepts an upload")
            if upload_id is not None and not db.execute(
                    "SELECT 1 FROM uploads WHERE deployment_id=? AND upload_id=? AND expires>?",
                    (deployment_id, upload_id, time.time())).fetchone():
                raise Problem(409, "Upload no longer owns this deployment")
            account = db.execute("SELECT * FROM accounts WHERE id=?", (actor["account_id"],)).fetchone()
            used = self.storage_used(db, actor["account_id"])
            reserved = db.execute("SELECT size FROM source_uploads WHERE deployment_id=?", (deployment_id,)).fetchone()
            if reserved:
                used -= reserved["size"]
            if used + size + unpacked_bytes > account["max_storage"]:
                raise Problem(429, "Artifact storage quota reached")
            path.replace(self.settings.root / "artifacts" / f"{deployment_id}.tar.gz")
            now = time.time()
            db.execute("UPDATE deployments SET state='queued', artifact_bytes=?, unpacked_bytes=?, updated=? WHERE id=?", (size, unpacked_bytes, now, deployment_id))
            db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)", (ident(), deployment["project_id"], "deploy", deployment_id, now, now))
            self.audit(db, actor, "deployment.upload", deployment_id)

    def claim(self, owner, *, logs_only=False, static_previews_only=False):
        if logs_only and static_previews_only:
            raise ValueError("A worker may use only one dedicated queue lane")
        now = time.time()
        lane = "j.kind='runtime-logs'" if logs_only else "j.kind!='runtime-logs'"
        params = [now, now]
        if static_previews_only:
            lane += """ AND j.kind='deploy' AND EXISTS (
                SELECT 1 FROM deployments d WHERE d.id=j.deployment_id
                AND d.runtime_driver=? AND json_extract(d.config,'$.target')='preview'
                AND coalesce(json_extract(d.config,'$.build_command'),'')='')"""
            params.append(ARTIFACT_DRIVER)
        if not logs_only:
            # The dedicated lane may proceed for another project, never while
            # an active mutation already owns this project's deployment state.
            lane += """ AND NOT EXISTS (
                SELECT 1 FROM jobs current WHERE current.project_id=j.project_id
                AND current.id!=j.id AND current.kind!='runtime-logs'
                AND current.state='running' AND current.lease_until>=?)"""
            params.append(now)
        with self.connection() as db:
            job = db.execute("SELECT j.* FROM jobs j WHERE ((j.state='queued' AND coalesce(j.lease_until,0)<?) "
                             "OR (j.state='running' AND j.lease_until<?)) AND " + lane +
                             " ORDER BY j.created LIMIT 1", params).fetchone()
            if not job:
                return None
            db.execute("UPDATE jobs SET state='running',owner=?,lease_until=?,attempts=attempts+1,updated=? WHERE id=?", (owner, now + 90, now, job["id"]))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())

    def heartbeat(self, job_id, owner):
        with self.connection() as db:
            changed = db.execute("UPDATE jobs SET lease_until=?,updated=? WHERE id=? AND owner=? AND state='running'", (time.time() + 90, time.time(), job_id, owner)).rowcount
            if changed != 1:
                raise RuntimeError("Worker lost its job lease")

    def expire_uploads(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('DELETE FROM uploads WHERE expires<?', (now,))
            db.execute("UPDATE deployments SET state='cancelled',error='Unsubmitted deployment expired after 24 hours',updated=? WHERE state='uploading' AND created<? AND id NOT IN (SELECT deployment_id FROM uploads)", (now,now-86400))
            from publisher.source_uploads import expire
            expire(self, db)
        # Streams have a 120-second deadline and a 180-second lease. Keep a full
        # hour of margin before removing abandoned temporary files after a crash.
        for path in (self.settings.root / 'uploads').glob('*.part'):
            if path.stat().st_mtime < now - 3600:
                path.unlink(missing_ok=True)

    def log(self, deployment_id, message):
        with self.connection() as db:
            db.execute("INSERT INTO logs(deployment_id,message,created) VALUES(?,?,?)", (deployment_id, message[:8000], time.time()))
