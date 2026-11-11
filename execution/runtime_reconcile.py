"""File-provider routes for awake apps; shared wake generator handles sleeping apps."""
import json
import fcntl
import os
from pathlib import Path
import re
import subprocess


def main():
    # Timer, deployment readiness and promotion share these route files.
    # Serialize the whole update, not only the final rename.
    with Path('/etc/dokploy/paas/reconcile.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        update_routes()


def update_routes():
    environment = os.environ | {'PAAS_PUBLISHER_ALIASES': '/etc/dokploy/paas/production.json'}
    subprocess.run(['python3', '/opt/paas-runtime/configure-wakeup.py'], env=environment, capture_output=True, check=True, timeout=30)
    ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=paas.publisher.release'], text=True).split()
    containers = json.loads(subprocess.check_output(['docker', 'inspect', *ids], text=True)) if ids else []
    path = Path('/etc/dokploy/paas/production.json')
    aliases = json.loads(path.read_text()) if path.exists() else {}
    routers, services, headers = {}, {}, {}
    for current in containers:
        labels = current['Config']['Labels']
        if labels.get('paas.publisher.migration'):
            continue
        if labels.get('sablier.enable') == 'true':
            continue
        release = labels['paas.publisher.release']; project = labels['paas.publisher.project']
        host = labels['paas.publisher.host']; port = labels['paas.publisher.port']
        name = current['Name'].lstrip('/')
        if not re.fullmatch(r'[a-f0-9]{24}', release) or not re.fullmatch(r'[a-f0-9]{24}', project) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', name) or not re.fullmatch(r'[a-zA-Z0-9.-]+', host) or not port.isdigit():
            raise ValueError('Invalid published runtime metadata')
        hosts = [host]
        if aliases.get(project) == release:
            hosts.append('app-' + project + '.' + host.split('.', 1)[1])
        key = 'publisher-direct-' + release
        routers[key] = {'rule': ' || '.join(f'Host(`{h}`)' for h in hosts), 'entryPoints': ['web'], 'service': key, 'middlewares': [key]}
        services[key] = {'loadBalancer': {'servers': [{'url': f'http://{name}:{port}'}]}}
        headers[key] = {'headers': {'customResponseHeaders': {'X-Paas-Deployment': release}}}
    live = Path('/etc/dokploy/traefik/dynamic/publisher-awake.yml')
    if not routers:
        live.unlink(missing_ok=True)
        return
    value = json.dumps({'http': {'routers': routers, 'services': services, 'middlewares': headers}}, sort_keys=True)
    live = Path('/etc/dokploy/traefik/dynamic/publisher-awake.yml')
    if not live.exists() or live.read_text() != value:
        pending = live.with_suffix('.next'); pending.write_text(value); pending.replace(live)


if __name__ == '__main__':
    main()
