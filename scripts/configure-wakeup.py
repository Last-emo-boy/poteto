#!/usr/bin/env python3
"""Run inside the guest after deploying labelled Compose services."""
import json
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys

def main():
    path = Path('/etc/dokploy/paas/wake')
    path.mkdir(parents=True,exist_ok=True)
    with (path/'configure.lock').open('a') as config_lock:
        fcntl.flock(config_lock,fcntl.LOCK_EX)
        configure(path)


def configure(path):
    ids = subprocess.check_output(["docker", "ps", "-aq", "--filter", "label=sablier.enable=true"], text=True).split()
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True)) if ids else []
    alias_path = os.environ.get("PAAS_PUBLISHER_ALIASES")
    aliases = json.loads(Path(alias_path).read_text()) if alias_path and Path(alias_path).exists() else {}
    routers = {}
    headers = {}
    blocks = ["{\n auto_https off\n}\n:8080 {"]
    for c in sorted(containers, key=lambda item: item["Name"]):
        labels = c["Config"]["Labels"]
        name = c["Name"].lstrip("/")
        group = labels["sablier.group"]
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", group):
            raise ValueError("Invalid container or group name")
        rules = [v for k, v in labels.items() if k.startswith("traefik.http.routers.") and k.endswith(".rule")]
        if labels.get("paas.publisher.host"):
            rules = [f"Host(`{labels['paas.publisher.host']}`)"]
        if len(rules) != 1 or not re.fullmatch(r"Host\(`[a-zA-Z0-9.-]+`\)", rules[0]):
            raise ValueError("An on-demand service must declare exactly one explicit Host rule")
        host = rules[0][6:-2]
        ports = [v for k, v in labels.items() if k.endswith(".loadbalancer.server.port")]
        if labels.get("paas.publisher.port"):
            ports = [labels["paas.publisher.port"]]
        if len(ports) != 1 or not ports[0].isdigit():
            raise ValueError("An on-demand service must declare one port")
        hostnames = [host]
        release = labels.get("paas.publisher.release")
        project = labels.get("paas.publisher.project")
        if release and project and aliases.get(project) == release:
            if not re.fullmatch(r"[a-f0-9]{24}", project):
                raise ValueError("Invalid project alias")
            hostnames.append("app-" + project + "." + host.split(".", 1)[1])
        rule = " || ".join(f"Host(`{h}`)" for h in hostnames)
        router = "wake-" + group
        routers[router] = {"rule": rule, "entryPoints": ["web"], "priority": 10000, "service": "paas-wake-proxy"}
        release = labels.get("paas.publisher.release")
        if release:
            if not re.fullmatch(r"[a-f0-9]{24}", release):
                raise ValueError("Invalid publisher release label")
            middleware = "wake-release-" + group
            headers[middleware] = {"headers": {"customResponseHeaders": {"X-Paas-Deployment": release}}}
            routers[router]["middlewares"] = [middleware]
        proxy = [f"   reverse_proxy {name}:{ports[0]}"]
        if alias_path:
            proxy = [f"   reverse_proxy {name}:{ports[0]} {{", "    header_up X-Forwarded-Proto https", "    header_up X-Forwarded-Host {http.request.host}", "   }"]
        blocks.extend([f" @h{len(routers)} host {' '.join(hostnames)}", f" handle @h{len(routers)} {{", "  route {", "   sablier http://paas-sablier:10000 {", f"    group {group}", "    session_duration 2m", "    blocking {", "     timeout 60s", "    }", "   }", *proxy, "  }", " }"])
    blocks.extend([" handle {", "  respond 404", " }", "}"])
    caddy = "\n".join(blocks) + "\n"
    dynamic = {"services": {"paas-wake-proxy": {"loadBalancer": {"servers": [{"url": "http://paas-wake-proxy:8080"}]}}}}
    if routers:
        dynamic["routers"] = routers
    if headers:
        dynamic["middlewares"] = headers
    config = {"http": dynamic}
    traefik = json.dumps(config, indent=2) + "\n"
    live = path / "Caddyfile"
    routes = Path("/etc/dokploy/traefik/dynamic/paas-wake.yml")
    if "--prepare" in sys.argv:
        prepared=path/'Caddyfile.prepare'
        prepared.write_text(caddy)
        prepared.replace(live)
        raise SystemExit(0)
    if live.exists() and routes.exists() and live.read_text() == caddy and routes.read_text() == traefik:
        raise SystemExit(0)
    candidate = path / "Caddyfile.next"
    candidate.write_text(caddy)
    subprocess.run(["docker", "exec", "paas-wake-proxy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile.next", "--adapter", "caddyfile"], check=True, capture_output=True)
    subprocess.run(["docker", "exec", "paas-wake-proxy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile.next", "--adapter", "caddyfile"], check=True, capture_output=True)
    candidate.replace(live)
    pending_routes = routes.with_suffix(".next")
    pending_routes.write_text(traefik)
    pending_routes.replace(routes)
    print("Configured on-demand routes:", len(routers))


if __name__ == '__main__':
    main()
