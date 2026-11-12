#!/bin/sh
# Build the standard Node starter entirely on the client and run its normalized image.
set -eu
root=$(mktemp -d)
release=$(python -c 'import secrets; print(secrets.token_hex(12))')
canonical="paas-release:$release"
container=
trap 'result=$?; if [ -n "$container" ]; then if [ "$result" -ne 0 ]; then docker logs --tail 30 "$container"; fi; docker rm -f "$container" >/dev/null; fi; docker image rm "$canonical" >/dev/null 2>&1 || :; rm -r "$root"' EXIT
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
python - "$endpoint" "$container" <<'PY'
import json, subprocess, sys, time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
deadline = time.monotonic() + 30
while True:
    try:
        with urlopen('http://' + sys.argv[1] + '/healthz', timeout=2) as response:
            assert json.load(response) == {'status': 'ok'}
        break
    except HTTPError:
        raise
    except (URLError, ConnectionError, TimeoutError):
        state = subprocess.check_output(['docker', 'inspect', '--format', '{{.State.Status}}', sys.argv[2]], text=True).strip()
        if state != 'running' or time.monotonic() >= deadline:
            raise
        time.sleep(0.2)  # The forwarded port may reset until Node binds its socket.
PY
echo 'PASS: local Node build, normalized image and runtime health'
