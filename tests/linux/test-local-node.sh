#!/bin/sh
# Build the standard Node starter entirely on the client and run its normalized image.
set -eu
root=$(mktemp -d)
release=$(python -c 'import secrets; print(secrets.token_hex(12))')
canonical="paas-release:$release"
container=
trap 'if [ -n "$container" ]; then docker rm -f "$container" >/dev/null; fi; docker image rm "$canonical" >/dev/null 2>&1 || :; rm -r "$root"' EXIT
poteto init "$root/app" --runtime node
poteto build "$root/app" > "$root/build.json"
python - "$root" "$release" <<'PY'
import json, sys
from pathlib import Path
from execution.prebuilt_image import normalize
root = Path(sys.argv[1])
report = json.loads((root / 'build.json').read_text())
assert report['runtime'] == 'image' and report['target'] == 'preview'
assert report['configuration']['health_check'] == '/healthz'
print(normalize(root / 'app/.poteto/image.tar.gz', root / 'normalized.tar', sys.argv[2]))
PY
docker image load --input "$root/normalized.tar"
container=$(docker run -d --pull never --user 10001:10001 -e PORT=3000 -p 127.0.0.1::3000 "$canonical")
endpoint=$(docker port "$container" 3000/tcp)
body=$(curl --fail --silent --show-error --retry 8 --retry-connrefused --max-time 3 "http://$endpoint/healthz")
test "$body" = '{"status":"ok"}'
echo 'PASS: local Node build, normalized image and runtime health'
