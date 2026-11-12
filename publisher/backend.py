"""Trusted Dokploy adapter. User archives never supply Compose or Docker options."""
import http.client
import json
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from publisher.container_logs import capture


class BackendError(RuntimeError):
    pass


class Dokploy:
    def __init__(self, config, settings):
        self.config = config
        self.settings = settings
        self.store = None
        self.releases = Path(config.get("releases", "/var/lib/paas-publisher/releases"))
        self.releases.mkdir(exist_ok=True, parents=True, mode=0o755)

    def api(self, name, data=None, query=None):
        url = self.config["url"].rstrip("/") + "/api/" + name
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"x-api-key": self.config["api_key"], "Content-Type": "application/json"}
        request = urllib.request.Request(url, None if data is None else json.dumps(data).encode(), headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return json.loads(body) if body else None
        except Exception as error:
            # Never copy upstream bodies, which may include environment secrets, into logs.
            raise BackendError(f"Dokploy {name} failed ({type(error).__name__})") from None

    @staticmethod
    def docker(*args, timeout=60):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise BackendError(f"Docker {args[0]} failed")
        return result.stdout

    def target(self, deployment_id):
        if self.store is None:
            return None
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM execution_targets WHERE deployment_id=?', (deployment_id,)).fetchone()
        return dict(row) if row else None

    def execution_client(self):
        from publisher.executor import ExecutionClient
        return ExecutionClient(json.loads(Path('/etc/paas-publisher/executor.json').read_text()))

    def find_container(self, deployment_id):
        target = self.target(deployment_id)
        if target:
            return self.execution_client().action(target['account_id'], deployment_id, 'inspect')['container']
        ids = self.docker("ps", "-aq", "--filter", "label=paas.publisher.release=" + deployment_id).split()
        if not ids:
            return None
        if len(ids) != 1:
            raise BackendError("Release has an unexpected number of containers")
        return json.loads(self.docker("inspect", ids[0]))[0]

    def runtime_logs(self, deployment):
        target=self.target(deployment['id'])
        if target:
            return self.execution_client().action(target['account_id'],deployment['id'],'logs')['log']
        current=self.find_container(deployment['id'])
        if not current:raise BackendError('Release container not found')
        try:return capture(current['Id'])
        except RuntimeError:raise BackendError('Runtime log collection failed') from None

    def prepare_static(self, deployment, source):
        config = json.loads(deployment["config"])
        output = (source / config["output_directory"]).resolve()
        if not output.is_relative_to(source.resolve()) or not (output / "index.html").is_file():
            raise BackendError("Static output_directory must contain index.html")
        directory = self.releases / deployment["id"]
        directory.mkdir(exist_ok=True, mode=0o755)
        nginx = directory / "nginx.conf"
        nginx.write_text(Path(__file__).with_name('static-nginx.conf').read_text())
        nginx.chmod(0o644)
        return {"image": "nginx:1.28-alpine", "user": "101:101", "read_only": True,
                "healthcheck": {"test": ["CMD", "wget", "-q", "-O", "/dev/null", "-T", "2", "http://127.0.0.1:8080/"],
                                "interval": "2s", "timeout": "3s", "start_period": "2s", "retries": 30},
                "volumes": [{"type": "bind", "source": str(output), "target": "/usr/share/nginx/html", "read_only": True},
                            {"type": "bind", "source": str(nginx), "target": "/etc/nginx/nginx.conf", "read_only": True}]}, 8080

    def render(self, deployment, service, port):
        release_id = deployment["id"]
        router = "pub-" + release_id
        config = json.loads(deployment["config"])
        host = self.hostname(deployment)
        labels = {"paas.publisher.release": release_id, "paas.publisher.project": deployment["project_id"],
                  'paas.publisher.resource':'','paas.publisher.kind':'application','paas.publisher.migration':'',
                  "traefik.enable": "true", "traefik.docker.network": "dokploy-network",
                  f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
                  f"traefik.http.routers.{router}.entrypoints": "web",
                  f"traefik.http.routers.{router}.middlewares": router,
                  f"traefik.http.middlewares.{router}.headers.customresponseheaders.X-Paas-Deployment": release_id,
                  f"traefik.http.services.{router}.loadbalancer.server.port": str(port)}
        if self.target(release_id):
            labels |= {'traefik.enable': 'false', 'paas.publisher.host': host, 'paas.publisher.port': str(port),
                       'sablier.enable': str(config['sleep']).lower(), 'sablier.group': router}
        if config["sleep"]:
            labels |= {"sablier.enable": "true", "sablier.group": router, "traefik.docker.allownonrunning": "true"}
        service |= {"restart": "unless-stopped", "mem_limit": f"{config['memory_mb']}m", "cpus": config["cpus"],
                    "pids_limit": 128, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                    "tmpfs": ["/tmp:rw,noexec,nosuid,size=67108864"], "networks": ["edge"], "labels": labels,
                    "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}}
        return {"services": {"app": service}, "networks": {"edge": {"external": True, "name": "dokploy-network"}}}

    def create_release(self, deployment, definition):
        target = self.target(deployment['id'])
        return self.create_compose('pub-' + deployment['id'], definition, target['server_id'] if target else None)

    def hostname(self, deployment, production=False):
        return self.settings.hostname(deployment['project_id'],None if production else deployment['id'],
                                      domain=deployment['app_domain'])

    def create_compose(self, name, definition, server_id=None):
        # Look up the stable name before creation, including recovery after a process crash.
        candidates = self.api("project.one", query={"projectId": self.config["project_id"]})
        composes = []
        for env in candidates.get("environments", []):
            composes.extend(env.get("compose", []))
        existing = next((c for c in composes if c.get("name") == name), None)
        if existing:
            compose_id = existing["composeId"]
        else:
            parameters = {"name": name, "appName": name, "environmentId": self.config["environment_id"], "composeType": "docker-compose"}
            if server_id:
                parameters['serverId'] = server_id
            created = self.api("compose.create", parameters)
            compose_id = created["composeId"]
        parameters = {"composeId": compose_id, "sourceType": "raw", "composeFile": json.dumps(definition)}
        if server_id:
            parameters['serverId'] = server_id
        self.api("compose.update", parameters)
        if server_id:
            effective = self.api('compose.one', query={'composeId': compose_id})
            if effective.get('serverId') != server_id:
                raise BackendError('Dokploy did not retain the isolated deployment target')
        return compose_id

    def deploy(self, deployment, compose_id):
        if not self.find_container(deployment["id"]):
            self.start_compose(compose_id, 'Publisher release ' + deployment['id'])

    def start_compose(self, compose_id, title):
        operations = self.api('deployment.allByCompose', query={'composeId':compose_id})
        if any(row.get('status') in {'running','queued'} for row in operations):
            return
        if any(row.get('status') not in {'done','error'} for row in operations):
            raise BackendError('Dokploy operation state is unknown; reconcile it before submitting another deployment')
        self.api('compose.deploy', {'composeId':compose_id,'title':title})

    def reconcile_release(self, deployment):
        self.deploy(deployment, deployment['backend_id'])

    def health(self, deployment, timeout=120, check=None):
        deadline = time.monotonic() + timeout
        path = json.loads(deployment["config"])["health_check"]
        host = self.hostname(deployment)
        last_status = None
        while time.monotonic() < deadline:
            if check:
                check()
            container = self.find_container(deployment["id"])
            target = self.target(deployment['id'])
            if container and not container["State"]["Running"]:
                if target:
                    self.execution_client().action(target['account_id'], deployment['id'], 'start')
                else:
                    self.docker("start", container["Id"])
            if container and container["State"]["Running"]:
                if target:
                    self.execution_client().action(target['account_id'], deployment['id'], 'reconcile')
                connection = http.client.HTTPConnection("127.0.0.1", 80, timeout=5)
                try:
                    connection.request("GET", path, headers={"Host": host})
                    response = connection.getresponse()
                    last_status = response.status
                    if 200 <= response.status < 300:
                        return container
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
            time.sleep(2)
        raise BackendError(f"Release failed its HTTP health check (last status: {last_status}); production traffic was not changed")

    def promote(self, deployment):
        if self.target(deployment['id']):
            return self.promote_remote(deployment)
        self.health(deployment)
        project_id = deployment["project_id"]
        # Point production at the immutable preview host. That path retains Sablier's wake handling.
        document = {"http": {"routers": {"publisher-" + project_id: {
            "rule": f"Host(`{self.hostname(deployment,production=True)}`)", "entryPoints": ["web"],
            "service": "publisher-" + project_id}},
            "services": {"publisher-" + project_id: {"loadBalancer": {"passHostHeader": True,
            "servers": [{"url": "http://127.0.0.1:80"}]}}},
            "middlewares": {}}}
        # Override the upstream Host with the preview host, avoiding a recursive production route.
        preview = self.hostname(deployment)
        document["http"]["middlewares"]["publisher-host-" + project_id] = {"headers": {"customRequestHeaders": {"Host": preview}, "customResponseHeaders": {"X-Paas-Deployment": deployment["id"]}}}
        document["http"]["routers"]["publisher-" + project_id]["middlewares"] = ["publisher-host-" + project_id]
        # Traefik runs in Docker: its own port 80 is the internal loopback listener.
        live = Path(self.config.get("routes", "/etc/dokploy/traefik/dynamic")) / ("publisher-" + project_id + ".yml")
        pending = live.with_suffix(".next")
        previous = live.read_bytes() if live.exists() else None
        pending.write_text(json.dumps(document))
        pending.replace(live)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                connection = http.client.HTTPConnection("127.0.0.1", 80, timeout=5)
                try:
                    connection.request("GET", json.loads(deployment["config"])["health_check"], headers={"Host": self.hostname(deployment,production=True)})
                    response = connection.getresponse()
                    if 200 <= response.status < 300 and response.getheader("X-Paas-Deployment") == deployment["id"]:
                        self.public_health(deployment, production=True)
                        return
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
                time.sleep(1)
            raise BackendError("Production route did not become healthy; previous route restored")
        except BaseException:
            if previous is None:
                live.unlink(missing_ok=True)
            else:
                pending.write_bytes(previous)
                pending.replace(live)
            raise

    def public_health(self, deployment, production=False, check=None):
        host = self.hostname(deployment,production)
        # Readiness was checked against the app internally. Prove ingress binding separately:
        # the shared edge handles /healthz itself, so an application health path is not a routing probe.
        path = "/__paas_route_probe__"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if check:
                check()
            connection = http.client.HTTPSConnection(host, timeout=8)
            try:
                connection.request("GET", path, headers={"User-Agent": "OVH-PaaS-Publisher/0.1"})
                response = connection.getresponse()
                if 200 <= response.status < 500 and response.getheader("X-Paas-Deployment") == deployment["id"]:
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(3)
        raise BackendError("Public HTTPS endpoint is not ready; inspect domain and edge connector")

    def refresh_wake(self):
        subprocess.run(["/usr/bin/python3", self.config.get("wake_script", "/opt/paas/scripts/configure-wakeup.py")], check=True, capture_output=True, timeout=30)

    def remove(self, deployment):
        target = self.target(deployment['id'])
        if target:
            self.execution_client().build_operation(target['account_id'], deployment['id'], 'cancel')
            # Preserve receipts for image-declared anonymous volumes before
            # Dokploy removes the container; managed /data is never included.
            self.find_container(deployment['id'])
        # Reconcile by the stable name as a crash may precede saving backend_id.
        project = self.api('project.one', query={'projectId': self.config['project_id']})
        candidates = [c for env in project.get('environments', []) for c in env.get('compose', [])
                      if c.get('name') == 'pub-' + deployment['id']]
        for candidate in candidates:
            self.api('compose.delete', {'composeId': candidate['composeId']})
        deadline = time.monotonic() + 120
        while self.find_container(deployment['id']):
            if time.monotonic() > deadline:
                raise BackendError('Deleted release still owns a runtime container; cleanup will need retrying')
            time.sleep(2)
        target = self.target(deployment['id'])
        if target:
            client = self.execution_client()
            client.action(target['account_id'], deployment['id'], 'cleanup')
            client.build_operation(target['account_id'], deployment['id'], 'delete')
        elif json.loads(deployment['config'])['sleep']:
            self.refresh_wake()
        path = Path(self.config.get('routes', '/etc/dokploy/traefik/dynamic')) / ('publisher-preview-' + deployment['id'] + '.yml')
        path.unlink(missing_ok=True)
        path.with_suffix('.next').unlink(missing_ok=True)
        directory = self.releases / deployment['id']
        if directory.exists():
            shutil.rmtree(directory)

    def cancel(self, deployment):
        self.remove(deployment)

    def remove_project_route(self, project_id):
        path=Path(self.config.get('routes','/etc/dokploy/traefik/dynamic'))/('publisher-'+project_id+'.yml')
        path.unlink(missing_ok=True);path.with_suffix('.next').unlink(missing_ok=True)

    def forward_route(self, deployment, target, production=False):
        project_id, deployment_id = deployment['project_id'], deployment['id']
        host = self.hostname(deployment,production)
        key = 'publisher-' + (project_id if production else 'preview-' + deployment_id)
        document = {'http': {'routers': {key: {'rule': f'Host(`{host}`)', 'entryPoints': ['web'], 'service': key, 'middlewares': [key]}},
                    'services': {key: {'loadBalancer': {'servers': [{'url': f"http://10.201.0.2:{target['http_port']}"}]}}},
                    'middlewares': {key: {'headers': {'customRequestHeaders': {'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': host}}}}}}
        path = Path(self.config.get('routes', '/etc/dokploy/traefik/dynamic')) / (key + '.yml')
        previous = path.read_bytes() if path.exists() else None
        pending = path.with_suffix('.next'); pending.write_text(json.dumps(document)); pending.replace(path)
        return path, previous

    def promote_remote(self, deployment):
        self.health(deployment)
        target = self.target(deployment['id'])
        client = self.execution_client()
        previous = client.action(target['account_id'], deployment['id'], 'promote', project_id=deployment['project_id'])['previous']
        path, old = self.forward_route(deployment,target,production=True)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                connection = http.client.HTTPConnection('127.0.0.1', 80, timeout=5)
                try:
                    connection.request('GET', json.loads(deployment['config'])['health_check'], headers={'Host': self.hostname(deployment,production=True)})
                    response = connection.getresponse()
                    if 200 <= response.status < 300 and response.getheader('X-Paas-Deployment') == deployment['id']:
                        self.public_health(deployment, production=True)
                        return
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
                time.sleep(1)
            raise BackendError('Isolated production route failed readiness validation')
        except BaseException:
            if old is None: path.unlink(missing_ok=True)
            else:
                pending = path.with_suffix('.next'); pending.write_bytes(old); pending.replace(path)
            client.action(target['account_id'], deployment['id'], 'restore_alias', project_id=deployment['project_id'], target_release=previous)
            raise

    def prepare_remote(self, deployment, archive, log, check=None, before_migrate=None, *, imported=None):
        from publisher.executor import BuildOutput
        from publisher.runtime import RuntimeRegistry
        config = json.loads(deployment['config'])
        with self.store.connection() as db:
            account_id = db.execute('SELECT account_id FROM projects WHERE id=?', (deployment['project_id'],)).fetchone()[0]
        record = RuntimeRegistry(self).ensure(account_id)
        with self.store.connection() as db:
            db.execute('INSERT INTO execution_targets VALUES(?,?,?,?,?) ON CONFLICT(deployment_id) DO NOTHING',
                       (deployment['id'], account_id, record['server_id'], record['http_port'], time.time()))
        client = self.execution_client()
        if check:
            check()
        try:
            status = client.build_status(deployment['id'])
        except Exception as error:
            # Only a confirmed absent resource permits submission; observation errors never restart a live build.
            from publisher.executor import ExecutionFailure
            if not isinstance(error, ExecutionFailure) or getattr(error, 'status', None) != 404:
                raise
            build = {k:config[k] for k in ['runtime','build_command','start_command','output_directory','dockerfile'] if k in config}
            if imported and imported['image_path'] is not None:
                build={'runtime':'image-import','image_reference':imported['reference']};archive=imported['image_path']
            status = client.start_build(deployment['id'], build | {'account_id': account_id, 'project_id': deployment['project_id']}, archive)
        driver = 'Native Docker' if status.get('driver') == 'native' else 'Isolated VM'
        log(driver + ' build job: ' + deployment['id'])
        output = BuildOutput(client, deployment['id'], log)
        deadline = time.monotonic() + 1250
        while status['state'] in {'queued', 'running'}:
            if check:
                check()
            if time.monotonic() >= deadline:
                raise BackendError('Build observation deadline exceeded; existing supervised job must be reconciled')
            if status.get('log_chunks') is True:
                output.read()
            time.sleep(2)
            status = client.build_status(deployment['id'])
        if status.get('log_chunks') is True:
            output.read(final=True)
        else:
            for line in client.build_logs(deployment['id']).splitlines():
                log(line)
        if status['state'] != 'succeeded':
            raise BackendError(status.get('error', 'Isolated build failed'))
        if check:
            check()
        client.load_image(account_id, deployment['id'])
        from publisher.data_backend import DataBackend
        data = DataBackend(self)
        if config.get('migration_command') and imported is None:
            if before_migrate:
                before_migrate()
            data.migrate(deployment, account_id, check, log)
        port = 8080 if config['runtime'] == 'static' else config['port']
        environment = self.store.unseal(deployment['environment']) | {'PORT': str(port),
            'PAAS_DEPLOYMENT_URL': 'https://' + self.hostname(deployment),
            'PAAS_PROJECT_URL': 'https://' + self.hostname(deployment,production=True)}
        service = {'image': status['image'], 'pull_policy': 'never', 'user': '101:101' if config['runtime'] == 'static' else '10001:10001',
                   'read_only': config['runtime'] == 'static', 'init': True,
                   'environment': {k: v.replace('$', '$$') for k, v in environment.items()},
                   'volumes': ['/opt/paas-runtime/healthcheck:/paas-healthcheck:ro'],
                   'healthcheck': {'test': ['CMD', '/paas-healthcheck', f"http://127.0.0.1:{port}" + config['health_check']],
                                   'interval': '2s', 'timeout': '3s', 'start_period': '2s', 'retries': 30}}
        if config['runtime'] == 'static':
            service['volumes'].append('/opt/paas-runtime/nginx.conf:/etc/nginx/nginx.conf:ro')
        self.forward_route(deployment,record)
        definition = self.render(deployment, service, port)
        data.attach(deployment, definition)
        return definition
