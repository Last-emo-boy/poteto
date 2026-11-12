"""Fixed PostgreSQL containers and owned storage, without tenant VMs."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time

from execution import sandbox, sandbox_network
from execution.database_storage import DatabaseStorage, atomic_json

MEMORY_MIB = 256
VCPUS = 0.25
SLOTS = range(25000, 25064)
PASSWORD = re.compile(r'[A-Za-z0-9_-]{40,80}')
IMAGE = re.compile(r'(?:sha256:[a-f0-9]{64}|[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64})')


def checked(value):
    if not isinstance(value, str) or not sandbox.ID.fullmatch(value):
        raise ValueError('Invalid native database identifier')
    return value


def records(root):
    for path in (Path(root) / 'native-databases').glob('*/database.json'):
        value = json.loads(path.read_text())
        if checked(path.parent.name) != value.get('resource_id'):
            raise ValueError('Native database receipt does not match its directory')
        yield value


class NativeDatabase:
    def __init__(self, root, config, resource_id, account_id, project_id, *, admit):
        self.root, self.config, self.admit = Path(root), config, admit
        self.resource = checked(resource_id)
        self.account, self.project = checked(account_id), checked(project_id)
        self.storage = DatabaseStorage(self.root, self.resource, self.account, self.project)
        self.path = self.storage.directory / 'database.json'
        self.name = 'poteto-db-' + self.resource
        self.network = 'poteto-data-' + self.resource
        self.labels = {'poteto.database.resource': self.resource, 'poteto.database.account': self.account,
                       'poteto.database.project': self.project}

    def read(self):
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise ValueError('Database receipt must not be a link')
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or (value.get('resource_id'), value.get('account_id'), value.get('project_id')) != (
                self.resource, self.account, self.project):
            raise ValueError('Database ownership does not match')
        if type(value.get('slot')) is not int or value['slot'] not in SLOTS or value.get('phase') not in {'starting', 'ready', 'deleting'}:
            raise ValueError('Invalid native database receipt')
        return value

    def container(self, record):
        container = sandbox.inspect('container', self.name)
        if container is None:
            return None
        sandbox.owned(container['Config']['Labels'], self.labels)
        limits = container['HostConfig']
        if (container['Image'] != record['image_id'] or limits['Runtime'] != sandbox.RUNTIME
                or not limits['ReadonlyRootfs'] or limits.get('Privileged')
                or limits['Memory'] != MEMORY_MIB * 1024**2
                or limits['NanoCpus'] != round(VCPUS * 10**9)
                or limits.get('CgroupParent') != 'paas-sandbox.slice'
                or container['Config'].get('User') != '70:70'
                or limits.get('NetworkMode') != self.network
                or limits.get('RestartPolicy', {}).get('Name') not in {'no', ''}
                or limits.get('CapDrop') != ['ALL']
                or not set(limits.get('SecurityOpt', [])) & {'no-new-privileges', 'no-new-privileges:true'}):
            raise ValueError('Native database runtime does not match its receipt')
        mounts = container.get('Mounts', [])
        if (len(mounts) != 1 or mounts[0].get('Source') != str(self.storage.mountpoint)
                or mounts[0].get('Destination') != '/var/lib/postgresql/data' or not mounts[0].get('RW')):
            raise ValueError('Native database storage mount does not match')
        networks = container.get('NetworkSettings', {}).get('Networks', {})
        if (set(networks) != {self.network}
                or (container['State']['Running'] and networks[self.network].get('IPAddress') != record['address'])):
            raise ValueError('Native database network does not match its receipt')
        return container

    def ensure_network(self, record):
        bridge = 'ptdb' + str(record['slot'])
        current = sandbox.inspect('network', self.network)
        if current is None:
            flags = [part for key, value in self.labels.items() for part in ('--label', key + '=' + value)]
            sandbox.docker('network', 'create', '--driver', 'bridge', '--internal', '--opt',
                           'com.docker.network.bridge.name=' + bridge, *flags, self.network)
            current = sandbox.inspect('network', self.network)
        sandbox.owned(current['Labels'], self.labels)
        if not current['Internal'] or current['Options'].get('com.docker.network.bridge.name') != bridge:
            raise ValueError('Native database network does not match')
        subnets = [ipaddress.ip_network(item['Subnet']) for item in current['IPAM']['Config'] if item.get('Subnet')]
        private = [ipaddress.ip_network(value) for value in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
        if (len(subnets) != 1 or subnets[0].version != 4 or subnets[0].num_addresses < 4
                or not any(subnets[0].subnet_of(network) for network in private)):
            raise ValueError('Native database needs one private IPv4 subnet')
        address = str(subnets[0].network_address + 2)
        if record.get('address', address) != address:
            raise ValueError('Native database address changed; reconcile its existing network')
        record['address'] = address
        atomic_json(self.path, record)
        return address

    def create_container(self, record):
        body = {'Image':record['image_id'], 'Tty':True, 'User':'70:70', 'Labels':self.labels,
                'Env':['POSTGRES_DB=app','POSTGRES_USER=paas_admin',
                       'POSTGRES_PASSWORD='+record['admin_password'],'PGDATA=/var/lib/postgresql/data/pgdata'],
                'Cmd':['postgres','-c','max_connections=20','-c','shared_buffers=32MB','-c','work_mem=2MB',
                       '-c','maintenance_work_mem=16MB','-c','max_wal_size=128MB'],
                'HostConfig':{'Runtime':sandbox.RUNTIME,'CgroupParent':'paas-sandbox.slice',
                    'NetworkMode':self.network,'ReadonlyRootfs':True,'CapDrop':['ALL'],
                    'SecurityOpt':['no-new-privileges:true'],'Memory':MEMORY_MIB*1024**2,
                    'MemorySwap':MEMORY_MIB*1024**2,'NanoCpus':round(VCPUS*10**9),'PidsLimit':128,
                    'ShmSize':64*1024**2,'RestartPolicy':{'Name':'no'},
                    'Tmpfs':{'/tmp':'rw,noexec,nosuid,size=67108864,uid=70,gid=70',
                             '/var/run/postgresql':'rw,noexec,nosuid,size=16777216,uid=70,gid=70'},
                    'Mounts':[{'Type':'bind','Source':str(self.storage.mountpoint),
                               'Target':'/var/lib/postgresql/data','ReadOnly':False}]},
                'NetworkingConfig':{'EndpointsConfig':{self.network:{
                    'IPAMConfig':{'IPv4Address':record['address']},'Aliases':['db-'+self.resource]}}}}
        connection = sandbox.DockerSocket('localhost', timeout=30)
        try:
            connection.request('POST', '/containers/create?name='+self.name, json.dumps(body).encode(),
                               {'Content-Type':'application/json'})
            response = connection.getresponse(); response.read(65536)
            if response.status != 201:
                raise ValueError('Docker did not create the native database')
        finally:
            connection.close()

    def metadata(self, record):
        return {'driver':'native-postgres','resource_id':self.resource,'network':self.network,
                'address':record.get('address'),'memory_mb':MEMORY_MIB,'cpus':VCPUS,
                'storage_bytes':self.storage.owner['bytes'],'ready':record['phase']=='ready'}

    def ensure(self, admin_password, app_password):
        if not all(isinstance(value,str) and PASSWORD.fullmatch(value) for value in (admin_password,app_password)):
            raise ValueError('Use generated native database credentials')
        record = self.read()
        if record is None:
            if self.config.get('native_databases') is not True:
                raise ValueError('Native database provisioning is disabled')
            image = self.config.get('native_database_image')
            if not isinstance(image,str) or not IMAGE.fullmatch(image):
                raise ValueError('Pin the native PostgreSQL image before provisioning')
            details = json.loads(sandbox.docker('image','inspect',image))[0]
            if (details.get('Os'), details.get('Architecture')) != ('linux','amd64'):
                raise ValueError('Native PostgreSQL requires a Linux/amd64 image')
            self.admit(self.storage.owner['bytes'])
            sandbox.check_pool(self.root,self.config,self.resource,MEMORY_MIB,VCPUS)
            occupied = {value['slot'] for value in records(self.root)}
            slot = next((value for value in SLOTS if value not in occupied),None)
            if slot is None:
                raise ValueError('Native database slots are reserved')
            self.storage.prepare()
            record = {'resource_id':self.resource,'account_id':self.account,'project_id':self.project,
                      'slot':slot,'image_id':details['Id'],'phase':'starting','memory_mb':MEMORY_MIB,
                      'cpus':VCPUS,'admin_password':admin_password,'app_password':app_password}
            atomic_json(self.path,record)
        else:
            sandbox.check_pool(self.root,self.config,self.resource,MEMORY_MIB,VCPUS)
        if record['phase']=='deleting':
            raise ValueError('Finish deleting the native database before provisioning again')
        if (record['admin_password'],record['app_password']) != (admin_password,app_password):
            raise ValueError('Database credentials do not match its existing receipt')
        self.storage.prepare()
        os.chown(self.storage.mountpoint,70,70)
        self.ensure_network(record)
        sandbox_network.configure(self.root,self.config)
        container = self.container(record)
        if container is None:
            self.create_container(record); container = self.container(record)
        if not container['State']['Running']:
            sandbox.docker('start',self.name)
        # Socket readiness alone can see PostgreSQL's temporary initdb server.
        deadline = time.monotonic()+90
        while time.monotonic()<deadline:
            container = self.container(record)
            if not container['State']['Running']:
                raise ValueError('Native PostgreSQL exited before readiness; storage was preserved')
            try:
                sandbox.docker('exec',self.name,'pg_isready','-t','1','-h','127.0.0.1','-U','paas_admin','-d','app')
                break
            except ValueError:
                time.sleep(1)
        else:
            raise ValueError('Native PostgreSQL did not become ready; storage was preserved')
        # The application never receives the initialization superuser.
        sql=f"""SELECT 'CREATE ROLE app LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app')\\gexec
ALTER ROLE app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN CONNECTION LIMIT 16 PASSWORD '{app_password}';
ALTER DATABASE app OWNER TO app;
\\connect app
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE,CREATE ON SCHEMA public TO app;
"""
        self.sql(sql)
        sandbox.observe_limits(self.container(record), MEMORY_MIB, VCPUS)
        record['phase']='ready'; atomic_json(self.path,record)
        return self.metadata(record)

    def sql(self, sql, *, database='postgres', user='paas_admin'):
        # Values travel on stdin, never in argv, Docker environment or diagnostics.
        process = subprocess.run(['/usr/bin/docker','--host','unix:///var/run/docker.sock','exec','-i',self.name,
            'psql','-X','-At','-v','ON_ERROR_STOP=1','-U',user,'-d',database],
            input=sql.encode(),capture_output=True,timeout=60)
        if process.returncode:
            raise ValueError('Native database SQL operation failed')
        return process.stdout.decode().strip()

    def remove(self):
        record=self.read()
        if record is None:
            if self.storage.metadata.exists() or self.storage.image.exists():
                raise ValueError('Database storage has no runtime receipt; reconcile before deletion')
            sandbox_network.configure(self.root,self.config)
            return {'deleted':True}
        for path in [*(self.root/'sandboxes').glob('*.json'), *(self.root/'sandbox-migrations').glob('*.json')]:
            if json.loads(path.read_text()).get('database_id')==self.resource:
                raise ValueError('A retained application still uses this database')
        container=self.container(record)
        network=sandbox.inspect('network',self.network)
        if network:
            sandbox.owned(network['Labels'],self.labels)
            attached=set(network.get('Containers', {}))
            if attached - ({container['Id']} if container else set()):
                raise ValueError('Database network still has attached applications')
        record['phase']='deleting';atomic_json(self.path,record)
        if container:
            if container['State']['Running']:sandbox.docker('stop','--time','15',self.name)
            sandbox.docker('rm',self.name)
        network=sandbox.inspect('network',self.network)
        if network:
            sandbox.owned(network['Labels'],self.labels)
            if network.get('Containers'):raise ValueError('Database network still has attached applications')
            sandbox.docker('network','rm',self.network)
        if self.storage.metadata.exists():self.storage.destroy()
        elif self.storage.image.exists():raise ValueError('Database storage receipt is missing')
        self.path.unlink()
        self.storage.directory.rmdir()
        sandbox_network.configure(self.root,self.config)
        return {'deleted':True}

    def stop(self):
        record = self.read()
        if record is None:
            raise ValueError('Database receipt is missing')
        container = self.container(record)
        if container and container['State']['Running']:
            sandbox.docker('stop', '--time', '30', self.name, timeout=45)
        self.storage.unmount()


def binding(root, config, resource_id, account_id, project_id):
    def no_allocation(_):
        raise ValueError('An application binding cannot allocate a database')
    database=NativeDatabase(root,config,resource_id,account_id,project_id,admit=no_allocation)
    record=database.read()
    if not record or record['phase']!='ready':
        raise ValueError('Native database is not ready')
    storage=database.storage.record()
    database.storage.validate_image(storage)
    if not database.storage.mounted(storage):
        raise ValueError('Native database storage is not mounted')
    current=database.container(record)
    if not current or not current['State']['Running']:
        raise ValueError('Native database is not running')
    return {'id':resource_id,'network':database.network,'address':record['address']}


def operation(root, config, resource_id, body, *, admit):
    database = NativeDatabase(root, config, resource_id, body.get('account_id'), body.get('project_id'), admit=admit)
    action = body.get('action')
    if action == 'ensure':
        return database.ensure(body.get('admin_password'), body.get('app_password'))
    if action == 'delete':
        return database.remove()
    if action == 'inspect':
        record = database.read()
        if record is None:
            return {'exists':False}
        container = database.container(record)
        return database.metadata(record) | {'exists':True,'running':bool(container and container['State']['Running'])}
    raise ValueError('Unsupported native database operation')


def reconcile(root, config, *, admit, stop=False):
    result = {'ready':[], 'stopped':[], 'deleting':[], 'failed':[]}
    for record in records(root):
        resource = record['resource_id']
        try:
            database = NativeDatabase(root, config, resource, record['account_id'], record['project_id'], admit=admit)
            if stop:
                database.stop()
                result['stopped'].append(resource)
            elif record['phase'] == 'deleting':
                result['deleting'].append(resource)
            else:
                database.ensure(record['admin_password'], record['app_password'])
                result['ready'].append(resource)
        except (OSError, ValueError, subprocess.SubprocessError):
            # One broken database must not prevent recovery of other projects.
            result['failed'].append(resource)
    return result


def main():
    import sys
    from execution.host import ROOT, admit_space, lock
    if sys.argv[1:] not in (['recover'], ['stop']):
        raise SystemExit('Use recover or stop')
    with lock('sandbox-placements'):
        config = json.loads((ROOT/'config.json').read_text())
        result = reconcile(ROOT, config, admit=lambda size: admit_space(incoming=size), stop=sys.argv[1]=='stop')
    print(json.dumps(result))
    if result['failed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
