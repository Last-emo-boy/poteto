#!/bin/sh
# Exercise real Docker save and load, not an image hand-written by a fixture.
set -eu
root=$(mktemp -d)
release=$(python -c 'import secrets; print(secrets.token_hex(12))')
source="poteto-prebuilt-ci:$(basename "$root" | tr '[:upper:]' '[:lower:]')"
canonical="paas-release:$release"
container=
trap 'if [ -n "$container" ]; then docker rm -f "$container" >/dev/null; fi; docker image rm "$source" "$canonical" >/dev/null 2>&1 || :; rm -r "$root"' EXIT

cat >"$root/server.go" <<'GO'
package main

import (
    "fmt"
    "net/http"
    "os"
)

func main() {
    http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
        fmt.Fprint(w, "poteto-prebuilt-runtime")
    })
    if err := http.ListenAndServe(":"+os.Getenv("PORT"), nil); err != nil {
        panic(err)
    }
}
GO
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o "$root/server" "$root/server.go"
cat >"$root/Dockerfile" <<'DOCKERFILE'
FROM scratch
COPY server /server
USER 10001:10001
ENTRYPOINT ["/server"]
DOCKERFILE
poteto build "$root"
source=$(python - "$root/.poteto/image.tar.gz" <<'PY'
import json,sys,tarfile
with tarfile.open(sys.argv[1]) as image:
    print(json.load(image.extractfile('manifest.json'))[0]['RepoTags'][0])
PY
)
if docker image inspect "$source" >/dev/null 2>&1; then
    echo 'Temporary local build tag was not removed' >&2
    exit 1
fi
poteto plan "$root" --prebuilt-image "$root/.poteto/image.tar.gz" --json
python - "$root" "$release" <<'PY'
from pathlib import Path
import sys
from execution.prebuilt_image import normalize

root = Path(sys.argv[1])
print(normalize(root / ".poteto/image.tar.gz", root / "normalized.tar", sys.argv[2]))
PY
docker image load --input "$root/normalized.tar"
if docker image inspect "$source" >/dev/null 2>&1; then
    echo 'Original tag leaked from the uploaded image archive' >&2
    exit 1
fi
container=$(docker run -d --pull never --user 10001:10001 -e PORT=3000 -p 127.0.0.1::3000 "$canonical")
endpoint=$(docker port "$container" 3000/tcp)
body=$(curl --fail --silent --show-error --retry 8 --retry-connrefused --max-time 3 "http://$endpoint/healthz")
test "$body" = poteto-prebuilt-runtime
