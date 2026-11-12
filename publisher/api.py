import asyncio
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from publisher.archive import ArchiveError, unpack
from publisher.config import Settings
from publisher.http_limits import ControlLimits
from publisher.models import PreflightInput, DeploymentInput, EnvironmentInput, EnvironmentPatchInput, ProjectInput, ReleaseInput, TokenInput
from publisher.store import PREVIEW_DATA_SCOPE, PREVIEW_ENV_SCOPE, PREVIEW_WEB_SCOPE, PREVIEW_SCOPES, PREVIEW_MANAGE_SCOPE, PREVIEW_SCOPE, SANDBOX_DRIVER, Problem, SCOPES, Store, encode, ident

bearer = HTTPBearer(auto_error=False)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    store = Store(settings)

    app = FastAPI(title="OVH PaaS Publishing API", version="0.1.0",
                  description="Upload a tar.gz archive, inspect deployment logs, publish a healthy release and roll back by deployment ID.")
    app.state.store = store
    from publisher.oidc_login import load_login
    login = load_login()
    app.add_middleware(ControlLimits, store=store, allow_cli_login=login is not None)

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(Problem)
    async def problem_handler(request, error):
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
        if error.status == 429:
            headers["Retry-After"] = "60"
        return JSONResponse({"error": error.message}, status_code=error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, error):
        return JSONResponse({"error": "Invalid request", "details": [{"location": list(e["loc"]), "message": e["msg"]} for e in error.errors()]}, status_code=422)

    def actor(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        cached = getattr(request.state, "actor", None)
        if cached is not None:
            return cached
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise Problem(401, "A platform Bearer token is required")
        return store.authenticate(credentials.credentials)

    if login is not None:
        login.register(app, store, actor)

    from publisher.data_api import register
    register(app, store, actor)
    from publisher.project_exports import register as register_exports
    register_exports(app,store,actor)
    from publisher.project_imports import register as register_imports
    register_imports(app,store,actor)
    from publisher.source_uploads import register as register_uploads
    register_uploads(app, store, actor)
    from publisher.preview_management import register as register_previews
    register_previews(app, store, actor)

    def release_view(row):
        config = json.loads(row["config"])
        preview = "https://" + settings.hostname(row["project_id"], row["id"],domain=row["app_domain"])
        return {"id": row["id"], "project_id": row["project_id"], "state": row["state"],
                "runtime_driver": row.get("runtime_driver"),
                "config": config, "created": row["created"], "updated": row["updated"],
                "artifact_bytes": row["artifact_bytes"], "error": row["error"],
                "url": preview if row["state"] == "ready" else None,
                "production_url": "https://" + settings.hostname(row["project_id"],domain=row["app_domain"]),
                "upload_url": f"/v1/deployments/{row['id']}/source" if row["state"] == "uploading" else None,
                "resumable_upload_url": f"/v1/deployments/{row['id']}/source-upload" if row["state"] == "uploading" else None}

    from publisher.redeploy import register as register_redeploy
    register_redeploy(app, store, actor, release_view)

    @app.get("/healthz", include_in_schema=False)
    def health():
        with store.connection() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/internal/routes", include_in_schema=False)
    def route_inventory(request: Request):
        token_file = os.environ.get("PAAS_EDGE_TOKEN_FILE")
        expected = Path(token_file).read_text().strip() if token_file else None
        supplied = request.headers.get("X-Paas-Edge-Key", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            raise Problem(401, "Invalid edge connector credential")
        with store.connection() as db:
            rows = db.execute("SELECT d.id,d.project_id,d.config,d.state,p.app_domain FROM deployments d JOIN projects p ON p.id=d.project_id WHERE p.state='active' AND d.state IN ('deploying','activating','ready')").fetchall()
            projects = db.execute("SELECT id,app_domain FROM projects WHERE state='active' AND (current_deployment IS NOT NULL OR id IN (SELECT project_id FROM jobs WHERE kind IN ('promote','rollback') AND state IN ('queued','running')))").fetchall()
        hosts = {settings.hostname(r["project_id"], r["id"],domain=r["app_domain"]) for r in rows}
        hosts.update(settings.hostname(r["project_id"],domain=r["app_domain"]) for r in rows if json.loads(r["config"])["target"] == "production")
        hosts.update(settings.hostname(r["id"],domain=r["app_domain"]) for r in projects)
        return {"hosts": sorted(hosts)}

    @app.get("/v1/me", tags=["Account"])
    def me(user=Depends(actor)):
        with store.connection() as db:
            account = dict(db.execute("SELECT * FROM accounts WHERE id=?", (user["account_id"],)).fetchone())
        return {"account": account, "token_id": user["id"], "scopes": user["scopes"], "project_ids": user["projects"]}

    @app.post("/v1/tokens", status_code=201, tags=["Account"])
    def issue_token(body: TokenInput, user=Depends(actor)):
        """Issue a project-scoped deployments:preview token for external upload, status and logs."""
        store.permit(user, "tokens:write")
        scopes = set(body.scopes)
        if not scopes or not scopes <= SCOPES | {"*"}:
            raise Problem(422, "Unknown or empty token scopes")
        if scopes & PREVIEW_SCOPES and (
                PREVIEW_SCOPE not in scopes or not scopes <= PREVIEW_SCOPES or not body.project_ids):
            raise Problem(422, "A preview token requires explicit project IDs and no other scopes")
        if "*" not in user["scopes"] and not scopes <= set(user["scopes"]):
            raise Problem(403, "A token cannot grant more permissions than its issuer")
        if user["projects"] is not None and (body.project_ids is None or not set(body.project_ids) <= set(user["projects"])):
            raise Problem(403, "A token cannot broaden its project restriction")
        expires = min(time.time() + body.expires_in_days * 86400, user["expires"])
        with store.connection() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=? AND enabled=1", (user["account_id"],)).fetchone():
                raise Problem(401, "Invalid or expired platform token")
            for project_id in body.project_ids or []:
                store.project(db, user, project_id, "tokens:write")
            return store.mint_token(db, user, name=body.name, scopes=sorted(scopes),
                                    project_ids=body.project_ids, expires=expires)

    @app.get("/v1/tokens", tags=["Account"])
    def list_tokens(user=Depends(actor)):
        store.permit(user, "tokens:write")
        with store.connection() as db:
            rows = db.execute("SELECT id,name,scopes,projects,expires,revoked,created FROM tokens WHERE account_id=? ORDER BY created", (user["account_id"],)).fetchall()
        return [dict(r) for r in rows if user["projects"] is None or (r["projects"] is not None and set(json.loads(r["projects"])) <= set(user["projects"]))]

    @app.delete("/v1/tokens/{token_id}", status_code=204, tags=["Account"])
    def revoke_token(token_id: str, user=Depends(actor)):
        store.permit(user, "tokens:write")
        with store.connection() as db:
            token = db.execute("SELECT * FROM tokens WHERE id=? AND account_id=?", (token_id, user["account_id"])).fetchone()
            if not token or (user["projects"] is not None and (token["projects"] is None or not set(json.loads(token["projects"])) <= set(user["projects"]))):
                raise Problem(404, "Token not found")
            db.execute("UPDATE tokens SET revoked=1 WHERE id=?", (token_id,))
            store.audit(db, user, "token.revoke", token_id)

    @app.post("/v1/projects", status_code=201, tags=["Projects"])
    def create_project(body: ProjectInput, user=Depends(actor)):
        return store.create_project(user, body.name)

    @app.get("/v1/projects", tags=["Projects"])
    def list_projects(user=Depends(actor)):
        store.permit(user, "projects:read")
        with store.connection() as db:
            rows = db.execute("SELECT id,name,current_deployment,created,state,app_domain FROM projects WHERE account_id=? AND state!='deleted' ORDER BY created", (user["account_id"],)).fetchall()
        return [dict(r) | {"url": "https://" + settings.hostname(r["id"],domain=r["app_domain"])} for r in rows if user["projects"] is None or r["id"] in user["projects"]]

    @app.get("/v1/projects/{project_id}", tags=["Projects"])
    def get_project(project_id: str, user=Depends(actor)):
        with store.connection() as db:
            row = store.project(db, user, project_id)
        return {k: v for k, v in row.items() if k not in {"environment", "preview_environment"}} | {"url": "https://" + settings.hostname(project_id,domain=row["app_domain"])}

    @app.get('/v1/projects/{project_id}/capabilities', tags=['Projects'])
    def project_capabilities(project_id: str, user=Depends(actor)):
        with store.connection() as db:
            project = store.project(db, user, project_id, 'deployments:read', allow_preview=True)
        personal = project['preview_mode'] == 'isolated'
        image = {'runtime': 'image', 'target': 'preview', 'memory_mb': 256, 'sleep': personal}
        return {'project_id': project_id, 'preview_mode': project['preview_mode'],
                'enabled': settings.personal_previews if personal else True,
                'local_build_required': personal,
                'static_vm_free': settings.artifact_enabled(project_id, {'runtime': 'static'}, personal=personal),
                'prebuilt_images': settings.personal_previews if personal else project_id in settings.prebuilt_image_projects,
                'stateless_preview_driver': 'gvisor' if settings.sandbox_enabled(project_id, image, personal=personal) else None,
                'image_defaults': {'memory_mb': 256, 'cpus': 0.1, 'sleep': True} if personal else None,
                'sandbox_pool': settings.sandbox_limits,
                'preview_environment': bool({'*', 'env:write', PREVIEW_ENV_SCOPE} & set(user['scopes'])),
                'preview_database': {'enabled': settings.native_databases and settings.personal_previews if personal else True,
                                     'driver': 'native-postgres' if personal else 'tenant-vm',
                                     'authorized': bool({'*', 'databases:write', PREVIEW_DATA_SCOPE} & set(user['scopes']))},
                'web_egress': {'enabled': settings.sandbox_web,
                               'authorized': bool({'*', 'deployments:write', PREVIEW_WEB_SCOPE} & set(user['scopes'])),
                               'ports': [80, 443], 'private_networks': False},
                'limitations': ['Outbound network requires explicit web permission', 'No volume binding', 'Image previews only; no VM fallback'] if personal else []}

    @app.patch('/v1/projects/{project_id}',tags=['Projects'])
    def rename_project(project_id: str,body: ProjectInput,user=Depends(actor)):
        return store.rename_project(user,project_id,body.name)

    @app.delete('/v1/projects/{project_id}',status_code=202,tags=['Projects'])
    def delete_project(project_id: str,destroy_data: bool=False,user=Depends(actor)):
        """Withdraw routes and delete every release; persistent data needs explicit consent. Repeat DELETE to resume a failed cleanup."""
        return store.delete_project(user,project_id,destroy_data)

    @app.put("/v1/projects/{project_id}/env", tags=["Projects"])
    def put_environment(project_id: str, body: EnvironmentInput, target: Literal['production','preview'] = 'production', user=Depends(actor)):
        with store.connection() as db:
            store.environment_project(db, user, project_id, target)
            column = "preview_environment" if target == "preview" else "environment"
            db.execute(f"UPDATE projects SET {column}=? WHERE id=?", (store.seal(body.values), project_id))
            store.audit(db, user, "environment.replace.preview" if target == "preview" else "environment.replace", project_id)
        return {"keys": sorted(body.values), "applies_to": "new deployments"}

    @app.get("/v1/projects/{project_id}/env", tags=["Projects"])
    def environment_keys(project_id: str, target: Literal['production','preview'] = 'production', user=Depends(actor)):
        with store.connection() as db:
            project = store.environment_project(db, user, project_id, target)
        return {"keys": sorted(store.environment(project,target))}

    @app.patch("/v1/projects/{project_id}/env", tags=["Projects"])
    def patch_environment(project_id: str, body: EnvironmentPatchInput,
                          target: Literal["production", "preview"] = "production", user=Depends(actor)):
        return store.patch_environment(user, project_id, body.values, body.remove, target)

    @app.post("/v1/projects/{project_id}/preflight", tags=["Deployments"])
    def preflight(project_id: str, body: PreflightInput, user=Depends(actor)):
        return store.preflight(user, project_id, store.configuration(user, project_id, body.configuration),
                               body.archive_bytes, body.unpacked_bytes, body.files)

    @app.post("/v1/projects/{project_id}/deployments", status_code=201, tags=["Deployments"])
    def create_deployment(project_id: str, body: DeploymentInput, request: Request, user=Depends(actor)):
        key = request.headers.get("Idempotency-Key")
        if key and not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", key):
            raise Problem(422, "Invalid Idempotency-Key")
        return release_view(store.create_deployment(user, project_id, store.configuration(user, project_id, body), key))

    @app.put("/v1/deployments/{deployment_id}/source", status_code=202, tags=["Deployments"],
             openapi_extra={"requestBody": {"required": True, "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}}}})
    async def upload_source(deployment_id: str, request: Request, user=Depends(actor)):
        with store.connection() as db:
            deployment = store.deployment(db, user, deployment_id, "deployments:write", allow_preview=True)
        if deployment["state"] != "uploading":
            raise Problem(409, "Deployment no longer accepts an upload")
        if request.headers.get("content-type", "").split(";")[0] not in {"application/gzip", "application/octet-stream"}:
            raise Problem(415, "Upload a tar.gz body with Content-Type: application/gzip")
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise Problem(400, "Invalid Content-Length") from None
        runtime = json.loads(deployment["config"])["runtime"]
        upload_limit, unpack_limit, file_limit = settings.upload_limits(runtime)
        if content_length > upload_limit:
            raise Problem(413, f"Upload exceeds {upload_limit // 1024**2} MiB")
        path = settings.root / "uploads" / (ident() + ".part")
        size = 0
        upload_id = ident()
        store.begin_upload(user, deployment_id, upload_id)
        try:
            async with asyncio.timeout(120):
                with path.open("xb") as out:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > upload_limit:
                            raise Problem(413, "Upload exceeds its limit")
                        out.write(chunk)
            try:
                if runtime == "image":
                    from starlette.concurrency import run_in_threadpool
                    stats = await run_in_threadpool(unpack, path, None, max_bytes=unpack_limit,
                                                    max_files=file_limit, required_file="manifest.json")
                else:
                    stats = unpack(path, None, max_bytes=settings.unpack_limit, max_files=settings.file_limit)
            except ArchiveError as error:
                raise Problem(422, str(error)) from None
            store.enqueue_upload(user, deployment_id, path, size, stats["unpacked_bytes"])
        except TimeoutError:
            raise Problem(408, "Upload did not finish within 120 seconds") from None
        finally:
            path.unlink(missing_ok=True)
            with store.connection() as db:
                db.execute("DELETE FROM uploads WHERE deployment_id=? AND upload_id=?", (deployment_id, upload_id))
        return {"id": deployment_id, "state": "queued", "status_url": f"/v1/deployments/{deployment_id}"}

    @app.post("/v1/deployments/{deployment_id}/retry", status_code=202, tags=["Deployments"])
    def retry_deployment(deployment_id: str, user=Depends(actor)):
        """Resume a failed deployment from its existing source/build/container; code changes need a new deployment."""
        with store.connection() as db:
            row = store.deployment(db, user, deployment_id, "deployments:write")
            if row['state'] != 'failed':
                raise Problem(409, 'Only a terminal failed deployment can be retried')
            if db.execute("SELECT 1 FROM jobs WHERE deployment_id=? AND state IN ('queued','running')", (deployment_id,)).fetchone():
                raise Problem(409, 'Deployment still has an active operation')
            if not (settings.root / 'artifacts' / (deployment_id + '.tar.gz')).is_file():
                raise Problem(409, 'Original source artifact is unavailable')
            store.check_concurrency(db, user)
            store.check_runtime_capacity(db, user["account_id"], json.loads(row["config"]), excluding=deployment_id,
                                         driver=row["runtime_driver"])
            now, job_id = time.time(), ident()
            db.execute("UPDATE deployments SET state='queued',error=NULL,updated=? WHERE id=?", (now, deployment_id))
            db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)", (job_id,row['project_id'],'deploy',deployment_id,now,now))
            store.audit(db,user,'deployment.retry',deployment_id)
        return {'job_id':job_id,'deployment_id':deployment_id,'state':'queued'}

    @app.get("/v1/projects/{project_id}/deployments", tags=["Deployments"])
    def list_deployments(project_id: str, user=Depends(actor)):
        with store.connection() as db:
            project=store.project(db, user, project_id, "deployments:read", allow_preview=True)
            rows = db.execute("SELECT * FROM deployments WHERE project_id=? AND (? OR preview_access=1) ORDER BY created DESC",
                              (project_id, PREVIEW_SCOPE not in user['scopes'])).fetchall()
        return [release_view(dict(r)|{'app_domain':project['app_domain']}) for r in rows]

    @app.get("/v1/deployments/{deployment_id}", tags=["Deployments"])
    def get_deployment(deployment_id: str, user=Depends(actor)):
        with store.connection() as db:
            return release_view(store.deployment(db, user, deployment_id, allow_preview=True))

    @app.get("/v1/deployments/{deployment_id}/logs", tags=["Deployments"])
    def get_logs(deployment_id: str, after: int = 0, user=Depends(actor)):
        with store.connection() as db:
            store.deployment(db, user, deployment_id, allow_preview=True)
            rows = db.execute("SELECT id,message,created FROM logs WHERE deployment_id=? AND id>? ORDER BY id LIMIT 500", (deployment_id, after)).fetchall()
        return {"logs": [dict(r) for r in rows], "next_cursor": rows[-1]["id"] if rows else after}

    @app.get('/v1/deployments/{deployment_id}/runtime-logs', tags=['Deployments'])
    def runtime_logs(deployment_id: str, user=Depends(actor)):
        """A bounded snapshot of the last 200 stdout/stderr lines; reading never wakes the app."""
        with store.connection() as db:
            release=store.deployment(db,user,deployment_id,allow_preview=True)
            if not release['backend_id'] or release['state']=='deleting':
                raise Problem(409,'This deployment has no available runtime container')
            cached=db.execute('SELECT * FROM runtime_logs WHERE deployment_id=?',(deployment_id,)).fetchone()
            job=db.execute("SELECT * FROM jobs WHERE deployment_id=? AND kind='runtime-logs' ORDER BY created DESC LIMIT 1",(deployment_id,)).fetchone()
            project_state=db.execute('SELECT state FROM projects WHERE id=?',(release['project_id'],)).fetchone()[0]
            if (settings.root/'maintenance.json').exists() or project_state!='active':
                return {'log':cached['content'] if cached else '', 'collected':cached['collected'] if cached else None,
                        'job_id':None,'state':'paused','error':None}
            pending=job and job['state'] in {'queued','running'}
            if not pending and (not job or job['updated']<time.time()-5):
                count=db.execute("SELECT count(*) FROM jobs j JOIN projects p ON j.project_id=p.id WHERE p.account_id=? AND j.kind='runtime-logs' AND j.state IN ('queued','running')",(user['account_id'],)).fetchone()[0]
                if count>=5:raise Problem(429,'Runtime log collection concurrency limit reached')
                # Snapshots are refreshable observations, not permanent release
                # operations. Retain only the two previous terminal receipts.
                db.execute("DELETE FROM jobs WHERE deployment_id=? AND kind='runtime-logs' AND state IN ('completed','failed') AND id NOT IN (SELECT id FROM jobs WHERE deployment_id=? AND kind='runtime-logs' ORDER BY created DESC LIMIT 2)",(deployment_id,deployment_id))
                job_id,now=ident(),time.time()
                db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)",(job_id,release['project_id'],'runtime-logs',deployment_id,now,now))
                job={'id':job_id,'state':'queued','error':None}
            return {'log':cached['content'] if cached else '', 'collected':cached['collected'] if cached else None,
                    'job_id':job['id'],'state':job['state'],'error':job['error']}

    @app.get("/v1/deployments/{deployment_id}/source", tags=["Deployments"])
    def download_source(deployment_id: str, user=Depends(actor)):
        with store.connection() as db:
            row = store.deployment(db, user, deployment_id)
            store.permit(user, "deployments:write", row["project_id"])
        path = settings.root / "artifacts" / f"{deployment_id}.tar.gz"
        if not path.is_file():
            raise Problem(404, "Source archive not uploaded")
        return FileResponse(path, media_type="application/gzip", filename=f"{deployment_id}.tar.gz")

    @app.post("/v1/deployments/{deployment_id}/cancel", status_code=202, tags=["Deployments"])
    def cancel_deployment(deployment_id: str, user=Depends(actor)):
        with store.connection() as db:
            row = store.deployment(db, user, deployment_id, "deployments:write")
            if row["state"] in {"cancelled", "cancelling"}:
                return {"id": deployment_id, "state": row["state"]}
            if row["state"] not in {"uploading", "queued", "building", "deploying"}:
                raise Problem(409, "Release has passed the cancellation boundary; delete an inactive release instead")
            if db.execute("SELECT 1 FROM uploads WHERE deployment_id=?", (deployment_id,)).fetchone():
                raise Problem(409, "An upload is still in progress")
            state = "cancelled" if row["state"] == "uploading" else "cancelling"
            db.execute("UPDATE deployments SET state=?,updated=? WHERE id=?", (state, time.time(), deployment_id))
            store.audit(db, user, "deployment.cancel", deployment_id)
        return {"id": deployment_id, "state": state}

    @app.delete("/v1/deployments/{deployment_id}", status_code=204, tags=["Deployments"],
                responses={202: {"description": "Resource deletion queued; poll job_id before assuming storage is freed"}})
    def delete_deployment(deployment_id: str, user=Depends(actor)):
        with store.connection() as db:
            row = store.deployment(db, user, deployment_id, "deployments:write")
            project = store.project(db, user, row["project_id"], "deployments:write")
            if project["current_deployment"] == deployment_id:
                raise Problem(409, "Promote another release before deleting the production release")
            if db.execute("SELECT 1 FROM uploads WHERE deployment_id=?", (deployment_id,)).fetchone():
                raise Problem(409, "An upload is still in progress")
            pending = db.execute("SELECT id FROM jobs WHERE deployment_id=? AND kind='delete' AND state IN ('queued','running')", (deployment_id,)).fetchone()
            if pending:
                return JSONResponse({"job_id": pending["id"], "deployment_id": deployment_id, "state": "deleting"}, status_code=202)
            if row["state"] not in {"ready", "failed", "uploading", "cancelled", "deleting"}:
                raise Problem(409, "Cancel the active deployment and wait before deleting it")
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running')", (row["project_id"],)).fetchone():
                raise Problem(409, "Project has a pending operation")
            if row["state"] == "uploading":
                store.purge_release(db, deployment_id)
                store.audit(db, user, "deployment.delete", deployment_id)
                return
            now, job_id = time.time(), ident()
            db.execute("UPDATE deployments SET state='deleting',error=NULL,updated=? WHERE id=?", (now, deployment_id))
            db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)", (job_id,row["project_id"],"delete",deployment_id,now,now))
            store.audit(db, user, "deployment.delete", deployment_id)
        return JSONResponse({"job_id": job_id, "deployment_id": deployment_id, "state": "deleting"}, status_code=202)

    def switch_release(project_id, body, user, kind):
        with store.connection() as db:
            project = store.project(db, user, project_id, "deployments:write")
            release = store.deployment(db, user, body.deployment_id, "deployments:write")
            if release["project_id"] != project_id:
                raise Problem(404, "Release does not belong to this project")
            if release["state"] != "ready":
                raise Problem(409, "Only a healthy ready release can receive production traffic")
            if release["runtime_driver"] == SANDBOX_DRIVER:
                raise Problem(409, "The sandbox pilot is preview-only; redeploy to production on the established runtime")
            config=json.loads(release['config'])
            if config['target']=='preview' and (config.get('database_id') or config.get('volume_id')):
                raise Problem(409,'Preview data cannot become production data; publish a production-target release instead')
            if release['preview_access'] and (store.environment(project,'production') or store.unseal(release['environment'])):
                raise Problem(409,'Preview and production use different environment settings; publish a production-target release instead')
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running')", (project_id,)).fetchone():
                raise Problem(409, "Project has a pending operation")
            now, job_id = time.time(), ident()
            db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)", (job_id, project_id, kind, body.deployment_id, now, now))
            store.audit(db, user, kind, body.deployment_id)
        return {"job_id": job_id, "state": "queued"}

    @app.post("/v1/projects/{project_id}/promote", status_code=202, tags=["Deployments"])
    def promote(project_id: str, body: ReleaseInput, user=Depends(actor)):
        return switch_release(project_id, body, user, "promote")

    @app.post("/v1/projects/{project_id}/rollback", status_code=202, tags=["Deployments"])
    def rollback(project_id: str, body: ReleaseInput, user=Depends(actor)):
        return switch_release(project_id, body, user, "rollback")

    @app.get("/v1/jobs/{job_id}", tags=["Deployments"])
    def get_job(job_id: str, user=Depends(actor)):
        with store.connection() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise Problem(404, "Job not found")
            scope = 'deployments:read'
            if job['kind'] == 'delete' and PREVIEW_SCOPE in user['scopes']:
                if not db.execute("SELECT 1 FROM audit WHERE account_id=? AND action='preview.prune' AND object_id=?",
                                  (user['account_id'], job_id)).fetchone():
                    raise Problem(404, 'Preview cleanup job not found')
                scope = PREVIEW_MANAGE_SCOPE
            if job['kind']=='project-delete':scope='projects:write'
            if job['kind']=='project-export':scope='exports:write'
            if job['kind']=='project-import':scope='imports:write'
            if job['kind'].startswith(('database-', 'volume-')):
                from publisher.data_store import DataStore
                scope = DataStore.scope(job['kind'].split('-')[0])
                if PREVIEW_SCOPE in user['scopes']:
                    if not job['preview_access']:
                        raise Problem(404, 'Preview data job not found')
                    scope = PREVIEW_DATA_SCOPE
            store.project(db, user, job["project_id"], scope,allow_inactive=True)
        return {k: job[k] for k in ("id", "kind", "deployment_id", "resource_id", "state", "error", "created", "updated")}

    return app
