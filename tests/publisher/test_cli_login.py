from contextlib import redirect_stdout
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tarfile
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
import jwt
from publisher import cli
from publisher.api import create_app
from publisher.config import Settings
from publisher.login_contract import DEFAULT_URL, ISSUER, LOGIN_PATH, REDIRECT_URI
from publisher.store import Store


class CliLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / "control", new_app_domain="poteto.best")
        self.store = Store(self.settings)
        owner = self.store.authenticate(self.store.create_account("owner")["token"])
        self.project = self.store.create_project(owner, "first")["id"]
        self.other_project = self.store.create_project(owner, "second")["id"]
        self.config = self.root / "login.json"
        self.config.write_text(json.dumps({
            "client_id": "poteto-cli", "projects_by_subject": {"allowed-logto-user": self.project},
        }))
        with patch.dict(os.environ, {"PAAS_OIDC_CONFIG_FILE": str(self.config)}):
            self.api = TestClient(create_app(self.settings))
        self.addCleanup(self.api.close)
        self.key = ec.generate_private_key(ec.SECP384R1())
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [jwk | {"kid": "key-1", "use": "sig", "alg": "ES384"}]}
        self.body = {"code": "login-code", "verifier": "v" * 43, "nonce": "n" * 43}

    def token_response(self, nonce, **claims):
        now = int(time.time())
        signed = jwt.encode({
            "sub": "allowed-logto-user", "aud": "poteto-cli", "iss": ISSUER,
            "iat": now, "exp": now + 60, "nonce": nonce,
        } | claims, self.key, algorithm="ES384", headers={"kid": "key-1"})
        return io.BytesIO(json.dumps({"id_token": signed}).encode())

    def test_browser_login_deploy_and_logout(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        redirect = f"http://127.0.0.1:{port}/callback"
        browser = {}
        callbacks = []
        workers = []

        def open_browser(url):
            auth = urlsplit(url)
            self.assertEqual(ISSUER + "/auth", f"{auth.scheme}://{auth.netloc}{auth.path}")
            browser.update({k: v[0] for k, v in parse_qs(auth.query).items()})
            self.assertEqual(redirect, browser["redirect_uri"])
            self.assertEqual("S256", browser["code_challenge_method"])
            def call():
                for state, code in (("wrong", "ignore-me"), (browser["state"], "login-code")):
                    try:
                        with urlopen(redirect + "?" + urlencode({"state": state, "code": code}), timeout=3) as r:
                            callbacks.append((r.status, r.read()))
                    except HTTPError as e:
                        with e:
                            callbacks.append((e.code, e.read()))
            worker = Thread(target=call, daemon=True)
            workers.append(worker)
            worker.start()
            return True

        def token_endpoint(request, timeout):
            form = parse_qs(request.data.decode())
            self.assertEqual(ISSUER + "/token", request.full_url)
            self.assertEqual(["login-code"], form["code"])
            self.assertEqual([redirect], form["redirect_uri"])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
            self.assertEqual(browser["code_challenge"], challenge.decode().rstrip("="))
            self.assertNotIn("client_secret", form)
            return self.token_response(browser["nonce"])

        api = self.api
        class LocalClient(cli.Client):
            def request(self, method, path, body=None, raw=False):
                headers = {"Authorization": "Bearer " + self.token} if self.token else {}
                if raw:
                    r = api.request(method, path, content=body, headers=headers | {"Content-Type": "application/gzip"})
                else:
                    r = api.request(method, path, json=body, headers=headers)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
                return r.json() if r.content else None

        app = self.root / "project"
        app.mkdir()
        (app / "index.html").write_text("<h1>Poteto</h1>")
        credential = app / "login.json"
        with (
            patch.dict(os.environ, {"PAAS_TOKEN": "", "PAAS_TOKEN_FILE": "", "POTETO_TOKEN_FILE": "", "PAAS_URL": DEFAULT_URL}),
            patch("publisher.cli.Client", LocalClient),
            patch("publisher.cli.default_token_file", return_value=credential),
            patch("publisher.cli_login.webbrowser.open", side_effect=open_browser),
            patch.multiple("publisher.cli_login", CALLBACK_PORT=port, REDIRECT_URI=redirect),
            patch("publisher.oidc_login.REDIRECT_URI", redirect),
            patch("publisher.oidc_login.urlopen", side_effect=token_endpoint),
            patch.object(jwt.PyJWKClient, "fetch_data", return_value=self.jwks),
        ):
            def command(*args):
                out = io.StringIO()
                with patch("sys.argv", ["poteto", *args]), redirect_stdout(out):
                    cli.main()
                return json.loads(out.getvalue())

            result = command("login")
            workers[0].join(timeout=3)
            token = json.loads(credential.read_text())["token"]
            self.assertNotIn("token", result)
            self.assertEqual(0o600, credential.stat().st_mode & 0o777)
            self.assertEqual([400, 200], [r[0] for r in callbacks])
            self.assertNotIn(token.encode(), callbacks[-1][1])
            self.assertEqual(self.project, result["project_id"])
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(403, api.post(f"/v1/projects/{self.project}/deployments",
                                         headers=headers, json={"target": "production"}).status_code)
            self.assertEqual(404, api.post(f"/v1/projects/{self.other_project}/deployments",
                                         headers=headers, json={"target": "preview"}).status_code)
            self.assertEqual(403, api.post("/v1/tokens", headers=headers, json={
                "name": "another", "scopes": ["*"],
            }).status_code)
            release = command("deploy", str(app), "--no-wait")
            self.assertEqual("preview", release["config"]["target"])
            self.assertEqual("queued", release["state"])
            with tarfile.open(self.settings.root / "artifacts" / (release["id"] + ".tar.gz")) as archive:
                self.assertEqual(["index.html"], archive.getnames())
            self.assertEqual({"revoked": True}, command("logout"))
            self.assertFalse(credential.exists())
            self.assertEqual(401, api.get("/v1/me", headers=headers).status_code)

    def test_refuses_wrong_identity_and_unapproved_subject(self):
        for override, status in (
            ({"aud": "accounts-app"}, 401), ({"iss": "https://wrong.example/oidc"}, 401),
            ({"nonce": "x" * 43}, 401), ({"sub": "not-approved"}, 403), ({"exp": 1}, 401),
        ):
            with self.subTest(override=override), patch.object(jwt.PyJWKClient, "fetch_data", return_value=self.jwks):
                claims = {"nonce": self.body["nonce"]} | override
                with patch("publisher.oidc_login.urlopen", return_value=self.token_response(**claims)):
                    r = self.api.post(LOGIN_PATH, json=self.body)
                self.assertEqual(status, r.status_code, r.text)
                self.assertNotIn("token", r.json())

    def test_disabled_and_maintenance_preserve_the_existing_auth_boundary(self):
        with patch.dict(os.environ, {"PAAS_OIDC_CONFIG_FILE": ""}):
            self.assertEqual(401, TestClient(create_app(self.settings)).get(LOGIN_PATH).status_code)
        self.assertEqual({"issuer": ISSUER, "client_id": "poteto-cli"}, self.api.get(LOGIN_PATH).json())
        self.assertEqual(401, self.api.post("/v1/projects", json={"name": "bad"}).status_code)
        self.assertEqual(401, self.api.delete(LOGIN_PATH).status_code)
        self.assertEqual(413, self.api.post(LOGIN_PATH, content=b"x" * 65537).status_code)
        (self.settings.root / "maintenance.json").write_text("{}")
        self.assertEqual(503, self.api.post(LOGIN_PATH, json=self.body).status_code)

    def test_exchange_reuses_exact_registered_callback(self):
        def provider(request, timeout):
            self.assertEqual([REDIRECT_URI], parse_qs(request.data.decode())["redirect_uri"])
            return self.token_response(self.body["nonce"])
        with patch("publisher.oidc_login.urlopen", side_effect=provider), patch.object(jwt.PyJWKClient, "fetch_data", return_value=self.jwks):
            response = self.api.post(LOGIN_PATH, json=self.body)
        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual(["deployments:preview"], response.json()["scopes"])
