"""Bounded, offset-checked uploads into the existing deployment queue."""
import asyncio
import hashlib
import json
import os
import time

from fastapi import Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from publisher.archive import ArchiveError, unpack
from publisher.models import Input
from publisher.store import Problem, ident

CHUNK = 2 * 1024**2


class UploadInput(Input):
    size: int = Field(gt=0, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def path(store, release):
    return store.settings.root / "uploads" / (release + ".resumable")


def lookup(db, store, actor, release):
    deployment = store.deployment(db, actor, release, "deployments:write", allow_preview=True)
    row = db.execute("SELECT * FROM source_uploads WHERE deployment_id=?", (release,)).fetchone()
    if row is None:
        raise Problem(404, "Resumable upload not found")
    return deployment, dict(row)


def view(deployment, row):
    return {"id": deployment["id"], "state": deployment["state"], "chunk_limit": CHUNK,
            **{key: row[key] for key in ("size", "sha256", "received")}}


def start(store, actor, release, body):
    with store.connection() as db:
        deployment = store.deployment(db, actor, release, "deployments:write", allow_preview=True)
        row = db.execute("SELECT * FROM source_uploads WHERE deployment_id=?", (release,)).fetchone()
        if row:
            if (row["size"], row["sha256"]) != (body.size, body.sha256):
                raise Problem(409, "This upload belongs to a different artifact; keep the original or create a new deployment")
            return view(deployment, row)
        if deployment["state"] != "uploading" or deployment["created"] + 86400 <= time.time():
            raise Problem(409, "Deployment no longer accepts an upload")
        if db.execute("SELECT 1 FROM uploads WHERE deployment_id=?", (release,)).fetchone():
            raise Problem(409, "An upload is already in progress")
        runtime = json.loads(deployment["config"])["runtime"]
        if body.size > store.settings.upload_limits(runtime)[0]:
            raise Problem(413, "Artifact exceeds this runtime's upload limit")
        store.check_upload_capacity(db, runtime)
        account = db.execute("SELECT max_storage FROM accounts WHERE id=?", (actor["account_id"],)).fetchone()
        if store.storage_used(db, actor["account_id"]) + body.size > account["max_storage"]:
            raise Problem(429, "Artifact storage quota reached")
        db.execute("INSERT INTO source_uploads(deployment_id,size,sha256) VALUES(?,?,?)",
                   (release, body.size, body.sha256))
        return view(*lookup(db, store, actor, release))


def lease(store, actor, release):
    upload_id = ident()
    store.begin_upload(actor, release, upload_id, resumable=True)
    return upload_id


def release_lease(store, release, upload_id):
    with store.connection() as db:
        db.execute("DELETE FROM uploads WHERE deployment_id=? AND upload_id=?", (release, upload_id))


def checked(db, store, actor, release, upload_id):
    deployment, row = lookup(db, store, actor, release)
    active = db.execute("SELECT 1 FROM uploads WHERE deployment_id=? AND upload_id=? AND expires>?",
                        (release, upload_id, time.time())).fetchone()
    if deployment["state"] != "uploading" or not active:
        raise Problem(409, "Upload no longer owns this deployment")
    return deployment, row


def next_part(store, actor, release, upload_id, offset, length):
    with store.connection() as db:
        _, row = checked(db, store, actor, release, upload_id)
        if offset != row["received"]:
            raise Problem(409, "Resume at the received offset returned by upload status")
        if length <= 0 or length != min(CHUNK, row["size"] - offset):
            raise Problem(400, "Content-Length does not match the next upload part")


def commit_part(store, actor, release, upload_id, offset, data):
    with store.connection() as db:
        deployment, row = checked(db, store, actor, release, upload_id)
        if row["received"] != offset or len(data) != min(CHUNK, row["size"] - offset):
            raise Problem(409, "Upload offset changed; read upload status before resuming")
        target = path(store, release)
        # A crash before the SQL commit can leave an uncommitted tail. Rewrite it.
        mode = "r+b" if target.exists() else "xb"
        with open(target, mode, opener=lambda p, f: os.open(p, f, 0o600)) as output:
            if output.seek(0, 2) < offset:
                raise Problem(409, "Upload data is missing; create a new deployment")
            output.truncate(offset)
            output.seek(offset)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        row["received"] = offset + len(data)
        db.execute("UPDATE source_uploads SET received=? WHERE deployment_id=?", (row["received"], release))
        return view(deployment, row)


def complete(store, actor, release, upload_id):
    with store.connection() as db:
        deployment, row = checked(db, store, actor, release, upload_id)
        if row["received"] != row["size"]:
            raise Problem(409, "Upload is incomplete")
    source = path(store, release)
    if not source.exists():
        # Recover a crash between the atomic archive rename and SQL commit.
        source = store.settings.root / "artifacts" / (release + ".tar.gz")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as stream:
            size = 0
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > row["size"]:
                    break
                digest.update(chunk)
    except FileNotFoundError:
        raise Problem(409, "Upload data is missing; create a new deployment") from None
    if size != row["size"] or digest.hexdigest() != row["sha256"]:
        raise Problem(422, "Artifact integrity check failed; create a new deployment")
    runtime = json.loads(deployment["config"])["runtime"]
    _, expanded, files = store.settings.upload_limits(runtime)
    try:
        stats = unpack(source, None, max_bytes=expanded, max_files=files,
                       required_file="manifest.json" if runtime == "image" else None)
    except ArchiveError as error:
        raise Problem(422, str(error)) from None
    store.enqueue_upload(actor, release, source, size, stats["unpacked_bytes"], upload_id=upload_id)
    return {"id": release, "state": "queued", "size": size, "received": size, "sha256": row["sha256"]}


def expire(store, db):
    for target in (store.settings.root / "uploads").glob("*.resumable"):
        row = db.execute("SELECT state FROM deployments WHERE id=?", (target.stem,)).fetchone()
        active = db.execute("SELECT 1 FROM uploads WHERE deployment_id=?", (target.stem,)).fetchone()
        if not active and (not row or row["state"] != "uploading"):
            target.unlink(missing_ok=True)


def register(app, store, actor):
    base = "/v1/deployments/{deployment_id}/source-upload"

    @app.post(base, tags=["Deployments"])
    def create(deployment_id: str, body: UploadInput, user=Depends(actor)):
        return start(store, user, deployment_id, body)

    @app.get(base, tags=["Deployments"])
    def status(deployment_id: str, user=Depends(actor)):
        with store.connection() as db:
            return view(*lookup(db, store, user, deployment_id))

    @app.put(base + "/part", tags=["Deployments"])
    async def upload(deployment_id: str, offset: int, request: Request, user=Depends(actor)):
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise Problem(415, "Send a binary upload part")
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise Problem(400, "Invalid Content-Length") from None
        upload_id = await run_in_threadpool(lease, store, user, deployment_id)
        try:
            await run_in_threadpool(next_part, store, user, deployment_id, upload_id, offset, length)
            data = bytearray()
            async with asyncio.timeout(45):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > length:
                        raise Problem(413, "Upload part exceeds its declared length")
                    data.extend(chunk)
            if len(data) != length:
                raise Problem(400, "Upload part ended early")
            return await run_in_threadpool(commit_part, store, user, deployment_id, upload_id, offset, data)
        except (TimeoutError, ClientDisconnect):
            raise Problem(408, "Upload part interrupted; resume at the last committed offset") from None
        finally:
            await run_in_threadpool(release_lease, store, deployment_id, upload_id)

    @app.post(base + "/complete", tags=["Deployments"])
    def submit(deployment_id: str, user=Depends(actor)):
        with store.connection() as db:
            deployment, row = lookup(db, store, user, deployment_id)
            if deployment["state"] != "uploading":
                return view(deployment, row)
        upload_id = lease(store, user, deployment_id)
        try:
            return complete(store, user, deployment_id, upload_id)
        finally:
            release_lease(store, deployment_id, upload_id)
