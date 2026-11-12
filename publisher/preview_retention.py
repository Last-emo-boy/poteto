"""Opt-in project retention; each durable job removes at most one safe preview."""
import time

from fastapi import Depends
from pydantic import Field

from publisher.backend import BackendError
from publisher.preview_management import PrunePolicy, inventory, plan, protection, selection
from publisher.store import Problem, ident

KIND = 'preview-retention'
INTERVAL = 60


class RetentionPolicy(PrunePolicy):
    older_than_days: int = Field(default=7, ge=1, le=3650, strict=True)
    keep: int = Field(default=3, ge=1, le=100, strict=True)


def candidate(db, project, policy):
    rows, published, _ = inventory(db, project['id'])
    keep, cutoff = selection(rows, project, published, policy)
    for row in reversed(rows):
        if (not protection(row, project, published, keep, cutoff)
                and not db.execute('SELECT 1 FROM uploads WHERE deployment_id=?', (row['id'],)).fetchone()):
            return row
    return None


def status(store, actor, project_id):
    with store.connection() as db:
        store.project(db, actor, project_id, 'deployments:read', allow_preview=True)
        row = db.execute('SELECT * FROM preview_retention WHERE project_id=?', (project_id,)).fetchone()
        policy = RetentionPolicy(older_than_days=row['older_than_days'], keep=row['keep']) if row else RetentionPolicy()
        jobs = [dict(r) for r in db.execute(
            'SELECT j.id,j.state,coalesce(r.deployment_id,j.deployment_id) deployment_id,j.error,j.created,j.updated '
            'FROM jobs j LEFT JOIN preview_retention_runs r ON r.job_id=j.id WHERE j.project_id=? AND j.kind=? '
            'ORDER BY j.created DESC,j.id DESC LIMIT 10', (project_id, KIND))]
    permissions = set(actor['scopes'])
    return {'project_id': project_id, 'enabled': bool(row and row['enabled']), 'policy': policy.model_dump(),
            'last_checked': row['checked_at'] if row else None, 'interval_seconds': INTERVAL,
            'can_manage': '*' in permissions or {'projects:write', 'deployments:write'} <= permissions,
            'recent_jobs': jobs, 'plan': plan(store, actor, project_id, policy)}


def configure(store, actor, project_id, policy=None):
    with store.connection() as db:
        store.project(db, actor, project_id, 'projects:write')
        store.permit(actor, 'deployments:write', project_id)
        if policy is None:
            db.execute('UPDATE preview_retention SET enabled=0,updated=? WHERE project_id=?', (time.time(), project_id))
        else:
            db.execute('INSERT INTO preview_retention(project_id,enabled,older_than_days,keep,updated,checked_at) '
                       'VALUES(?,1,?,?,?,0) ON CONFLICT(project_id) DO UPDATE SET enabled=1, '
                       'older_than_days=excluded.older_than_days,keep=excluded.keep,updated=excluded.updated',
                       (project_id, policy.older_than_days, policy.keep, time.time()))
        store.audit(db, actor, 'retention.disable' if policy is None else 'retention.configure', project_id)
    return status(store, actor, project_id)


def enqueue_due(store):
    if (store.settings.root/'maintenance.json').exists():
        return []
    now, queued = time.time(), []
    with store.connection() as db:
        if (store.settings.root/'maintenance.json').exists():
            return []
        policies = db.execute(
            "SELECT r.*,p.account_id,p.current_deployment FROM preview_retention r JOIN projects p ON p.id=r.project_id "
            "JOIN accounts a ON a.id=p.account_id WHERE r.enabled=1 AND p.state='active' AND a.enabled=1 "
            'AND r.checked_at<=? ORDER BY r.checked_at,r.project_id LIMIT 20', (now-INTERVAL,)).fetchall()
        for row in policies:
            project = {'id': row['project_id'], 'current_deployment': row['current_deployment']}
            db.execute('UPDATE preview_retention SET checked_at=? WHERE project_id=?', (now, project['id']))
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('uploading','queued','running')",
                          (project['id'],)).fetchone():
                continue
            policy = RetentionPolicy(older_than_days=row['older_than_days'], keep=row['keep'])
            if candidate(db, project, policy) is None:
                continue
            job = ident()
            db.execute('INSERT INTO jobs(id,project_id,kind,created,updated) VALUES(?,?,?,?,?)',
                       (job, project['id'], KIND, now, now))
            store.audit(db, {'id': None, 'account_id': row['account_id']}, 'preview.retention', job)
            queued.append(job)
    return queued


def begin(worker, job, deployment):
    """Recheck the current policy at execution; a begun deletion is resumable."""
    store = worker.store
    with store.connection() as db:
        worker.guard(db)
        project = db.execute('SELECT p.* FROM projects p JOIN accounts a ON a.id=p.account_id '
                             "WHERE p.id=? AND p.state='active' AND a.enabled=1", (job['project_id'],)).fetchone()
        if project is None:
            if deployment is not None:
                raise BackendError('Retention project is no longer active; inspect the begun cleanup')
            return None
        if deployment is None:
            row = db.execute('SELECT * FROM preview_retention WHERE project_id=? AND enabled=1', (project['id'],)).fetchone()
            if row is None:
                return None
            policy = RetentionPolicy(older_than_days=row['older_than_days'], keep=row['keep'])
            deployment = candidate(db, project, policy)
            if deployment is None:
                return None
            deployment.update({'app_domain': project['app_domain'], 'preview_mode': project['preview_mode']})
            db.execute('UPDATE jobs SET deployment_id=? WHERE id=?', (deployment['id'], job['id']))
            db.execute('INSERT INTO preview_retention_runs(job_id,deployment_id) VALUES(?,?)', (job['id'], deployment['id']))
            db.execute("UPDATE deployments SET state='deleting',error=NULL,updated=? WHERE id=?",
                       (time.time(), deployment['id']))
            job['deployment_id'] = deployment['id']
        else:
            _, published, _ = inventory(db, project['id'])
            reason = protection(deployment, project, published, set(), float('inf'), replay=True)
            if deployment['state'] != 'deleting' or reason:
                raise BackendError('Retention cleanup is no longer safe; inspect this release before retrying')
        return deployment


def register(app, store, actor):
    @app.get('/v1/projects/{project_id}/previews/retention', tags=['Deployments'])
    def get_policy(project_id: str, user=Depends(actor)):
        return status(store, user, project_id)

    @app.put('/v1/projects/{project_id}/previews/retention', tags=['Deployments'])
    def set_policy(project_id: str, body: RetentionPolicy, user=Depends(actor)):
        return configure(store, user, project_id, body)

    @app.delete('/v1/projects/{project_id}/previews/retention', tags=['Deployments'])
    def disable_policy(project_id: str, user=Depends(actor)):
        return configure(store, user, project_id)
