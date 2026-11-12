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
from publisher.login_contract import ISSUER, LOGIN_PATH, REDIRECT_URI, PRODUCTION_SCOPES
from publisher.models import Input
from publisher.store import PREVIEW_DATA_SCOPE, PREVIEW_ENV_SCOPE, PREVIEW_WEB_SCOPE, PREVIEW_MANAGE_SCOPE, PREVIEW_SCOPE, Problem

LOGIN_ISSUE = "token.cli-login"
PROJECT_MANAGEMENT_SCOPES = ['projects:write', 'env:write', 'databases:write', 'storage:write',
                             'exports:write', 'imports:write']


class LoginExchange(Input):
    code: str = Field(min_length=1, max_length=2048)
    verifier: str = Field(pattern=r"^[A-Za-z0-9._~-]{43,128}$")
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    manage_previews: bool = Field(default=False, strict=True)
    manage_env: bool = Field(default=False, strict=True)
    allow_web: bool = Field(default=False, strict=True)
    manage_data: bool = Field(default=False, strict=True)
    production: bool = Field(default=False, strict=True)
    manage_project: bool = Field(default=False, strict=True)
    manage_workspace: bool = Field(default=False, strict=True)
    project_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{24}$')


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
            return {"issuer": ISSUER, "client_id": self.client_id, "preview_management": True,
                    "preview_environment": True, "web_egress": store.settings.sandbox_web,
                    "preview_data": store.settings.native_databases,
                    "production_publishing": store.settings.personal_production and store.settings.personal_previews,
                    'project_selection': store.settings.personal_workspaces and store.settings.personal_previews,
                    'workspace_management': store.settings.personal_workspaces and store.settings.personal_production and store.settings.personal_previews}

        @app.post(LOGIN_PATH, status_code=201, tags=["Account"])
        def exchange(body: LoginExchange):
            if (body.manage_project or body.manage_workspace) and not body.production:
                raise Problem(422, 'Project management requires explicit production authorization')
            if body.manage_project and body.manage_workspace:
                raise Problem(422, 'Choose project or workspace management, not both')
            if (body.manage_workspace or body.project_id is not None) and not (
                    store.settings.personal_workspaces and store.settings.personal_previews):
                raise Problem(503, 'Personal workspaces are not enabled')
            if body.production and any((body.manage_previews, body.manage_env, body.allow_web, body.manage_data)):
                raise Problem(422, 'Keep preview-only login options separate from production authorization')
            if body.production and not (store.settings.personal_production and store.settings.personal_previews):
                raise Problem(503, 'Personal production publishing is not enabled')
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
                    "SELECT p.id,p.account_id,p.preview_mode,p.state FROM projects p JOIN accounts a ON a.id=p.account_id "
                    "WHERE p.id=? AND a.enabled=1", (project_id,),
                ).fetchone()
                if project is None:
                    raise Problem(403, "The linked Poteto project is no longer available")
                if (body.manage_workspace or body.project_id is not None) and project['preview_mode'] != 'isolated':
                    raise Problem(403, 'This linked project requires its existing operator-issued authorization')
                account_id = project['account_id']
                if body.project_id is not None:
                    project = db.execute("SELECT id,account_id,preview_mode,state FROM projects "
                                         "WHERE id=? AND account_id=? AND state='active'",
                                         (body.project_id,account_id)).fetchone()
                    if project is None:
                        raise Problem(404, 'The selected project is not available in this workspace')
                    project_id = project['id']
                if project['state'] != 'active':
                    if not body.manage_workspace:
                        raise Problem(403, 'Select an active project or use workspace management to create one')
                    project_id = None
                if body.production and project['preview_mode'] != 'isolated':
                    raise Problem(403, 'This linked project requires its existing operator-issued production authorization')
                scopes = [PREVIEW_SCOPE]
                for enabled, scope in [(body.manage_previews, PREVIEW_MANAGE_SCOPE),
                                       (body.manage_env, PREVIEW_ENV_SCOPE), (body.allow_web, PREVIEW_WEB_SCOPE),
                                       (body.manage_data, PREVIEW_DATA_SCOPE)]:
                    if enabled:
                        scopes.append(scope)
                if body.allow_web and not store.settings.sandbox_web:
                    raise Problem(503, "Web egress is not enabled on this platform")
                if body.manage_data and not store.settings.native_databases:
                    raise Problem(503, "Preview data management is not enabled on this platform")
                if body.production:
                    scopes = PRODUCTION_SCOPES + (PROJECT_MANAGEMENT_SCOPES if body.manage_project or body.manage_workspace else [])
                if body.manage_workspace:
                    scopes = scopes + ['tokens:write']
                grant = store.mint_token(db, {"id": None, "account_id": account_id},
                                         name="Poteto workspace" if body.manage_workspace else "Poteto CLI",
                                         scopes=scopes, project_ids=None if body.manage_workspace else [project_id],
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
