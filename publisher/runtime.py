"""Register same-host tenant VMs as Dokploy deployment targets."""
import fcntl
import json
import os
from pathlib import Path
import re
from publisher.backend import BackendError
from publisher.executor import ExecutionClient


class RuntimeRegistry:
    def __init__(self, backend, directory=Path('/etc/paas-publisher')):
        self.backend = backend
        self.directory = directory
        self.executor = ExecutionClient(json.loads((directory / 'executor.json').read_text()))

    def save(self, value):
        path = self.directory / 'runtime-registry.json.next'
        with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as out:
            json.dump(value, out)
        path.replace(self.directory / 'runtime-registry.json')

    def ensure(self, account_id):
        if not re.fullmatch(r'[a-f0-9]{24}', account_id):
            raise ValueError('Invalid tenant identifier')
        with (self.directory / 'runtime-registry.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.directory / 'runtime-registry.json'
            registry = json.loads(path.read_text()) if path.exists() else {'tenants': {}}
            public_key = (self.directory / 'runtime-deploy.pub').read_text().strip()
            target = self.executor.ensure_runtime(account_id, public_key)
            if 'ssh_key_id' not in registry:
                project = self.backend.api('project.one', query={'projectId': self.backend.config['project_id']})
                keys = self.backend.api('sshKey.all')
                key = next((k for k in keys if k['name'] == 'publisher-runtime-deploy'), None)
                if key and key['publicKey'].strip() != public_key:
                    raise RuntimeError('Runtime deployment key name belongs to a different public key')
                if key is None:
                    self.backend.api('sshKey.create', {'name': 'publisher-runtime-deploy', 'description': 'Dedicated same-host tenant VMs',
                        'privateKey': (self.directory / 'runtime-deploy').read_text(), 'publicKey': public_key, 'organizationId': project['organizationId']})
                    key = next(k for k in self.backend.api('sshKey.all') if k['name'] == 'publisher-runtime-deploy')
                registry['ssh_key_id'] = key['sshKeyId']
                self.save(registry)
            name = 'publisher-' + account_id
            if account_id not in registry['tenants']:
                servers = self.backend.api('server.all')
                server = next((s for s in servers if s['name'] == name), None)
                if server is None:
                    server = self.backend.api('server.create', {'name': name, 'description': 'KVM tenant runtime on existing OVH',
                        'ipAddress': '10.201.0.2', 'port': target['ssh_port'], 'username': 'root',
                        'sshKeyId': registry['ssh_key_id'], 'serverType': 'deploy', 'enableDockerCleanup': False})
                registry['tenants'][account_id] = target | {'server_id': server['serverId'], 'prepared': False}
                self.save(registry)
            record = registry['tenants'][account_id]
            if not record['prepared']:
                self.backend.api('server.setup', {'serverId': record['server_id']})
                record['prepared'] = True
                self.save(registry)
            self.executor.bootstrap(account_id)
            return record

    def retire(self, account_id):
        if not re.fullmatch(r'[a-f0-9]{24}',account_id):
            raise ValueError('Invalid tenant identifier')
        with (self.directory/'runtime-registry.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            path=self.directory/'runtime-registry.json'
            registry=json.loads(path.read_text()) if path.exists() else {'tenants':{}}
            saved=registry['tenants'].get(account_id)
            prepared=self.executor.retire_runtime(account_id)
            slot=prepared.get('slot')
            if (prepared.get('account_id')!=account_id or prepared.get('state') not in {'prepared','completed'} or
                    (slot is not None and (type(slot) is not int or not 0<=slot<4))):
                raise BackendError('Unexpected executor retirement receipt')
            port=22100+slot if slot is not None else None
            if saved and (slot is None or saved['account_id']!=account_id or saved['ssh_port']!=port or
                          saved['http_port']!=22200+slot):
                raise BackendError('Dokploy registry does not match the retiring tenant placement')
            name='publisher-'+account_id
            matches=[s for s in self.backend.api('server.all') if s.get('name')==name or
                     (saved and s.get('serverId')==saved['server_id'])]
            if matches:
                project=self.backend.api('project.one',query={'projectId':self.backend.config['project_id']})
                server=matches[0]
                server_id=server['serverId']
                if (len(matches)!=1 or port is None or server.get('name')!=name or server.get('port')!=port or
                    server.get('ipAddress')!='10.201.0.2' or server.get('serverType')!='deploy' or
                    server.get('username')!='root' or
                    server.get('organizationId')!=project['organizationId'] or (saved and saved['server_id']!=server_id)):
                    raise BackendError('Refusing to remove a Dokploy server with unconfirmed tenant ownership')
                if any(c.get('serverId')==server_id for env in project['environments'] for c in env.get('compose',[])):
                    raise BackendError('Dokploy server still has an attached application')
                self.backend.api('server.remove',{'serverId':server_id})
                if any(s.get('serverId')==server_id or s.get('name')==name for s in self.backend.api('server.all')):
                    raise BackendError('Dokploy server removal is not confirmed; tenant disk was retained')
            result=self.executor.retire_runtime(account_id,commit=True)
            if result!={'account_id':account_id,'slot':slot,'state':'completed'}:
                raise BackendError('Executor did not confirm the tenant slot was released')
            if saved:
                del registry['tenants'][account_id]
                self.save(registry)
            return result
