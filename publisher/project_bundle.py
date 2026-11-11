"""Validate an already authenticated project bundle before allocating resources."""
from contextlib import contextmanager
import json
from pathlib import PurePosixPath
import re
import shutil
import stat
from typing import Annotated,Literal
import zipfile
from pydantic import Field,ValidationError,model_validator
from publisher.archive import ArchiveError,unpack
from publisher.models import DeploymentInput,EnvironmentInput,Input
from publisher.transfer_wire import BUNDLE_LIMIT,FORMAT,BoundedZipInput,BundleError

DATA_LIMIT=12*1024**3
ID=Annotated[str,Field(pattern=r'^[a-f0-9]{24}$')]


class Resource(Input):
    id: ID
    kind: Literal['database','volume']
    stage: Literal['production','preview']


class Manifest(Input):
    format: Literal['ovh-paas-project/1']
    bundle_id: ID
    name: str=Field(pattern=r'^[a-z][a-z0-9-]{0,39}$')
    source_release: ID
    image: str
    configuration: DeploymentInput
    project_environment: dict[str,str]
    preview_environment: dict[str,str]=Field(default_factory=dict)
    release_environment: dict[str,str]
    resources: list[Resource]=Field(max_length=5)

    @model_validator(mode='after')
    def contract(self):
        EnvironmentInput(values=self.project_environment)
        EnvironmentInput(values=self.preview_environment)
        EnvironmentInput(values=self.release_environment)
        by_id={r.id:r for r in self.resources}
        if len(by_id)!=len(self.resources) or len({(r.kind,r.stage) for r in self.resources})!=len(self.resources):
            raise ValueError('Duplicate resource inventory')
        config=self.configuration
        for kind,rid in [('database',config.database_id),('volume',config.volume_id)]:
            if rid and (rid not in by_id or by_id[rid].kind!=kind or by_id[rid].stage!=config.target):
                raise ValueError('Resource binding does not match the manifest')
        if self.image!='paas-release:'+self.source_release and not self.legacy_static:
            raise ValueError('Invalid exported image identity')
        return self

    @property
    def legacy_static(self):
        return self.configuration.runtime=='static' and bool(re.fullmatch(r'sha256:[a-f0-9]{64}',self.image))


@contextmanager
def open_zip(path,*,limit,max_entries):
    try:
        with path.open('rb') as source:
            bounded=BoundedZipInput(source)
            if not 0<bounded.size<=limit:raise BundleError('ZIP size is outside the supported limit')
            with zipfile.ZipFile(bounded) as archive:
                entries=archive.infolist();names={entry.filename:entry for entry in entries}
                if len(entries)>max_entries or len(names)!=len(entries):raise BundleError('ZIP has too many entries or duplicate paths')
                for entry in entries:
                    mode=stat.S_IFMT(entry.external_attr>>16)
                    permitted={0,stat.S_IFDIR if entry.is_dir() else stat.S_IFREG}
                    if (entry.filename!=entry.orig_filename or mode not in permitted or entry.flag_bits&1 or
                            entry.compress_type!=zipfile.ZIP_STORED or entry.compress_size!=entry.file_size or
                            (entry.is_dir() and entry.file_size)):
                        raise BundleError('ZIP contains unsupported or unsafe entries')
                yield archive,names
    except (zipfile.BadZipFile,EOFError,OSError) as error:
        raise BundleError('Invalid or incomplete project ZIP') from error


def validate_data(path,resources):
    expected={('databases/'+r.id+'.dump' if r.kind=='database' else 'volumes/'+r.id+'/'):r for r in resources}
    volumes={name for name,r in expected.items() if r.kind=='volume'}
    with open_zip(path,limit=DATA_LIMIT+16*1024**2,max_entries=20005) as (archive,entries):
        if not expected.keys()<=entries.keys() or sum(info.file_size for info in entries.values())>DATA_LIMIT:
            raise BundleError('Managed data inventory or size does not match')
        if any(name+'/' in entries for name in entries if not name.endswith('/')):
            raise BundleError('Managed volume path is both a file and a directory')
        for name,resource in expected.items():
            if resource.kind=='database':
                with archive.open(name) as dump:
                    if dump.read(5)!=b'PGDMP':raise BundleError('Database snapshot is not a PostgreSQL custom archive')
        for name in entries.keys()-expected.keys():
            prefix=next((value for value in volumes if name.startswith(value)),None)
            if prefix is None:raise BundleError('Data entry is outside its declared resource')
            relative=name[len(prefix):].removesuffix('/')
            parts=PurePosixPath(relative)
            if not relative or parts.is_absolute() or parts.as_posix()!=relative or '..' in parts.parts or len(relative)>240 or '\\' in relative or any(ord(c)<32 for c in relative):
                raise BundleError('Unsafe managed volume path')
            parent=parts.parent.as_posix()
            if parent!='.' and prefix+parent+'/' not in entries:raise BundleError('Managed volume directory is missing')


def unpack_bundle(path,destination,*,source_stats=None):
    if any(destination.iterdir()):raise BundleError('Bundle staging directory must be empty')
    with open_zip(path,limit=BUNDLE_LIMIT,max_entries=5) as (archive,entries):
        manifest=entries.get('manifest.json')
        if not manifest or not 0<manifest.file_size<=256*1024:raise BundleError('Project manifest is absent or too large')
        try:value=Manifest.model_validate_json(archive.read(manifest))
        except ValidationError:raise BundleError('Project manifest does not match the supported format') from None
        limits={'manifest.json':256*1024,'source.tar.gz':50*1024**2,'image.tar':1024**3}
        if value.configuration.runtime=='static':limits['nginx.conf']=64*1024
        if value.resources:limits['data.zip']=DATA_LIMIT+16*1024**2
        if entries.keys()!=limits.keys() or any(not 0<entries[name].file_size<=limit for name,limit in limits.items()):
            raise BundleError('Project bundle entries do not match the manifest and limits')
        for name in limits.keys()-{'manifest.json'}:
            target=destination/name
            with archive.open(name) as source,target.open('xb') as out:
                target.chmod(0o600);shutil.copyfileobj(source,out,1024**2)
            if target.stat().st_size!=entries[name].file_size:raise BundleError('Project bundle entry is truncated')
    try:stats=unpack(destination/'source.tar.gz',None,max_bytes=200*1024**2,max_files=10000)
    except ArchiveError as error:raise BundleError(str(error)) from None
    if source_stats is not None:source_stats.update(stats)
    if value.resources:validate_data(destination/'data.zip',value.resources)
    return value
