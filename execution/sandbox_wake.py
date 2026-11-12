"""Sablier/Caddy routes for owned, sleeping gVisor previews."""
import fcntl
import json
from pathlib import Path
import re
import sys
import time

APP_PORTS = range(23000, 23064)
PROXY_PORT = 23100
PROXY = "poteto-sandbox-wake-proxy"
HEALTHCHECK = Path("/opt/paas-execution/healthcheck")


def options(release, body):
    if not body.get("sleep", False):
        return None
    host, health = body.get("hostname"), body.get("health_check")
    if (body.get("sleep") is not True or not isinstance(host, str) or len(host) > 253
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]+[a-z0-9]", host)
            or not host.startswith("preview-" + release + ".")
            or not isinstance(health, str) or not health.startswith("/") or len(health) > 2048
            or any(ord(c) < 33 or ord(c) == 127 for c in health)
            or type(body.get("port")) is not int or not 1024 <= body["port"] <= 65535):
        raise ValueError("Invalid sandbox wake host or health check")
    return {"hostname": host, "health_check": health, "port": body["port"]}


def configuration(records, idle_seconds=120):
    if type(idle_seconds) is not int or not 10 <= idle_seconds <= 3600:
        raise ValueError("sandbox_idle_seconds must be between 10 and 3600")
    lines = ["{", " auto_https off", " admin 127.0.0.1:23102", "}",
             f":{PROXY_PORT} {{", " bind 127.0.0.1"]
    hosts = set()
    for release, record in sorted(records):
        if not record.get("wake_ready"):
            continue
        if not re.fullmatch(r"[a-f0-9]{24}", release) or record["host_port"] not in APP_PORTS:
            raise ValueError("Invalid sandbox wake receipt")
        spec = options(release, record["wake"] | {"sleep": True})
        host = spec["hostname"]
        if host in hosts:
            raise ValueError("Duplicate sandbox wake host")
        hosts.add(host)
        production = None
        if record.get('project_id'):
            if not re.fullmatch(r'[a-f0-9]{24}', record['project_id']):
                raise ValueError('Invalid sandbox project identity')
            production = 'app-' + record['project_id'] + '.' + host.split('.', 1)[1]
            lines += [f' @p{release} header X-Forwarded-Host {production}']
        lines += [f" @r{release} host {host}", f" handle @r{release} {{", "  route {"]
        if production:
            # The controller fixes this header to its matched public route.
            # Rewrite only after selecting the immutable release's wake group.
            lines += [f'   request_header @p{release} Host {production}']
        lines += [
                  "   sablier http://127.0.0.1:23101 {", f"    group poteto-sbx-{release}",
                  f"    session_duration {idle_seconds}s", "    blocking {", "     timeout 60s", "    }", "   }",
                  f"   reverse_proxy 127.0.0.1:{record['host_port']} {{",
                  "    header_up X-Forwarded-Proto https", "    header_up X-Forwarded-Host {http.request.host}",
                  "   }", "  }", " }"]
    lines += [" handle {", "  respond 404", " }", "}"]
    return "\n".join(lines) + "\n"


def render(root, config):
    return configuration([(p.stem, json.loads(p.read_text())) for p in (root / "sandboxes").glob("*.json")],
                         config.get("sandbox_idle_seconds", 120))


def configure(root, config, docker):
    directory = root / "sandbox-wake"
    directory.mkdir(mode=0o755, exist_ok=True)
    live = directory / "Caddyfile"
    value = render(root, config)
    if live.exists() and live.read_text() == value:
        return
    pending = directory / "Caddyfile.next"
    pending.write_text(value)
    docker("exec", PROXY, "caddy", "validate", "--config", "/etc/caddy/Caddyfile.next", "--adapter", "caddyfile")
    docker("exec", PROXY, "caddy", "reload", "--address", "127.0.0.1:23102",
           "--config", "/etc/caddy/Caddyfile.next", "--adapter", "caddyfile")
    pending.replace(live)


def quarantine(root, port):
    directory = root / "sandbox-retired-ports"
    directory.mkdir(mode=0o700, exist_ok=True)
    # A removed route can still have a request waiting for Sablier's 60s deadline.
    path = directory / str(port)
    pending = path.with_suffix(".next")
    pending.write_text(json.dumps({"boot": boot_id(),
                                   "until": time.monotonic() + 70}))
    pending.replace(path)


def unavailable(root):
    paths = list((root / "sandbox-retired-ports").glob("[0-9]*"))
    if not paths:
        return set()
    boot = boot_id()
    result = set()
    for path in paths:
        if path.suffix == ".next":
            continue
        data = json.loads(path.read_text())
        if data["boot"] == boot and data["until"] > time.monotonic():
            result.add(int(path.name))
        else:
            path.unlink()
    return result


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text()


def main():
    root = Path("/srv/ovh-paas-execution")
    with (root / "sandbox-placements.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads((root / "config.json").read_text())
        if sys.argv[1:] == ["prepare"]:
            directory = root / "sandbox-wake"
            directory.mkdir(mode=0o755, exist_ok=True)
            (directory / "Caddyfile").write_text(render(root, config))
        elif sys.argv[1:] == ["reload"]:
            from execution.sandbox import docker
            configure(root, config, docker)
        else:
            raise SystemExit("Use prepare or reload")


if __name__ == "__main__":
    main()
