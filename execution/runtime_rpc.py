"""Trusted guest operations. Never accepts shell commands from the publishing API."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from publisher.container_logs import capture


def docker(*args):
    return subprocess.check_output(['docker', *args], timeout=30)


def container(release):
    ids = docker('ps', '-aq', '--filter', 'label=paas.publisher.release=' + release).split()
    if not ids:
        return None
    if len(ids) != 1:
        raise RuntimeError('Release container inventory is ambiguous')
    current=json.loads(docker('inspect', ids[0].decode()))[0]
    remember_volumes(release,current)
    return current


def remember_volumes(release,current):
    directory=Path('/etc/dokploy/paas/ephemeral')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/(release+'.json')
    names=set(json.loads(path.read_text())) if path.exists() else set()
    names.update(m['Name'] for m in current.get('Mounts',[]) if m.get('Type')=='volume' and re.fullmatch(r'[a-f0-9]{64}',m.get('Name','')))
    pending=path.with_suffix('.next')
    with open(pending,'w',opener=lambda p,f:os.open(p,f,0o600)) as out:
        json.dump(sorted(names),out)
    pending.replace(path)


def cleanup_volumes(release):
    path=Path('/etc/dokploy/paas/ephemeral')/(release+'.json')
    for name in json.loads(path.read_text()) if path.exists() else []:
        if not re.fullmatch(r'[a-f0-9]{64}',name):
            raise ValueError('Invalid ephemeral volume receipt')
        result=subprocess.run(['docker','volume','inspect',name],capture_output=True,timeout=30)
        if not result.returncode:
            docker('volume','rm',name)
    path.unlink(missing_ok=True)


def reconcile():
    subprocess.run(['python3', '/opt/paas-runtime/reconcile.py'], check=True, capture_output=True, timeout=40)


def main():
    request = json.load(sys.stdin)
    action = request['action']
    if action == 'reconcile':
        reconcile()
        return {'ok': True}
    release = request['release_id']
    if not re.fullmatch(r'[a-f0-9]{24}', release):
        raise ValueError('Invalid release identifier')
    current = container(release)
    if action == 'inspect':
        if current is None:
            return {'container': None}
        return {'container': {k: current[k] for k in ['Id', 'Name', 'State']}}
    if action == 'cleanup':
        if current is not None:
            raise ValueError('Remove the deployment container before cleaning its image')
        migration='paas-migration-'+release
        result=subprocess.run(['docker','container','inspect',migration],capture_output=True,timeout=30)
        if not result.returncode:
            item=json.loads(result.stdout)[0]
            if item['Config']['Labels'].get('paas.publisher.migration')!=release:
                raise ValueError('Migration container ownership does not match')
            remember_volumes(release,item)
            if item['State']['Running']:
                docker('stop','--time','10',migration)
            docker('rm',migration)
        cleanup_volumes(release)
        path = Path('/etc/dokploy/paas/production.json')
        aliases = json.loads(path.read_text()) if path.exists() else {}
        aliases = {k: v for k, v in aliases.items() if v != release}
        pending = path.with_suffix('.next')
        pending.write_text(json.dumps(aliases)); pending.replace(path)
        if docker('image', 'ls', '-q', '--filter', 'reference=paas-release:' + release).strip():
            docker('image', 'rm', 'paas-release:' + release)
        reconcile()
        return {'cleaned': True}
    if current is None:
        raise ValueError('Release container not found')
    if action == 'logs':
        return {'log':capture(current['Id'])}
    if action in {'start', 'stop'}:
        docker(action, current['Id'])
        return {'ok': True}
    if action in {'promote', 'restore_alias'}:
        project = request['project_id']
        if not re.fullmatch(r'[a-f0-9]{24}', project) or current['Config']['Labels'].get('paas.publisher.project') != project:
            raise ValueError('Project does not own this release')
        path = Path('/etc/dokploy/paas/production.json')
        aliases = json.loads(path.read_text()) if path.exists() else {}
        previous = aliases.get(project)
        target = request.get('target_release', release)
        if target is not None and not re.fullmatch(r'[a-f0-9]{24}', target):
            raise ValueError('Invalid production release')
        if target is not None:
            target_container = container(target)
            if not target_container or target_container['Config']['Labels'].get('paas.publisher.project') != project:
                raise ValueError('Production alias target does not belong to project')
            aliases[project] = target
        else:
            aliases.pop(project, None)
        pending = path.with_suffix('.next')
        pending.write_text(json.dumps(aliases)); pending.replace(path)
        try:
            reconcile()
        except BaseException:
            if previous is None: aliases.pop(project, None)
            else: aliases[project] = previous
            pending.write_text(json.dumps(aliases)); pending.replace(path)
            reconcile()
            raise
        return {'previous': previous}
    raise ValueError('Unsupported guest operation')


if __name__ == '__main__':
    try:
        print(json.dumps(main()))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(1)
