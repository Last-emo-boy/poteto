#!/usr/bin/env bash
set -euo pipefail
root=$(mktemp -d)
out=${1:?Output directory is required}
mkdir -p "$out"
out=$(cd "$out" && pwd)
nonce=$(python -c 'import secrets; print(secrets.token_hex(12))')
source="poteto-next-postgres:$nonce"
canonical="paas-release:$nonce"
network="poteto-next-$nonce"
database="poteto-db-$nonce"
app=
cleanup() {
    result=$?
    if [[ "$result" != 0 && -n "$app" ]]; then docker logs --tail 30 "$app"; fi
    [[ -z "$app" ]] || docker rm -f "$app" >/dev/null
    docker rm -f "$database" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    docker image rm "$source" "$canonical" >/dev/null 2>&1 || true
    rm -r "$root"
}
trap cleanup EXIT

docker buildx build --platform linux/amd64 --load --tag "$source" fixtures/next-postgres
docker image inspect "$source" --format '{{.Size}}' > "$out/uncompressed-image-bytes"
docker image save --platform linux/amd64 "$source" | gzip -3 > "$out/image.tar.gz"
wc -c < "$out/image.tar.gz" > "$out/compressed-image-bytes"
echo "Image size: $(cat "$out/compressed-image-bytes") bytes compressed, $(cat "$out/uncompressed-image-bytes") bytes in Docker"
# This must satisfy the real CLI contract before any runtime work starts.
poteto plan fixtures/next-postgres --prebuilt-image "$out/image.tar.gz" --json > "$out/plan.json"
python - "$out" "$nonce" <<'PY'
import json,sys
from pathlib import Path
from execution.prebuilt_image import normalize
root = Path(sys.argv[1])
receipt = normalize(root / "image.tar.gz", root / "normalized.tar", sys.argv[2])
print(json.dumps(receipt))
(root / 'image-receipt.json').write_text(json.dumps(receipt))
PY
docker image load --input "$out/normalized.tar"
rm "$out/normalized.tar"
docker network create "$network" >/dev/null
password=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')
write_key=$(python -c 'import secrets; print(secrets.token_hex(24))')
printf 'POSTGRES_PASSWORD=%s\nPOSTGRES_USER=app\nPOSTGRES_DB=app\n' "$password" > "$root/db.env"
docker run -d --name "$database" --network "$network" --network-alias db --env-file "$root/db.env" postgres:16-alpine >/dev/null
for attempt in {1..60}; do
    if docker exec "$database" pg_isready -h 127.0.0.1 -U app -d app >/dev/null 2>&1; then break; fi
    sleep 1
done
docker exec "$database" pg_isready -h 127.0.0.1 -U app -d app
printf 'DATABASE_URL=postgresql://app:%s@db:5432/app\nTEST_WRITE_KEY=%s\n' "$password" "$write_key" > "$root/app.env"
docker run --rm --network "$network" --env-file "$root/app.env" "$canonical" node migrate.mjs
app=$(docker run -d --network "$network" --env-file "$root/app.env" -p 127.0.0.1::3000 "$canonical")
endpoint=$(docker port "$app" 3000/tcp)
TEST_WRITE_KEY="$write_key" python - "$endpoint" <<'PY'
import json,os,sys,time
from urllib.request import Request,urlopen
from urllib.error import URLError,HTTPError
base='http://'+sys.argv[1]
for attempt in range(60):
    try:
        with urlopen(base+'/api/healthz',timeout=2) as response:
            state=json.load(response)
        if state=={'status':'ok','version':'v1','count':0}:break
    except HTTPError:
        raise
    except (URLError, ConnectionError, TimeoutError):
        pass  # Docker can accept/reset the port before the Node server has bound it.
    time.sleep(1)
else:raise RuntimeError('Next.js did not become ready')
request=Request(base+'/api/notes',b'{"text":"persistent fixture"}',
                {'Content-Type':'application/json','X-Test-Key':os.environ['TEST_WRITE_KEY']})
with urlopen(request,timeout=5) as response:assert response.status==201
with urlopen(base+'/api/healthz',timeout=5) as response:assert json.load(response)['count']==1
with urlopen(base,timeout=5) as response:
    page=response.read()
    assert b'SSR + PostgreSQL' in page and b'Saved notes:' in page
print('PASS: standalone Next.js SSR, migration, PostgreSQL write/read')
PY
