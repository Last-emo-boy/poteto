"""Private supervisor for tenant VMs and opt-in trusted native builds."""
import base64
from contextlib import ExitStack, contextmanager, nullcontext
import fcntl
import http.server
import io
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import selectors
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

from execution import builder_profile, native_build, prebuilt_image, sandbox

ROOT = Path(os.environ.get("PAAS_EXEC_ROOT", "/srv/ovh-paas-execution"))
RESTORES = Path('/srv/ovh-paas-platform/restore')
CODE = Path(__file__).resolve().parents[1]
RUNTIME_VERSION = "11"
ID = re.compile(r"[a-f0-9]{24}")
BUILD_LOG_LIMIT = 2 * 1024**2
STATIC_ARCHIVE_LIMIT = 50 * 1024**2


class ExecutionError(RuntimeError):
    pass


def checked_id(value):
    if not ID.fullmatch(value):
        raise ValueError("Invalid resource identifier")
    return value


def write_json(path, value, *, durable=False):
    pending = path.with_suffix(".next")
    with pending.open("w") as out:
        json.dump(value, out, sort_keys=True)
        if durable:
            out.flush()
            os.fsync(out.fileno())
    pending.replace(path)
    if durable:
        sync_directory(path.parent)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def lock(name, *, shared=False):
    with (ROOT / (name + ".lock")).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def unit_state(name):
    result = subprocess.run(["systemctl", "show", name, "--property=ActiveState", "--value"], text=True, capture_output=True, check=True)
    return result.stdout.strip()


def systemctl(*args):
    subprocess.run(["systemctl", *args], check=True, capture_output=True, timeout=120)


def ssh(port, alias, command, *, data=None, timeout=120, output=None):
    args = ["ssh", "-i", str(ROOT / "admin.key"), "-p", str(port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + str(ROOT / "known_hosts"), "-o", "HostKeyAlias=" + alias,
            "ubuntu@127.0.0.1", command]
    return subprocess.run(args, input=data, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True)


def cloud_seed(directory, role, deploy_key=None, bootstrap=False, *, prepared_builder=False):
    keys = [(ROOT / "admin.key.pub").read_text().strip()]
    if deploy_key:
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [a-zA-Z0-9_.-]+)?", deploy_key):
            raise ValueError("Invalid deployment public key")
        keys.append(deploy_key)
    commands = [["sh", "-c", "echo PAAS_EXECUTION_READY > /dev/ttyS0"]]
    files = []
    if not bootstrap:
        commands = [["bash", "-c", "systemctl stop docker.service docker.socket && rm -f /etc/docker/key.json /var/lib/docker/engine-id && systemctl start docker && echo PAAS_EXECUTION_READY > /dev/ttyS0"]]
    if prepared_builder:
        commands = [["bash", "-c", "systemctl start docker && echo PAAS_EXECUTION_READY > /dev/ttyS0"]]
    if bootstrap:
        files = [{"path": "/usr/local/bin/paas-install-docker", "permissions": "0700", "content": (CODE / "execution/docker-bootstrap.sh").read_text()}]
        commands = [["bash", "-c", "/usr/local/bin/paas-install-docker && echo PAAS_EXECUTION_READY > /dev/ttyS0"]]
    users = [{"name": "ubuntu", "groups": ["adm", "sudo"], "shell": "/bin/bash",
              "sudo": "ALL=(ALL) NOPASSWD:ALL", "lock_passwd": True, "ssh_authorized_keys": keys}]
    if deploy_key:
        users.append({"name": "root", "lock_passwd": True, "ssh_authorized_keys": [deploy_key]})
    user_data = {"hostname": "paas-" + role, "manage_etc_hosts": True, "ssh_pwauth": False, "disable_root": not bool(deploy_key),
                 "ssh_deletekeys": True, "users": users,
                 "write_files": files, "runcmd": commands}
    (directory / "user-data").write_text("#cloud-config\n" + json.dumps(user_data))
    (directory / "meta-data").write_text("instance-id: " + secrets.token_hex(16) + "\n")
    (directory / "seed.iso").unlink(missing_ok=True)
    subprocess.run(["cloud-localds", str(directory / "seed.iso"), str(directory / "user-data"), str(directory / "meta-data")], check=True, capture_output=True)


def restore_reservations():
    remaining=0
    for path in RESTORES.iterdir() if RESTORES.exists() else []:
        if not path.is_dir() or not ID.fullmatch(path.name):continue
        try:
            request=json.loads((path/'status.json').read_text())
            if any(type(request[k]) is not int or request[k]<=0 for k in ['archive_bytes','payload_bytes','vm_count']) or request['vm_count']>5:
                raise ValueError('Invalid reservation size')
            total=request['payload_bytes']+request['vm_count']*4*1024**3
            if request['state'] not in {'extracted','starting','running','stopped'}:
                total+=request['archive_bytes']
        except (OSError,ValueError,KeyError,TypeError):
            raise ExecutionError('Restore capacity reservation is unreadable; inspect that operation before admitting more writes') from None
        # Snapshot bases are hard-linked into VM folders. Count their physical
        # blocks once, or the links would falsely release unwritten capacity.
        allocated=0;seen=set()
        try:
            for file in path.rglob('*'):
                info=file.lstat();key=(info.st_dev,info.st_ino)
                if stat.S_ISREG(info.st_mode) and key not in seen:
                    allocated+=info.st_blocks*512;seen.add(key)
        except OSError:
            raise ExecutionError('Restore storage changed during the capacity check; retry after that operation settles') from None
        remaining+=max(0,total-allocated)
    return remaining


def admit_space(role=None, incoming=0):
    # Reserve every tenant disk's remaining growth before admitting more host writes.
    # This prevents sparse guest disks from consuming the space kept for existing services.
    roles = {'builder'}
    index = ROOT / 'placements.json'
    if index.exists():
        roles.update('runtime' + str(r['slot']) for r in json.loads(index.read_text()).values())
    if role: roles.add(role)
    remaining = restore_reservations()
    for current in roles:
        maximum = (18 if current == 'builder' else 14) * 1024**3
        disk = ROOT / 'vm' / current / 'root.qcow2'
        allocated = disk.stat().st_blocks * 512 if disk.exists() else 0
        remaining += max(0, maximum - allocated)
    if shutil.disk_usage(ROOT).free - remaining - incoming < 70 * 1024**3:
        raise ExecutionError('Host disk capacity is reserved for existing services and committed VM disks')


def reset_disk(role, bootstrap=False, deploy_key=None, *, profile=None):
    if role != "builder" and not re.fullmatch(r"runtime[0-3]", role):
        raise ValueError("Unknown VM role")
    if profile is not None and (role != "builder" or bootstrap):
        raise ValueError("Only disposable build guests can use a prepared base")
    admit_space(role)
    directory = ROOT / "vm" / role
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    image = directory / "root.qcow2"
    if image.exists():
        if role != "builder":
            raise ExecutionError("Refusing to replace a persistent tenant disk")
        if unit_state("paas-builder.service") not in {"inactive", "failed"}:
            raise ExecutionError("Refusing to replace an active build disk")
        image.unlink()
    base = (builder_profile.base_path(ROOT, profile["generation"]) if profile else
            ROOT / ("base.img" if bootstrap else "template.qcow2"))
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(image), "16G" if role == "builder" and not bootstrap else "12G"], check=True, capture_output=True)
    (directory / "monitor.sock").unlink(missing_ok=True)
    cloud_seed(directory, role, deploy_key, bootstrap, prepared_builder=bool(profile))
    (directory / "serial.log").write_text("")
    username = "paasbuild" if role == "builder" else "paasrt" + role[-1]
    user = pwd.getpwnam(username)
    os.chown(directory, user.pw_uid, user.pw_gid)
    for path in directory.iterdir():
        os.chown(path, user.pw_uid, user.pw_gid)
        path.chmod(0o600)
    return directory


def wait_ready(role, port, alias, timeout=600, *, require_marker=False):
    directory = ROOT / "vm" / role
    unit = "paas-builder.service" if role == "builder" else f"paas-runtime@{role[-1]}.service"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if unit_state(unit) not in {"active", "activating"}:
            raise ExecutionError("Execution VM stopped before startup completed")
        serial = (directory / "serial.log").read_text(errors="replace")[-65536:]
        if "PAAS_EXECUTION_READY" in serial or (not require_marker and re.search(r"Cloud-init v\..*finished at", serial)):
            try:
                ssh(port, alias, "sudo -n docker info --format '{{.ServerVersion}}'", timeout=10)
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        time.sleep(3)
    raise ExecutionError("Execution VM startup did not complete")


def prepare_template():
    if (ROOT / "template.qcow2").exists():
        raise ExecutionError("Golden template already exists; no in-place replacement is permitted")
    with lock("builder"):
        systemctl("stop", "paas-builder.service")
        directory = reset_disk("builder", bootstrap=True)
        systemctl("start", "paas-builder.service")
        try:
            wait_ready("builder", 22099, "template-bootstrap")
            ssh(22099, "template-bootstrap", "sudo -n cloud-init clean --logs --machine-id")
        finally:
            systemctl("stop", "paas-builder.service")
        subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(directory / "root.qcow2"), str(ROOT / "template.qcow2.next")], check=True, timeout=180)
        (ROOT / "template.qcow2.next").replace(ROOT / "template.qcow2")
        (ROOT / "template.qcow2").chmod(0o644)
        print("Clean Docker golden template prepared", flush=True)


def prepare_builder_base(reference):
    """Seal only a clean guest and operator-pinned Node layers, never a tenant overlay."""
    reference = builder_profile.node_image(reference)
    with lock("builder"):
        if unit_state("paas-builder.service") not in {"inactive", "failed"}:
            raise ExecutionError("Wait for the current disposable builder to stop")
        admit_space("builder", incoming=6 * 1024**3)
        generation = secrets.token_hex(12)
        alias = "builder-prewarm-" + generation
        directory = reset_disk("builder")
        systemctl("start", "paas-builder.service")
        try:
            wait_ready("builder", 22099, alias, timeout=180)
            print("Preparing trusted Node layers in a clean builder guest", flush=True)
            dockerfile = ("FROM " + reference + " AS build\nWORKDIR /app\nRUN [\"node\", \"--version\"]\n").encode()
            log = ssh(22099, alias, "sudo -n docker build --platform linux/amd64 --network none "
                      "--progress=plain --tag poteto-builder-prewarm:1 -",
                      data=dockerfile, timeout=420)
            print(log.stderr.decode(errors="replace")[-6000:], flush=True)
            ssh(22099, alias, "sudo -n systemctl disable --now docker.service docker.socket && "
                "sudo -n rm -f /etc/docker/key.json /var/lib/docker/engine-id && "
                "sudo -n cloud-init clean --logs --machine-id")
        finally:
            systemctl("stop", "paas-builder.service")
        result = subprocess.run(["systemctl", "show", "paas-builder.service", "--property=Result", "--value"],
                                check=True, capture_output=True, timeout=10)
        if result.stdout.strip() != b"success":
            raise ExecutionError("Builder did not shut down cleanly; template was not sealed")
        base = builder_profile.base_path(ROOT, generation)
        pending = base.with_suffix(".next")
        try:
            subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(directory / "root.qcow2"), str(pending)],
                           check=True, capture_output=True, timeout=240)
            pending.chmod(0o644)
            pending.replace(base)
            write_json(ROOT / builder_profile.PROFILE, {
                "generation": generation, "node_image": reference, "created": time.time(),
            }, durable=True)
        finally:
            pending.unlink(missing_ok=True)
        print("Prepared static builder base: " + generation, flush=True)


def runtime(account_id, deploy_key=None):
    checked_id(account_id)
    with lock("placements"):
        if retirement_path(account_id).exists():
            raise ExecutionError("Tenant runtime is retired or reserved for retirement")
        index_path = ROOT / "placements.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
        if account_id not in index:
            if deploy_key is None:
                raise KeyError("Runtime not provisioned")
            used = {r["slot"] for r in index.values()}
            slot = next((i for i in range(4) if i not in used), None)
            if slot is None:
                raise ExecutionError("Tenant VM capacity reached")
            admit_space('runtime'+str(slot))
            record = {"account_id": account_id, "slot": slot, "ssh_port": 22100 + slot, "http_port": 22200 + slot,
                      "alias": "runtime-" + account_id, "state": "provisioning"}
            index[account_id] = record
            write_json(index_path, index)
        record = index[account_id]
    slot = record["slot"]
    with lock("runtime" + str(slot)):
        if record["state"] == "ready":
            if unit_state(f"paas-runtime@{slot}.service") not in {"active", "activating"}:
                systemctl("start", f"paas-runtime@{slot}.service")
                wait_ready(f"runtime{slot}", record["ssh_port"], record["alias"], timeout=180)
            return record

        directory = ROOT / "vm" / f"runtime{slot}"
        if not (directory / "root.qcow2").exists():
            reset_disk(f"runtime{slot}", deploy_key=deploy_key)
        systemctl("enable", "--now", f"paas-runtime@{slot}.service")
        wait_ready(f"runtime{slot}", record["ssh_port"], record["alias"], timeout=180)
        ssh(record["ssh_port"], record["alias"], "sudo -n usermod -aG docker ubuntu")
        record["state"] = "ready"
        with lock("placements"):
            index = json.loads((ROOT / "placements.json").read_text())
            index[account_id] = record
            write_json(ROOT / "placements.json", index)
        return record


def runtime_status(account_id):
    checked_id(account_id)
    path=ROOT/'placements.json'
    index=json.loads(path.read_text()) if path.exists() else {}
    if account_id not in index:
        raise KeyError('Runtime not provisioned')
    record=index[account_id]
    return record | {'unit_state':unit_state(f"paas-runtime@{record['slot']}.service"),
        'disk_exists':(ROOT/'vm'/f"runtime{record['slot']}"/'root.qcow2').is_file()}


def retirement_path(account_id):
    return ROOT / 'retired' / (checked_id(account_id) + '.json')


@contextmanager
def active_account(account_id):
    with lock('account-' + checked_id(account_id), shared=True):
        if retirement_path(account_id).exists():
            raise ExecutionError('Tenant runtime is retired or reserved for retirement')
        yield


def confirm_empty_runtime(target):
    port, alias = target['ssh_port'], target['alias']
    containers = ssh(port, alias, 'sudo -n docker ps -a --format "{{.Names}}"', timeout=30).stdout.decode().split()
    allowed = {'dokploy-traefik', 'paas-wake-proxy', 'paas-runtime-sablier-1'}
    volumes = ssh(port, alias, 'sudo -n docker volume ls -q', timeout=30).stdout.strip()
    if set(containers) - allowed or volumes:
        raise ExecutionError('Tenant VM still owns containers or volumes; refusing to remove its disk')


def placement_for_retirement(index, account):
    record = index.get(account)
    if record is None:
        return None
    slot = record['slot']
    if (type(slot) is not int or not 0 <= slot < 4 or record['account_id'] != account or
        record['alias'] != 'runtime-'+account or record['ssh_port'] != 22100+slot or
        record['http_port'] != 22200+slot or any(
            row['slot']==slot for other,row in index.items() if other!=account)):
        raise ExecutionError('Runtime placement ownership could not be confirmed')
    return record


def retire_runtime(account, phase):
    receipt = retirement_path(account)
    index_path = ROOT / 'placements.json'
    def placements():
        return json.loads(index_path.read_text()) if index_path.exists() else {}

    with lock('account-'+account):
        if phase == 'prepare':
            if receipt.exists():
                return json.loads(receipt.read_text())
            for spec in (ROOT/'builds').glob('*/spec.json'):
                if json.loads(spec.read_text())['account_id'] == account:
                    raise ExecutionError('Tenant still owns a build; remove it through project cleanup first')
            with lock('placements'):
                record = placement_for_retirement(placements(), account)
            if record:
                slot=record['slot']
                folder=ROOT/'vm'/f'runtime{slot}'
                image=folder/'root.qcow2'
                if folder.is_symlink() or image.is_symlink():
                    raise ExecutionError('Unexpected tenant disk path')
                if image.is_file():
                    confirm_empty_runtime(runtime(account))
                elif folder.exists() or unit_state(f'paas-runtime@{slot}.service') not in {'inactive','failed'}:
                    raise ExecutionError('Tenant disk is missing or has an incomplete directory; inspect it before retirement')
            receipt.parent.mkdir(mode=0o700,exist_ok=True)
            sync_directory(ROOT)
            result={'account_id':account,'slot':record['slot'] if record else None,'state':'prepared'}
            write_json(receipt,result,durable=True)
            return result

        if phase != 'commit' or not receipt.is_file():
            raise ExecutionError('Prepare the empty tenant before committing retirement')
        result=json.loads(receipt.read_text())
        with lock('placements'):
            record=placement_for_retirement(placements(),account)
        if not record and result['state']=='completed':
            return result
        if result['account_id']!=account or result['slot']!=(record['slot'] if record else None):
            raise ExecutionError('Prepared retirement no longer matches the tenant placement')
        if record:
            slot=record['slot'];unit=f'paas-runtime@{slot}.service'
            with lock(f'runtime{slot}'):
                systemctl('disable','--now',unit)
                if unit_state(unit) not in {'inactive','failed'}:
                    raise ExecutionError('Runtime has not stopped; its disk is retained')
                folder=ROOT/'vm'/f'runtime{slot}'
                if folder.is_symlink():
                    raise ExecutionError('Unexpected tenant disk path')
                if folder.exists():
                    shutil.rmtree(folder)
                    sync_directory(folder.parent)
        result['state']='completed'
        write_json(receipt,result,durable=True)
        if record:
            with lock('placements'):
                index=placements()
                if placement_for_retirement(index,account)!=record:
                    raise ExecutionError('Runtime placement changed during retirement')
                del index[account]
                write_json(index_path,index,durable=True)
        return result


def status(build_id):
    checked_id(build_id)
    path = ROOT / "builds" / build_id / "status.json"
    result = json.loads(path.read_text())
    if result["state"] in {"queued", "running"} and unit_state(f"paas-build@{build_id}.service") in {"inactive", "failed"}:
        result |= {"state": "failed", "error": "Build supervisor terminated; inspect the existing job before resubmission"}
    return result | {'log_chunks': True}


def build_log_chunk(build_id, offset):
    path = ROOT / 'builds' / checked_id(build_id) / 'build.log'
    if not 0 <= offset <= BUILD_LOG_LIMIT:
        raise ValueError('Invalid build log offset')
    try:
        with path.open('rb') as log:
            log.seek(offset)
            chunk = log.read(min(65536, BUILD_LOG_LIMIT - offset))
    except FileNotFoundError:
        chunk = b''  # The builder has not started emitting output yet.
    return {'data': base64.b64encode(chunk).decode('ascii')}


def collect_build_output(command, path, timeout, *, archive=None, env=None, archive_limit=1024**3, intro=b""):
    """Drain logs while receiving a bounded JSON receipt or image archive."""
    deadline = time.monotonic() + timeout
    receipt = bytearray()
    size = 0
    image = archive.open("xb") if archive else nullcontext()
    with image as output, path.open('wb', buffering=0) as log, selectors.DefaultSelector() as selector, \
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as process:
        log.write(intro)
        selector.register(process.stdout, selectors.EVENT_READ, 'receipt')
        selector.register(process.stderr, selectors.EVENT_READ, 'log')
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not (events := selector.select(remaining)):
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in events:
                    data = os.read(key.fd, 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                    elif key.data == 'log':
                        log.write(data[:max(0, BUILD_LOG_LIMIT - log.tell())])
                    elif output is not None:
                        size += len(data)
                        if size > archive_limit:
                            raise ExecutionError("Build output exceeds its archive size limit")
                        output.write(data)
                    else:
                        if len(receipt) + len(data) > 4096:
                            raise ExecutionError('Guest build receipt exceeds its limit')
                        receipt.extend(data)
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                raise ExecutionError('Build process failed; inspect deployment logs')
            return size if output is not None else json.loads(receipt)
        finally:
            if process.poll() is None:
                process.kill()


def provision_builder(alias, job, spec):
    modules = {
        "execution/guest_build.py": "opt/paas-execution/guest_build.py",
        "execution/build_source.py": "opt/paas-execution/build_source.py",
        "publisher/archive.py": "opt/paas-execution/publisher/archive.py",
        "publisher/build_recipe.py": "opt/paas-execution/publisher/build_recipe.py",
        "publisher/__init__.py": "opt/paas-execution/publisher/__init__.py",
    }
    # The source remains an opaque inner archive. Only fixed platform-owned
    # member names are ever passed to the guest's privileged tar command.
    image_import = spec.get("runtime") == "image-import"
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode="w") as bundle:
            for source, target in modules.items():
                bundle.add(CODE / source, arcname=target)
            payload = json.dumps(spec).encode()
            entry = tarfile.TarInfo("work/spec.json")
            entry.size, entry.mode = len(payload), 0o600
            bundle.addfile(entry, io.BytesIO(payload))
            if not image_import:
                bundle.add(job / "source.tar.gz", arcname="work/source.tar.gz")
        stream.seek(0)
        result = subprocess.run(["/opt/paas-execution/ssh-stream", "22099", alias,
                                 "sudo -n tar --no-same-owner --no-same-permissions -xf - -C / && "
                                 "cat /proc/sys/kernel/random/boot_id"],
                                stdin=stream, capture_output=True, check=True, timeout=120)
    boot_id = result.stdout.decode().strip()
    if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot_id):
        raise ExecutionError("Disposable builder did not report a valid boot identity")
    if image_import:
        with (job / "source.tar.gz").open("rb") as archive:
            subprocess.run(["/opt/paas-execution/ssh-stream", "22099", alias, "sudo -n tee /work/source.tar.gz >/dev/null"],
                           stdin=archive, capture_output=True, check=True, timeout=120)
    return boot_id


def build_in_vm(build_id, job, spec):
    (ROOT / 'active-build').write_text(build_id)
    (ROOT / 'last-build').write_text(build_id)
    alias = "build-" + build_id
    try:
        profile = builder_profile.load(ROOT) if spec.get("output_kind") == "static" else None
        started = time.monotonic()
        systemctl("stop", "paas-builder.service")
        reset_disk("builder", profile=profile)
        systemctl("start", "paas-builder.service")
        wait_ready("builder", 22099, alias, timeout=180, require_marker=bool(profile))
        ready = time.monotonic()
        guest_spec = spec | {"id": build_id}
        if profile:
            guest_spec["builder_node_image"] = profile["node_image"]
        boot_id = provision_builder(alias, job, guest_spec)
        metrics = {"boot_id": boot_id, "vm_boot_seconds": round(ready - started, 1),
                   "vm_setup_seconds": round(time.monotonic() - ready, 1)}
        if profile:
            metrics["builder_generation"] = profile["generation"]
        write_json(job / "status.json", {"id": build_id, "state": "running", "driver": "vm"} | metrics)
        command = ['/opt/paas-execution/ssh-stream', '22099', alias,
                   'sudo -n env POTETO_STREAM_BUILD_LOG=1 python3 /opt/paas-execution/guest_build.py']
        base = 'prepared Node base' if profile else 'standard base'
        intro = f"Disposable builder: {metrics['vm_boot_seconds']}s boot, {metrics['vm_setup_seconds']}s setup ({base})\n"
        result = collect_build_output(command, job / 'build.log', timeout=900, intro=intro.encode())
        if result['state'] != 'succeeded':
            raise ExecutionError("Application build failed; inspect deployment logs")
        if spec.get("output_kind") == "static":
            if result.get("output_kind") != "static":
                raise ExecutionError("Builder did not return static files")
            pending = job / "static.tar.gz.next"
            transfer = ["/opt/paas-execution/ssh-stream", "22099", alias, "sudo -n gzip -n -c /work/static.tar"]
            try:
                size = collect_build_output(transfer, job / "artifact-transfer.log", timeout=120,
                                            archive=pending, archive_limit=STATIC_ARCHIVE_LIMIT)
                if not size:
                    raise ExecutionError("Static build archive is empty")
                pending.replace(job / "static.tar.gz")
            finally:
                pending.unlink(missing_ok=True)
            return {"output_kind": "static", "artifact_bytes": size} | metrics
        image_disk_bytes=int(ssh(22099,alias,'sudo -n docker image inspect --format "{{.Size}}" paas-release:'+build_id).stdout.strip())
        if not 0<image_disk_bytes<=16*1024**3:
            raise ExecutionError('Built image disk size is invalid')
        # Bound host storage even if uploaded code compromises its own disposable guest.
        process = subprocess.Popen(["/opt/paas-execution/ssh-stream", str(22099), alias, "sudo -n cat /work/image.tar"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        size = 0
        try:
            with (job / "image.tar.next").open("wb") as out:
                while chunk := process.stdout.read(1024**2):
                    size += len(chunk)
                    if size > 1024**3:
                        raise ExecutionError("Built image exceeds 1 GiB")
                    out.write(chunk)
            if process.wait(timeout=30):
                raise ExecutionError("Built image transfer failed")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        (job / "image.tar.next").replace(job / "image.tar")
        return {"image": "paas-release:" + build_id, "image_bytes": size, "image_disk_bytes": image_disk_bytes} | metrics
    finally:
        systemctl("stop", "paas-builder.service")
        (ROOT / 'active-build').unlink(missing_ok=True)


def stop_native_build(build_id):
    active = ROOT / 'active-native-build'
    if active.exists() and active.read_text() == build_id:
        job = ROOT / 'builds' / build_id
        project = json.loads((job / 'spec.json').read_text())['project_id']
        native_build.finish(ROOT, job, project)
        active.unlink()


def run_build(build_id):
    job = ROOT / "builds" / checked_id(build_id)
    spec = json.loads((job / "spec.json").read_text())
    config_path = ROOT / 'config.json'
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if 'npm_registry' in config:
        spec['npm_registry'] = config['npm_registry']
    status_path = job / 'status.json'
    driver = json.loads(status_path.read_text()).get('driver', 'vm') if status_path.exists() else 'vm'
    with lock("image-admission" if driver == "prebuilt" else "builder"):
        receipt = {'id': build_id, 'driver': driver}
        started = time.monotonic()
        write_json(status_path, receipt | {'state': 'running'})
        try:
            if driver == 'native':
                if (ROOT / 'active-native-build').exists():
                    raise ExecutionError('A previous native builder has not stopped')
                if not native_build.allowed(config, spec):
                    raise ExecutionError('Project is no longer authorized for native builds')
                admit_space(incoming=10 * 1024**3)
                native_build.validate_builder(ROOT, spec['project_id'])
                (ROOT / 'active-native-build').write_text(build_id)
                try:
                    result = native_build.build(ROOT, job, spec, collect_build_output)
                finally:
                    stop_native_build(build_id)
            elif driver == 'vm':
                result = build_in_vm(build_id, job, spec)
            elif driver == 'prebuilt':
                if spec.get('runtime') != 'image':
                    raise ExecutionError('Invalid prebuilt image specification')
                admit_space(incoming=3 * prebuilt_image.IMAGE_LIMIT + 4 * 1024**2)
                result = prebuilt_image.normalize(job / 'source.tar.gz', job / 'image.tar', build_id)
                (job / 'build.log').write_text('Validated linux/amd64 image; no server build or builder VM\n')
            else:
                raise ExecutionError('Unknown build driver')
            write_json(status_path, receipt | result | {'state': 'succeeded', 'build_seconds': round(time.monotonic() - started, 1)})
        except Exception as error:
            # Never rewrite bytes that a live reader may already have consumed.
            if not (job / 'build.log').exists():
                prefix = 'Guest' if driver == 'vm' else 'Native'
                (job / 'build.log').write_text(f'{prefix} build output was unavailable after {type(error).__name__}\n')
            message = (f"Build step exceeded its {error.timeout}-second time limit; inspect deployment logs"
                       if isinstance(error, subprocess.TimeoutExpired) else
                       str(error) if isinstance(error, (ExecutionError, ValueError)) else "Host build driver failed")
            write_json(status_path, receipt | {'state': 'failed', 'error': message})
            raise


def finish_build(build_id):
    """Stop only the builder still owned by this job, including after SIGKILL."""
    checked_id(build_id)
    with (ROOT / 'builder.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        stop_native_build(build_id)
        active = ROOT / 'active-build'
        if active.exists() and active.read_text() == build_id:
            systemctl('stop', 'paas-builder.service')
            active.unlink()


def build_operation(build_id, account_id, action):
    checked_id(build_id)
    checked_id(account_id)
    job = ROOT / 'builds' / build_id
    if not job.exists():
        return {'absent': True}
    if json.loads((job / 'spec.json').read_text())['account_id'] != account_id:
        raise ExecutionError('Build does not belong to this tenant')
    if action == 'cancel':
        systemctl('stop', f'paas-build@{build_id}.service')
        finish_build(build_id)
        write_json(job / 'status.json', {'id': build_id, 'state': 'cancelled'})
        return {'state': 'cancelled'}
    if action == 'delete':
        if unit_state(f'paas-build@{build_id}.service') not in {'inactive', 'failed'}:
            raise ExecutionError('Build is still supervised; cancel it before deleting')
        active_native = ROOT / 'active-native-build'
        if active_native.exists() and active_native.read_text() == build_id:
            finish_build(build_id)
            if active_native.exists() and active_native.read_text() == build_id:
                raise ExecutionError('Native builder still needs cleanup before deleting')
        shutil.rmtree(job)
        with (ROOT / 'builder.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'deleted': True}
            last = ROOT / 'last-build'
            if last.exists() and last.read_text() == build_id and unit_state('paas-builder.service') in {'inactive', 'failed'}:
                for name in ['root.qcow2', 'seed.iso', 'user-data', 'meta-data', 'serial.log']:
                    (ROOT / 'vm' / 'builder' / name).unlink(missing_ok=True)
                last.unlink()
        return {'deleted': True}
    raise ValueError('Unsupported build operation')


def bootstrap_runtime(account_id):
    target = runtime(account_id)
    port, alias = target['ssh_port'], target['alias']
    version = ssh(port, alias, "if test -f /opt/paas-runtime/platform.version; then cat /opt/paas-runtime/platform.version; fi").stdout.strip()
    if version.decode() == RUNTIME_VERSION:
        return {'prepared': True}
    ssh(port, alias, 'sudo -n mkdir -p /opt/paas-runtime')
    ssh(port, alias, 'sudo -n install -d -m 0755 /opt/paas-runtime/publisher')
    for name in ['__init__.py','container_logs.py']:
        path='/opt/paas-runtime/publisher/'+name
        ssh(port,alias,'sudo -n tee '+path+'.install >/dev/null && sudo -n mv '+path+'.install '+path,data=(CODE/'publisher'/name).read_bytes())
    files = {'runtime_rpc.py': 'rpc.py', 'runtime_data.py':'data.py', 'runtime_reconcile.py': 'reconcile.py', 'runtime_bootstrap.sh': 'bootstrap.sh',
             'platform.json': 'platform.json', 'reconcile.service': 'reconcile.service', 'reconcile.timer': 'reconcile.timer'}
    for source, destination in files.items():
        path='/opt/paas-runtime/'+destination
        ssh(port, alias, 'sudo -n tee '+path+'.install >/dev/null && sudo -n mv '+path+'.install '+path, data=(CODE / 'execution' / source).read_bytes())
    for source, destination in [(CODE / 'healthcheck', 'healthcheck'), (CODE / 'configure-wakeup.py', 'configure-wakeup.py'), (CODE / 'static-nginx.conf', 'nginx.conf')]:
        path='/opt/paas-runtime/'+destination
        ssh(port, alias, 'sudo -n tee '+path+'.install >/dev/null && sudo -n mv '+path+'.install '+path, data=source.read_bytes())
    ssh(port, alias, 'sudo -n chmod 0755 /opt/paas-runtime/healthcheck')
    with (ROOT / 'wake-proxy-1.tar').open('rb') as image:
        subprocess.run(['/opt/paas-execution/ssh-stream', str(port), alias, 'sudo -n docker load -q'], stdin=image, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=120)
    ssh(port, alias, 'sudo -n bash /opt/paas-runtime/bootstrap.sh', timeout=180)
    ssh(port, alias, 'sudo -n tee /opt/paas-runtime/platform.version >/dev/null', data=(RUNTIME_VERSION + '\n').encode())
    return {'prepared': True}


def runtime_action(account_id, release_id, body):
    target = runtime(account_id)
    allowed = {'inspect', 'start', 'stop', 'reconcile', 'promote', 'restore_alias', 'cleanup', 'logs'}
    if body.get('action') not in allowed:
        raise ValueError('Unsupported runtime action')
    body = body | {'release_id': checked_id(release_id)}
    result = ssh(target['ssh_port'], target['alias'], 'sudo -n python3 /opt/paas-runtime/rpc.py', data=json.dumps(body).encode(), timeout=60)
    return json.loads(result.stdout)


def load_image(account_id, build_id):
    checked_id(account_id)
    checked_id(build_id)
    job = ROOT / "builds" / build_id
    if json.loads((job / "spec.json").read_text())["account_id"] != account_id:
        raise ExecutionError("Image does not belong to this tenant")
    result=status(build_id)
    if result["state"] != "succeeded":
        raise ExecutionError("Build has not succeeded")
    target = runtime(account_id)
    admit_runtime_write(target,max(result.get('image_disk_bytes',0),result['image_bytes']))
    with (job / "image.tar").open("rb") as image:
        subprocess.run(["/opt/paas-execution/ssh-stream", str(target["ssh_port"]), target["alias"], "sudo -n docker load -q"], stdin=image, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=180)
    ssh(target["ssh_port"], target["alias"], "sudo -n docker image inspect paas-release:" + build_id, timeout=30)
    return {"image": "paas-release:" + build_id, "account_id": account_id}


def admit_runtime_write(target, required):
    available=int(ssh(target['ssh_port'],target['alias'],'df -B1 --output=avail /var/lib/docker | tail -1').stdout.strip())
    if available<required+1024**3:
        raise ExecutionError('Tenant disk capacity is reserved for running applications and data; delete unused releases before loading another image')


def data_action(account_id, body):
    checked_id(account_id);resource=checked_id(body['resource_id'])
    checked_id(body['project_id'])
    if body.get('action')=='restore-idle':
        try:target=runtime_status(account_id)
        except KeyError:return {'idle':True}
        if target['unit_state'] in {'inactive','failed'}:return {'idle':True}
        observed=ssh(target['ssh_port'],target['alias'],shlex.join(['systemctl','show','paas-resource-restore-'+resource+'.service','-p','ActiveState','--value']),timeout=30)
        if observed.stdout.strip() not in {b'inactive',b'failed'}:
            raise ExecutionError('An owned resource restore is still active; wait before deleting it')
        return {'idle':True}
    target=runtime(account_id)
    result=ssh(target['ssh_port'],target['alias'],'sudo -n python3 /opt/paas-runtime/data.py',data=json.dumps(body).encode(),timeout=90)
    return json.loads(result.stdout)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PaaS-Execution"

    def response(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + (ROOT / "api.token").read_text().strip()
        return secrets.compare_digest(supplied, expected)

    def send_image(self,build_id):
        job=ROOT/'builds'/checked_id(build_id)
        account=checked_id(self.headers.get('X-Paas-Account',''))
        spec=json.loads((job/'spec.json').read_text())
        if spec['account_id']!=account or status(build_id)['state']!='succeeded':
            return self.response(404,{'error':'Build image unavailable'})
        with (job/'image.tar').open('rb') as source:
            size=os.fstat(source.fileno()).st_size
            if not 0<size<=1024**3:raise ExecutionError('Build image exceeds its export limit')
            self.send_response(200);self.send_header('Content-Type','application/octet-stream')
            self.send_header('Content-Length',str(size));self.end_headers()
            try:shutil.copyfileobj(source,self.wfile,1024**2)
            except (BrokenPipeError,ConnectionResetError):return

    def send_static_artifact(self, build_id):
        job = ROOT / "builds" / checked_id(build_id)
        account = checked_id(self.headers.get("X-Paas-Account", ""))
        project = checked_id(self.headers.get("X-Paas-Project", ""))
        spec = json.loads((job / "spec.json").read_text())
        receipt = status(build_id)
        if (spec.get("account_id"), spec.get("project_id"), receipt.get("state"), receipt.get("output_kind")) != (
                account, project, "succeeded", "static"):
            return self.response(404, {"error": "Built static artifact unavailable"})
        with (job / "static.tar.gz").open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            if not 0 < size <= STATIC_ARCHIVE_LIMIT or size != receipt["artifact_bytes"]:
                raise ExecutionError("Built static archive size does not match its receipt")
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            try:
                shutil.copyfileobj(source, self.wfile, 1024**2)
            except (BrokenPipeError, ConnectionResetError):
                return

    def snapshot_stream(self,account,body):
        operation=checked_id(body['operation']);project=checked_id(body['project_id'])
        target=runtime(account)
        program='/opt/paas-runtime/data.py'
        recovery=shlex.join(['/usr/bin/python3',program,'resume-export',operation,project])
        command=shlex.join(['sudo','-n','systemd-run','--quiet','--pipe','--wait','--collect',
            '--unit=paas-project-export-'+operation,'--property=RuntimeMaxSec=360',
            '--property=TimeoutStopSec=45','--property=UMask=0077','--property=MemoryMax=128M',
            '--property=ExecStopPost='+recovery,'/usr/bin/python3',program,'export',operation])
        process=subprocess.Popen([str(CODE/'ssh-stream'),str(target['ssh_port']),target['alias'],command],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.protocol_version='HTTP/1.1';self.close_connection=True;self.connection.settimeout(60)
            self.send_response(200);self.send_header('Content-Type','application/zip')
            self.send_header('Transfer-Encoding','chunked');self.send_header('Connection','close');self.end_headers()
            process.stdin.write(json.dumps(body).encode());process.stdin.close()
            total=0
            while chunk:=process.stdout.read(1024**2):
                total+=len(chunk)
                if total>12*1024**3+16*1024**2:raise ExecutionError('Snapshot stream exceeds its limit')
                self.wfile.write(f'{len(chunk):x}\r\n'.encode());self.wfile.write(chunk);self.wfile.write(b'\r\n')
            if process.wait(timeout=45):
                self.log_error('Project snapshot %s failed: %s',operation,process.stderr.read(2048).decode(errors='replace').strip())
                return
            # A truncated stream never gets the final chunk. The publisher may
            # commit the encrypted bundle only after the runtime has resumed.
            self.wfile.write(b'0\r\n\r\n')
        except (OSError,ExecutionError,subprocess.TimeoutExpired) as error:
            self.log_error('Project snapshot %s interrupted (%s)',operation,type(error).__name__)
        finally:
            if process.poll() is None:process.kill();process.wait()
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def restore_stream(self,account):
        size=int(self.headers.get('Content-Length','0'))
        if self.headers.get('Content-Type')!='application/octet-stream' or not 0<size<=12*1024**3+16*1024**2:
            raise ValueError('Invalid owned resource restore upload')
        body=json.loads(base64.urlsafe_b64decode(self.headers.get('X-Paas-Restore-Spec','')))
        resource=checked_id(body['resource_id']);checked_id(body['project_id']);checked_id(body['operation'])
        if body['kind'] not in {'database','volume'}:raise ValueError('Invalid resource restore kind')
        body={k:body[k] for k in ('resource_id','project_id','operation','kind')}|{'size':size}
        target=runtime(account)
        # A retry may be reconciling an already committed receipt. The guest
        # checks new writes against its own disk before restoring any bytes.
        command=shlex.join(['sudo','-n','systemd-run','--quiet','--pipe','--wait','--collect',
            '--unit=paas-resource-restore-'+resource,'--property=RuntimeMaxSec=900','--property=UMask=0077',
            '--property=MemoryMax=192M','/usr/bin/python3','/opt/paas-runtime/data.py','restore-data'])
        process=subprocess.Popen([str(CODE/'ssh-stream'),str(target['ssh_port']),target['alias'],command],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.connection.settimeout(120)
            process.stdin.write(json.dumps(body).encode()+b'\n')
            while size:
                chunk=self.rfile.read(min(size,1024**2))
                if not chunk:raise ValueError('Incomplete resource restore upload')
                process.stdin.write(chunk);size-=len(chunk)
            process.stdin.close()
            if process.wait(timeout=920):
                self.log_error('Resource restore %s failed: %s',resource,process.stderr.read(2048).decode(errors='replace').strip())
                raise ExecutionError('Owned resource restore failed; retry the same import after inspection')
            result=json.loads(process.stdout.read(4096))
            if result!={'restored':True}:raise ExecutionError('Resource restore receipt was not confirmed')
            return self.response(200,result)
        finally:
            if process.poll() is None:process.kill();process.wait()
            for stream in (process.stdin,process.stdout,process.stderr):
                try:stream.close()
                except BrokenPipeError:pass

    def do_GET(self):
        if not self.authorized():
            return self.response(401, {"error": "Execution supervisor credential required"})
        try:
            parts = self.path.strip("/").split("/")
            if len(parts)==4 and parts[:2]==['v1','runtimes'] and parts[3]=='status':
                return self.response(200,runtime_status(parts[2]))
            if len(parts) == 4 and parts[:2] == ["v1", "builds"] and parts[3] == "logs":
                checked_id(parts[2])
                path = ROOT / "builds" / parts[2] / "build.log"
                return self.response(200, {"log": path.read_text(errors="replace") if path.exists() else ""})
            if len(parts) == 5 and parts[:2] == ['v1', 'builds'] and parts[3] == 'logs':
                return self.response(200, build_log_chunk(parts[2], int(parts[4])))
            if len(parts)==4 and parts[:2]==['v1','builds'] and parts[3]=='image':
                return self.send_image(parts[2])
            if len(parts) == 4 and parts[:2] == ["v1", "builds"] and parts[3] == "static":
                return self.send_static_artifact(parts[2])
            if len(parts) == 3 and parts[:2] == ["v1", "builds"]:
                return self.response(200, status(parts[2]))
            if len(parts) == 3 and parts[:2] == ["v1", "runtimes"]:
                with active_account(parts[2]):
                    return self.response(200, runtime(parts[2]))
            self.response(404, {"error": "Unknown execution operation"})
        except (ValueError, KeyError, FileNotFoundError) as error:
            self.response(404, {"error": str(error)})
        except ExecutionError as error:
            self.response(409, {"error": str(error)})

    def do_POST(self):
        if not self.authorized():
            return self.response(401, {"error": "Execution supervisor credential required"})
        leases=ExitStack()
        try:
            parts = self.path.strip("/").split("/")
            if len(parts)==4 and parts[:2]==['v1','runtimes'] and parts[3] in {'prepare-retirement','commit-retirement'}:
                return self.response(200,retire_runtime(parts[2],parts[3].split('-')[0]))
            if len(parts)>=3 and parts[:2]==['v1','runtimes']:
                leases.enter_context(active_account(parts[2]))
            if len(parts)==4 and parts[:2]==['v1','runtimes'] and parts[3]=='snapshot':
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=4096:raise ValueError('Invalid project snapshot request size')
                return self.snapshot_stream(checked_id(parts[2]),json.loads(self.rfile.read(size)))
            if len(parts)==4 and parts[:2]==['v1','runtimes'] and parts[3]=='restore-data':
                return self.restore_stream(checked_id(parts[2]))
            if len(parts)==4 and parts[:2]==['v1','runtimes'] and parts[3]=='data':
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=131072:
                    raise ValueError('Invalid data operation size')
                return self.response(200,data_action(parts[2],json.loads(self.rfile.read(size))))
            if len(parts) == 4 and parts[:2] == ['v1', 'builds'] and parts[3] in {'cancel', 'delete'}:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024:
                    raise ValueError('Invalid build operation')
                body = json.loads(self.rfile.read(size))
                return self.response(200, build_operation(parts[2], body['account_id'], parts[3]))
            if len(parts) == 4 and parts[:2] == ['v1', 'runtimes'] and parts[3] == 'bootstrap':
                return self.response(200, bootstrap_runtime(checked_id(parts[2])))
            if len(parts) == 5 and parts[:2] == ['v1', 'runtimes'] and parts[3] == 'releases':
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024:
                    raise ValueError('Invalid runtime operation')
                return self.response(200, runtime_action(checked_id(parts[2]), checked_id(parts[4]), json.loads(self.rfile.read(size))))
            if len(parts) == 5 and parts[:2] == ["v1", "runtimes"] and parts[3] == "images":
                return self.response(200, load_image(parts[2], parts[4]))
            if len(parts) != 3 or parts[0] != "v1":
                return self.response(404, {"error": "Unknown execution operation"})
            resource_id = checked_id(parts[2])
            size = int(self.headers.get("Content-Length", "0"))
            if parts[1] == "sandboxes":
                if not 0 < size <= 400 * 1024:
                    raise ValueError("Invalid sandbox request size")
                self.connection.settimeout(15)
                body = json.loads(self.rfile.read(size))
                leases.enter_context(active_account(checked_id(body["account_id"])))
                with lock("sandbox-placements"):
                    config = json.loads((ROOT / "config.json").read_text())
                    return self.response(200, sandbox.operation(ROOT, config, resource_id, body, status))
            if parts[1] == "runtimes":
                if not 0 < size <= 4096:
                    raise ValueError("Invalid runtime request size")
                body = json.loads(self.rfile.read(size))
                return self.response(200, runtime(resource_id, body["deploy_public_key"]))
            if parts[1] != "builds" or not 0 < size <= 1024**3:
                raise ValueError("Invalid build upload")
            spec = json.loads(base64.urlsafe_b64decode(self.headers["X-Paas-Build-Spec"]))
            if "builder_node_image" in spec:
                raise ValueError("Builder base images are selected by the executor")
            leases.enter_context(active_account(spec["account_id"]))
            if 'project_id' in spec:
                checked_id(spec['project_id'])
            require_native = spec.get('require_native', False)
            if type(require_native) is not bool:
                raise ValueError('Invalid native build requirement')
            output_kind = spec.get("output_kind", "image")
            if output_kind != "image" and (output_kind != "static" or spec.get("runtime") != "static"
                    or not isinstance(spec.get("build_command"), str) or not spec["build_command"].strip()
                    or require_native or not spec.get("project_id")):
                raise ValueError("Invalid static build request")
            if require_native and not native_build.allowed(json.loads((ROOT / 'config.json').read_text()), spec):
                raise ExecutionError('Sandbox preview requires a trusted native build')
            if spec.get('runtime')=='image-import':
                reference=spec.get('image_reference')
                if self.headers.get('Content-Type')!='application/x-tar' or not isinstance(reference,str) or not re.fullmatch(r'(?:paas-release:[a-f0-9]{24}|sha256:[a-f0-9]{64})',reference):
                    raise ValueError('Invalid isolated image import')
            elif spec.get("runtime") == "image":
                if (self.headers.get('Content-Type') != 'application/gzip' or require_native
                        or not spec.get('project_id') or size > prebuilt_image.TRANSPORT_LIMIT):
                    raise ValueError('Invalid prebuilt image upload')
            elif spec.get("runtime") not in {"static", "node", "dockerfile"} or size>50*1024**2:
                raise ValueError("Unsupported build runtime")
            admit_space('builder', incoming=size + 1024**3)
            job = ROOT / "builds" / resource_id
            job.mkdir(mode=0o700, exist_ok=True)
            self.connection.settimeout(120)
            pending = job / (secrets.token_hex(12) + ".part")
            try:
                with pending.open("xb") as out:
                    while size:
                        chunk = self.rfile.read(min(size, 1024**2))
                        if not chunk:
                            raise ValueError("Incomplete build upload")
                        out.write(chunk)
                        size -= len(chunk)
                with lock("submit"):
                    if (job / "status.json").exists():
                        previous = json.loads((job / "spec.json").read_text())
                        if previous != spec and not ('project_id' not in previous and
                                                     previous == {k: v for k, v in spec.items() if k != 'project_id'}):
                            return self.response(409, {"error": "Build ID already has a different specification"})
                    else:
                        config_path = ROOT / 'config.json'
                        config = json.loads(config_path.read_text()) if config_path.exists() else {}
                        if spec.get('runtime') == 'image':
                            driver = 'prebuilt'
                        else:
                            driver = 'native' if output_kind == "image" and native_build.allowed(config, spec) else 'vm'
                        if require_native and driver != 'native':
                            raise ExecutionError('Project is no longer authorized for native builds')
                        pending.replace(job / "source.tar.gz")
                        write_json(job / "spec.json", spec)
                        write_json(job / "status.json", {"id": resource_id, "state": "queued", "driver": driver})
                        systemctl("start", f"paas-build@{resource_id}.service")
            finally:
                pending.unlink(missing_ok=True)
            self.response(202, status(resource_id))
        except (ValueError, KeyError, FileNotFoundError) as error:
            self.response(400, {"error": str(error)})
        except ExecutionError as error:
            self.response(409, {"error": str(error)})
        except Exception:
            self.response(500, {"error": "Execution supervisor failed; administrator inspection required"})
        finally:
            leases.close()


def main():
    command = sys.argv[1]
    if command == "serve":
        http.server.ThreadingHTTPServer(("127.0.0.1", 21999), Handler).serve_forever()
    elif command == "prepare-template":
        prepare_template()
    elif command == "prepare-builder-base":
        prepare_builder_base(sys.argv[2])
    elif command == "run-build":
        run_build(sys.argv[2])
    elif command == 'finish-build':
        finish_build(sys.argv[2])
    else:
        raise ValueError("Unknown supervisor command")


if __name__ == "__main__":
    main()
