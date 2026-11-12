"""Trusted-project BuildKit driver; never boots or modifies a tenant VM."""
import re
import shutil
import subprocess

from execution.build_source import prepare_source


ID = re.compile(r'[0-9a-f]{24}')


def allowed(config, spec):
    projects = config.get('native_build_projects', {})
    if not isinstance(projects, dict):
        raise ValueError('native_build_projects must map project IDs to owning account IDs')
    project, account = spec.get('project_id'), spec.get('account_id')
    return (spec.get('runtime') in {'static', 'node', 'dockerfile'}
            and isinstance(project, str) and ID.fullmatch(project) is not None
            and isinstance(account, str) and ID.fullmatch(account) is not None
            and projects.get(project) == account)


def builder_name(project):
    if not ID.fullmatch(project):
        raise ValueError('Invalid native build project')
    return 'poteto-native-' + project


def docker(root):
    config = root / 'native-docker'
    # The dedicated configuration does not inherit the operator's registry logins or contexts.
    return ['docker', '--config', str(config), '--host', 'unix:///var/run/docker.sock'], {
        'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': str(config), 'BUILDX_CONFIG': str(config / 'buildx')}


def validate_builder(root, project):
    command, env = docker(root)
    try:
        info = subprocess.run(command + ['buildx', 'inspect', builder_name(project)], env=env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise ValueError('Native builder is unavailable; provision the dedicated local Buildx builder') from error
    endpoints = re.findall(r'^Endpoint:[ \t]+(\S+)', info, re.MULTILINE)
    if not re.search(r'^Driver:[ \t]+docker-container[ \t]*$', info, re.MULTILINE) or endpoints != ['unix:///var/run/docker.sock']:
        raise ValueError('Native build requires one dedicated local docker-container BuildKit node')


def build(root, job, spec, collect):
    work = job / 'native-work'
    work.mkdir(mode=0o700)
    source, dockerfile = prepare_source(spec, work, job / 'source.tar.gz', cached=True)
    command, env = docker(root)
    image = 'paas-release:' + job.name
    # Uncompressed layers let the archive size conservatively reserve tenant disk space.
    exporter = f'type=docker,name={image},dest=-,compression=uncompressed,force-compression=true'
    size = collect(command + ['buildx', 'build', '--builder', builder_name(spec['project_id']),
                              '--platform', 'linux/amd64', '--progress=plain', '--provenance=false',
                              '--output', exporter, '--file', str(dockerfile), str(source)],
                   job / 'build.log', timeout=900, archive=job / 'image.tar.next', env=env)
    if not size:
        raise ValueError('Native builder returned an empty image')
    (job / 'image.tar.next').replace(job / 'image.tar')
    return {'image': image, 'image_bytes': size, 'image_disk_bytes': size}


def finish(root, job, project):
    command, env = docker(root)
    subprocess.run(command + ['buildx', 'stop', builder_name(project)], env=env, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    # Keep the builder's named cache volume; delete only this job's scratch state.
    work = job / 'native-work'
    if work.exists():
        shutil.rmtree(work)
    (job / 'image.tar.next').unlink(missing_ok=True)
