"""Trusted Compose definitions for data; source archives never supply Docker options."""
import json
import time
from publisher.backend import BackendError
from publisher.runtime import RuntimeRegistry
from publisher.executor import ExecutionFailure
from publisher.store import NATIVE_DATABASE_DRIVER


class DataBackend:
    def __init__(self, backend):
        self.backend=backend
        self.store=backend.store
        self.client=backend.execution_client()

    def account(self,project_id):
        with self.store.connection() as db:
            return db.execute('SELECT account_id FROM projects WHERE id=?',(project_id,)).fetchone()[0]

    def request(self,resource,action,**fields):
        return self.client.data(self.account(resource['project_id']),resource['id'],resource['project_id'],action,**fields)

    def wait_for_database_container(self, resource, compose, check):
        # Image transfer is a supervised deployment stage, not database health.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            check()
            if self.request(resource, 'inspect-database')['container']:
                return
            operations = self.backend.api('deployment.allByCompose', query={'composeId': compose})
            states = {row.get('status') for row in operations}
            if states - {'queued', 'running', 'done', 'error'}:
                raise BackendError('PostgreSQL deployment state is unknown; inspect its existing operation')
            if states and not states & {'queued', 'running'}:
                raise BackendError('PostgreSQL deployment ended without a container; inspect its existing operation')
            time.sleep(2)
        raise BackendError('PostgreSQL container launch did not complete; inspect the existing deployment before retrying')

    def create(self,resource,check,*,import_id=None):
        account=self.account(resource['project_id'])
        if resource.get('runtime_driver') == NATIVE_DATABASE_DRIVER:
            if import_id:
                raise BackendError('Native database imports are not enabled yet')
            check()
            credentials=self.store.unseal(resource['definition'])
            result=self.client.native_database(account,resource['id'],resource['project_id'],'ensure',**credentials)
            check()
            return result
        target=RuntimeRegistry(self.backend).ensure(account)
        check()
        labels={'paas.publisher.import':import_id} if import_id else {}
        self.request(resource,'ensure-volume',kind=resource['kind'],**({'import_id':import_id} if import_id else {}))
        if resource['kind']=='volume':
            return target
        rid=resource['id']
        credentials=self.store.unseal(resource['definition'])
        definition={
            'services':{'postgres':{
                'image':'postgres:16-alpine','restart':'unless-stopped','mem_limit':'256m','cpus':0.25,
                'pids_limit':128,'security_opt':['no-new-privileges:true'],
                'environment':{'POSTGRES_DB':'app','POSTGRES_USER':'paas_admin','POSTGRES_PASSWORD':credentials['admin_password']},
                'command':['postgres','-c','max_connections=20','-c','shared_buffers=32MB','-c','work_mem=2MB','-c','maintenance_work_mem=16MB','-c','max_wal_size=128MB'],
                'volumes':['data:/var/lib/postgresql/data'],'networks':{'private':{'aliases':['db-'+rid]}},
                'labels':{'paas.publisher.resource':rid,'paas.publisher.project':resource['project_id'],'paas.publisher.kind':'database','traefik.enable':'false'}|labels,
                'healthcheck':{'test':['CMD','pg_isready','-h','127.0.0.1','-U','paas_admin','-d','app'],'interval':'2s','timeout':'3s','retries':45,'start_period':'3s'},
                'logging':{'driver':'json-file','options':{'max-size':'5m','max-file':'2'}}}},
            'volumes':{'data':{'external':True,'name':'paas-data-'+rid}},
            'networks':{'private':{'name':'paas-data-'+rid,'internal':True,'labels':{'paas.publisher.resource':rid}}}}
        compose=self.backend.create_compose('pubdb-'+rid,definition,target['server_id'])
        metadata={'account_id':account,'server_id':target['server_id'],'compose_id':compose}
        with self.store.connection() as db:
            db.execute('UPDATE data_resources SET backend=? WHERE id=?',(json.dumps(metadata),rid))
        current=self.request(resource,'inspect-database')['container']
        check()
        if not current:
            self.backend.start_compose(compose,'Managed PostgreSQL '+rid)
            self.wait_for_database_container(resource, compose, check)
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            check()
            if self.request(resource,'database-role',app_password=credentials['app_password'])['ready']:
                return metadata
            time.sleep(2)
        raise BackendError('PostgreSQL did not become ready; existing volume was preserved')

    def remove(self,resource,check):
        if resource.get('runtime_driver') == NATIVE_DATABASE_DRIVER:
            check()
            return self.client.native_database(self.account(resource['project_id']),resource['id'],resource['project_id'],'delete')
        if self.store.unseal(resource['definition']).get('import_id'):
            self.request(resource,'restore-idle')
        candidates=self.backend.api('project.one',query={'projectId':self.backend.config['project_id']})
        found=False
        for env in candidates.get('environments',[]):
            for compose in env.get('compose',[]):
                if compose.get('name')=='pubdb-'+resource['id']:
                    found=True
                    self.backend.api('compose.delete',{'composeId':compose['composeId']})
        check()
        if not resource['backend'] and not found:
            try:
                status=self.client.runtime_status(self.account(resource['project_id']))
            except ExecutionFailure as error:
                if error.status!=404:raise
                return
            if not status['disk_exists']:
                return
        self.request(resource,'destroy')

    def attach(self,deployment,definition):
        config=json.loads(deployment['config'])
        service=definition['services']['app']
        if config.get('database_id'):
            key='data-'+config['database_id']
            definition['networks'][key]={'external':True,'name':'paas-data-'+config['database_id']}
            service['networks'].append(key)
        if config.get('volume_id'):
            definition.setdefault('volumes',{})['persistent']={'external':True,'name':'paas-data-'+config['volume_id']}
            service['volumes'].append('persistent:/data')

    def migrate(self,deployment,account,check,log=None):
        config=json.loads(deployment['config'])
        environment=self.store.unseal(deployment['environment'])
        deadline=time.monotonic()+300
        while time.monotonic()<deadline:
            if check:
                check()
            result=self.client.data(account,config['database_id'],deployment['project_id'],'migrate',
                release_id=deployment['id'],command=config['migration_command'],environment=environment,
                memory_mb=config['memory_mb'],cpus=config['cpus'],volume_id=config.get('volume_id'))
            if log and result.get('log'):
                for line in result['log'].splitlines():
                    log('migration: '+line)
            if result['state']=='succeeded':
                return
            if result['state']=='failed':
                raise BackendError('Database migration failed; production traffic was not switched and data was retained')
            time.sleep(2)
        self.client.data(account,config['database_id'],deployment['project_id'],'stop-migration',release_id=deployment['id'])
        raise BackendError('Database migration exceeded 300 seconds; inspect the existing migration before retrying')
