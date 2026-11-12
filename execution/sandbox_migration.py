"""One supervised, immutable migration per owned prebuilt preview release."""
import json
from pathlib import Path
import shlex
import subprocess
import sys

from execution import sandbox
from execution.database_storage import atomic_json
from execution.native_database import binding, checked

TIMEOUT = 300
CODE = Path(__file__).resolve().parents[1]


def path(root, release):
    return Path(root)/'sandbox-migrations'/(checked(release)+'.json')


def read(root, release, account=None, project=None):
    source = path(root, release)
    if not source.exists():
        return None
    value = json.loads(source.read_text())
    if value['release_id'] != release or (account is not None and value['account_id'] != account) or (
            project is not None and value['project_id'] != project):
        raise ValueError('Migration does not belong to this project')
    return value


def name(release):
    return 'poteto-migration-'+checked(release)


def unit(release):
    return 'paas-migration-'+checked(release)+'.service'


def labels(record):
    return {'poteto.migration.release':record['release_id'], 'poteto.migration.account':record['account_id'],
            'poteto.migration.project':record['project_id'], 'poteto.migration.database':record['database_id']}


def container(record):
    current = sandbox.inspect('container', name(record['release_id']))
    if current is None:
        return None
    sandbox.owned(current['Config']['Labels'], labels(record))
    host = current['HostConfig']
    if (current['Image'] not in record['image_ids'] or current['Config']['User'] != '10001:10001'
            or current['Config']['Entrypoint'] != ['sh','-c'] or current['Config']['Cmd'] != [record['command']]
            or host['Runtime'] != sandbox.RUNTIME or not host['ReadonlyRootfs'] or host['Privileged']
            or host['Memory'] != record['memory_mb']*1024**2 or host['MemorySwap'] != host['Memory']
            or host['NanoCpus'] != round(record['cpus']*10**9) or host['PidsLimit'] != 128
            or host['CgroupParent'] != 'paas-sandbox.slice' or host['CapDrop'] != ['ALL']
            or not set(host['SecurityOpt']) & {'no-new-privileges','no-new-privileges:true'}
            or host['NetworkMode'] != record['database']['network'] or host.get('PortBindings')
            or current.get('Mounts') or host['RestartPolicy']['Name'] not in {'','no'}):
        raise ValueError('Migration runtime does not match its ownership receipt')
    expected = [f'db-{record["database_id"]}:{record["database"]["address"]}']
    if host['ExtraHosts'] != expected or set(current['NetworkSettings']['Networks']) != {record['database']['network']}:
        raise ValueError('Migration is attached to an unexpected network')
    environment = dict(value.split('=',1) for value in current['Config'].get('Env', []))
    if any(environment.get(key) != value for key,value in record['environment'].items()):
        raise ValueError('Migration environment does not match its receipt')
    return current


def create(record):
    body = {'Image':record['image'], 'Tty':True, 'User':'10001:10001', 'Labels':labels(record),
            'Entrypoint':['sh','-c'], 'Cmd':[record['command']],
            'Env':[key+'='+value for key,value in record['environment'].items()],
            'Healthcheck':{'Test':['NONE']},
            'HostConfig':{'Runtime':sandbox.RUNTIME, 'CgroupParent':'paas-sandbox.slice',
                'NetworkMode':record['database']['network'], 'ReadonlyRootfs':True, 'CapDrop':['ALL'],
                'SecurityOpt':['no-new-privileges:true'], 'Memory':record['memory_mb']*1024**2,
                'MemorySwap':record['memory_mb']*1024**2, 'NanoCpus':round(record['cpus']*10**9),
                'PidsLimit':128, 'Dns':['127.0.0.1'], 'RestartPolicy':{'Name':'no'},
                'ExtraHosts':[f'db-{record["database_id"]}:{record["database"]["address"]}'],
                'Tmpfs':{'/tmp':'rw,noexec,nosuid,size=67108864'},
                'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}}},
            'NetworkingConfig':{'EndpointsConfig':{record['database']['network']:{}}}}
    connection = sandbox.DockerSocket('localhost', timeout=30)
    try:
        connection.request('POST', '/containers/create?name='+name(record['release_id']), json.dumps(body).encode(),
                           {'Content-Type':'application/json'})
        response = connection.getresponse(); response.read(65536)
        if response.status != 201:
            raise ValueError('Docker did not create the migration container')
    finally:
        connection.close()


def start_unit(release):
    cleanup = shlex.join(['/usr/bin/python3','-m','execution.sandbox_migration','finish',release])
    subprocess.run(['systemd-run','--quiet','--unit='+unit(release),'--property=Type=exec',
        '--property=WorkingDirectory='+str(CODE), '--property=RuntimeMaxSec='+str(TIMEOUT+15),
        '--property=TimeoutStopSec=45', '--property=KillMode=control-group', '--property=Restart=no',
        '--property=ExecStopPost='+cleanup, '/usr/bin/python3','-m','execution.sandbox_migration','run',release],
        check=True, capture_output=True, timeout=15)


def stop_unit(release):
    current = subprocess.run(['systemctl','show',unit(release),'--property=LoadState','--value'],
                             capture_output=True,text=True,timeout=15)
    if current.stdout.strip() == 'not-found':
        return
    if current.returncode or current.stdout.strip() != 'loaded':
        raise ValueError('Migration supervisor status is unavailable')
    subprocess.run(['systemctl','stop',unit(release)],check=True,capture_output=True,timeout=60)


def finish(root, release):
    record = read(root, release)
    if record is None:
        return
    current = container(record)
    if current and current['State']['Running']:
        record['forced_failure'] = True
        sandbox.docker('stop','--time','10',name(release),timeout=25)
        current = container(record)
    record['phase'] = 'succeeded' if current and current['State']['Status']=='exited' and (
        current['State']['ExitCode']==0 and not record.get('forced_failure')) else 'failed'
    atomic_json(path(root,release),record)


def run(root, release):
    record = read(root,release)
    if not record or record['phase'] != 'prepared':
        raise ValueError('Migration has already started; it cannot run again')
    try:
        if container(record) is None:
            raise ValueError('Migration container is missing')
        record['phase']='running';atomic_json(path(root,release),record)
        sandbox.docker('start',name(release))
        current = container(record)
        if current['State']['Running']:
            sandbox.observe_limits(current,record['memory_mb'],record['cpus'])
        sandbox.docker('wait',name(release),timeout=TIMEOUT)
    except BaseException:
        record['forced_failure']=True;atomic_json(path(root,release),record)
        raise
    finally:
        finish(root,release)


def result(root, record):
    current = container(record)
    terminal = record['phase'] in {'succeeded','failed'}
    if current is None:
        if record['phase'] == 'failed':
            return {'state':'failed','log':'Migration setup did not complete; deploy a new release after inspection'}
        raise ValueError('Migration container is missing; do not resubmit the same migration')
    log = ''
    if terminal:
        log = ('Migration container did not start; inspect its supervisor' if current['State']['Status']=='created'
               else sandbox.docker('logs','--tail','200',name(record['release_id']))[-32768:])
    return {'state':record['phase'] if terminal else 'running','log':log}


def operation(root, config, release, body, build_status):
    account, project = checked(body.get('account_id')), checked(body.get('project_id'))
    record = read(root,release,account,project)
    if body['action'] == 'stop-migration':
        if record:
            record['forced_failure']=True;atomic_json(path(root,release),record)
            stop_unit(release)
            finish(root,release)
        return {'stopped':True}
    database = checked(body.get('database_id'))
    memory, cpu, _, environment = sandbox.validate(body|{'port':3000})
    command = body.get('command')
    if not isinstance(command,str) or not 0<len(command)<=2000 or '\x00' in command:
        raise ValueError('Invalid migration command')
    specification = {'account_id':account, 'project_id':project, 'database_id':database,
                     'command':command, 'environment':environment, 'memory_mb':memory, 'cpus':cpu}
    if record:
        if any(record[key] != value for key,value in specification.items()):
            raise ValueError('Migration definition cannot change after submission')
        return result(root,record)
    if config.get('personal_previews') is not True or config.get('native_databases') is not True:
        raise ValueError('Native preview migrations are not enabled')
    build = json.loads((Path(root)/'builds'/release/'spec.json').read_text())
    status = build_status(release)
    if (build.get('account_id'),build.get('project_id'),build.get('runtime'),build.get('personal_preview'),
            status.get('driver'),status.get('state'),status.get('image')) != (
            account,project,'image',True,'prebuilt','succeeded','paas-release:'+release):
        raise ValueError('Migration requires this project\'s successful prebuilt image')
    bound = binding(root,config,database,account,project)
    sandbox.check_pool(Path(root),config,release,memory,cpu)
    source = Path(root)/'builds'/release/'image.tar'
    image = 'paas-release:'+release
    identities = sandbox.image_identities(source,image)
    directory = path(root,release).parent;directory.mkdir(mode=0o700,exist_ok=True)
    record = specification|{'release_id':release,'image':image,'image_ids':identities,'database':bound,'phase':'prepared'}
    atomic_json(path(root,release),record)
    try:
        if not sandbox.has_tag(image):
            with source.open('rb') as archive:
                sandbox.docker('load','-q',image=archive,timeout=180)
        details = json.loads(sandbox.docker('image','inspect',image))[0]
        if details['Id'] not in identities or details['Config'].get('Volumes'):
            raise ValueError('Migration image does not match its normalized archive')
        create(record)
    except BaseException:
        record['phase']='failed';atomic_json(path(root,release),record)
        raise
    start_unit(release)
    return {'state':'running','log':''}


def completed_image(root, release, account, project):
    record = read(root,release,account,project)
    if record is None:
        return None
    if record['phase'] != 'succeeded':
        raise ValueError('Migration must succeed before starting the preview')
    return record['image_ids']


def remove(root, release, account, project):
    record = read(root,release,account,project)
    if record is None:
        return None
    stop_unit(release)
    finish(root,release)
    current = container(record)
    if current:
        if current['State']['Running']:
            raise ValueError('Migration is still running')
        sandbox.docker('rm',name(release))
    path(root,release).unlink()
    return record['image_ids']


def main():
    from execution.host import ROOT
    if len(sys.argv) != 3 or sys.argv[1] not in {'run','finish'}:
        raise SystemExit('Use run or finish and a release ID')
    {'run':run,'finish':finish}[sys.argv[1]](ROOT,checked(sys.argv[2]))


if __name__ == '__main__':
    main()
