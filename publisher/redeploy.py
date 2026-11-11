"""Queue a new deployment from immutable uploaded source, with current runtime variables."""
import json
import re
import shutil
from fastapi import Depends, Request
from publisher.models import RedeployInput
from publisher.store import Problem, ident


def register(app, store, actor, release_view):
    @app.post("/v1/deployments/{deployment_id}/redeploy", status_code=202, tags=["Deployments"])
    def redeploy(deployment_id: str, body: RedeployInput, request: Request, user=Depends(actor)):
        key = request.headers.get("Idempotency-Key")
        if not key or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", key):
            raise Problem(422, "Provide an Idempotency-Key of 1 to 64 letters, digits, dots, underscores or hyphens")
        with store.connection() as db:
            source = store.deployment(db, user, deployment_id, "deployments:write", allow_preview=True)
        if source["state"] not in {"ready", "failed", "cancelled"}:
            raise Problem(409, "Wait for the original deployment to finish before redeploying")
        path = store.settings.root / "artifacts" / (source["id"] + ".tar.gz")
        try:
            original = path.open("rb")
        except OSError:
            raise Problem(409, "The original source archive is unavailable") from None
        with original:
            config = json.loads(source["config"]) | {"target": body.target}
            release = store.create_deployment(user, source["project_id"], config,
                                             "redeploy:" + source["id"] + ":" + key)
            if release["state"] != "uploading":
                return release_view(release)
            lease = ident()
            store.begin_upload(user, release["id"], lease)
            temporary = store.settings.root / "uploads" / (ident() + ".part")
            try:
                with temporary.open("xb") as output:
                    shutil.copyfileobj(original, output, length=1024 * 1024)
                if temporary.stat().st_size != source["artifact_bytes"]:
                    raise Problem(409, "The original source archive is incomplete")
                store.enqueue_upload(user, release["id"], temporary, source["artifact_bytes"], source["unpacked_bytes"])
            except OSError:
                raise Problem(503, "Source copy was interrupted; repeat with the same Idempotency-Key") from None
            finally:
                temporary.unlink(missing_ok=True)
                with store.connection() as db:
                    db.execute("DELETE FROM uploads WHERE deployment_id=? AND upload_id=?", (release["id"], lease))
            with store.connection() as db:
                return release_view(store.deployment(db, user, release["id"], allow_preview=True))
