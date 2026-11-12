"""Bounded PostgreSQL archives for owned native databases, using application roles."""
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import zipfile

from execution import sandbox
from execution.native_database import NativeDatabase, binding, checked
from execution.runtime_data import RestoreInput

LIMIT = 2*1024**3


def database(root, config, request):
    account, project, resource, operation = (checked(request[key]) for key in (
        'account_id','project_id','resource_id','operation'))
    def no_allocation(_):
        raise ValueError('An archive operation cannot allocate a database')
    value = NativeDatabase(root,config,resource,account,project,admit=no_allocation)
    binding(root,config,resource,account,project)
    return value, operation


def application_name(action, operation):
    if action not in {'dump','restore'}:
        raise ValueError('Unsupported native archive operation')
    return 'poteto-'+action+'-'+checked(operation)


def finish(root, config, request, action):
    value, operation = database(root,config,request)
    application = application_name(action,operation)
    value.sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"' AND pid<>pg_backend_pid();")


def dump(root, config, request, output):
    value, operation = database(root,config,request)
    process = subprocess.Popen(['/usr/bin/docker','--host','unix:///var/run/docker.sock','exec',
        '--env','PGAPPNAME='+application_name('dump',operation),
        '--env','PGOPTIONS=-c statement_timeout=330000 -c lock_timeout=30000',value.name,
        'pg_dump','--username=app','--dbname=app','--format=custom','--compress=0',
        '--no-owner','--no-privileges','--no-password'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        with zipfile.ZipFile(output,'w') as archive:
            with archive.open('databases/'+value.resource+'.dump','w',force_zip64=True) as target:
                total=0
                while chunk:=process.stdout.read(1024**2):
                    total+=len(chunk)
                    if total>LIMIT:
                        raise ValueError('Native database archive exceeds 2 GiB')
                    target.write(chunk)
                if not total or process.wait(timeout=15):
                    raise ValueError('Native PostgreSQL snapshot failed')
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill();process.wait()


def restore(root, config, request, source):
    value, operation = database(root,config,request)
    if value.read().get('import_id') != operation:
        raise ValueError('Restore requires a new database owned by this import')
    incoming = RestoreInput(source,request['size'])
    if not 0<incoming.remaining<=LIMIT:
        raise ValueError('Native restore payload must not exceed 2 GiB')
    # This ledger is in the administrative postgres database, not in the
    # application-owned database represented by the untrusted archive.
    value.sql('CREATE TABLE IF NOT EXISTS public.paas_restore_receipts(operation text PRIMARY KEY);')
    completed = value.sql("SELECT operation FROM public.paas_restore_receipts WHERE operation='"+operation+"';")
    if completed == operation:
        incoming.copy(None,incoming.remaining);incoming.finish()
    else:
        for directory in ('sandboxes','sandbox-migrations'):
            if any(json.loads(path.read_text()).get('database_id')==value.resource
                   for path in (Path(root)/directory).glob('*.json')):
                raise ValueError('Cannot restore over a database with retained application bindings')
        value.sql('DROP DATABASE IF EXISTS paas_restore WITH (FORCE); CREATE DATABASE paas_restore OWNER app;')
        if shutil.disk_usage(value.storage.mountpoint).free < incoming.remaining+64*1024**2:
            raise ValueError('Native database filesystem cannot reserve the restore workspace')
        value.sql('REVOKE ALL ON SCHEMA public FROM PUBLIC; GRANT USAGE,CREATE ON SCHEMA public TO app;',database='paas_restore')
        process = subprocess.Popen(['/usr/bin/docker','--host','unix:///var/run/docker.sock','exec','-i',
            '--env','PGAPPNAME='+application_name('restore',operation),
            '--env','PGOPTIONS=-c statement_timeout=840000',value.name,
            'pg_restore','--username=app','--dbname=paas_restore','--single-transaction','--exit-on-error',
            '--no-owner','--no-privileges','--no-tablespaces','--no-publications','--no-subscriptions',
            '--no-security-labels','--no-password'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            incoming.copy(process.stdin,incoming.remaining);incoming.finish();process.stdin.close()
            if process.wait(timeout=840):
                raise ValueError('Native PostgreSQL restore failed; original database was retained')
        finally:
            if process.poll() is None:
                process.kill();process.wait()
            if not process.stdin.closed:
                process.stdin.close()
        value.sql("BEGIN; ALTER DATABASE app RENAME TO paas_restore_old; ALTER DATABASE paas_restore RENAME TO app; "
                  "INSERT INTO public.paas_restore_receipts VALUES ('"+operation+"'); COMMIT;")
    value.sql('DROP DATABASE IF EXISTS paas_restore_old WITH (FORCE);')
    return {'restored':True}


def main():
    from execution.host import ROOT
    if len(sys.argv)<2 or sys.argv[1] not in {'dump','restore','finish-dump','finish-restore'}:
        raise SystemExit('Use dump, restore, finish-dump or finish-restore')
    action=sys.argv[1]
    if action.startswith('finish-'):
        if len(sys.argv)!=6:
            raise SystemExit('Cleanup requires account, project, resource and operation IDs')
        request=dict(zip(('account_id','project_id','resource_id','operation'),map(checked,sys.argv[2:])))
    else:
        if len(sys.argv)!=2:
            raise SystemExit('Archive requests use stdin')
        request=json.loads(sys.stdin.buffer.readline(8192))
    config=json.loads((ROOT/'config.json').read_text())
    def interrupted(*_):
        raise InterruptedError('Native archive supervisor stopped')
    signal.signal(signal.SIGTERM,interrupted)
    if action.startswith('finish-'):
        finish(ROOT,config,request,action.removeprefix('finish-'))
    elif action=='dump':
        dump(ROOT,config,request,sys.stdout.buffer)
    else:
        print(json.dumps(restore(ROOT,config,request,sys.stdin.buffer)))


if __name__=='__main__':
    main()
