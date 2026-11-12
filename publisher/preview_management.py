"""Reservation visibility and explicit, production-safe preview cleanup."""
import json
import re
import time

from fastapi import Depends, Query
from pydantic import Field

from publisher import capacity
from publisher.models import Input
from publisher.store import PREVIEW_MANAGE_SCOPE, PREVIEW_SCOPE, Problem, ident


class PrunePolicy(Input):
    older_than_days: int = Field(default=7, ge=0, le=3650, strict=True)
    keep: int = Field(default=3, ge=0, le=100, strict=True)


class PruneInput(PrunePolicy):
    ids: list[str] = Field(min_length=1, max_length=100)


def inventory(db, project):
    rows = [dict(r) for r in db.execute("SELECT * FROM deployments WHERE project_id=? ORDER BY created DESC,id DESC", (project,))]
    published = {r[0] for r in db.execute("SELECT deployment_id FROM jobs WHERE project_id=? AND kind IN ('promote','rollback')", (project,))}
    pending = {r['deployment_id']: dict(r) for r in db.execute(
        "SELECT id,deployment_id,state FROM jobs WHERE project_id=? AND kind IN ('delete','preview-retention') AND deployment_id IS NOT NULL AND state IN ('queued','running')", (project,))}
    return rows, published, pending


def visible(actor, row, project, published):
    if PREVIEW_SCOPE not in actor['scopes']:
        return True
    return (row['preview_access'] and json.loads(row['config'])['target'] == 'preview'
            and row['id'] != project['current_deployment'] and row['id'] not in published)


def storage(row, uploads):
    return row['artifact_bytes'] + row['unpacked_bytes'] + row['built_bytes'] + (
        uploads.get(row['id'], 0) if row['state'] == 'uploading' else 0)


def view(row, uploads):
    config = json.loads(row['config'])
    return {'id': row['id'], 'state': row['state'], 'created': row['created'],
            'runtime_driver': row['runtime_driver'], 'sleep_configured': config.get('sleep', False),
            'artifact_reserved_bytes': storage(row, uploads), 'runtime_reserved': capacity.release(row)}


def usage(store, actor, project_id):
    with store.connection() as db:
        project = store.project(db, actor, project_id, 'deployments:read', allow_preview=True)
        account = db.execute('SELECT * FROM accounts WHERE id=?', (actor['account_id'],)).fetchone()
        totals = capacity.reservations(db)
        rows, published, _ = inventory(db, project_id)
        uploads = {r[0]: r[1] for r in db.execute('SELECT deployment_id,size FROM source_uploads')}
        releases = [view(r, uploads) for r in rows if visible(actor, r, project, published)]
        summaries = {}
        discover = bool({'*', 'projects:read'} & set(actor['scopes']))
        for row in db.execute("SELECT id,name FROM projects WHERE account_id=? AND state!='deleted'", (actor['account_id'],)):
            if row['id'] != project_id and (not discover or (actor['projects'] is not None and row['id'] not in actor['projects'])):
                continue
            summaries[row['id']] = {'id': row['id'], 'name': row['name'], 'artifact_reserved_bytes': 0,
                                    'runtime_reserved': capacity.empty()}
        for row in db.execute('SELECT d.* FROM deployments d JOIN projects p ON p.id=d.project_id WHERE p.account_id=?', (actor['account_id'],)):
            if row['project_id'] in summaries:
                summary = summaries[row['project_id']]
                summary['artifact_reserved_bytes'] += storage(row, uploads)
                capacity.add(summary['runtime_reserved'], capacity.release(row))
        for row in db.execute("SELECT r.project_id FROM data_resources r JOIN projects p ON p.id=r.project_id WHERE p.account_id=? AND r.kind='database'", (actor['account_id'],)):
            if row['project_id'] in summaries:
                capacity.add(summaries[row['project_id']]['runtime_reserved'], capacity.DATABASE)
        return {'measured_at': time.time(), 'measurement': 'admission_reservations_not_live_cpu_or_rss',
                'project_id': project_id, 'project_name': project['name'],
                'restricted_to_previews': PREVIEW_SCOPE in actor['scopes'],
                'artifact_storage': {'reserved_bytes': store.storage_used(db, actor['account_id']),
                                     'limit_bytes': account['max_storage']},
                'account_runtime': {'reserved': totals['accounts'].get(actor['account_id'], capacity.empty()),
                                    'limit': capacity.ACCOUNT},
                'platform_runtime': {'reserved': totals['platform'], 'limit': capacity.PLATFORM},
                'sandbox_runtime': {'reserved': totals['sandbox'], 'limit': store.settings.sandbox_limits},
                'deployment_slots': {'used': capacity.active_count(db, actor['account_id']), 'limit': account['max_concurrent']},
                'project_slots': {'used': db.execute("SELECT count(*) FROM projects WHERE account_id=? AND state!='deleted'",
                                                    (actor['account_id'],)).fetchone()[0], 'limit': account['max_projects']},
                'projects': list(summaries.values()),
                'releases': releases,
                'data_storage': 'Persistent database and volume files are separate from artifact storage and are never pruned.'}


def protection(row, project, published, keep, cutoff, *, replay=False):
    config = json.loads(row['config'])
    if row['id'] == project['current_deployment'] or row['id'] in published:
        return 'published_or_rollback_release'
    if config['target'] != 'preview' or not row['preview_access']:
        return 'not_a_preview'
    if config.get('database_id') or config.get('volume_id'):
        return 'persistent_data_binding'
    if row['created'] > cutoff:
        return 'too_recent'
    if row['id'] in keep:
        return 'keep_latest_ready'
    if row['state'] not in {'ready', 'failed', 'cancelled', 'deleting'}:
        return 'active_deployment'
    if row['state'] == 'deleting' and not replay:
        return 'cleanup_in_progress'
    return None


def selection(rows, project, published, policy):
    ready = [r['id'] for r in rows if r['state'] == 'ready' and json.loads(r['config'])['target'] == 'preview'
             and r['id'] != project['current_deployment'] and r['id'] not in published]
    return set(ready[:policy.keep]), time.time() - policy.older_than_days * 86400


def plan(store, actor, project_id, policy):
    with store.connection() as db:
        project = store.project(db, actor, project_id, 'deployments:read', allow_preview=True)
        rows, published, pending = inventory(db, project_id)
        keep, cutoff = selection(rows, project, published, policy)
        uploads = {r[0]: r[1] for r in db.execute('SELECT deployment_id,size FROM source_uploads')}
        candidates, protected = [], []
        for row in rows:
            if not visible(actor, row, project, published):
                continue
            reason = protection(row, project, published, keep, cutoff, replay=row['id'] not in pending)
            item = view(row, uploads)
            if reason:
                protected.append(item | {'reason': reason})
            else:
                candidates.append(item)
        reserved = capacity.empty()
        for item in candidates:
            capacity.add(reserved, item['runtime_reserved'])
        return {'project_id': project_id, 'dry_run': True, 'policy': policy.model_dump(),
                'candidates': candidates, 'protected': protected,
                'would_release': {'artifact_bytes': sum(r['artifact_reserved_bytes'] for r in candidates), 'runtime': reserved},
                'can_apply': bool({'*', 'deployments:write', PREVIEW_MANAGE_SCOPE} & set(actor['scopes']))}


def apply(store, actor, project_id, body):
    if len(body.ids) != len(set(body.ids)) or any(not re.fullmatch(r'[a-f0-9]{24}', value) for value in body.ids):
        raise Problem(422, 'Provide distinct 24-character preview IDs')
    needed = PREVIEW_MANAGE_SCOPE if PREVIEW_SCOPE in actor['scopes'] else 'deployments:write'
    if needed not in actor['scopes'] and '*' not in actor['scopes']:
        raise Problem(403, 'Preview cleanup requires previews:manage or deployments:write; use an explicitly authorized management credential')
    store.permit(actor, needed, project_id)
    with store.connection() as db:
        project = store.project(db, actor, project_id, 'deployments:write', allow_preview=True)
        rows, published, pending = inventory(db, project_id)
        lookup = {r['id']: r for r in rows}
        keep, cutoff = selection(rows, project, published, body)
        # Every selected release is rechecked under the same write transaction.
        for release in body.ids:
            row = lookup.get(release)
            if row is None:
                if db.execute('SELECT 1 FROM deployments WHERE id=?', (release,)).fetchone():
                    raise Problem(404, 'Preview does not belong to this project')
                continue
            reason = protection(row, project, published, keep, cutoff, replay=True)
            if reason:
                raise Problem(409, f'Preview {release} is protected: {reason}; refresh the cleanup plan')
            if db.execute('SELECT 1 FROM uploads WHERE deployment_id=?', (release,)).fetchone():
                raise Problem(409, 'A selected preview is still uploading')
        for job in db.execute("SELECT kind,deployment_id FROM jobs WHERE project_id=? AND state IN ('uploading','queued','running')", (project_id,)):
            if job['kind'] not in {'delete','preview-retention'} or job['deployment_id'] not in body.ids:
                raise Problem(409, 'Project has another active operation; wait before pruning')
        result = []
        for release in body.ids:
            if release not in lookup:
                result.append({'id': release, 'state': 'absent', 'job_id': None})
                continue
            if release in pending:
                result.append({'id': release, 'state': 'deleting', 'job_id': pending[release]['id']})
                continue
            now, job = time.time(), ident()
            db.execute("UPDATE deployments SET state='deleting',error=NULL,updated=? WHERE id=?", (now, release))
            db.execute('INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)',
                       (job, project_id, 'delete', release, now, now))
            store.audit(db, actor, 'preview.prune', job)
            result.append({'id': release, 'state': 'deleting', 'job_id': job})
        return {'project_id': project_id, 'dry_run': False, 'releases': result}


def register(app, store, actor):
    @app.get('/v1/projects/{project_id}/usage', tags=['Projects'])
    def get_usage(project_id: str, user=Depends(actor)):
        return usage(store, user, project_id)

    @app.get('/v1/projects/{project_id}/previews/prune', tags=['Deployments'])
    def get_plan(project_id: str, older_than_days: int = Query(7, ge=0, le=3650),
                 keep: int = Query(3, ge=0, le=100), user=Depends(actor)):
        return plan(store, user, project_id, PrunePolicy(older_than_days=older_than_days, keep=keep))

    @app.post('/v1/projects/{project_id}/previews/prune', status_code=202, tags=['Deployments'])
    def prune(project_id: str, body: PruneInput, user=Depends(actor)):
        return apply(store, user, project_id, body)
