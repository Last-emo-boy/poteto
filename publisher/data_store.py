"""Owned data resources and immutable release bindings."""
import json
import secrets
import time
from publisher.store import Problem, ident


class DataStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def scope(kind):
        return 'databases:write' if kind == 'database' else 'storage:write'

    def resource(self, db, actor, resource_id, kind=None, scope=None):
        row = db.execute('SELECT * FROM data_resources WHERE id=?', (resource_id,)).fetchone()
        if not row or (kind and row['kind'] != kind):
            raise Problem(404, 'Data resource not found')
        self.store.project(db, actor, row['project_id'], scope or self.scope(row['kind']))
        return dict(row)

    def create(self, actor, project_id, kind, stage):
        with self.store.connection() as db:
            self.store.project(db, actor, project_id, self.scope(kind))
            resource_id=self.allocate(db,actor['account_id'],project_id,kind,stage)
            job_id,now=ident(),time.time()
            db.execute('INSERT INTO jobs(id,project_id,kind,resource_id,created,updated) VALUES(?,?,?,?,?,?)',(job_id,project_id,kind+'-create',resource_id,now,now))
            self.store.audit(db,actor,kind+'.create',resource_id)
        return {'id':resource_id,'project_id':project_id,'kind':kind,'stage':stage,'state':'queued','job_id':job_id}

    def allocate(self,db,account,project,kind,stage,*,import_id=None):
        if db.execute('SELECT 1 FROM data_resources WHERE project_id=? AND kind=? AND stage=?',(project,kind,stage)).fetchone():
            raise Problem(409,'This project already has that data resource; inspect or retry it')
        count=db.execute('SELECT count(*) FROM data_resources r JOIN projects p ON r.project_id=p.id WHERE p.account_id=? AND r.kind=?',(account,kind)).fetchone()[0]
        if count>=(2 if kind=='database' else 3):raise Problem(429,'Account data resource quota reached')
        if kind=='database':self.store.check_runtime_capacity(db,account,{'memory_mb':256,'cpus':0.25})
        resource,now=ident(),time.time()
        definition={'admin_password':secrets.token_urlsafe(36),'app_password':secrets.token_urlsafe(36)} if kind=='database' else {}
        if import_id:definition['import_id']=import_id
        db.execute('INSERT INTO data_resources VALUES(?,?,?,?,?,?,?,?,?,?)',(resource,project,kind,stage,'restoring' if import_id else 'queued',self.store.seal(definition),None,None,now,now))
        return resource

    def bind_resources(self,db,project_id,config,environment):
        for key, kind in [('database_id','database'),('volume_id','volume')]:
            resource_id = config.get(key)
            if not resource_id:
                continue
            resource=db.execute('SELECT * FROM data_resources WHERE id=? AND kind=? AND project_id=?',(resource_id,kind,project_id)).fetchone()
            if resource is None:
                raise Problem(404,'Data resource does not belong to this project')
            if resource['stage'] != config['target']:
                raise Problem(409,'Release target and data stage must match; preview cannot use production data')
            if resource['state'] != 'ready':
                raise Problem(409,'Data resource is not ready')
            if kind == 'database':
                password = self.store.unseal(resource['definition'])['app_password']
                environment['DATABASE_URL'] = f'postgresql://app:{password}@db-{resource_id}:5432/app'
            else:
                environment['PAAS_DATA_DIR'] = '/data'
        return environment

    def queue(self, actor, resource_id, operation, destroy_data=False):
        with self.store.connection() as db:
            resource = self.resource(db,actor,resource_id)
            active = db.execute("SELECT id FROM jobs WHERE resource_id=? AND state IN ('queued','running')",(resource_id,)).fetchone()
            if active:
                raise Problem(409,'Data resource has an active operation')
            if operation == 'delete':
                if not destroy_data:
                    raise Problem(409,'Deletion destroys stored data; pass destroy_data=true explicitly')
                # Retained releases are also rollback targets; keep their data usable.
                references = db.execute("SELECT config FROM deployments WHERE project_id=?",(resource['project_id'],)).fetchall()
                if any(resource_id in (json.loads(r['config']).get('database_id'),json.loads(r['config']).get('volume_id')) for r in references):
                    raise Problem(409,'Delete all releases bound to this resource before destroying its data')
            elif resource['state'] != 'failed':
                raise Problem(409,'Only a failed resource can be retried')
            job_id, now = ident(), time.time()
            state = 'deleting' if operation == 'delete' else 'queued'
            db.execute('UPDATE data_resources SET state=?,error=NULL,updated=? WHERE id=?',(state,now,resource_id))
            db.execute('INSERT INTO jobs(id,project_id,kind,resource_id,created,updated) VALUES(?,?,?,?,?,?)',(job_id,resource['project_id'],resource['kind']+('-delete' if operation=='delete' else '-create'),resource_id,now,now))
            self.store.audit(db,actor,resource['kind']+'.'+operation,resource_id)
        return {'job_id':job_id,'id':resource_id,'state':state}

    @staticmethod
    def view(row):
        return {key:row[key] for key in ['id','project_id','kind','stage','state','error','created','updated']} | {
            'engine':'postgresql-16' if row['kind']=='database' else 'filesystem',
            'persistence':'independent of release deletion and app sleep',
            'disk_limit':{'scope':'shared tenant VM','gib':12},
            'connection':'DATABASE_URL injected into explicitly bound dynamic releases' if row['kind']=='database' else 'Mounted at /data; PAAS_DATA_DIR injected'}
