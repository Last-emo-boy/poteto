"""Recreate ready stateless containers from restored artifacts and metadata."""
import argparse
import json
import os
import re
import time

from publisher.config import Settings
from publisher.store import SANDBOX_DRIVER, Problem, Store, ident


def recover(store, operation, deployments, *, apply=False):
    if not re.fullmatch(r'[a-f0-9]{24}', operation):
        raise ValueError('Use the 24-character restore operation ID')
    if deployments is not None and (not deployments or len(deployments) != len(set(deployments))
                                    or any(not re.fullmatch(r'[a-f0-9]{24}', d) for d in deployments)):
        raise ValueError('Use explicit deployment IDs, or select all restored sandboxes')
    if apply:
        barrier = store.settings.root/'maintenance.json'
        if not barrier.exists() or json.loads(barrier.read_text()).get('operation') != operation:
            raise ValueError('Enter maintenance with this restore operation ID before applying')
    with store.connection() as db:
        selected = {r['id']: dict(r) for r in db.execute('SELECT * FROM deployments WHERE runtime_driver=?', (SANDBOX_DRIVER,))}
        if deployments is not None and not set(deployments) <= selected.keys():
            raise ValueError('A selected deployment is not a recorded sandbox')
        rows = [selected[d] for d in deployments] if deployments is not None else [
            row for row in selected.values() if row['state'] == 'ready' or db.execute(
                "SELECT 1 FROM audit WHERE action='sandbox.restore' AND object_id=?", (operation+':'+row['id'],)).fetchone()]
        result = []
        for row in rows:
            receipt = operation + ':' + row['id']
            done = db.execute("SELECT 1 FROM audit WHERE action='sandbox.restore' AND object_id=?", (receipt,)).fetchone()
            if done:
                job = db.execute("SELECT id,state FROM jobs WHERE deployment_id=? AND kind='deploy' ORDER BY created DESC LIMIT 1", (row['id'],)).fetchone()
                result.append({'id': row['id'], 'state': row['state'], 'already_requested': True,
                               'job_id': job['id'] if job else None})
                continue
            if row['state'] != 'ready':
                raise ValueError('Only ready sandboxes may be reconstructed; recover their existing jobs otherwise')
            source = store.settings.root/'artifacts'/(row['id']+'.tar.gz')
            if not source.is_file():
                raise ValueError('Restore the source archive before reconstructing sandbox '+row['id'])
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running','uploading')", (row['project_id'],)).fetchone():
                raise ValueError('Finish existing operations before reconstructing this project')
            result.append({'id': row['id'], 'state': 'planned'})
        if apply:
            for item in result:
                if item['state'] != 'planned':
                    continue
                row = selected[item['id']];now = time.time();job = ident()
                db.execute("UPDATE deployments SET state='queued',error=NULL,updated=? WHERE id=?", (now,row['id']))
                db.execute('INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)',
                           (job,row['project_id'],'deploy',row['id'],now,now))
                account = db.execute('SELECT account_id FROM projects WHERE id=?',(row['project_id'],)).fetchone()[0]
                store.audit(db, {'account_id':account,'id':None}, 'sandbox.restore', operation+':'+row['id'])
                item.update(state='queued',job_id=job)
    return {'operation':operation,'dry_run':not apply,'deployments':result}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation',required=True)
    selection=parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--all',action='store_true')
    selection.add_argument('--deployment',action='append')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0:
        parser.error('Run this recovery command as controller root')
    try:
        settings = Settings.from_env()
        if not settings.db.is_file():
            raise ValueError('Restore the control database before recovering sandboxes')
        print(json.dumps(recover(Store(settings),args.operation,args.deployment,apply=args.apply),indent=2))
    except (ValueError,Problem) as error:
        raise SystemExit(str(error)) from None


if __name__=='__main__':
    main()
