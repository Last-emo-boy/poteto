"""Fixed data and migration operations inside a tenant VM, never on the OVH host."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import struct
import sys
import zipfile
from pathlib import PurePosixPath

EXPORT_ROOT=Path('/var/lib/paas-project-export')
RESTORE_ROOT=Path('/var/lib/paas-resource-restore')
VOLUME_ROOT=Path('/var/lib/docker/volumes')
DATA_LIMIT=12*1024**3


def checked(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{24}',value):
        raise ValueError('Invalid resource identifier')
    return value


def docker(*args, data=None, timeout=30, check=True):
    return subprocess.run(['docker',*args],input=data,capture_output=True,timeout=timeout,check=check)


def object_labels(resource,project,kind):
    return {'paas.publisher.resource':resource,'paas.publisher.project':project,'paas.publisher.kind':kind}


def ensure_volume(request):
    resource, project = checked(request['resource_id']),checked(request['project_id'])
    kind=request['kind']
    if kind not in {'database','volume'}:
        raise ValueError('Invalid data resource kind')
    name='paas-data-'+resource
    result=docker('volume','inspect',name,check=False)
    expected=object_labels(resource,project,kind)
    if request.get('import_id'):expected['paas.publisher.import']=checked(request['import_id'])
    if result.returncode:
        if shutil.disk_usage('/var/lib/docker').free < 1024**3:
            raise ValueError('Tenant disk reserve is exhausted; free space before creating more data resources')
        args=['volume','create']
        for key,value in expected.items():
            args+=['--label',key+'='+value]
        docker(*args,name)
        result=docker('volume','inspect',name)
    volume=json.loads(result.stdout)[0]
    if any((volume.get('Labels') or {}).get(k)!=v for k,v in expected.items()):
        raise ValueError('Volume name is owned by another resource')
    if kind=='volume':
        path=Path(volume['Mountpoint'])
        if path != VOLUME_ROOT/name/'_data' or path.is_symlink():
            raise ValueError('Unexpected managed volume path')
        os.chown(path,10001,10001)
    return {'volume':name,'ready':True}


def database(request):
    resource,project=checked(request['resource_id']),checked(request['project_id'])
    ids=docker('ps','-aq','--filter','label=paas.publisher.resource='+resource,'--filter','label=paas.publisher.kind=database').stdout.split()
    if not ids:
        return None
    if len(ids)!=1:
        raise ValueError('Database container inventory is ambiguous')
    current=json.loads(docker('inspect',ids[0].decode()).stdout)[0]
    if any(current['Config']['Labels'].get(k)!=v for k,v in object_labels(resource,project,'database').items()):
        raise ValueError('Database ownership does not match')
    return current


def database_role(request):
    current=database(request)
    if not current or not current['State']['Running'] or current['State'].get('Health',{}).get('Status')!='healthy':
        return {'ready':False}
    password=request['app_password']
    if not re.fullmatch(r'[a-zA-Z0-9_-]{40,80}',password):
        raise ValueError('Invalid generated database credential')
    # Only generated fixed role names enter SQL. The application never receives
    # the initialization superuser, and each project has its own database server.
    sql=f"""SELECT 'CREATE ROLE app LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app')\\gexec
ALTER ROLE app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN PASSWORD '{password}';
ALTER DATABASE app OWNER TO app;
\\connect app
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE,CREATE ON SCHEMA public TO app;
"""
    docker('exec','-i',current['Id'],'psql','-X','-U','paas_admin','-d','postgres','-v','ON_ERROR_STOP=1',data=sql.encode())
    return {'ready':True,'non_superuser':True}


def inspect_database(request):
    current=database(request)
    return {'container':{key:current[key] for key in ['Id','State']} if current else None}


def destroy(request):
    resource=checked(request['resource_id'])
    RESTORE_ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (RESTORE_ROOT/(resource+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=destroy_resource(request)
        (RESTORE_ROOT/(resource+'.json')).unlink(missing_ok=True)
        return result


def destroy_resource(request):
    resource,project=checked(request['resource_id']),checked(request['project_id'])
    current=database(request)
    if current is not None:
        if current['State']['Running']:
            docker('stop','--time','10',current['Id'])
        docker('rm',current['Id'])
    # Migrations may remain as receipts. Remove only this resource's stopped
    # migration containers before Docker checks whether its network is in use.
    ids=docker('ps','-aq','--filter','label=paas.publisher.database='+resource).stdout.split()
    for value in ids:
        current=json.loads(docker('inspect',value.decode()).stdout)[0]
        if current['Config']['Labels'].get('paas.publisher.project')!=project or current['State']['Running']:
            raise ValueError('A migration still uses this database')
        docker('rm',current['Id'])
    name='paas-data-'+resource
    result=docker('volume','inspect',name,check=False)
    if not result.returncode:
        volume=json.loads(result.stdout)[0];labels=volume.get('Labels') or {}
        if labels.get('paas.publisher.project')!=project or labels.get('paas.publisher.resource')!=resource:
            raise ValueError('Data volume ownership does not match')
        if labels.get('paas.publisher.import'):
            operation=checked(labels['paas.publisher.import']);base=Path(volume['Mountpoint'])
            if base!=VOLUME_ROOT/name/'_data' or base.is_symlink():raise ValueError('Unexpected managed volume path')
            stage=base.parent/('restore-'+operation)
            if stage.exists():shutil.rmtree(stage)
        docker('volume','rm',name)
    network=docker('network','inspect',name,check=False)
    if not network.returncode:
        labels=json.loads(network.stdout)[0].get('Labels') or {}
        if labels.get('paas.publisher.resource')!=resource:
            raise ValueError('Data network ownership does not match')
        docker('network','rm',name)
    return {'deleted':True}


def migration(request):
    release,project,resource=checked(request['release_id']),checked(request['project_id']),checked(request['resource_id'])
    name='paas-migration-'+release
    current=docker('container','inspect',name,check=False)
    if current.returncode:
        command=request['command']
        environment=request['environment']
        if not isinstance(command,str) or not 0<len(command)<=2000 or len(json.dumps(environment))>65536:
            raise ValueError('Invalid migration definition')
        args=['create','--name',name,'--network','paas-data-'+resource,'--user','10001:10001',
              '--memory',str(request['memory_mb'])+'m','--cpus',str(request['cpus']),'--pids-limit','128',
              '--cap-drop','ALL','--security-opt','no-new-privileges:true','--init',
              '--tmpfs','/tmp:rw,noexec,nosuid,size=67108864',
              '--label','paas.publisher.migration='+release,'--label','paas.publisher.project='+project,
              '--label','paas.publisher.database='+resource,'--label','paas.publisher.release=',
              '--label','paas.publisher.resource=','--label','paas.publisher.kind=migration',
              '--label','sablier.enable=false','--label','traefik.enable=false',
              '--log-opt','max-size=1m','--log-opt','max-file=1']
        for key,value in environment.items():
            if not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,63}',key) or not isinstance(value,str) or '\x00' in value:
                raise ValueError('Invalid migration environment')
            args+=['--env',key+'='+value]
        volume=request.get('volume_id')
        if volume:
            args+=['--mount','type=volume,src=paas-data-'+checked(volume)+',dst=/data']
        args+=['--entrypoint','sh','paas-release:'+release,'-c',command]
        docker(*args)
        current=docker('container','inspect',name)
    container=json.loads(current.stdout)[0]
    labels=container['Config']['Labels']
    if labels.get('paas.publisher.project')!=project or labels.get('paas.publisher.database')!=resource:
        raise ValueError('Migration ownership does not match')
    if container['State']['Status']=='created':
        docker('start',name)
        return {'state':'running'}
    if container['State']['Running']:
        return {'state':'running'}
    log=docker('logs','--tail','200',name,check=False)
    output=(log.stdout+log.stderr)[-32768:].decode(errors='replace')
    return {'state':'succeeded' if container['State']['ExitCode']==0 else 'failed','exit_code':container['State']['ExitCode'],'log':output}


def stop_migration(request):
    release,project,resource=checked(request['release_id']),checked(request['project_id']),checked(request['resource_id'])
    name='paas-migration-'+release
    result=docker('container','inspect',name,check=False)
    if not result.returncode:
        current=json.loads(result.stdout)[0]
        labels=current['Config']['Labels']
        if labels.get('paas.publisher.project')!=project or labels.get('paas.publisher.database')!=resource:
            raise ValueError('Migration ownership does not match')
        if current['State']['Running']:
            docker('stop','--time','10',name)
    return {'stopped':True}


def project_apps(project):
    ids=docker('ps','-aq','--filter','label=paas.publisher.project='+checked(project)).stdout.decode().split()
    if not ids:return {}
    result={}
    for row in json.loads(docker('inspect',*ids).stdout):
        labels=row['Config']['Labels']
        if labels.get('paas.publisher.kind')=='database':continue
        if labels.get('paas.publisher.project')!=project or labels.get('paas.publisher.kind') not in {'application','migration'}:
            raise ValueError('Unexpected project container inventory')
        result[row['Id']]=row['State']
    return result


def release_capture(path,operation,project):
    if not path.is_file():return
    receipt=json.loads(path.read_text())
    if receipt['operation']!=operation:return
    current=project_apps(project)
    for cid,started in receipt['paused'].items():
        state=current.get(cid)
        if state and state['StartedAt']==started and state['Paused']:
            docker('unpause',cid)
    path.unlink()


def resume_export(operation,project):
    path=EXPORT_ROOT/(checked(project)+'.json')
    if not path.is_file():return
    with (EXPORT_ROOT/(project+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        release_capture(path,checked(operation),project)


def export_copy(source,target,remaining):
    while chunk:=source.read(1024**2):
        remaining[0]-=len(chunk)
        if remaining[0]<0:raise ValueError('Project data exceeds the export size limit')
        target.write(chunk)


def export_database(request,archive,remaining):
    current=database(request)
    if not current or not current['State']['Running']:
        raise ValueError('The managed database is not running')
    process=subprocess.Popen(['docker','exec',current['Id'],'pg_dump','--username=app','--dbname=app','--format=custom',
        '--compress=0','--no-owner','--no-privileges','--no-password'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        with archive.open('databases/'+request['resource_id']+'.dump','w',force_zip64=True) as out:
            export_copy(process.stdout,out,remaining)
        if process.wait(timeout=10):raise ValueError('Managed PostgreSQL snapshot failed')
    finally:
        process.stdout.close()
        if process.poll() is None:process.kill();process.wait()


def export_volume(request,archive,remaining,entries):
    rid=request['resource_id'];project=request['project_id'];name='paas-data-'+rid
    volume=json.loads(docker('volume','inspect',name).stdout)[0]
    if any((volume.get('Labels') or {}).get(k)!=v for k,v in object_labels(rid,project,'volume').items()):
        raise ValueError('Managed volume ownership does not match')
    base=Path(volume['Mountpoint']);prefix='volumes/'+rid
    if base!=VOLUME_ROOT/name/'_data' or base.is_symlink():
        raise ValueError('Unexpected managed volume path')
    archive.write(base,prefix)
    def fail(error):raise error
    for root,dirs,files in os.walk(base,followlinks=False,onerror=fail):
        for name in sorted(dirs+files):
            path=Path(root)/name;relative=path.relative_to(base).as_posix();info=path.lstat()
            entries[0]+=1
            if entries[0]>20000 or len(relative)>240 or '\\' in relative or any(ord(c)<32 for c in relative):
                raise ValueError('Managed volume entry exceeds export limits')
            if stat.S_ISDIR(info.st_mode):archive.write(path,prefix+'/'+relative)
            elif stat.S_ISREG(info.st_mode):
                entry=zipfile.ZipInfo.from_file(path,prefix+'/'+relative,strict_timestamps=False)
                with path.open('rb') as src,archive.open(entry,'w',force_zip64=True) as out:
                    export_copy(src,out,remaining)
            else:raise ValueError('Managed volume contains a link or special file; snapshot refused')


def capture_export(operation,request,output):
    operation=checked(operation);project=checked(request['project_id']);resources=request['resources']
    if not 1<=len(resources)<=5 or len({checked(row['id']) for row in resources})!=len(resources) or any(row['kind'] not in {'database','volume'} for row in resources):
        raise ValueError('Invalid project export inventory')
    EXPORT_ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (EXPORT_ROOT/(project+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=EXPORT_ROOT/(project+'.json')
        if path.exists():raise ValueError('This project still has a snapshot recovery receipt')
        before=project_apps(project);paused={}
        try:
            for cid,state in before.items():
                if state['Running'] and not state['Paused']:
                    paused[cid]=state['StartedAt']
                    pending=path.with_suffix('.next')
                    pending.write_text(json.dumps({'operation':operation,'paused':paused}));pending.replace(path)
                    docker('pause',cid)
            remaining=[DATA_LIMIT];entries=[0]
            with zipfile.ZipFile(output,'w',strict_timestamps=False) as archive:
                for row in resources:
                    spec={'resource_id':row['id'],'project_id':project}
                    if row['kind']=='database':export_database(spec,archive,remaining)
                    else:export_volume(spec,archive,remaining,entries)
            after=project_apps(project)
            if set(after)!=set(before) or any(after[cid]['StartedAt']!=state['StartedAt'] or after[cid]['Running']!=state['Running'] or (state['Running'] and not after[cid]['Paused']) for cid,state in before.items()):
                raise ValueError('An application changed during the snapshot; discard it and retry')
        finally:
            release_capture(path,operation,project)


class RestoreInput:
    def __init__(self,source,size):self.source=source;self.remaining=size

    def read(self,size):
        if not 0<=size<=self.remaining:raise ValueError('Restore payload exceeds its declared length')
        value=self.source.read(size)
        if len(value)!=size:raise ValueError('Restore payload ended early')
        self.remaining-=size
        return value

    def copy(self,output,size):
        if not 0<=size<=self.remaining:raise ValueError('Invalid restore entry length')
        while size:
            chunk=self.read(min(size,1024**2));size-=len(chunk)
            if output is not None:output.write(chunk)

    def finish(self):
        if self.remaining or self.source.read(1):raise ValueError('Unexpected restore payload length')


def pg_admin(container,sql,*,database_name='postgres',operation=''):
    return docker('exec','-i',container,'psql','-X','--no-password','-At','-U','paas_admin','-d',database_name,
                  '-v','ON_ERROR_STOP=1','-v','operation='+operation,data=sql.encode(),timeout=60).stdout.decode().strip()


def restore_database(request,source):
    current=database(request);operation=request['operation']
    if not current or not current['State']['Running'] or current['Config']['Labels'].get('paas.publisher.import')!=operation:
        raise ValueError('Database is not owned by this import')
    cid=current['Id']
    pg_admin(cid,'CREATE TABLE IF NOT EXISTS public.paas_restore_receipts(operation text PRIMARY KEY);')
    if pg_admin(cid,"SELECT operation FROM public.paas_restore_receipts WHERE operation=:'operation';",operation=operation)==operation:
        source.copy(None,source.remaining);source.finish()
    else:
        if project_apps(request['project_id']):raise ValueError('Cannot restore over a project with application containers')
        pg_admin(cid,'DROP DATABASE IF EXISTS paas_restore WITH (FORCE); CREATE DATABASE paas_restore OWNER app;')
        if shutil.disk_usage('/var/lib/docker').free<source.remaining+1024**3:raise ValueError('Tenant disk reserve is insufficient for database restore')
        pg_admin(cid,'REVOKE ALL ON SCHEMA public FROM PUBLIC; GRANT USAGE,CREATE ON SCHEMA public TO app;',database_name='paas_restore')
        process=subprocess.Popen(['docker','exec','-i',cid,'pg_restore','--username=app','--dbname=paas_restore',
            '--single-transaction','--exit-on-error','--no-owner','--no-privileges','--no-tablespaces',
            '--no-publications','--no-subscriptions','--no-security-labels','--no-password'],stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            source.copy(process.stdin,source.remaining);source.finish();process.stdin.close()
            if process.wait(timeout=840):raise ValueError('PostgreSQL restore as application role failed')
        finally:
            if process.poll() is None:process.kill();process.wait()
            if not process.stdin.closed:
                try:process.stdin.close()
                except BrokenPipeError:pass
        # The receipt and database-name swap commit together. A lost response
        # never causes a retry to overwrite the already restored database.
        pg_admin(cid,"BEGIN; ALTER DATABASE app RENAME TO paas_restore_old; ALTER DATABASE paas_restore RENAME TO app; INSERT INTO public.paas_restore_receipts VALUES (:'operation'); COMMIT;",operation=operation)
    pg_admin(cid,'DROP DATABASE IF EXISTS paas_restore_old WITH (FORCE);')


def restore_volume(request,source,receipt):
    rid=request['resource_id'];operation=request['operation'];name='paas-data-'+rid
    volume=json.loads(docker('volume','inspect',name).stdout)[0]
    labels=object_labels(rid,request['project_id'],'volume')|{'paas.publisher.import':operation}
    if any((volume.get('Labels') or {}).get(k)!=v for k,v in labels.items()):raise ValueError('Volume is not owned by this import')
    base=Path(volume['Mountpoint']);stage=base.parent/('restore-'+operation)
    if base!=VOLUME_ROOT/name/'_data' or base.is_symlink():raise ValueError('Unexpected managed volume path')
    if receipt.exists():
        saved=json.loads(receipt.read_text())
        if saved['operation']!=operation:raise ValueError('Volume receipt belongs to another import')
        if base.stat().st_ino==saved['inode']:
            source.copy(None,source.remaining);source.finish();return
        if not stage.is_dir() or stage.stat().st_ino!=saved['inode']:
            raise ValueError('Prepared volume is missing; operator inspection required')
        source.copy(None,source.remaining);source.finish()
    else:
        if any(base.iterdir()) or docker('ps','-aq','--filter','volume='+name).stdout.strip():
            raise ValueError('Restore requires an empty unattached volume')
        if stage.exists():shutil.rmtree(stage)
        if shutil.disk_usage('/var/lib/docker').free<source.remaining+1024**3:raise ValueError('Tenant disk reserve is insufficient for volume restore')
        stage.mkdir(mode=0o700)
        count=0
        while length:=struct.unpack('!I',source.read(4))[0]:
            count+=1
            if count>20000 or length>2048:raise ValueError('Volume entry limit exceeded')
            entry=json.loads(source.read(length));name=entry['name'];path=PurePosixPath(name)
            if (not name or path.is_absolute() or path.as_posix()!=name or '..' in path.parts or name=='.' or len(name)>240 or
                    '\\' in name or any(ord(c)<32 for c in name)):
                raise ValueError('Unsafe volume path')
            target=stage.joinpath(*path.parts)
            if entry['directory']:
                if entry['size']!=0:raise ValueError('Invalid directory size')
                target.mkdir(mode=0o700)
            else:
                with target.open('xb') as out:source.copy(out,entry['size'])
                target.chmod(0o700 if entry['executable'] else 0o600)
            os.chown(target,10001,10001)
        source.finish();os.chown(stage,10001,10001)
        pending=receipt.with_suffix('.next')
        pending.write_text(json.dumps({'operation':operation,'inode':stage.stat().st_ino}));pending.replace(receipt)
    if any(base.iterdir()) or docker('ps','-aq','--filter','volume=paas-data-'+rid).stdout.strip():
        raise ValueError('Restore target became attached or nonempty')
    stage.replace(base)


def restore_data(stream):
    header=stream.readline(4097)
    if not header.endswith(b'\n') or len(header)>4096:raise ValueError('Invalid restore request')
    request=json.loads(header);resource=checked(request['resource_id']);checked(request['project_id']);checked(request['operation'])
    if request['kind'] not in {'database','volume'} or type(request['size']) is not int or not 0<request['size']<=DATA_LIMIT+16*1024**2:
        raise ValueError('Invalid data restore size or kind')
    RESTORE_ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (RESTORE_ROOT/(resource+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        source=RestoreInput(stream,request['size'])
        if request['kind']=='database':restore_database(request,source)
        else:restore_volume(request,source,RESTORE_ROOT/(resource+'.json'))
    return {'restored':True}


def main(request):
    actions={'ensure-volume':ensure_volume,'database-role':database_role,'inspect-database':inspect_database,'destroy':destroy,'migrate':migration,'stop-migration':stop_migration}
    if request['action'] not in actions:
        raise ValueError('Unsupported data operation')
    return actions[request['action']](request)


if __name__=='__main__':
    if len(sys.argv)>1:
        try:
            if sys.argv[1]=='export':capture_export(sys.argv[2],json.load(sys.stdin),sys.stdout.buffer)
            elif sys.argv[1]=='resume-export':resume_export(sys.argv[2],sys.argv[3])
            elif sys.argv[1]=='restore-data':print(json.dumps(restore_data(sys.stdin.buffer)))
            else:raise ValueError('Unknown runtime snapshot operation')
        except Exception as error:
            print('Project data transfer failed: '+(str(error) if isinstance(error,ValueError) else type(error).__name__),file=sys.stderr)
            raise SystemExit(1) from None
        raise SystemExit(0)
    try:
        print(json.dumps(main(json.load(sys.stdin))))
    except Exception:
        # PostgreSQL/Docker exception details can contain credentials or SQL.
        print(json.dumps({'error':'Managed data operation failed; inspect its bounded runtime state'}))
        raise SystemExit(1)
