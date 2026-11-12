"""Exchange the native IF.Link OAuth code for a scoped Poteto token."""
import json
import os
from pathlib import Path
import re
import secrets
from threading import BoundedSemaphore
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import Depends
import jwt
from pydantic import Field
from publisher.login_contract import ISSUER, LOGIN_PATH, REDIRECT_URI
from publisher.models import Input
from publisher.store import PREVIEW_SCOPE, Problem

LOGIN_ISSUE = "token.cli-login"


class LoginExchange(Input):
    code: str = Field(min_length=1, max_length=2048)
    verifier: str = Field(pattern=r"^[A-Za-z0-9._~-]{43,128}$")
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


def request_id_token(client_id, request):
    data = urlencode({"grant_type": "authorization_code", "client_id": client_id,
                      "code": request.code, "code_verifier": request.verifier,
                      "redirect_uri": REDIRECT_URI}).encode()
    outbound = Request(ISSUER + "/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(outbound, timeout=10) as response:
            token = json.loads(response.read(65537))["id_token"]
    except HTTPError as error:
        error.close()
        if error.code in {400, 401}:
            raise Problem(401, "IF.Link rejected the login code; run poteto login again") from None
        raise Problem(502, "IF.Link login is temporarily unavailable") from None
    except (URLError, OSError, ValueError, KeyError, TypeError):
        raise Problem(502, "IF.Link login is temporarily unavailable") from None
    if not isinstance(token, str) or not 1 <= len(token) <= 16384:
        raise Problem(502, "IF.Link returned an invalid identity token")
    return token


class CliLogin:
    def __init__(self, client_id, subjects, *, auto_provision=False):
        self.client_id = client_id
        self.subjects = subjects
        self.auto_provision = auto_provision
        self.keys = jwt.PyJWKClient(ISSUER + "/jwks", timeout=5)
        self.in_flight = BoundedSemaphore(4)

    def identity(self, request):
        token = request_id_token(self.client_id, request)
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["ES384"], audience=self.client_id,
                                issuer=ISSUER, options={"require": ["exp", "iat", "sub", "nonce"]})
        except jwt.PyJWKClientConnectionError:
            raise Problem(502, "IF.Link signing keys are temporarily unavailable") from None
        except jwt.PyJWTError:
            raise Problem(401, "IF.Link identity could not be verified") from None
        nonce, subject = claims["nonce"], claims["sub"]
        if (not isinstance(nonce, str) or not nonce.isascii() or not secrets.compare_digest(nonce, request.nonce)
                or not isinstance(subject, str) or not 1 <= len(subject) <= 255
                or claims.get("azp", self.client_id) != self.client_id
                or claims["aud"] not in (self.client_id, [self.client_id])):
            raise Problem(401, "IF.Link identity does not match this Poteto login")
        return subject

    def register(self, app, store, actor):
        @app.get(LOGIN_PATH, tags=["Account"])
        def config():
            return {"issuer": ISSUER, "client_id": self.client_id}

        @app.post(LOGIN_PATH, status_code=201, tags=["Account"])
        def exchange(body: LoginExchange):
            if not self.in_flight.acquire(blocking=False):
                raise Problem(429, "Too many login attempts in progress; try again")
            try:
                subject = self.identity(body)
            finally:
                self.in_flight.release()
            project_id = self.subjects.get(subject)
            if project_id is None and not self.auto_provision:
                raise Problem(403, "This IF.Link account has not been approved for Poteto CLI login")
            with store.connection() as db:
                if project_id is None:
                    project_id = store.enroll_cli_identity(db, ISSUER, subject)
                project = db.execute(
                    "SELECT p.account_id FROM projects p JOIN accounts a ON a.id=p.account_id "
                    "WHERE p.id=? AND p.state='active' AND a.enabled=1", (project_id,),
                ).fetchone()
                if project is None:
                    raise Problem(403, "The linked Poteto project is no longer available")
                grant = store.mint_token(db, {"id": None, "account_id": project["account_id"]},
                                         name="Poteto CLI", scopes=[PREVIEW_SCOPE], project_ids=[project_id],
                                         expires=time.time() + 7 * 86400, action=LOGIN_ISSUE)
            return grant | {"project_id": project_id}

        @app.delete(LOGIN_PATH, status_code=204, tags=["Account"])
        def revoke(user=Depends(actor)):
            with store.connection() as db:
                issued = db.execute("SELECT 1 FROM audit WHERE account_id=? AND action=? AND object_id=?",
                                    (user["account_id"], LOGIN_ISSUE, user["id"])).fetchone()
                if issued is None:
                    raise Problem(403, "Only an IF.Link CLI login token can revoke itself here")
                db.execute("UPDATE tokens SET revoked=1 WHERE id=?", (user["id"],))
                store.audit(db, user, "token.revoke", user["id"])


def load_login():
    path = os.environ.get("PAAS_OIDC_CONFIG_FILE")
    if not path:
        return None
    config = json.loads(Path(path).read_text())
    client_id = config["client_id"]
    subjects = config.get("projects_by_subject", {})
    auto_provision = config.get("auto_provision", False)
    if (not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id)
            or type(auto_provision) is not bool or not isinstance(subjects, dict)
            or not (auto_provision or subjects)
            or not all(isinstance(k, str) and 1 <= len(k) <= 255 and isinstance(v, str)
                       and re.fullmatch(r"[a-f0-9]{24}", v) for k, v in subjects.items())):
        raise ValueError("Invalid Poteto IF.Link client ID or approved project bindings")
    return CliLogin(client_id, subjects, auto_provision=auto_provision)
