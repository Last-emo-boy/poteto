"""Owned, encrypted, point-in-time project bundles."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from fastapi import Depends
from fastapi.responses import Response
from pydantic import Field
from publisher.backend import BackendError
from publisher.data_store import DataStore
from publisher.models import Input
from publisher.project_bundle import BUNDLE_LIMIT,FORMAT
from publisher.store import Problem, ident

AGE=Path('/opt/paas-publisher/age')
LIMIT=BUNDLE_LIMIT
CHUNK=8*1024**2
TTL=86400
MANAGED={'DATABASE_URL','PORT','PAAS_DATA_DIR','PAAS_DEPLOYMENT_URL','PAAS_PROJECT_URL'}


class ExportInput(Input):
    recipient: str=Field(pattern=r'^age1[023456789acdefghjklmnpqrstuvwxyz]{58}$')
    quiesce: bool=False


def directory(store):return store.settings.root/'exports'


def artifact(store,operation):return directory(store)/(operation+'.zip.age')


def view(row):
    return {key:row[key] for key in ['id','project_id','state','size','expires','error']}|{'job_id':row['id']}


def export_row(db,store,actor,operation):
    row=db.execute('SELECT e.*,j.state,j.error,j.project_id FROM project_exports e JOIN jobs j ON j.id=e.id WHERE e.id=?',(operation,)).fetchone()
    if row is None:raise Problem(404,'Export not found')
    project=store.project(db,actor,row['project_id'],'exports:write',allow_inactive=True)
    if project['state']!='active':raise Problem(404,'Export not found')
    for scope in json.loads(row['scopes']):store.permit(actor,scope,project['id'])
    if row['expires']<=time.time() and row['state'] not in {'queued','running'}:raise Problem(410,'Export has expired')
    return row


def create(store,actor,project_id,body):
    with store.connection() as db:
        project=store.project(db,actor,project_id,'exports:write')
        scopes={'deployments:write','env:write'}
        resources=db.execute('SELECT kind,state FROM data_resources WHERE project_id=?',(project_id,)).fetchall()
        scopes.update(DataStore.scope(r['kind']) for r in resources)
        for scope in scopes:store.permit(actor,scope,project_id)
        if resources and not body.quiesce:
            raise Problem(409,'Set quiesce=true to allow the project apps to pause during the data snapshot (up to six minutes)')
        if any(r['state']!='ready' for r in resources):raise Problem(409,'Finish or remove incomplete data resources before export')
        release=db.execute("SELECT id FROM deployments WHERE id=? AND project_id=? AND state='ready'",(project['current_deployment'],project_id)).fetchone()
        if not release:raise Problem(409,'Publish a ready production release before exporting this project')
        if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running')",(project_id,)).fetchone():
            raise Problem(409,'Finish the current project operation before exporting')
        if db.execute('SELECT 1 FROM uploads u JOIN deployments d ON d.id=u.deployment_id WHERE d.project_id=?',(project_id,)).fetchone():
            raise Problem(409,'A project upload is still running')
        now=time.time()
        count=db.execute("SELECT count(*) FROM project_exports e JOIN jobs j ON j.id=e.id JOIN projects p ON p.id=j.project_id WHERE p.account_id=? AND (j.state IN ('queued','running') OR (e.size>0 AND e.expires>?))",(actor['account_id'],now)).fetchone()[0]
        if count>=2:raise Problem(429,'Account has two active or downloadable exports; remove one before starting another')
        if not AGE.is_file():raise Problem(503,'Encrypted export is unavailable until the operator installs age')
        operation=ident()
        db.execute("INSERT INTO jobs(id,project_id,kind,deployment_id,created,updated) VALUES(?,?,?,?,?,?)",(operation,project_id,'project-export',release['id'],now,now))
        db.execute('INSERT INTO project_exports(id,recipient,scopes,expires) VALUES(?,?,?,?)',(operation,body.recipient,json.dumps(sorted(scopes)),now+TTL))
        store.audit(db,actor,'project.export',operation)
    return {'id':operation,'job_id':operation,'project_id':project_id,'state':'queued'}


def register(app,store,actor):
    @app.post('/v1/projects/{project_id}/exports',status_code=202,tags=['Exports'])
    def start(project_id: str,body: ExportInput,user=Depends(actor)):
        """Encrypt the current release, project settings, and managed data to an age public recipient."""
        return create(store,user,project_id,body)

    @app.get('/v1/exports/{operation}',tags=['Exports'])
    def get(operation: str,user=Depends(actor)):
        with store.connection() as db:return view(export_row(db,store,user,operation))

    @app.get('/v1/exports/{operation}/data',tags=['Exports'])
    def download(operation: str,offset: int=0,length: int=CHUNK,user=Depends(actor)):
        """Read at most 8 MiB of the encrypted bundle at the specified byte offset."""
        if offset<0 or not 1<=length<=CHUNK:raise Problem(416,'Invalid export offset or chunk length')
        with store.connection() as db:
            row=export_row(db,store,user,operation)
            if row['state']!='completed':raise Problem(409,'Export is not ready for download')
            if offset>row['size']:raise Problem(416,'Offset is outside the export')
            try:
                with artifact(store,operation).open('rb') as source:
                    source.seek(offset);chunk=source.read(min(length,row['size']-offset))
            except FileNotFoundError:raise Problem(404,'Export artifact is unavailable') from None
            if len(chunk)!=min(length,row['size']-offset):raise Problem(503,'Export artifact is incomplete')
        return Response(chunk,media_type='application/octet-stream',headers={'X-Paas-Offset':str(offset),'X-Paas-Size':str(row['size'])})

    @app.delete('/v1/exports/{operation}',status_code=204,tags=['Exports'])
    def remove(operation: str,user=Depends(actor)):
        with store.connection() as db:
            row=export_row(db,store,user,operation)
            if row['state'] in {'queued','running'}:raise Problem(409,'Wait for the active export operation')
            artifact(store,operation).unlink(missing_ok=True)
            db.execute('DELETE FROM project_exports WHERE id=?',(operation,))
            store.audit(db,user,'export.delete',operation)


def expire(store):
    now=time.time()
    with store.connection() as db:
        rows=db.execute("SELECT e.id FROM project_exports e JOIN jobs j ON j.id=e.id WHERE e.expires<=? AND j.state IN ('completed','failed')",(now,)).fetchall()
        for row in rows:
            artifact(store,row['id']).unlink(missing_ok=True)
            db.execute('DELETE FROM project_exports WHERE id=?',(row['id'],))
    for pending in directory(store).glob('*.part'):
        if pending.stat().st_mtime<now-3600:pending.unlink(missing_ok=True)


def purge_project(db,store,project_id):
    for row in db.execute('SELECT e.id FROM project_exports e JOIN jobs j ON j.id=e.id WHERE j.project_id=?',(project_id,)):
        artifact(store,row['id']).unlink(missing_ok=True)
    db.execute('DELETE FROM project_exports WHERE id IN (SELECT id FROM jobs WHERE project_id=?)',(project_id,))


class BoundedOutput:
    def __init__(self,target):self.target=target;self.size=0

    def write(self,data):
        self.size+=len(data)
        if self.size>LIMIT:raise BackendError('Project bundle exceeds 14 GiB')
        return self.target.write(data)

    def flush(self):self.target.flush()


def legacy_image(image,output,check):
    process=subprocess.Popen(['docker','save',image],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        size=0
        while chunk:=process.stdout.read(1024**2):
            check();size+=len(chunk)
            if size>1024**3:raise BackendError('Legacy image exceeds 1 GiB')
            output.write(chunk)
        if process.wait(timeout=10) or not size:raise BackendError('Legacy release image is unavailable')
    finally:
        process.stdout.close()
        if process.poll() is None:process.kill();process.wait()


def execute(worker,job,release):
    store,backend=worker.store,worker.backend
    operation=job['id'];project_id=job['project_id'];final=artifact(store,operation)
    with store.connection() as db:
        worker.guard(db)
        export=dict(db.execute('SELECT * FROM project_exports WHERE id=?',(operation,)).fetchone())
        project=dict(db.execute('SELECT * FROM projects WHERE id=?',(project_id,)).fetchone())
        resources=[dict(row) for row in db.execute('SELECT id,kind,stage FROM data_resources WHERE project_id=? ORDER BY kind,stage',(project_id,))]
    pending=None
    try:
        if not final.is_file():
            from publisher.project_imports import remaining_space
            with store.connection() as db:reserved=remaining_space(db,time.time())
            if shutil.disk_usage(directory(store)).free<LIMIT+2*1024**3+reserved:raise BackendError('Not enough control-plane space to reserve a project export')
            if subprocess.run([str(AGE),'-r',export['recipient'],os.devnull],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10).returncode:
                raise BackendError('Invalid age public recipient')
            source=store.settings.root/'artifacts'/(release['id']+'.tar.gz')
            if not source.is_file():raise BackendError('Published release source is unavailable')
            artifact_driver=release.get('runtime_driver')=='static-artifact'
            target=None if artifact_driver else backend.target(release['id']);account=project['account_id']
            if artifact_driver:
                image='static-artifact'
            elif target:
                if target['account_id']!=account:raise BackendError('Release image ownership does not match')
                image='paas-release:'+release['id']
            else:
                current=backend.find_container(release['id'])
                image=current.get('Image') if current else None
                if not image or not re.fullmatch(r'sha256:[a-f0-9]{64}',image):raise BackendError('Legacy release image is unavailable')
            config=json.loads(release['config'])
            manifest={'format':FORMAT,'bundle_id':operation,'name':project['name'],'source_release':release['id'],
                'image':image,'configuration':config,'project_environment':store.unseal(project['environment']),
                'release_environment':{k:v for k,v in store.unseal(release['environment']).items() if k not in MANAGED},'resources':resources}
            preview_environment=store.environment(project,'preview')
            if preview_environment:manifest['preview_environment']=preview_environment
            client=backend.execution_client() if target or resources else None
            worker.check()
            with tempfile.NamedTemporaryFile(dir=directory(store),prefix=operation+'-',suffix='.part',delete=False) as output:
                pending=Path(output.name);pending.chmod(0o640)
                process=subprocess.Popen([str(AGE),'-r',export['recipient']],stdin=subprocess.PIPE,stdout=output,stderr=subprocess.DEVNULL)
                try:
                    with zipfile.ZipFile(BoundedOutput(process.stdin),'w') as bundle:
                        bundle.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
                        bundle.write(source,'source.tar.gz')
                        if not artifact_driver:
                            if config['runtime']=='static':bundle.write(Path(__file__).with_name('static-nginx.conf'),'nginx.conf')
                            with bundle.open('image.tar','w',force_zip64=True) as out:
                                if target:client.stream_export('GET','/v1/builds/'+release['id']+'/image',out,1024**3,worker.check,headers={'X-Paas-Account':account})
                                else:legacy_image(image,out,worker.check)
                        if resources:
                            client.bootstrap(account)
                            with bundle.open('data.zip','w',force_zip64=True) as out:
                                client.stream_export('POST','/v1/runtimes/'+account+'/snapshot',out,12*1024**3+16*1024**2,worker.check,
                                    body={'operation':operation,'project_id':project_id,'resources':resources})
                    process.stdin.close()
                    if process.wait(timeout=30):raise BackendError('Project bundle encryption failed')
                    os.fsync(output.fileno())
                finally:
                    if process.poll() is None:process.kill();process.wait()
                    if not process.stdin.closed:
                        try:process.stdin.close()
                        except BrokenPipeError:pass
        with store.connection() as db:
            worker.guard(db)
            if pending:pending.replace(final)
            size=final.stat().st_size
            if not 0<size<=LIMIT+32*1024**2:raise BackendError('Encrypted bundle size is invalid')
            now=time.time()
            db.execute('UPDATE project_exports SET size=?,expires=? WHERE id=?',(size,now+TTL,operation))
            db.execute("UPDATE jobs SET state='completed',updated=?,error=NULL,lease_until=NULL WHERE id=? AND owner=?",(now,operation,worker.owner))
    finally:
        if pending:pending.unlink(missing_ok=True)
