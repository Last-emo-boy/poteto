"""Single durable worker, separate from the unprivileged HTTP process."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import threading
import time
import traceback
import urllib.parse
from publisher.archive import unpack
from publisher.backend import BackendError, Dokploy
from publisher.config import Settings
from publisher.controlflow import Cancelled, LeaseLost, ObservationUnavailable
from publisher.executor import ExecutionFailure
from publisher.store import ARTIFACT_DRIVER, SANDBOX_DRIVER, Store, ident


class Worker:
    def __init__(self, store, backend, *, logs_only=False, static_previews_only=False):
        self.store, self.backend = store, backend
        self.backend.store = store
        self.owner = socket.gethostname() + "-" + ident()
        self.job = None
        self.last_maintenance = None
        self.logs_only = logs_only
        self.static_previews_only = static_previews_only

    def guard(self, db, cancel=True):
        if self.job is None:
            return
        current = db.execute('SELECT * FROM jobs WHERE id=?', (self.job['id'],)).fetchone()
        if not current or current['owner'] != self.owner or current['state'] != 'running' or current['lease_until'] <= time.time():
            raise LeaseLost('Worker no longer owns this operation')
        if cancel and current['kind'] == 'deploy':
            row = db.execute('SELECT state FROM deployments WHERE id=?', (current['deployment_id'],)).fetchone()
            if row and row['state'] == 'cancelling':
                raise Cancelled('Deployment cancelled by its owner')

    def check(self):
        with self.store.connection() as db:
            self.guard(db)

    def log(self, deployment, message):
        if deployment is None:
            print('Job ' + self.job['id'] + ': ' + message, flush=True)
            return
        self.store.log(deployment['id'],self.redact(deployment,message))

    def redact(self, deployment, message):
        environment=self.store.unseal(deployment['environment'])
        values = list(environment.values())
        if environment.get('DATABASE_URL'):
            values.append(urllib.parse.urlsplit(environment['DATABASE_URL']).password)
        for value in sorted((v for v in values if v), key=len, reverse=True):
            message = message.replace(value, "[REDACTED]")
        return message

    def state(self, deployment_id, state, **fields):
        allowed = {"backend_id", "error"}
        if not set(fields) <= allowed:
            raise ValueError("Invalid deployment update")
        assignments = ["state=?", "updated=?"] + [f"{key}=?" for key in fields]
        with self.store.connection() as db:
            self.guard(db, cancel=state not in {'cancelled', 'failed'})
            db.execute("UPDATE deployments SET " + ",".join(assignments) + " WHERE id=?", (state, time.time(), *fields.values(), deployment_id))

    def finish(self, job, error=None):
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET state=?,error=?,updated=?,lease_until=NULL WHERE id=? AND owner=?", ("failed" if error else "completed", error, time.time(), job["id"], self.owner))

    def heartbeat(self, job, stop, errors):
        while not stop.wait(15):
            try:
                self.store.heartbeat(job["id"], self.owner)
            except Exception as error:
                errors.append(error)
                return

    def run_once(self):
        if not (self.logs_only or self.static_previews_only) and (
                self.last_maintenance is None or time.monotonic() - self.last_maintenance > 60):
            self.store.expire_uploads()
            from publisher.project_exports import expire
            expire(self.store)
            from publisher.project_imports import expire as expire_imports
            expire_imports(self.store)
            from publisher.preview_retention import enqueue_due
            enqueue_due(self.store)
            self.last_maintenance = time.monotonic()
        job = self.store.claim(self.owner, logs_only=self.logs_only,
                               static_previews_only=self.static_previews_only)
        if not job:
            return False
        self.job = job
        stop, errors = threading.Event(), []
        thread = threading.Thread(target=self.heartbeat, args=(job, stop, errors), daemon=True)
        thread.start()
        with self.store.connection() as db:
            resource=dict(db.execute('SELECT * FROM data_resources WHERE id=?',(job['resource_id'],)).fetchone()) if job['resource_id'] else None
            deployment = dict(db.execute("SELECT d.*,p.app_domain,p.preview_mode FROM deployments d JOIN projects p ON p.id=d.project_id WHERE d.id=?",
                                         (job["deployment_id"],)).fetchone()) if job['deployment_id'] else None
        try:
            self.check()
            if job["attempts"] > 3:
                raise BackendError("Operation recovery limit exceeded; inspect logs before retrying")
            if job['kind'] == 'preview-retention':
                from publisher.preview_retention import begin
                deployment = begin(self, job, deployment)
                if deployment is None:
                    self.finish(job)
                    return True
            if job['kind']=='runtime-logs':
                text=self.redact(deployment,self.backend.runtime_logs(deployment))
                text=text.encode()[-65536:].decode(errors='replace')
                with self.store.connection() as db:
                    self.guard(db)
                    db.execute('INSERT INTO runtime_logs VALUES(?,?,?) ON CONFLICT(deployment_id) DO UPDATE SET content=excluded.content,collected=excluded.collected',(deployment['id'],text,time.time()))
            elif job['kind']=='project-delete':
                self.delete_project(job)
                return True
            elif job['kind']=='project-export':
                from publisher.project_exports import execute
                execute(self,job,deployment)
                return True
            elif job['kind']=='project-import':
                from publisher.project_imports import execute
                execute(self,job)
                return True
            elif job['kind']=='project-import-cleanup':
                deployment=None
                from publisher.project_imports import cleanup
                cleanup(self,job)
                return True
            elif resource:
                if self.data_job(job, resource):
                    return True
            elif job["kind"] == "deploy":
                if job['skip_migration'] and deployment.get('runtime_driver')==SANDBOX_DRIVER:
                    self.deploy(job,deployment,imported={'skip_migration':True})
                else:
                    self.deploy(job, deployment)
            elif job["kind"] in {"promote", "rollback"}:
                self.check()
                self.backend.promote(deployment)
                self.commit_production(deployment)
                self.log(deployment, job["kind"] + " completed")
            elif job['kind'] in {'delete','preview-retention'}:
                self.backend.remove(deployment)
                self.check()
                (self.store.settings.root / 'artifacts' / (deployment['id'] + '.tar.gz')).unlink(missing_ok=True)
                with self.store.connection() as db:
                    self.guard(db)
                    self.store.purge_release(db, deployment['id'])
                    db.execute("UPDATE jobs SET state='completed',updated=?,lease_until=NULL WHERE id=? AND owner=?", (time.time(),job['id'],self.owner))
                return True
            else:
                raise BackendError("Unsupported queued operation")
            if errors:
                self.check()
            self.finish(job)
        except LeaseLost:
            print(f"Job {job['id']} lease transferred; old owner stopped", flush=True)
        except ObservationUnavailable as error:
            with self.store.connection() as db:
                try:
                    self.guard(db, cancel=False)
                except LeaseLost:
                    return True
                if job['attempts'] < 3:
                    db.execute("UPDATE jobs SET state='queued',owner=NULL,lease_until=?,error=?,updated=? WHERE id=?", (time.time()+10,str(error),time.time(),job['id']))
                else:
                    db.execute("UPDATE jobs SET state='failed',lease_until=NULL,error=?,updated=? WHERE id=?", (str(error),time.time(),job['id']))
                    if job['kind'] == 'deploy':
                        db.execute("UPDATE deployments SET state='failed',error=?,updated=? WHERE id=?", ('Execution unavailable after observation retries; use retry to reconcile the existing operation',time.time(),deployment['id']))
                    elif resource:
                        db.execute('UPDATE data_resources SET state=?,error=?,updated=? WHERE id=?',('deleting' if job['kind'].endswith('-delete') else 'failed',str(error),time.time(),resource['id']))
                    elif job['kind']=='project-import':
                        from publisher.project_imports import fail_release
                        fail_release(db,job['id'],str(error))
            self.log(deployment, str(error))
        except Cancelled:
            try:
                self.backend.cancel(deployment)
                self.state(deployment['id'], 'cancelled', error=None, backend_id=None)
                self.log(deployment, 'Deployment cancelled; build and runtime resources removed')
                self.finish(job)
            except LeaseLost:
                print(f"Job {job['id']} lease transferred during cancellation", flush=True)
            except Exception:
                traceback.print_exc()
                message = 'Cancellation cleanup failed; retry deletion to reconcile remaining resources'
                self.state(deployment['id'], 'failed', error=message)
                self.finish(job, message)
        except Exception as error:
            # Only domain errors are safe to expose; unexpected exceptions stay in systemd logs.
            message = str(error) if isinstance(error, (BackendError, ExecutionFailure, ValueError)) else "Worker operation failed; administrator inspection required"
            if not isinstance(error, (BackendError, ExecutionFailure, ValueError)):
                traceback.print_exc()
            # An external observation can return after another worker recovered
            # the lease. That old response must not overwrite the new owner.
            try:
                with self.store.connection() as db:
                    self.guard(db, cancel=False)
            except LeaseLost:
                return True
            self.log(deployment, message)
            if job["kind"] == "deploy":
                self.state(deployment["id"], "failed", error=message)
            elif job['kind'] in {'delete','preview-retention'} and deployment is not None:
                self.state(deployment['id'], 'deleting', error=message)
            elif job['kind']=='project-import':
                from publisher.project_imports import fail_release
                with self.store.connection() as db:
                    self.guard(db);fail_release(db,job['id'],message)
            elif resource:
                with self.store.connection() as db:
                    self.guard(db)
                    db.execute('UPDATE data_resources SET state=?,error=?,updated=? WHERE id=?',('deleting' if job['kind'].endswith('-delete') else 'failed',message,time.time(),resource['id']))
            self.finish(job, message)
            print(f"Job {job['id']} failed ({type(error).__name__})", flush=True)
        finally:
            stop.set()
            thread.join(timeout=20)
            self.job = None
        return True

    def commit_production(self, deployment):
        with self.store.connection() as db:
            self.guard(db)
            db.execute("UPDATE projects SET current_deployment=? WHERE id=?", (deployment["id"], deployment["project_id"]))
            db.execute("UPDATE deployments SET preview_access=0 WHERE id=?", (deployment["id"],))

    def delete_project(self, job):
        project_id=job['project_id']
        from publisher.project_imports import remove_work
        with self.store.connection() as db:
            self.guard(db)
            for row in db.execute('SELECT i.id FROM project_imports i JOIN jobs j ON j.id=i.id WHERE j.project_id=?',(project_id,)):
                remove_work(self.store,row['id'])
        self.check();self.backend.remove_project_route(project_id)
        with self.store.connection() as db:
            self.guard(db)
            db.execute('UPDATE projects SET current_deployment=NULL WHERE id=?',(project_id,))
            releases=[dict(r) for r in db.execute('SELECT d.*,p.app_domain,p.preview_mode FROM deployments d JOIN projects p ON p.id=d.project_id WHERE d.project_id=? ORDER BY d.created',(project_id,))]
        for release in releases:
            self.check()
            if release['state']!='uploading':self.backend.remove(release)
            self.check()
            (self.store.settings.root/'artifacts'/(release['id']+'.tar.gz')).unlink(missing_ok=True)
            with self.store.connection() as db:
                self.guard(db);self.store.purge_release(db,release['id'])
        with self.store.connection() as db:
            resources=[dict(r) for r in db.execute('SELECT * FROM data_resources WHERE project_id=? ORDER BY created',(project_id,))]
        if resources:
            from publisher.data_backend import DataBackend
            data=DataBackend(self.backend)
            for resource in resources:
                self.check()
                with self.store.connection() as db:
                    self.guard(db)
                    db.execute("UPDATE data_resources SET state='deleting' WHERE id=?",(resource['id'],))
                data.remove(resource,self.check)
                with self.store.connection() as db:
                    self.guard(db)
                    db.execute('UPDATE jobs SET resource_id=NULL WHERE resource_id=?',(resource['id'],))
                    db.execute('DELETE FROM data_resources WHERE id=?',(resource['id'],))
        with self.store.connection() as db:
            self.guard(db)
            if db.execute('SELECT 1 FROM deployments WHERE project_id=?',(project_id,)).fetchone() or db.execute('SELECT 1 FROM data_resources WHERE project_id=?',(project_id,)).fetchone():
                raise BackendError('Project still owns resources; cleanup did not complete')
            from publisher.project_exports import purge_project
            purge_project(db,self.store,project_id)
            from publisher.project_imports import purge_project as purge_imports
            purge_imports(db,self.store,project_id)
            db.execute('DELETE FROM preview_retention WHERE project_id=?', (project_id,))
            # Keep only ownership for durable job receipts. Release the public
            # name/quota and erase mutable project secrets after actual cleanup.
            db.execute("UPDATE projects SET state='deleted',name=?,environment=?,preview_environment=NULL,current_deployment=NULL WHERE id=?",('~deleted:'+project_id,self.store.seal({}),project_id))
            db.execute("UPDATE jobs SET state='completed',updated=?,lease_until=NULL WHERE id=? AND owner=?",(time.time(),job['id'],self.owner))

    def data_job(self,job,resource):
        from publisher.data_backend import DataBackend
        data=DataBackend(self.backend)
        if job['kind'].endswith('-create'):
            with self.store.connection() as db:
                self.guard(db)
                db.execute("UPDATE data_resources SET state='provisioning',updated=? WHERE id=?",(time.time(),resource['id']))
            metadata=data.create(resource,self.check)
            with self.store.connection() as db:
                self.guard(db)
                db.execute("UPDATE data_resources SET state='ready',backend=?,error=NULL,updated=? WHERE id=?",(json.dumps(metadata),time.time(),resource['id']))
        elif job['kind'].endswith('-delete'):
            data.remove(resource,self.check)
            with self.store.connection() as db:
                self.guard(db)
                db.execute('UPDATE jobs SET resource_id=NULL WHERE resource_id=?',(resource['id'],))
                db.execute('DELETE FROM data_resources WHERE id=?',(resource['id'],))
                db.execute("UPDATE jobs SET state='completed',updated=?,lease_until=NULL WHERE id=? AND owner=?",(time.time(),job['id'],self.owner))
            return True
        else:
            raise BackendError('Unsupported managed data job')

    def deploy(self, job, deployment, *, imported=None):
        config = json.loads(deployment["config"])
        if deployment['state'] == 'activating':
            # A crash after switching traffic must not reopen cancellation or
            # remove a release that may already be serving production.
            self.log(deployment, 'Reconciling the committed production activation boundary')
            self.backend.promote(deployment)
            self.commit_production(deployment)
            self.state(deployment['id'], 'ready', error=None)
            return
        # Recover a submitted release without resubmitting or rebuilding a live Dokploy operation.
        artifact = deployment.get('runtime_driver') == ARTIFACT_DRIVER
        sandbox = deployment.get('runtime_driver') == SANDBOX_DRIVER
        if sandbox:
            self.state(deployment['id'], 'building')
            archive = self.store.settings.root / 'artifacts' / f"{deployment['id']}.tar.gz"
            self.backend.publish_sandbox(deployment, archive, lambda text: self.log(deployment, text), self.check,
                                         before_migrate=lambda:self.state(deployment['id'],'migrating'),
                                         skip_migration=bool(imported and imported.get('skip_migration')))
            self.state(deployment['id'], 'deploying', backend_id=SANDBOX_DRIVER)
        elif artifact:
            archive = self.store.settings.root / 'artifacts' / f"{deployment['id']}.tar.gz"
            if config.get('build_command'):
                self.state(deployment['id'], 'building')
                restored = (imported or {}).get('built_path')
                self.log(deployment, 'Restoring built static files; no build or runtime VM' if restored else
                         'Building static files for direct hosting; no runtime VM')
                self.backend.publish_artifact(deployment, archive, self.check,
                    log=lambda text: self.log(deployment, text), restored=restored)
            else:
                self.log(deployment, 'Publishing prebuilt static files; no build or runtime VM')
                self.backend.publish_artifact(deployment, archive, self.check)
            self.state(deployment['id'], 'deploying', backend_id=ARTIFACT_DRIVER)
        elif deployment["backend_id"]:
            self.log(deployment, "Reconciling the existing deployment after worker restart")
            self.backend.reconcile_release(deployment)
        else:
            if deployment['state']!='migrating':
                self.state(deployment["id"], "building")
            if self.backend.config.get('execution_enabled') is True:
                definition = self.backend.prepare_remote(deployment, self.store.settings.root / 'artifacts' / f"{deployment['id']}.tar.gz",
                                                         lambda text: self.log(deployment, text), check=self.check,
                                                         before_migrate=lambda:self.state(deployment['id'],'migrating'),
                                                         **({'imported':imported} if imported is not None else {}))
            else:
                if config["runtime"] != "static" or config["build_command"]:
                    raise BackendError("Isolated source builds are not connected yet; upload prebuilt static output for this release")
                root = self.backend.releases / deployment["id"]
                root.mkdir(exist_ok=True, mode=0o755)
                source = root / "source"
                pending = root / "source.next"
                if pending.exists():
                    shutil.rmtree(pending)
                pending.mkdir(mode=0o755)
                unpack(self.store.settings.root / "artifacts" / f"{deployment['id']}.tar.gz", pending,
                       max_bytes=self.store.settings.unpack_limit, max_files=self.store.settings.file_limit)
                if source.exists():
                    shutil.rmtree(source)
                pending.replace(source)
                self.log(deployment, "Validated static artifact; preparing immutable release")
                service, port = self.backend.prepare_static(deployment, source)
                definition = self.backend.render(deployment, service, port)
            compose_id = self.backend.create_release(deployment, definition)
            deployment["backend_id"] = compose_id
            self.state(deployment["id"], "deploying", backend_id=compose_id)
            self.backend.deploy(deployment, compose_id)
        self.state(deployment["id"], "deploying")
        self.backend.health(deployment, check=self.check)
        if config["sleep"] and not artifact and not sandbox and not self.backend.target(deployment["id"]):
            self.backend.refresh_wake()
        self.log(deployment, "HTTP health check passed")
        self.backend.public_health(deployment, check=self.check)
        if config["target"] == "production":
            # Serialize the cancellation cutoff with the API. Once activating,
            # production switching is reconciled rather than interrupted.
            self.state(deployment['id'], 'activating')
            self.backend.promote(deployment)
            self.commit_production(deployment)
        self.state(deployment["id"], "ready", error=None)
        self.log(deployment, "Release ready")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    lane = parser.add_mutually_exclusive_group()
    lane.add_argument('--logs-only',action='store_true')
    lane.add_argument('--static-previews-only',action='store_true')
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(settings)
    config = json.loads(Path(os.environ["PAAS_BACKEND_CONFIG"]).read_text())
    worker = Worker(store, Dokploy(config, settings), logs_only=args.logs_only,
                    static_previews_only=args.static_previews_only)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.is_set():
        worked = worker.run_once()
        if args.once:
            break
        if not worked:
            stopping.wait(2)


if __name__ == "__main__":
    main()
