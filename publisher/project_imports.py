"""Resumable encrypted staging and durable restoration into an empty project."""
import asyncio
import fcntl
import json
import os
from pathlib import PurePosixPath
import shutil
import stat
import struct
import time
from typing import Literal
from cryptography.fernet import InvalidToken
from fastapi import Depends,Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from publisher.backend import BackendError
from publisher.controlflow import ObservationUnavailable
from publisher.data_backend import DataBackend
from publisher.data_store import DataStore
from publisher.models import Input
from publisher.project_bundle import BUNDLE_LIMIT,DATA_LIMIT,ID,BundleError,open_zip,unpack_bundle
from publisher.store import Problem,encode,ident
from publisher.transfer_wire import CHUNK

TTL=86400
HEADROOM=2*1024**3
BASE_SCOPES={'imports:write','projects:write','deployments:write','env:write'}


class ImportInput(Input):
    bundle_id: ID
    size: int=Field(ge=1,le=BUNDLE_LIMIT)
    kinds: list[Literal['database','volume']]=Field(default_factory=list,max_length=2)


def payload_dir(store,operation):return store.settings.root/'imports'/operation


def work_dir(store,operation):return store.settings.root/'import-work'/operation


def remaining_space(db,now):
    return sum(2*r['size']+(4*(r['size']-r['received'])+2)//3+1024**2 for r in db.execute(
        "SELECT i.size,i.received FROM project_imports i JOIN jobs j ON j.id=i.id WHERE (j.state IN ('uploading','failed') AND i.expires>?) OR j.state IN ('queued','running')",(now,)))


def scopes(kinds):return BASE_SCOPES|{DataStore.scope(kind) for kind in kinds}


def view(row,store):
    return {k:row[k] for k in ('id','project_id','bundle_id','size','received','state','error','deployment_id','expires')}|{
        'job_id':row['id'],'chunk_limit':CHUNK,'url':'https://'+store.settings.hostname(row['project_id'],domain=row['app_domain']) if row['state']=='completed' else None}


def lookup(db,store,actor,operation):
    row=db.execute('SELECT i.*,j.state,j.error,j.project_id,j.deployment_id FROM project_imports i JOIN jobs j ON j.id=i.id WHERE i.id=?',(operation,)).fetchone()
    if row is None:raise Problem(404,'Import not found')
    project=store.project(db,actor,row['project_id'],'imports:write',allow_inactive=True)
    if project['state']!='active':raise Problem(404,'Import not found')
    for scope in scopes(json.loads(row['kinds'])):store.permit(actor,scope,project['id'])
    return dict(row)|{'app_domain':project['app_domain']}


def create(store,actor,project_id,body):
    with store.connection() as db:
        project=store.project(db,actor,project_id,'imports:write')
        for scope in scopes(body.kinds):store.permit(actor,scope,project_id)
        if project['current_deployment'] or any(store.environment(project,target) for target in ('production','preview')) or db.execute('SELECT 1 FROM deployments WHERE project_id=?',(project_id,)).fetchone() or db.execute('SELECT 1 FROM data_resources WHERE project_id=?',(project_id,)).fetchone():
            raise Problem(409,'Import requires an empty destination without releases, data or environment values')
        if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('uploading','queued','running')",(project_id,)).fetchone():
            raise Problem(409,'Destination already has an active operation')
        now=time.time()
        if remaining_space(db,now):raise Problem(429,'Finish or discard the current project import before starting another')
        exports=db.execute("SELECT count(*) FROM jobs WHERE kind='project-export' AND state IN ('queued','running')").fetchone()[0]
        needed=HEADROOM+2*body.size+(4*body.size+2)//3+1024**2+exports*BUNDLE_LIMIT
        if shutil.disk_usage(store.settings.root).free<needed:raise Problem(507,'Control plane cannot reserve encrypted parts and restore workspace')
        operation=ident();payload_dir(store,operation).mkdir(mode=0o700)
        db.execute("INSERT INTO jobs(id,project_id,kind,state,created,updated) VALUES(?,?,?,'uploading',?,?)",(operation,project_id,'project-import',now,now))
        db.execute('INSERT INTO project_imports(id,bundle_id,kinds,size,expires) VALUES(?,?,?,?,?)',(operation,body.bundle_id,encode(sorted(set(body.kinds))),body.size,now+TTL))
        store.audit(db,actor,'project.import',operation)
        return view(lookup(db,store,actor,operation),store)


def part_binding(operation,offset):return b'paas-project-import/1\0'+operation.encode()+offset.to_bytes(8,'big')


def part_path(store,operation,offset):return payload_dir(store,operation)/f'{offset:014d}.part'


def begin_part(store,actor,operation,offset,length):
    with store.connection() as db:
        row=lookup(db,store,actor,operation);now=time.time()
        if row['state']!='uploading' or row['expires']<=now:raise Problem(409,'Import no longer accepts parts')
        if offset!=row['received']:raise Problem(409,'Resume at the received offset returned by the import status')
        size=min(CHUNK,row['size']-offset)
        if size<=0 or length!=size:raise Problem(400,'Content-Length does not match the next import part')
        if row['upload_until'] and row['upload_until']>now:raise Problem(409,'An import part is already in progress')
        if shutil.disk_usage(store.settings.root).free<HEADROOM+remaining_space(db,now):raise Problem(507,'Import disk reserve is insufficient')
        lease=ident()
        db.execute('UPDATE project_imports SET upload_lease=?,upload_until=? WHERE id=?',(lease,now+180,operation))
        return lease,size


def finish_part(store,actor,operation,offset,lease,data):
    pending=payload_dir(store,operation)/(lease+'.next')
    try:
        encrypted=store.cipher.encrypt(part_binding(operation,offset)+data)
        with pending.open('xb') as out:
            out.write(encrypted);out.flush();os.fsync(out.fileno())
        with store.connection() as db:
            row=lookup(db,store,actor,operation)
            if row['state']!='uploading' or row['upload_lease']!=lease or row['received']!=offset or row['upload_until']<=time.time():
                raise Problem(409,'Import part no longer owns the upload lease')
            if len(data)!=min(CHUNK,row['size']-offset):raise Problem(400,'Import part has the wrong length')
            pending.replace(part_path(store,operation,offset))
            db.execute('UPDATE project_imports SET received=?,upload_lease=NULL,upload_until=NULL WHERE id=?',(offset+len(data),operation))
            return view(lookup(db,store,actor,operation),store)
    finally:pending.unlink(missing_ok=True)


def release_part(store,operation,lease):
    with store.connection() as db:
        db.execute('UPDATE project_imports SET upload_lease=NULL,upload_until=NULL WHERE id=? AND upload_lease=?',(operation,lease))


def register(app,store,actor):
    @app.post('/v1/projects/{project_id}/imports',status_code=201,tags=['Imports'])
    def start(project_id: str,body: ImportInput,user=Depends(actor)):
        """Reserve an empty destination and begin a resumable project import."""
        return create(store,user,project_id,body)

    @app.get('/v1/imports/{operation}',tags=['Imports'])
    def status(operation: str,user=Depends(actor)):
        with store.connection() as db:return view(lookup(db,store,user,operation),store)

    @app.put('/v1/imports/{operation}/part',tags=['Imports'])
    async def upload(operation: str,offset: int,request: Request,user=Depends(actor)):
        """Upload the next 8 MiB (or final remainder) of the authenticated ZIP; parts are encrypted before storage."""
        if request.headers.get('content-type','').split(';')[0]!='application/octet-stream':raise Problem(415,'Send a binary import part')
        try:length=int(request.headers.get('content-length','0'))
        except ValueError:raise Problem(400,'Invalid Content-Length') from None
        lease,size=await run_in_threadpool(begin_part,store,user,operation,offset,length)
        try:
            data=bytearray()
            async with asyncio.timeout(120):
                async for chunk in request.stream():
                    if len(data)+len(chunk)>size:raise Problem(413,'Import part exceeds its declared length')
                    data.extend(chunk)
            if len(data)!=size:raise Problem(400,'Import part ended early')
            return await run_in_threadpool(finish_part,store,user,operation,offset,lease,bytes(data))
        except (TimeoutError,ClientDisconnect):raise Problem(408,'Import part interrupted; resume at the last committed offset') from None
        finally:await run_in_threadpool(release_part,store,operation,lease)

    def queue(operation,user,retry):
        with store.connection() as db:
            row=lookup(db,store,user,operation);now=time.time()
            if row['state'] in {'queued','running','completed'}:return view(row,store)
            if row['state']!=('failed' if retry else 'uploading') or row['expires']<=now or row['received']!=row['size']:
                raise Problem(409,'Import is incomplete, expired or not retryable')
            if row['upload_until'] and row['upload_until']>now:raise Problem(409,'An import part is still uploading')
            if shutil.disk_usage(store.settings.root).free<HEADROOM+2*row['size']:raise Problem(507,'Control-plane restore workspace is unavailable')
            db.execute("UPDATE jobs SET state='queued',attempts=0,owner=NULL,lease_until=NULL,error=NULL,updated=? WHERE id=?",(now,operation))
            db.execute('UPDATE project_imports SET expires=? WHERE id=?',(now+TTL,operation))
            store.audit(db,user,'import.retry' if retry else 'import.submit',operation)
            return view(lookup(db,store,user,operation),store)

    @app.post('/v1/imports/{operation}/submit',status_code=202,tags=['Imports'])
    def submit(operation: str,user=Depends(actor)):return queue(operation,user,False)

    @app.post('/v1/imports/{operation}/retry',status_code=202,tags=['Imports'])
    def retry(operation: str,user=Depends(actor)):return queue(operation,user,True)

    @app.delete('/v1/imports/{operation}',status_code=204,tags=['Imports'])
    def cancel(operation: str,user=Depends(actor)):
        with store.connection() as db:
            row=lookup(db,store,user,operation)
            if row['state'] not in {'uploading','failed','cancelled'} or row['deployment_id'] or json.loads(row['resource_map']):
                raise Problem(409,'This import owns resources or is applying; use the project deletion workflow after it stops')
            if row['upload_until'] and row['upload_until']>time.time():raise Problem(409,'An import part is still uploading')
            remove_parts(store,operation)
            db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?",(time.time(),operation))


def remove_parts(store,operation):
    path=payload_dir(store,operation)
    if path.exists():shutil.rmtree(path)


def remove_work(store,operation):
    path=work_dir(store,operation)
    if path.exists():
        with (path/'apply.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ObservationUnavailable('The same import apply operation is still active') from None
            shutil.rmtree(path)


def expire(store):
    now=time.time()
    with store.connection() as db:
        rows=db.execute("SELECT i.id,j.state FROM project_imports i JOIN jobs j ON j.id=i.id WHERE j.state IN ('completed','cancelled') OR (j.state IN ('uploading','failed') AND i.expires<=? AND coalesce(i.upload_until,0)<=?)",(now,now)).fetchall()
        for row in rows:
            try:remove_work(store,row['id'])
            except ObservationUnavailable:continue
            remove_parts(store,row['id'])
            if row['state']=='uploading':db.execute("UPDATE jobs SET state='cancelled',error='Unsubmitted import expired',updated=? WHERE id=?",(now,row['id']))


def purge_project(db,store,project):
    for row in db.execute('SELECT i.id FROM project_imports i JOIN jobs j ON j.id=i.id WHERE j.project_id=?',(project,)):
        remove_work(store,row['id']);remove_parts(store,row['id'])
    db.execute('DELETE FROM project_imports WHERE id IN (SELECT id FROM jobs WHERE project_id=?)',(project,))


def assemble(store,row,target,check):
    with target.open('xb') as output:
        for offset in range(0,row['size'],CHUNK):
            check();size=min(CHUNK,row['size']-offset);binding=part_binding(row['id'],offset)
            try:
                with part_path(store,row['id'],offset).open('rb') as part:
                    encrypted=part.read((4*size+2)//3+1024)
                    if part.read(1):raise BundleError('Encrypted import part exceeds its limit')
                data=store.cipher.decrypt(encrypted)
            except (FileNotFoundError,InvalidToken):raise BundleError('Import part is missing or failed authentication') from None
            if len(data)!=len(binding)+size or not data.startswith(binding):raise BundleError('Import part does not match this operation and offset')
            output.write(data[len(binding):])


def allocate(worker,job,row,manifest,account,source_size,expanded):
    store=worker.store;project=job['project_id'];operation=job['id'];data=DataStore(store)
    with store.connection() as db:
        worker.guard(db)
        existing=db.execute('SELECT * FROM deployments WHERE id=? AND project_id=?',(operation,project)).fetchone()
        if existing:
            release=dict(existing)
            if release['state']=='failed':
                store.check_concurrency(db,{'account_id':account})
                store.check_runtime_capacity(db,account,json.loads(release['config']),excluding=operation)
                db.execute("UPDATE deployments SET state='building',error=NULL,updated=? WHERE id=?",(time.time(),operation))
                release['state']='building';release['error']=None
            return release,json.loads(row['resource_map'])
        quota=store.check_concurrency(db,{'account_id':account})
        used=db.execute('SELECT coalesce(sum(d.artifact_bytes+d.unpacked_bytes),0) FROM deployments d JOIN projects p ON p.id=d.project_id WHERE p.account_id=?',(account,)).fetchone()[0]
        if used+source_size+expanded>quota['max_storage']:raise Problem(429,'Artifact storage quota reached')
        config=manifest.configuration.configuration()
        if config['target']=='preview' and (config.get('database_id') or config.get('volume_id')):
            raise BundleError('A preview with data cannot become the imported production release')
        config['target']='production'
        mapping={r.id:data.allocate(db,account,project,r.kind,r.stage,import_id=operation) for r in manifest.resources}
        for field in ['database_id','volume_id']:
            if field in config:config[field]=mapping[config[field]]
        store.check_runtime_capacity(db,account,config)
        now=time.time()
        db.execute("INSERT INTO deployments(id,project_id,state,config,environment,artifact_bytes,unpacked_bytes,created,updated) VALUES(?,?,'building',?,?,?,?,?,?)",
            (operation,project,encode(config),store.seal(manifest.release_environment),source_size,expanded,now,now))
        db.execute('UPDATE jobs SET deployment_id=? WHERE id=?',(operation,operation))
        db.execute('UPDATE project_imports SET resource_map=? WHERE id=?',(encode(mapping),operation))
        return dict(db.execute('SELECT * FROM deployments WHERE id=?',(operation,)).fetchone()),mapping


def resource_payload(bundle,resource,target):
    with open_zip(bundle,limit=DATA_LIMIT+16*1024**2,max_entries=20005) as (archive,entries),target.open('xb') as output:
        if resource.kind=='database':
            with archive.open('databases/'+resource.id+'.dump') as source:shutil.copyfileobj(source,output,1024**2)
            return
        prefix='volumes/'+resource.id+'/'
        # Parent directories must come before any children, including a root file.
        names=sorted((name for name in entries if name.startswith(prefix) and name!=prefix),
                     key=lambda n:(len(PurePosixPath(n[len(prefix):]).parts),not entries[n].is_dir(),n))
        for name in names:
            entry=entries[name];header=json.dumps({'name':name[len(prefix):].removesuffix('/'),'size':entry.file_size,
                'directory':entry.is_dir(),'executable':bool((entry.external_attr>>16)&stat.S_IXUSR)},ensure_ascii=False).encode()
            output.write(struct.pack('!I',len(header)));output.write(header)
            if not entry.is_dir():
                with archive.open(entry) as source:shutil.copyfileobj(source,output,1024**2)
        output.write(struct.pack('!I',0))


def apply(worker,job,row,project,stage):
    store=worker.store;operation=job['id'];account=project['account_id']
    plain=stage/'bundle.zip';assemble(store,row,plain,worker.check)
    content=stage/'content';content.mkdir(mode=0o700);stats={}
    manifest=unpack_bundle(plain,content,source_stats=stats);plain.unlink()
    if manifest.bundle_id!=row['bundle_id'] or {r.kind for r in manifest.resources}!=set(json.loads(row['kinds'])):
        raise BundleError('Bundle identity or declared resource permissions do not match')
    if worker.backend.config.get('execution_enabled') is not True:raise BackendError('Project import requires isolated execution')
    source=content/'source.tar.gz'
    try:release,mapping=allocate(worker,job,row,manifest,account,source.stat().st_size,stats['unpacked_bytes'])
    except Problem as error:raise BackendError(error.message) from None
    artifact=store.settings.root/'artifacts'/(operation+'.tar.gz')
    if not artifact.is_file():
        pending=artifact.with_suffix('.next');shutil.copyfile(source,pending);pending.chmod(0o640);pending.replace(artifact)
    if manifest.resources:
        data=DataBackend(worker.backend)
        for resource in manifest.resources:
            worker.check()
            with store.connection() as db:
                target=dict(db.execute('SELECT * FROM data_resources WHERE id=? AND project_id=?',(mapping[resource.id],project['id'])).fetchone())
            if target['state']=='ready':continue
            metadata=data.create(target,worker.check,import_id=operation)
            payload=stage/'resource.bin'
            try:
                resource_payload(content/'data.zip',resource,payload);worker.check()
                data.client.restore_data(account,{'operation':operation,'project_id':project['id'],'resource_id':target['id'],'kind':resource.kind},payload)
            finally:payload.unlink(missing_ok=True)
            with store.connection() as db:
                worker.guard(db)
                db.execute("UPDATE data_resources SET state='ready',backend=?,error=NULL,updated=? WHERE id=?",(encode(metadata),time.time(),target['id']))
    with store.connection() as db:
        worker.guard(db)
        env=DataStore(store).bind_resources(db,project['id'],json.loads(release['config']),dict(manifest.release_environment))
        db.execute('UPDATE deployments SET environment=? WHERE id=?',(store.seal(env),operation))
        release=dict(db.execute('SELECT * FROM deployments WHERE id=?',(operation,)).fetchone())|{'app_domain':project['app_domain']}
    if release['state']!='ready':
        worker.deploy(job,release,imported={'image_path':None if manifest.legacy_static else content/'image.tar','reference':manifest.image})
    with store.connection() as db:
        worker.guard(db)
        db.execute('UPDATE projects SET environment=?,preview_environment=? WHERE id=?',
                   (store.seal(manifest.project_environment),
                    store.seal(manifest.preview_environment) if manifest.preview_environment else None,project['id']))
        db.execute("UPDATE jobs SET state='completed',error=NULL,updated=?,lease_until=NULL WHERE id=? AND owner=?",(time.time(),operation,worker.owner))


def execute(worker,job):
    store=worker.store;operation=job['id'];work=work_dir(store,operation)
    work.parent.mkdir(mode=0o700,exist_ok=True);work.mkdir(mode=0o700,exist_ok=True)
    with (work/'apply.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ObservationUnavailable('The same import apply operation is still active') from None
        with store.connection() as db:
            worker.guard(db)
            row=dict(db.execute('SELECT * FROM project_imports WHERE id=?',(operation,)).fetchone())
            project=dict(db.execute('SELECT * FROM projects WHERE id=?',(job['project_id'],)).fetchone())
        stage=work/'stage'
        if stage.exists():shutil.rmtree(stage)
        if shutil.disk_usage(store.settings.root).free<HEADROOM+2*row['size']:raise BackendError('Control-plane restore workspace is unavailable')
        stage.mkdir(mode=0o700)
        try:apply(worker,job,row,project,stage)
        finally:shutil.rmtree(stage)
    remove_parts(store,operation);remove_work(store,operation)


def fail_release(db,operation,message):
    db.execute("UPDATE deployments SET state='failed',error=?,updated=? WHERE id=? AND state NOT IN ('ready','activating')",
               (message,time.time(),operation))
