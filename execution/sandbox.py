"""Opt-in, stateless gVisor previews from native builds or normalized images."""
import hashlib
import http.client
import json
import math
from pathlib import Path
import re
import socket
import subprocess
import tarfile
from execution import sandbox_network, sandbox_wake

RUNTIME = "io.containerd.runsc.v1"
PORTS = sandbox_wake.APP_PORTS
ID = re.compile(r"[a-f0-9]{24}")
ENV = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")


def docker(*args, image=None, timeout=25):
    result = subprocess.run(
        ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args],
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        stdin=image, capture_output=True, timeout=timeout,
    )
    if result.returncode:
        raise ValueError("Sandbox Docker operation failed: " + args[0])
    return result.stdout.decode(errors="replace").strip()


def inspect(kind, name):
    # The exact-name check avoids interpreting an arbitrary Docker error as absence.
    if kind == "container":
        names = docker("ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}")
    else:
        names = docker("network", "ls", "--filter", "name=^" + name + "$", "--format", "{{.Name}}")
    if name not in names.splitlines():
        return None
    return json.loads(docker(kind, "inspect", name))[0]


def validate(spec):
    memory, cpu, port, env = (spec.get(k) for k in ("memory_mb", "cpus", "port", "environment"))
    if spec.get('outbound_network', 'none') not in ('none', 'web'):
        raise ValueError('Invalid sandbox outbound network policy')
    if (type(memory) is not int or not 256 <= memory <= 1024
            or type(cpu) not in (int, float) or not math.isfinite(cpu) or not 0.1 <= cpu <= 1
            or type(port) is not int or not 1024 <= port <= 65535
            or not isinstance(env, dict) or len(env) > (104 if spec.get('database_id') else 103)
            or not all(isinstance(k, str) and ENV.fullmatch(k) and isinstance(v, str)
                       and len(v) <= 16384 and "\x00" not in v for k, v in env.items())):
        raise ValueError("Invalid sandbox preview configuration (minimum memory: 256 MiB)")
    return memory, cpu, port, env


def labels(release, account, project):
    return {"poteto.sandbox.release": release, "poteto.sandbox.account": account,
            "poteto.sandbox.project": project}


def owned(effective, expected):
    if not all((effective or {}).get(k) == v for k, v in expected.items()):
        raise ValueError("Sandbox resource ownership does not match")


def save(path, receipt):
    pending = path.with_suffix(".next")
    pending.write_text(json.dumps(receipt))
    pending.chmod(0o600)
    pending.replace(path)


def image_identities(archive, tag):
    with tarfile.open(archive, mode="r:") as bundle:
        def content(name):
            member = bundle.getmember(name)
            if not member.isfile() or not 0 < member.size <= 1024**2:
                raise ValueError("Invalid sandbox image metadata")
            with bundle.extractfile(member) as source:
                return source.read()

        def entry(name):
            return json.loads(content(name))

        manifest = entry("manifest.json")
        if not isinstance(manifest, list) or len(manifest) != 1 or manifest[0].get("RepoTags") != [tag]:
            raise ValueError("Native build did not export exactly the owned image tag")
        match = re.fullmatch(r"blobs/sha256/([a-f0-9]{64})", manifest[0].get("Config", ""))
        if not match:
            raise ValueError("Native build did not export a Docker image config")
        config = "sha256:" + match[1]
        if hashlib.sha256(content(manifest[0]["Config"])).hexdigest() != match[1]:
            raise ValueError("Sandbox image configuration digest does not match")
        # Docker's classic and containerd image stores report different IDs.
        # Both must refer to descriptors in this same single-platform export.
        index = entry("index.json")["manifests"]
        digest = index[0]["digest"] if len(index) == 1 else ""
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Native build image descriptors do not match")
        data = content("blobs/sha256/" + digest.split(":", 1)[1])
        if hashlib.sha256(data).hexdigest() != digest.split(":", 1)[1] or json.loads(data)["config"]["digest"] != config:
            raise ValueError("Sandbox image manifest digest does not match")
        return [config, digest]


def has_tag(tag):
    refs = docker("image", "ls", "--filter", "reference=" + tag, "--format", "{{.Repository}}:{{.Tag}}")
    return tag in refs.splitlines()


class DockerSocket(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def create(name, image, port, host_port, memory, cpu, env, ownership, *, wake=None, dns=None, database=None):
    key = f"{port}/tcp"
    body = {
        "Image": image, "User": "10001:10001", "Tty": True,
        "Env": [k + "=" + v for k, v in env.items()],
        "Labels": ownership | {"sablier.enable": str(bool(wake)).lower(),
                               "sablierapp.dev/enable": str(bool(wake)).lower(),
                               "sablier.group": name, "sablierapp.dev/group": name},
        "ExposedPorts": {key: {}}, "Healthcheck": {"Test": ["NONE"]},
        "HostConfig": {
            "Runtime": RUNTIME, "CgroupParent": "paas-sandbox.slice", "NetworkMode": name,
            "Dns": dns or ["127.0.0.1"], "PortBindings": {key: [{"HostIp": "127.0.0.1", "HostPort": str(host_port)}]},
            "ReadonlyRootfs": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=67108864"},
            "NanoCpus": round(cpu * 10**9), "Memory": memory * 1024**2, "MemorySwap": memory * 1024**2,
            "PidsLimit": 128, "LogConfig": {"Type": "local", "Config": {"max-size": "5m", "max-file": "2"}},
        },
        "NetworkingConfig": {"EndpointsConfig": {name: {}}},
    }
    if database:
        body['NetworkingConfig']['EndpointsConfig'][database['network']] = {}
        body['HostConfig']['ExtraHosts'] = ['db-' + database['id'] + ':' + database['address']]
    if dns:
        body['HostConfig']['Mounts'] = [{'Type':'bind', 'Source':str(sandbox_network.RESOLV),
                                        'Target':'/etc/resolv.conf', 'ReadOnly':True}]
    if wake:
        body["Healthcheck"] = {"Test": ["CMD", "/poteto-healthcheck", f"http://127.0.0.1:{port}" + wake["health_check"]],
                               "Interval": 2_000_000_000, "Timeout": 3_000_000_000,
                               "StartPeriod": 2_000_000_000, "Retries": 30}
        body["HostConfig"].setdefault("Mounts", []).append({"Type": "bind", "Source": str(sandbox_wake.HEALTHCHECK),
                                                         "Target": "/poteto-healthcheck", "ReadOnly": True})
    conn = DockerSocket("localhost", timeout=25)
    try:
        conn.request("POST", "/containers/create?name=" + name, json.dumps(body).encode(),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read(65536)
        if response.status != 201:
            raise ValueError("Docker did not create the sandbox container")
    finally:
        conn.close()


def observe(container, spec):
    limits = container["HostConfig"]
    memory, cpu, port, _ = validate(spec)
    if limits["Runtime"] != RUNTIME or not limits["ReadonlyRootfs"] or limits["Privileged"]:
        raise ValueError("Effective sandbox configuration does not match")
    expected_mounts = {}
    if spec.get('outbound_network') == 'web':
        if limits.get('Dns') != spec['dns']:
            raise ValueError('Sandbox DNS configuration does not match')
        expected_mounts['/etc/resolv.conf'] = str(sandbox_network.RESOLV)
    if spec.get("sleep"):
        expected_mounts['/poteto-healthcheck'] = str(sandbox_wake.HEALTHCHECK)
        test = ["CMD", "/poteto-healthcheck", f"http://127.0.0.1:{port}" + spec["health_check"]]
        if container["Config"]["Healthcheck"]["Test"] != test:
            raise ValueError("Sandbox readiness probe is not isolated")
    mounts = container.get('Mounts', [])
    if len({mount.get('Destination') for mount in mounts}) != len(expected_mounts) or len(mounts) != len(expected_mounts) or any(
            mount.get('Type', 'bind') != 'bind' or mount.get('RW')
            or expected_mounts.get(mount.get('Destination')) != mount.get('Source') for mount in mounts):
        raise ValueError('Sandbox host mounts do not match its platform-owned files')
    if spec.get('database'):
        database = spec['database']
        if (set(container['NetworkSettings']['Networks']) != {spec['network_name'], database['network']}
                or limits.get('ExtraHosts') != ['db-' + database['id'] + ':' + database['address']]):
            raise ValueError('Sandbox database network does not match its owned binding')
    bindings = container["NetworkSettings"]["Ports"].get(f"{port}/tcp", [])
    if bindings != [{"HostIp": "127.0.0.1", "HostPort": str(spec["host_port"])}]:
        raise ValueError("Sandbox ingress is not restricted to host loopback")
    observe_limits(container, memory, cpu)


def observe_limits(container, memory, cpu):
    pid = container["State"]["Pid"]
    entry = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
    if not entry.startswith("/paas.slice/paas-sandbox.slice/"):
        raise ValueError("Sandbox is outside its host resource envelope")
    group = Path("/sys/fs/cgroup") / entry.lstrip("/")
    actual_cpu, period = map(int, (group / "cpu.max").read_text().split())
    if (actual_cpu != round(cpu * period) or (group / "memory.max").read_text().strip() != str(memory * 1024**2)
            or (group / "memory.swap.max").read_text().strip() != "0"
            or (group / "pids.max").read_text().strip() != "128"):
        raise ValueError("Sandbox resource limits were not enforced by the host")


def pool_state():
    group = Path('/sys/fs/cgroup/paas.slice/paas-sandbox.slice')
    return (int((group / 'memory.max').read_text()), tuple(map(int, (group / 'cpu.max').read_text().split())),
            int((group / 'memory.swap.max').read_text()))


def check_pool(root, config, release, memory, cpu):
    limit = config.get('sandbox_memory_limit_mib', 2048)
    cpus = config.get('sandbox_cpu_limit', 1.5)
    if (type(limit) is not int or not 256 <= limit <= 16384 or type(cpus) not in (int, float)
            or not math.isfinite(cpus) or not 0.1 <= cpus <= 8):
        raise ValueError('Invalid sandbox pool limits')
    subprocess.run(['systemctl', 'start', 'paas-sandbox.slice'], check=True, timeout=15)
    effective_memory, (actual, period), swap = pool_state()
    if effective_memory != limit * 1024**2 or actual != round(cpus * period) or swap != 0:
        raise ValueError('Sandbox pool configuration does not match its effective cgroup limits')
    reservations = (list((root / 'sandboxes').glob('*.json'))
                    + list((root / 'native-databases').glob('*/database.json'))
                    + list((root / 'sandbox-migrations').glob('*.json')))
    for record in reservations:
        identity = record.parent.name if record.name == 'database.json' else record.stem
        if identity == release:
            continue
        value = json.loads(record.read_text())
        if record.parent.name == 'sandbox-migrations' and value['phase'] in {'succeeded','failed'}:
            continue
        if 'memory_mb' not in value or 'cpus' not in value:
            container = inspect('container', 'poteto-sbx-' + record.stem)
            if container is None:
                raise ValueError('An older sandbox receipt needs reconciliation before admitting another preview')
            owned(container['Config']['Labels'], labels(record.stem, value['account_id'], value['project_id']))
            value |= {'memory_mb': container['HostConfig']['Memory'] // 1024**2,
                      'cpus': container['HostConfig']['NanoCpus'] / 10**9}
        memory += value['memory_mb']
        cpu += value['cpus']
    if memory > limit or round(cpu, 6) > cpus:
        raise ValueError('Sandbox pool capacity is reserved; remove an unused preview')


def operation(root, config, release, body, build_status):
    account, project, action = (body.get(k) for k in ("account_id", "project_id", "action"))
    if not all(isinstance(v, str) and ID.fullmatch(v) for v in (release, account, project)):
        raise ValueError("Invalid sandbox ownership")
    if action in {'migrate','stop-migration'}:
        from execution import sandbox_migration
        return sandbox_migration.operation(root,config,release,body,build_status)
    if action not in {"start", "inspect", "logs", "delete"}:
        raise ValueError("Invalid sandbox operation")
    records = root / "sandboxes"
    records.mkdir(mode=0o700, exist_ok=True)
    path = records / (release + ".json")
    receipt = json.loads(path.read_text()) if path.exists() else None
    if receipt and (receipt["account_id"], receipt["project_id"]) != (account, project):
        raise ValueError("Sandbox does not belong to this project")
    expected = labels(release, account, project)
    name = "poteto-sbx-" + release
    if action == "start":
        from execution.sandbox_migration import completed_image
        migration_image = completed_image(root,release,account,project)
        personal = body.get('personal_preview', False)
        if type(personal) is not bool or (personal and config.get('personal_previews') is not True):
            raise ValueError('Personal preview execution is not enabled')
        if not personal and config.get("sandbox_projects", {}).get(project) != account:
            raise ValueError("Project is not authorized for sandbox previews")
        memory, cpu, port, env = validate(body)
        outbound = body.get('outbound_network', 'none')
        if outbound == 'web' and config.get('sandbox_web') is not True:
            raise ValueError('Sandbox web egress is not enabled')
        dns = sandbox_network.dns_servers(config) if outbound == 'web' else None
        database = None
        if body.get('database_id'):
            from execution.native_database import binding
            database = binding(root, config, body['database_id'], account, project)
        wake = sandbox_wake.options(release, body)
        if wake and ((not personal and config.get("sandbox_sleep_projects", {}).get(project) != account)
                     or not sandbox_wake.HEALTHCHECK.is_file()):
            raise ValueError("Sandbox sleep is not provisioned for this project")
        build = root / "builds" / release
        spec = json.loads((build / "spec.json").read_text())
        image = "paas-release:" + release
        status = build_status(release)
        driver = status.get("driver")
        if spec.get('outbound_network', 'none') != outbound:
            raise ValueError('Sandbox network policy does not match its owned build receipt')
        if personal and (driver != 'prebuilt' or spec.get('personal_preview') is not True):
            raise ValueError('Personal previews require an owned prebuilt image receipt')
        source = ((driver == "native" and spec.get("runtime") in {"node", "dockerfile"})
                  or (driver == "prebuilt" and spec.get("runtime") == "image"))
        if not source or (spec.get("account_id"), spec.get("project_id"), status.get("state"),
                          status.get("image")) != (account, project, "succeeded", image):
            raise ValueError("Sandbox requires an owned, trusted native build or normalized prebuilt image")
        if personal:
            check_pool(root, config, release, memory, cpu)
        for command in (["systemctl", "is-active", "--quiet", "paas-sandbox-network.service"],
                        ["nft", "list", "table", "inet", "poteto_sandbox"]):
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if receipt is None:
            used = {json.loads(p.read_text())["host_port"] for p in records.glob("*.json")} | sandbox_wake.unavailable(root)
            host_port = next((v for v in PORTS if v not in used), None)
            if host_port is None:
                raise ValueError("No sandbox preview slot is available")
            if inspect("container", name) or inspect("network", name):
                raise ValueError("Sandbox name is already in use")
            receipt = {"account_id": account, "project_id": project, "host_port": host_port}
            receipt |= {'memory_mb': memory, 'cpus': cpu, 'personal_preview': personal, 'outbound_network': outbound}
            if database:
                receipt['database_id'] = database['id']
            if wake:
                receipt["wake"] = wake
            save(path, receipt)
        if receipt.get('database_id') != (database['id'] if database else None):
            raise ValueError('Sandbox database binding does not match its receipt')
        if receipt.get('outbound_network', 'none') != outbound:
            raise ValueError('Sandbox network policy does not match its receipt')
        if receipt.get("wake") != wake:
            raise ValueError("Sandbox wake configuration does not match its receipt")
        if (receipt.get('personal_preview', False) != personal or receipt.get('memory_mb', memory) != memory
                or receipt.get('cpus', cpu) != cpu):
            raise ValueError('Sandbox resources do not match the existing receipt')
        bridge = "psbx" + str(receipt["host_port"])
        network = inspect("network", name)
        flags = [item for k, v in expected.items() for item in ("--label", k + "=" + v)]
        if network is None:
            docker("network", "create", "--driver", "bridge", "--opt",
                   "com.docker.network.bridge.name=" + bridge, *flags, name)
        else:
            owned(network["Labels"], expected)
            if network["Options"].get("com.docker.network.bridge.name") != bridge:
                raise ValueError("Sandbox is attached to an unexpected bridge")
        if config.get('sandbox_web') or outbound == 'web':
            sandbox_network.configure(root, config)
        container = inspect("container", name)
        if container is None:
            archive_path = build / "image.tar"
            if not receipt.get("image_ids"):
                previous = receipt.get("image_id")
                if not previous and has_tag(image):
                    previous_ids = migration_image
                    if previous_ids is None:
                        raise ValueError("Sandbox image tag is already in use")
                    if previous_ids != image_identities(archive_path,image):
                        raise ValueError('Migration image does not match this preview archive')
                receipt["image_ids"] = image_identities(archive_path, image)
                if previous and previous not in receipt["image_ids"]:
                    raise ValueError("The existing image receipt does not match the native export")
                receipt.pop("image_id", None)
                save(path, receipt)
            if not has_tag(image):
                with archive_path.open("rb") as archive:
                    docker("load", "-q", image=archive, timeout=180)
            metadata = json.loads(docker("image", "inspect", image))[0]
            if metadata["Id"] not in receipt["image_ids"]:
                raise ValueError("Sandbox preview image does not match its native export")
            if metadata["Config"].get("Volumes"):
                raise ValueError("Sandbox preview cannot create anonymous volumes")
            if wake and any(k.startswith(("sablier.", "sablierapp.dev/", "com.docker.compose."))
                            for k in metadata["Config"].get("Labels", {}) or {}):
                raise ValueError("Sleeping sandbox images cannot supply lifecycle labels")
            create(name, metadata["Id"], port, receipt["host_port"], memory, cpu, env, expected, wake=wake, dns=dns, database=database)
            container = inspect("container", name)
        owned(container["Config"]["Labels"], expected)
        if not container["State"]["Running"]:
            docker("start", name, timeout=45)
        try:
            observe(inspect("container", name), body | {"host_port": receipt["host_port"], "dns": dns, "database": database, "network_name": name})
        except Exception:
            docker("stop", "-t", "2", name)
            raise
        if wake:
            receipt["wake_ready"] = True
            save(path, receipt)
            sandbox_wake.configure(root, config, docker)
        return {"http_port": sandbox_wake.PROXY_PORT if wake else receipt["host_port"]}
    if action == 'delete':
        from execution.sandbox_migration import remove
        migration_image = remove(root,release,account,project)
        if receipt is None and migration_image and has_tag('paas-release:'+release):
            if docker('image','inspect','--format','{{.Id}}','paas-release:'+release) not in migration_image:
                raise ValueError('Refusing to delete an image without its migration receipt')
            docker('image','rm','paas-release:'+release)
    if receipt is None:
        return {"container": None, "log": "", "deleted": True}
    container = inspect("container", name)
    if container is not None:
        owned(container["Config"]["Labels"], expected)
    if action == "inspect":
        value = {"container": {"State": {"Running": container["State"]["Running"]}} if container else None}
        if container:
            value['http_port'] = sandbox_wake.PROXY_PORT if receipt.get('wake') else receipt['host_port']
        return value
    if action == "logs":
        return {"log": docker("logs", "--tail", "200", name)[-65536:] if container else ""}
    if receipt.get("wake"):
        receipt["wake_ready"] = False
        save(path, receipt)
        sandbox_wake.configure(root, config, docker)
        sandbox_wake.quarantine(root, receipt["host_port"])
    if container is not None:
        docker("rm", "-f", name)
    network = inspect("network", name)
    if network is not None:
        owned(network["Labels"], expected)
        docker("network", "rm", name)
    image_ids = receipt.get("image_ids")
    if not image_ids and receipt.get("image_id"):
        image_ids = image_identities(root / "builds" / release / "image.tar", "paas-release:" + release)
        if receipt["image_id"] not in image_ids:
            raise ValueError("The existing image receipt does not match the native export")
    if image_ids:
        image = "paas-release:" + release
        if has_tag(image):
            effective = docker("image", "inspect", "--format", "{{.Id}}", image)
            if effective not in image_ids:
                raise ValueError("Refusing to delete an image without an ownership receipt")
            docker("image", "rm", image)
    if config.get('sandbox_web') or receipt.get('outbound_network') == 'web':
        sandbox_network.configure(root, config, exclude=release)
    path.unlink()
    return {"deleted": True}
