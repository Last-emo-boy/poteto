"""Authenticate on the operator computer, then extract and boot offline restore clones."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
from execution.host import admit_space, checked_id, unit_state, write_json
from publisher.archive import unpack
from execution import native_snapshot

ROOT=Path('/srv/ovh-paas-platform/restore')
CODE=Path('/opt/paas-execution')
EXECUTION=Path('/srv/ovh-paas-execution')


def run(*args,**kwargs):return subprocess.run(list(args),check=True,capture_output=True,**kwargs)


def folder(operation):return ROOT/checked_id(operation)


def role_checked(role):
    if role!='controller' and not re.fullmatch('runtime[0-3]',role):raise ValueError('Invalid restore role')
    return role


def ports(role):
    role_checked(role)
    return (22522,22580) if role=='controller' else (22600+int(role[-1]),22700+int(role[-1]))


def user(role):return 'paasrestorec' if role=='controller' else 'paasrestorer'+role[-1]


def prepare(operation,request):
    for key in ['archive_bytes','payload_bytes','vm_count']:
        if not isinstance(request[key],int) or request[key]<=0:raise ValueError('Invalid restore size/inventory')
    if request['vm_count']>5:raise ValueError('Unexpected VM count')
    # Reserve both received tar and extracted files plus bounded COW/serial growth.
    admit_space(incoming=request['archive_bytes']+request['payload_bytes']+request['vm_count']*4*1024**3)
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o755)
    ROOT.parent.chmod(0o755);ROOT.chmod(0o755)
    path=folder(operation);path.mkdir(mode=0o700)
    write_json(path/'status.json',request|{'id':operation,'state':'prepared'})


def receive(operation):
    path=folder(operation);state=json.loads((path/'status.json').read_text())
    if state['state']!='prepared':raise ValueError('Restore input already exists; inspect or clean this restore operation')
    size=0
    with (path/'incoming.tar.gz').open('xb') as out:
        while chunk:=sys.stdin.buffer.read(1024**2):
            size+=len(chunk)
            if size>state['archive_bytes']:raise ValueError('Decrypted input exceeds its authenticated archive bound')
            out.write(chunk)
    write_json(path/'status.json',state|{'state':'received','received_bytes':size})


def extract(operation):
    path=folder(operation);state=json.loads((path/'status.json').read_text())
    if state['state']!='received':raise ValueError('Finish and authenticate decryption before extraction')
    destination=path/'payload';destination.mkdir(mode=0o700)
    unpack(path/'incoming.tar.gz',destination,max_bytes=state['payload_bytes'],max_files=20000,private=True)
    manifest=json.loads((destination/'platform/manifest.json').read_text())
    if manifest['format'] not in {'ovh-paas-platform/2','ovh-paas-platform/3'} or len(manifest['vms'])!=state['vm_count']:
        raise ValueError('Backup manifest does not match the completed snapshot')
    roles=[role_checked(v['role']) for v in manifest['vms']]
    if len(set(roles))!=len(roles) or 'controller' not in roles:raise ValueError('Invalid VM inventory')
    for item in manifest['vms']:
        if item['role']!='controller':checked_id(item['account_id'])
        for name in ['disk.qcow2','seed.iso']:
            if not (destination/'platform/vms'/item['role']/name).is_file():raise ValueError('Snapshot VM files are missing')
    databases=manifest.get('native_databases',[])
    if databases:
        if manifest['format']!='ovh-paas-platform/3' or manifest.get('native_database_format')!=native_snapshot.FORMAT:
            raise ValueError('Unsupported native database snapshot format')
        native_snapshot.validate(destination/'platform/executor/native-databases',databases)
    (path/'incoming.tar.gz').unlink()
    write_json(path/'status.json',state|{'state':'extracted','vms':manifest['vms'],'native_databases':databases})


def restore_native(operation):
    path=folder(operation);state=json.loads((path/'status.json').read_text())
    if state['state'] not in {'extracted','stopped'}:
        raise ValueError('Extract the authenticated backup before restoring native databases')
    if unit_state('paas-execution-api.service') not in {'inactive','failed'}:
        raise ValueError('Stop execution admission before restoring native databases')
    from execution.host import lock
    with lock('sandbox-placements'):
        restored=native_snapshot.restore(path/'payload/platform/executor/native-databases',EXECUTION,
            json.loads((EXECUTION/'config.json').read_text()),state.get('native_databases',[]),operation,
            admit=lambda size:admit_space(incoming=size))
    write_json(path/'status.json',state|{'native_restored':restored})
    return restored


def isolation():
    names=['paasrestorec',*[f'paasrestorer{i}' for i in range(4)]]
    for name in names:
        try:pwd.getpwnam(name)
        except KeyError:run('useradd','--system','--home','/nonexistent','--shell','/usr/sbin/nologin','--groups','kvm',name)
    uids=', '.join(str(pwd.getpwnam(name).pw_uid) for name in names)
    rules=f'''destroy table inet ovh_paas_restore
table inet ovh_paas_restore {{
 chain offline_output {{
  type filter hook output priority -20; policy accept;
  meta skuid {{ {uids} }} ct state established,related accept
  meta skuid {{ {uids} }} counter reject
 }}
}}
'''
    run('nft','-c','-f','-',input=rules.encode());run('nft','-f','-',input=rules.encode())


def qemu(operation,role):
    role_checked(role);path=folder(operation)/'vms'/role;ssh,http=ports(role)
    network=f'user,id=net0,net=10.201.0.0/24,host=10.201.0.2,dns=10.201.0.3,hostfwd=tcp:127.0.0.1:{ssh}-:22,hostfwd=tcp:127.0.0.1:{http}-:80'
    return ['/usr/bin/qemu-system-x86_64','-name','paas-restore-'+role,'-machine','q35,accel=kvm','-cpu','host',
        '-smp','2','-m','8192' if role=='controller' else '6144','-display','none','-nodefaults',
        '-serial','file:'+str(path/'serial.log'),'-monitor','unix:'+str(path/'monitor.sock')+',server=on,wait=off',
        '-drive','file='+str(path/'root.qcow2')+',if=virtio,format=qcow2,discard=unmap',
        '-drive','file='+str(path/'seed.iso')+',if=virtio,format=raw,readonly=on',
        '-netdev',network,'-device','virtio-net-pci,netdev=net0']


def configure_resources(vms):
    # A backup may contain all four tenant VMs, not just today's first tenant.
    # Refuse before boot if the host cannot retain headroom for existing services.
    guest_gib=8+6*(len(vms)-1)
    memory=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
    available=int(memory['MemAvailable'].split()[0])*1024
    if available<(guest_gib+8)*1024**3:
        raise ValueError('Restore VMs would consume the memory reserve for existing services')
    device=Path(run('findmnt','--noheadings','--output','SOURCE','--target',str(ROOT)).stdout.decode().strip())
    if not str(device).startswith('/dev/') or not device.is_block_device():
        raise ValueError('Restore needs an explicit backing block device for I/O limits')
    text=('[Slice]\nMemoryMax='+str(10+7*(len(vms)-1))+'G\nCPUQuota=200%\nIOWeight=10\n'
          f'IOReadBandwidthMax={device} 50M\nIOWriteBandwidthMax={device} 25M\n')
    Path('/etc/systemd/system/paas-restore.slice').write_text(text)
    run('systemctl','daemon-reload')


def start(operation):
    path=folder(operation);state=json.loads((path/'status.json').read_text())
    if state['state']!='extracted':raise ValueError('A complete extracted snapshot is required')
    live=run('systemctl','list-units','--plain','--no-legend','--state=active,activating,deactivating','paas-restore-*.service').stdout.strip()
    if live:raise ValueError('Another restore VM is alive; finish that same test before starting another')
    configure_resources(state['vms'])
    isolation()
    # Parent paths are traversable, but each VM and the extracted secrets stay private.
    path.chmod(0o755);vmdir=path/'vms';vmdir.mkdir(mode=0o755);vmdir.chmod(0o755)
    for item in state['vms']:
        role=item['role'];name=user(role);owner=pwd.getpwnam(name)
        destination=vmdir/role;destination.mkdir(mode=0o700)
        os.chown(destination,owner.pw_uid,owner.pw_gid)
        source=path/'payload/platform/vms'/role
        for src,dst in [('disk.qcow2','base.qcow2'),('seed.iso','seed.iso')]:
            os.link(source/src,destination/dst)
            os.chown(destination/dst,0,owner.pw_gid);(destination/dst).chmod(0o640)
        info=json.loads(run('runuser','-u',name,'--','qemu-img','info','--output=json',str(destination/'base.qcow2')).stdout)
        if info.get('backing-filename'):raise ValueError('Restore requires standalone qcow2 images')
        run('runuser','-u',name,'--','qemu-img','create','-f','qcow2','-F','qcow2','-b',str(destination/'base.qcow2'),str(destination/'root.qcow2'))
    write_json(path/'status.json',state|{'state':'starting'})
    try:
        for item in state['vms']:
            role=item['role'];destination=vmdir/role;name=user(role)
            run('systemd-run','--unit=paas-restore-'+operation+'-'+role,'--property=User='+name,'--property=Group='+name,
                '--property=SupplementaryGroups=kvm','--property=Slice=paas-restore.slice',
                '--property=WorkingDirectory=/opt/paas-execution',
                '--property=MemoryMax='+('10G' if role=='controller' else '7G'),'--property=CPUQuota=200%',
                '--property=LimitFSIZE=2147483648','--property=LimitCORE=0','--property=NoNewPrivileges=true',
                '--property=CapabilityBoundingSet=','--property=ProtectSystem=strict','--property=ProtectHome=true',
                '--property=PrivateTmp=true','--property=DevicePolicy=closed','--property=DeviceAllow=/dev/kvm rw',
                '--property=ReadWritePaths='+str(destination),'--property=RuntimeMaxSec=1800','--property=TimeoutStopSec=120',
                '--property=KillMode=control-group','--property=ExecStop=/opt/paas-execution/stop-vm',
                '--setenv=PAAS_MONITOR_DIR='+str(destination),
                '/usr/bin/python3','-m','execution.platform_restore','boot',operation,role)
        write_json(path/'status.json',state|{'state':'running','ports':{v['role']:{'ssh':ports(v['role'])[0],'http':ports(v['role'])[1]} for v in state['vms']}})
    except BaseException:
        stop(operation,purge=False)
        raise


def stop(operation,purge=False):
    path=folder(operation);state=json.loads((path/'status.json').read_text())
    for item in state.get('vms',[]):
        name='paas-restore-'+checked_id(operation)+'-'+role_checked(item['role'])+'.service'
        if unit_state(name) in {'active','activating','deactivating'}:run('systemctl','stop',name,timeout=150)
        if unit_state(name) not in {'inactive','failed'}:raise ValueError('Restore VM has not stopped; its files were preserved')
    if purge:shutil.rmtree(path)
    else:write_json(path/'status.json',state|{'state':'stopped'})


def main():
    parser=argparse.ArgumentParser(description='Offline same-host restore proof; never exposes clones publicly')
    parser.add_argument('action',choices=['prepare','receive','extract','start','status','stop','cleanup','boot','restore-native'])
    parser.add_argument('operation');parser.add_argument('role',nargs='?')
    args=parser.parse_args();os.umask(0o077)
    if args.action=='boot':
        command=qemu(args.operation,args.role);os.execv(command[0],command)
    if os.geteuid()!=0:raise SystemExit('Root is required')
    if args.action=='prepare':prepare(args.operation,json.load(sys.stdin))
    elif args.action=='receive':receive(args.operation)
    elif args.action=='extract':extract(args.operation)
    elif args.action=='start':start(args.operation)
    elif args.action=='restore-native':restore_native(args.operation)
    elif args.action in {'stop','cleanup'}:stop(args.operation,purge=args.action=='cleanup')
    if args.action!='cleanup':print((folder(args.operation)/'status.json').read_text())


if __name__=='__main__':main()
