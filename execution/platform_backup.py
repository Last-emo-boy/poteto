"""Cold multi-VM snapshots. This administrator tool never stops unrelated services."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from execution.host import admit_space, checked_id, unit_state, write_json, wait_ready

ROOT=Path('/srv/ovh-paas-platform')
CONTROL=Path('/srv/ovh-paas')
EXECUTION=Path('/srv/ovh-paas-execution')
CODE=Path('/opt/paas-execution')


def run(*args,**kwargs):
    return subprocess.run(list(args),check=True,capture_output=True,**kwargs)


def directory(operation):
    return ROOT/'backups'/checked_id(operation)


def recipient(value):
    # Only built-in X25519 recipients, never an executable age plugin.
    if not re.fullmatch(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}',value):
        raise ValueError('Use a standard age X25519 public recipient')
    return value


def inventory():
    placements=json.loads((EXECUTION/'placements.json').read_text())
    records=[{'role':'controller','unit':'ovh-paas-vm.service','disk':str(CONTROL/'vm/root.qcow2'),'seed':str(CONTROL/'vm/seed.iso')}]
    for account,record in placements.items():
        checked_id(account)
        slot=record['slot']
        if not isinstance(slot,int) or not 0<=slot<4 or record['state']!='ready':
            raise ValueError('Finish or repair runtime provisioning before taking a platform snapshot')
        records.append({'role':'runtime'+str(slot),'account_id':account,'slot':slot,
            'unit':f'paas-runtime@{slot}.service','disk':str(EXECUTION/'vm'/f'runtime{slot}'/'root.qcow2'),
            'seed':str(EXECUTION/'vm'/f'runtime{slot}'/'seed.iso')})
    return records


def build_units_active():
    result=run('systemctl','list-units','--state=active,activating,deactivating','--plain','--no-legend','paas-build@*.service')
    return bool(result.stdout.strip())


def prepare(operation,request):
    recipient(request['recipient'])
    # Check age's checksum/parser before entering any downtime.
    run(str(CODE/'age'),'-r',request['recipient'],'/dev/null')
    path=directory(operation)
    path.mkdir(parents=True,mode=0o700,exist_ok=False)
    os.chmod(path,0o700)
    vms=inventory()
    for item in vms:
        if not Path(item['disk']).is_file() or not Path(item['seed']).is_file():
            raise ValueError('A persistent VM disk or seed is missing')
    units=[item['unit'] for item in vms]+['paas-execution-api.service']
    states={unit:unit_state(unit) for unit in units}
    if any(state not in {'active','inactive','failed'} for state in states.values()):
        raise ValueError('A VM/service is transitioning; wait for its actual operation to finish')
    value=request|{'format':'ovh-paas-platform/2','id':operation,'created':time.time(),'vms':vms,
        'resume_units':[unit for unit,state in states.items() if state=='active']}
    write_json(path/'request.json',value)
    write_json(path/'status.json',{'id':operation,'state':'prepared','created':value['created']})
    return value


def phase(path,name,**fields):
    previous=json.loads((path/'status.json').read_text())
    write_json(path/'status.json',previous|{'state':name,'updated':time.time()}|fields)


def copy_file(source,target,hardlink=False):
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if hardlink:
        os.link(source,target)
    else:
        shutil.copy2(source,target,follow_symlinks=True)


def copy_tree(source,target):
    shutil.copytree(source,target,symlinks=False,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))


def metadata(stage,request):
    destination=stage/'executor'
    destination.mkdir(mode=0o700)
    for name in ['admin.key','admin.key.pub','api.token','config.json','placements.json','known_hosts','resolv.conf','base.img','template.qcow2','wake-proxy-1.tar']:
        source=EXECUTION/name
        if source.exists():copy_file(source,destination/'state'/name)
    if (EXECUTION/'retired').is_dir():copy_tree(EXECUTION/'retired',destination/'state/retired')
    copy_tree(CODE,destination/'code')
    builds=EXECUTION/'builds'
    for job in builds.iterdir():
        if not job.is_dir() or not re.fullmatch('[a-f0-9]{24}',job.name):
            continue
        for name in ['status.json','spec.json','source.tar.gz','build.log','image.tar']:
            if (job/name).is_file():copy_file(job/name,destination/'state/builds'/job.name/name,hardlink=True)
    for name in ['bin','infra','scripts']:
        copy_tree(CONTROL/name,stage/'controller-host'/name)
    for name in ['config.env','resolv.conf','egress.nft','publisher-edge.token','publisher-domains.json']:
        if (CONTROL/name).is_file():copy_file(CONTROL/name,stage/'controller-host'/name)
    units=stage/'units';units.mkdir(mode=0o700)
    for pattern in ['ovh-paas-*.service','paas-*.service','paas-execution.slice']:
        for file in Path('/etc/systemd/system').glob(pattern):
            if file.is_file():copy_file(file,units/file.name)
    for name in ['ovh-paas-vm.service.d','ovh-paas-egress.service.d']:
        source=Path('/etc/systemd/system')/name
        if source.exists():copy_tree(source,units/name)
    manifest={key:request[key] for key in ['format','id','created','code_revision','vms']}
    manifest['encryption']='age/X25519'
    manifest['restore']='standalone qcow2; restore clones must use independent UIDs and loopback ports'
    write_json(stage/'manifest.json',manifest)


def resume(operation,allow_active=False):
    path=directory(operation)
    request=json.loads((path/'request.json').read_text())
    unit='paas-platform-backup-'+operation+'.service'
    state=unit_state(unit)
    main=int(run('systemctl','show',unit,'--property=MainPID','--value').stdout.strip() or '0')
    if not allow_active and state in {'active','activating','deactivating'} and main:
        raise ValueError('Backup process is still alive; observe that same job instead of restarting its VMs')
    # Supervisor first, then tenants, finally the controller.
    order=sorted(request['resume_units'],key=lambda name:(name=='ovh-paas-vm.service',name!='paas-execution-api.service',name))
    path=EXECUTION/'placements.json'
    placements=json.loads(path.read_text()) if path.exists() else {}
    for name in order:
        item=next((v for v in request['vms'] if v['unit']==name and v['role'].startswith('runtime')),None)
        if item and (placements.get(item['account_id'],{}).get('slot')!=item['slot'] or not Path(item['disk']).is_file()):
            continue
        if unit_state(name) not in {'active','activating'}:
            run('systemctl','start',name,timeout=180)
            if item:
                wait_ready(item['role'],22100+item['slot'],'runtime-'+item['account_id'],timeout=180)


def snapshot(operation):
    path=directory(operation);request=json.loads((path/'request.json').read_text())
    with (ROOT/'backup.lock').open('a') as lock, (EXECUTION/'builder.lock').open('a') as builder:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            fcntl.flock(builder, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A build or builder preparation still owns the disposable guest') from None
        if build_units_active():
            raise ValueError('An actual build unit is still active; wait for its supervisor')
        stage=path/'payload'
        if stage.exists():raise ValueError('Snapshot payload already exists; inspect this operation before starting a new one')
        # Two copies plus the host reserve cover flattened snapshots and the
        # encrypted archive; sparse virtual capacity is not treated as free space.
        used=sum(Path(v['disk']).stat().st_blocks*512 for v in request['vms'])
        used+=sum(p.stat().st_blocks*512 for p in [EXECUTION/'template.qcow2',EXECUTION/'base.img'])
        used+=sum(p.stat().st_blocks*512 for p in (EXECUTION/'builds').rglob('image.tar'))
        admit_space(incoming=used*2)
        stage.mkdir(mode=0o700)
        phase(path,'stopping')
        try:
            run('systemctl','stop','paas-execution-api.service',timeout=180)
            if inventory()!=request['vms']:
                raise ValueError('Tenant placement changed after backup preparation; start a new inventory')
            for item in request['vms']:
                if unit_state(item['unit']) in {'active','activating'}:
                    run('systemctl','stop',item['unit'],timeout=180)
                if unit_state(item['unit']) not in {'inactive','failed'} or run('systemctl','show',item['unit'],'--property=Result','--value').stdout.strip()!=b'success':
                    raise ValueError('VM did not shut down cleanly; live disk copying is refused')
            phase(path,'snapshotting')
            for item in request['vms']:
                folder=stage/'vms'/item['role'];folder.mkdir(parents=True,mode=0o700)
                run('nice','-n','10','ionice','-c','2','-n','7','qemu-img','convert','-c','-m','2','-f','qcow2','-O','qcow2',item['disk'],str(folder/'disk.qcow2'),timeout=1800)
                info=json.loads(run('qemu-img','info','--output=json',str(folder/'disk.qcow2')).stdout)
                if info.get('backing-filename'):raise ValueError('Snapshot is not standalone')
                write_json(folder/'image.json',info)
                copy_file(Path(item['seed']),folder/'seed.iso')
            metadata(stage,request)
        finally:
            resume(operation,allow_active=True)
        payload_bytes=sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
        phase(path,'encrypting',payload_bytes=payload_bytes)
        target=path/'platform.tar.gz.age.part'
        with target.open('xb') as out:
            encryption=subprocess.Popen([str(CODE/'age'),'-r',request['recipient']],stdin=subprocess.PIPE,stdout=out,stderr=subprocess.PIPE)
            try:
                with tarfile.open(fileobj=encryption.stdin,mode='w|gz',dereference=True,compresslevel=1) as archive:
                    archive.add(stage,arcname='platform')
                encryption.stdin.close()
                if encryption.wait(timeout=1800):raise RuntimeError('Backup encryption failed')
            finally:
                if encryption.poll() is None:
                    encryption.terminate();encryption.wait(timeout=30)
        target.replace(path/'platform.tar.gz.age')
        phase(path,'completed',bytes=(path/'platform.tar.gz.age').stat().st_size,vm_count=len(request['vms']))
        shutil.rmtree(stage)


def main():
    parser=argparse.ArgumentParser(description='Administrator-only encrypted multi-VM snapshots')
    parser.add_argument('action',choices=['prepare','run','status','recover'])
    parser.add_argument('operation')
    args=parser.parse_args()
    os.umask(0o077)
    if os.geteuid()!=0:raise SystemExit('Root is required')
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o755)
    (ROOT/'backups').mkdir(exist_ok=True,mode=0o700)
    if args.action=='prepare':
        request=json.load(sys.stdin);prepare(args.operation,request)
    elif args.action=='run':
        def stop(*_):raise InterruptedError('Backup service was stopped')
        signal.signal(signal.SIGTERM,stop)
        try:snapshot(args.operation)
        except BaseException as error:
            phase(directory(args.operation),'failed',error=type(error).__name__+': '+str(error))
            raise
    elif args.action=='recover':resume(args.operation)
    print((directory(args.operation)/'status.json').read_text())


if __name__=='__main__':main()
