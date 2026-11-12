"""Portable resumable artifact transfer; no credentials in local checkpoints."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.error

if __package__:
    from .cli_project import local_directory
    from .image_contract import UPLOAD_LIMIT
else:
    from cli_project import local_directory
    from image_contract import UPLOAD_LIMIT


class RequestError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def files(directory, release):
    if not re.fullmatch(r"[a-f0-9]{24}", release):
        raise ValueError("Use a 24-character deployment ID for --resume")
    folder = local_directory(directory.resolve(), create=True)
    paths = [folder / ("upload-" + release + suffix) for suffix in (".json", ".tar.gz")]
    if any(p.is_symlink() or p.resolve() != p for p in paths):
        raise ValueError("Upload checkpoints must not be links")
    return paths


def checkpoint(directory, client, release, source):
    metadata, artifact = files(directory, release["id"])
    created = metadata_created = False
    try:
        with open(artifact, "xb", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            created = True
            output.write(source)
        with open(metadata, "x", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            metadata_created = True
            json.dump({"version": 1, "url": client.url, "project_id": release["project_id"],
                       "config": release["config"]}, output)
    except BaseException:
        if created:
            artifact.unlink(missing_ok=True)
        if metadata_created:
            metadata.unlink(missing_ok=True)
        raise


def resume(directory, client, release, project):
    metadata, artifact = files(directory, release)
    try:
        saved = json.loads(metadata.read_text(encoding="utf-8"))
        with artifact.open("rb") as stream:
            source = stream.read(UPLOAD_LIMIT + 1)
    except FileNotFoundError:
        raise RuntimeError("No local checkpoint for this deployment; resume from the original deployment directory") from None
    if not isinstance(saved, dict) or not isinstance(saved.get("config"), dict):
        raise RuntimeError("Invalid local upload checkpoint")
    if saved.get("version") != 1 or saved.get("url") != client.url or saved.get("project_id") != project:
        raise RuntimeError("Checkpoint belongs to a different API or project")
    if not 0 < len(source) <= UPLOAD_LIMIT:
        raise RuntimeError("Invalid checkpoint artifact size")
    current = client.request("GET", "/v1/deployments/" + release)
    if current["project_id"] != project or current["config"] != saved["config"]:
        raise RuntimeError("Checkpoint does not match this deployment")
    return current, source


def discard(directory, release):
    for path in files(directory, release):
        path.unlink(missing_ok=True)


def retryable(error):
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, RequestError):
        return error.status in {408, 409, 429, 499, 500, 502, 503, 504}
    return isinstance(error, (OSError, http.client.HTTPException))


def transfer(client, release, source):
    base = "/v1/deployments/" + release + "/source-upload"
    identity = {"size": len(source), "sha256": hashlib.sha256(source).hexdigest()}

    def checked(status):
        if any(status.get(k) != v for k, v in identity.items()):
            raise RuntimeError("Upload status belongs to a different artifact")
        if type(status.get("received")) is not int or not 0 <= status["received"] <= len(source):
            raise RuntimeError("Invalid upload offset returned by server")
        return status

    status = checked(client.request("POST", base, identity))
    if status["state"] != "uploading":
        return
    limit = status.get("chunk_limit")
    if type(limit) is not int or not 1 <= limit <= 8 * 1024**2:
        raise RuntimeError("Invalid upload part limit returned by server")
    offset = status["received"]
    total = len(source) / 1024**2
    print(f"Uploading {total:.1f} MiB; resuming at {offset / 1024**2:.1f} MiB", file=sys.stderr, flush=True)
    progress = offset * 10 // len(source)
    while offset < len(source):
        end = min(offset + limit, len(source))
        for attempt in range(3):
            try:
                status = checked(client.request("PUT", base + "/part?offset=" + str(offset),
                                                source[offset:end], raw="application/octet-stream", timeout=55))
                if status["received"] != end:
                    raise RuntimeError("Server did not commit the expected upload offset")
                break
            except (RuntimeError, OSError, http.client.HTTPException) as error:
                if not retryable(error) or attempt == 2:
                    raise
                time.sleep(1)
                status = checked(client.request("GET", base))
                if status["received"] == end:
                    break  # The server committed the part but its reply was lost.
                if status["received"] != offset:
                    raise RuntimeError("Upload offset changed unexpectedly") from None
        offset = end
        current = offset * 10 // len(source)
        if current != progress:
            print(f"Uploaded {offset / 1024**2:.1f} / {total:.1f} MiB", file=sys.stderr, flush=True)
            progress = current
    # A lost completion reply is safe to replay on an explicit --resume.
    print("Verifying uploaded artifact on the server", file=sys.stderr, flush=True)
    checked(client.request("POST", base + "/complete", timeout=180))
