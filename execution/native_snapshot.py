"""Portable native databases and owned application state in platform snapshots."""
import json
from pathlib import Path
import subprocess
import zipfile

from execution import native_archive, native_database, sandbox
from execution.database_storage import atomic_json
from publisher.transfer_wire import BoundedZipInput

FORMAT='poteto-native-postgres/1'


def provider(root, config, item, *, admit=None):
    def no_allocation(_):
        raise ValueError('Snapshot observation cannot allocate database storage')
    return native_database.NativeDatabase(root,config,item['resource_id'],item['account_id'],item['project_id'],
                                          admit=admit or no_allocation)


def inventory(root, config):
    result=[]
    for record in native_database.records(root):
        database=provider(root,config,record)
        record=database.read()
        if record['phase']!='ready':
            raise ValueError('Finish native database provisioning or deletion before a platform snapshot')
        database.storage.validate_image(database.storage.record())
        current=database.container(record)
        result.append({key:record[key] for key in ('resource_id','account_id','project_id')}|
                      {'running':bool(current and current['State']['Running'])})
    return sorted(result,key=lambda item:item['resource_id'])


def applications(root):
    result=[]
    for path in (Path(root)/'sandboxes').glob('*.json'):
        release=native_database.checked(path.stem);record=json.loads(path.read_text())
        account=native_database.checked(record['account_id']);project=native_database.checked(record['project_id'])
        current=sandbox.inspect('container','poteto-sbx-'+release)
        if current:sandbox.owned(current['Config']['Labels'],sandbox.labels(release,account,project))
        result.append({'release_id':release,'account_id':account,'project_id':project,
                       'running':bool(current and current['State']['Running'])})
    return sorted(result,key=lambda item:item['release_id'])


def set_applications(items, *, resume=False):
    for item in items:
        if not item['running']:continue
        release=native_database.checked(item['release_id']);name='poteto-sbx-'+release
        current=sandbox.inspect('container',name)
        if current is None:raise ValueError('A snapshotted application container is missing')
        sandbox.owned(current['Config']['Labels'],sandbox.labels(release,item['account_id'],item['project_id']))
        if resume and not current['State']['Running']:sandbox.docker('start',name)
        if not resume and current['State']['Running']:sandbox.docker('stop','--time','30',name,timeout=45)


def restore_running(root, config, items):
    failures=[]
    for item in items:
        try:
            database=provider(root,config,item);record=database.read()
            if item['running']:
                database.ensure(record['admin_password'],record['app_password'],import_id=record.get('import_id'))
            else:
                database.stop()
        except (ValueError,OSError,subprocess.SubprocessError) as error:
            failures.append(item['resource_id']+': '+str(error))
    return failures


def capture(root, config, items, operation, destination):
    destination=Path(destination);destination.mkdir(mode=0o700,parents=True,exist_ok=True)
    for item in items:
        database=provider(root,config,item);record=database.read()
        directory=destination/item['resource_id'];directory.mkdir(mode=0o700)
        atomic_json(directory/'database.json',record)
        body={key:item[key] for key in ('resource_id','account_id','project_id')}|{'operation':operation}
        try:
            database.ensure(record['admin_password'],record['app_password'],import_id=record.get('import_id'))
            with (directory/'data.zip').open('xb') as output:
                (directory/'data.zip').chmod(0o600)
                native_archive.dump(root,config,body,output)
        finally:
            try:native_archive.finish(root,config,body,'dump')
            finally:
                if not item['running']:database.stop()


def validate(source, items):
    if not isinstance(items,list) or len(items)>64:
        raise ValueError('Invalid native database snapshot inventory')
    expected=[]
    for item in items:
        ids={key:native_database.checked(item[key]) for key in ('resource_id','account_id','project_id')}
        if type(item.get('running')) is not bool:
            raise ValueError('Native database snapshot has no runtime state')
        directory=Path(source)/ids['resource_id']
        record=json.loads((directory/'database.json').read_text())
        if any(record.get(key)!=value for key,value in ids.items()) or record.get('phase')!='ready' or any(
                not isinstance(record.get(key),str) or not native_database.PASSWORD.fullmatch(record[key])
                for key in ('admin_password','app_password')):
            raise ValueError('Native snapshot ownership or credentials do not match')
        if record.get('import_id') is not None:native_database.checked(record['import_id'])
        path=directory/'data.zip'
        if not 0<path.stat().st_size<=native_archive.LIMIT+16*1024**2:
            raise ValueError('Native database snapshot is too large')
        with path.open('rb') as stream,zipfile.ZipFile(BoundedZipInput(stream)) as archive:
            members=archive.infolist();name='databases/'+ids['resource_id']+'.dump'
            if len(members)!=1 or members[0].filename!=name or members[0].compress_type!=zipfile.ZIP_STORED or (
                    members[0].flag_bits&1 or not 5<=members[0].file_size<=native_archive.LIMIT):
                raise ValueError('Invalid native database archive member')
            with archive.open(name) as data:
                if data.read(5)!=b'PGDMP':raise ValueError('Invalid PostgreSQL custom archive')
        expected.append(ids)
    if len({item['resource_id'] for item in expected})!=len(expected):
        raise ValueError('Duplicate native snapshot resource')
    return expected


def restore(source, root, config, items, operation, *, admit):
    operation=native_database.checked(operation);expected=validate(source,items)
    if not expected:return []
    journal=Path(root)/('native-restore-'+operation+'.json')
    if journal.exists():
        if json.loads(journal.read_text())!={'operation':operation,'resources':expected}:
            raise ValueError('Native restore operation belongs to another inventory')
    else:
        if any(provider(root,config,item).path.exists() for item in expected):
            raise ValueError('Native restore cannot overwrite an existing database')
        admit(len(expected)*2048*1024**2)
        atomic_json(journal,{'operation':operation,'resources':expected})
    completed=[]
    for item in expected:
        database=provider(root,config,item,admit=admit)
        original=json.loads((Path(source)/item['resource_id']/'database.json').read_text())
        current=database.read()
        if current and current.get('platform_restore')==operation:
            database.storage.validate_image(database.storage.record())
            completed.append(item['resource_id']);continue
        if current and current.get('import_id')!=operation:
            raise ValueError('Native database is not owned by this restore operation')
        database.ensure(original['admin_password'],original['app_password'],import_id=operation)
        with (Path(source)/item['resource_id']/'data.zip').open('rb') as stream,zipfile.ZipFile(BoundedZipInput(stream)) as archive:
            name='databases/'+item['resource_id']+'.dump'
            with archive.open(name) as payload:
                native_archive.restore(root,config,item|{'operation':operation,'size':archive.getinfo(name).file_size},payload)
        current=database.read();current.pop('import_id',None)
        if original.get('import_id'):current['import_id']=original['import_id']
        current['platform_restore']=operation;atomic_json(database.path,current)
        completed.append(item['resource_id'])
    return completed
