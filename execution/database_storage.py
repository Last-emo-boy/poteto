"""Hard-bounded database filesystems; callers serialize each resource lifecycle."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

ID = re.compile(r'[a-f0-9]{24}')


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.PIPE, text=True, timeout=60).strip()


def atomic_json(path, value):
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output)
            output.flush(); os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


class DatabaseStorage:
    def __init__(self, root, resource_id, account_id, project_id, *, limit_mib=2048):
        if any(not isinstance(value, str) or not ID.fullmatch(value)
               for value in (resource_id, account_id, project_id)):
            raise ValueError('Use owned 24-character database identifiers')
        if type(limit_mib) is not int or not 64 <= limit_mib <= 8192:
            raise ValueError('Database storage limit must be 64 through 8192 MiB')
        self.directory = Path(root) / 'native-databases' / resource_id
        self.metadata = self.directory / 'storage.json'
        self.image = self.directory / 'data.img'
        self.mountpoint = self.directory / 'data'
        self.owner = {'format': 1, 'resource_id': resource_id, 'account_id': account_id,
                      'project_id': project_id, 'bytes': limit_mib * 1024**2}

    def record(self):
        if self.directory.is_symlink() or self.metadata.is_symlink():
            raise ValueError('Database storage paths must not be links')
        value = json.loads(self.metadata.read_text())
        if not isinstance(value, dict) or any(value.get(key) != expected for key, expected in self.owner.items()):
            raise ValueError('Database storage ownership or quota does not match')
        try:
            valid_uuid = isinstance(value.get('uuid'), str) and str(uuid.UUID(value['uuid'])) == value['uuid']
        except ValueError:
            valid_uuid = False
        if not valid_uuid or value.get('phase') not in {'creating', 'prepared'}:
            raise ValueError('Invalid database filesystem receipt')
        return value

    def mounted(self, record):
        if self.mountpoint.is_symlink():
            raise ValueError('Database mountpoint must not be a link')
        result = subprocess.run(['findmnt', '--json', '-M', str(self.mountpoint),
                                 '-o', 'SOURCE,FSTYPE,OPTIONS,UUID'], capture_output=True, text=True, timeout=10)
        if result.returncode == 1:
            return False
        if result.returncode:
            raise ValueError('Database mount inventory is unavailable')
        filesystems = json.loads(result.stdout)['filesystems']
        if len(filesystems) != 1:
            raise ValueError('Database mount inventory is ambiguous')
        mounted = filesystems[0]
        if (mounted['fstype'] != 'ext4' or mounted.get('uuid') != record['uuid']
                or not {'nodev', 'nosuid', 'noexec'} <= set(mounted['options'].split(','))):
            raise ValueError('Database mount does not match its bounded filesystem')
        backing = run('losetup', '--noheadings', '--output', 'BACK-FILE', mounted['source'])
        if Path(backing) != self.image:
            raise ValueError('Database loop device is owned by another image')
        return True

    def validate_image(self, record):
        info = self.image.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != record['bytes']
                or info.st_uid != os.geteuid()):
            raise ValueError('Database image type, ownership or size does not match')
        if run('blkid', '-p', '-s', 'UUID', '-o', 'value', str(self.image)) != record['uuid']:
            raise ValueError('Database filesystem UUID does not match')

    def prepare(self):
        self.directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError('Database storage directory must not be a link')
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if not self.metadata.exists():
            if any(self.directory.iterdir()):
                raise ValueError('Refusing to adopt an unowned database directory')
            atomic_json(self.metadata, self.owner | {'uuid': str(uuid.uuid4()), 'phase': 'creating'})
        record = self.record()
        if not self.image.exists():
            if record['phase'] != 'creating':
                raise ValueError('Database image is missing; restore it instead of creating an empty database')
            staging = self.directory / 'data.pending'
            if staging.exists() and (staging.is_symlink() or not staging.is_file()):
                raise ValueError('Invalid database staging image')
            # A pending image is never mounted or used by PostgreSQL.
            staging.unlink(missing_ok=True)
            with open(staging, 'xb', opener=lambda p, f: os.open(p, f, 0o600)):
                pass
            run('fallocate', '--length', str(record['bytes']), str(staging))
            run('mkfs.ext4', '-q', '-F', '-m', '0', '-U', record['uuid'], str(staging))
            with staging.open('rb') as source: os.fsync(source.fileno())
            os.replace(staging, self.image)
        self.validate_image(record)
        if record['phase'] != 'prepared':
            record['phase'] = 'prepared'; atomic_json(self.metadata, record)
        if not self.mounted(record):
            self.mountpoint.mkdir(mode=0o700, exist_ok=True)
            if any(self.mountpoint.iterdir()):
                raise ValueError('Refusing to hide files beneath the database mount')
            run('mount', '-o', 'loop,nodev,nosuid,noexec', str(self.image), str(self.mountpoint))
            if not self.mounted(record):
                raise ValueError('Database filesystem did not mount')
        return self.mountpoint

    def unmount(self):
        record = self.record()
        self.validate_image(record)
        if self.mounted(record):
            run('umount', str(self.mountpoint))
        if self.mounted(record):
            raise ValueError('Database filesystem is still mounted')

    def destroy(self):
        record = self.record()
        pending = self.directory/'data.pending'
        if pending.exists() or pending.is_symlink():
            info=pending.lstat()
            if (record['phase']!='creating' or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                    or info.st_uid!=os.geteuid() or info.st_size>record['bytes']):
                raise ValueError('Database staging image is not owned by this creation attempt')
        if self.image.exists():
            self.unmount()
        elif self.mounted(record):
            raise ValueError('Database is still mounted without its owned image')
        if self.mountpoint.exists():
            self.mountpoint.rmdir()
        self.image.unlink(missing_ok=True)
        pending.unlink(missing_ok=True)
        self.metadata.unlink()
