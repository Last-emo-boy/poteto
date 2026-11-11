#!/bin/bash
set -euo pipefail
ROOT=/opt/paas-runtime
install -d -m 0755 /etc/dokploy/paas/wake /etc/dokploy/traefik/dynamic
PAAS_PUBLISHER_ALIASES=/etc/dokploy/paas/production.json python3 "$ROOT/configure-wakeup.py" --prepare
cat > /etc/dokploy/traefik/traefik.yml <<'JSON'
{"global":{"sendAnonymousUsage":false},"providers":{"file":{"directory":"/etc/dokploy/traefik/dynamic","watch":true}},"entryPoints":{"web":{"address":":80","forwardedHeaders":{"trustedIPs":["10.201.0.2/32"]}}}}
JSON
# The remote server is dedicated to the publisher, and bootstrap runs before its first user deployment.
if [ ! -f "$ROOT/platform.version" ]; then
 docker rm -f dokploy-traefik
fi
docker compose -p paas-runtime -f "$ROOT/platform.json" up -d --wait --wait-timeout 45
docker exec paas-wake-proxy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
install -m 0644 "$ROOT/reconcile.service" /etc/systemd/system/paas-runtime-reconcile.service
install -m 0644 "$ROOT/reconcile.timer" /etc/systemd/system/paas-runtime-reconcile.timer
systemctl daemon-reload
systemctl enable --now paas-runtime-reconcile.timer
python3 "$ROOT/reconcile.py"
