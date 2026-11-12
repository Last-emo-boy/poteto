"""Native browser login. Only the scoped Poteto credential is saved."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

if __package__:
    from .login_contract import CALLBACK_HOST, CALLBACK_PATH, CALLBACK_PORT, ISSUER, LOGIN_PATH, REDIRECT_URI
else:
    from login_contract import CALLBACK_HOST, CALLBACK_PATH, CALLBACK_PORT, ISSUER, LOGIN_PATH, REDIRECT_URI


def default_token_file():
    legacy = Path.home() / ".config/poteto/token.json"
    if sys.platform == "win32" and not legacy.exists() and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Poteto" / "token.json"
    return legacy


class CallbackServer(HTTPServer):
    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(3)
        return sock, address


def authorize(client_id, *, timeout=180):
    verifier, state, nonce = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = ISSUER + "/auth?" + urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": "openid", "state": state, "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    callback = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log the callback URL: it contains the code.

        def do_GET(self):
            valid = False
            if len(self.path) <= 4096 and self.headers.get("Host") == f"{CALLBACK_HOST}:{CALLBACK_PORT}":
                path, _, query = self.path.partition("?")
                params = parse_qs(query)
                if path == CALLBACK_PATH and params.get("state") == [state] and params.get("iss", [ISSUER]) == [ISSUER]:
                    if params.get("error"):
                        callback["denied"] = True
                        valid = True
                    elif len(params.get("code", [])) == 1:
                        callback["code"] = params["code"][0]
                        valid = True
            body = (b"Return to your terminal to finish Poteto login." if valid else b"Invalid login callback.")
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = CallbackServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    except OSError:
        raise RuntimeError(f"Poteto could not listen on {CALLBACK_HOST}:{CALLBACK_PORT}; close the other login and try again") from None
    with server:
        server.timeout = 0.5
        if not webbrowser.open(url):
            import sys
            print(f"Open this URL in a browser on this computer:\n{url}", file=sys.stderr)
        deadline = time.monotonic() + timeout
        while not callback and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in callback:
        raise RuntimeError("IF.Link login was cancelled" if callback else "IF.Link login timed out")
    return {"code": callback["code"], "verifier": verifier, "nonce": nonce}


def login(client, output, *, manage_previews=False, manage_env=False, allow_web=False, manage_data=False):
    endpoint = urlsplit(client.url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None or endpoint.path or endpoint.query or endpoint.fragment:
        raise ValueError("IF.Link login requires the HTTPS origin of a Poteto API")
    output = output.expanduser()
    if output.exists() or output.is_symlink():
        raise RuntimeError("The token file already exists; run poteto logout first or choose another --output")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = client.request("GET", LOGIN_PATH)
    if config.get("issuer") != ISSUER or not config.get("client_id"):
        raise RuntimeError("This endpoint does not offer Poteto login with IF.Link")
    for selected, capability in [(manage_previews, "preview_management"),
                                  (manage_env, "preview_environment"), (allow_web, "web_egress"),
                                  (manage_data, "preview_data")]:
        if selected and not config.get(capability):
            raise RuntimeError("This server does not support the requested login permission: " + capability)
    # Reserve the destination before authenticating or issuing a token.
    with open(output, "x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as out:
        try:
            request = authorize(config["client_id"])
            for selected, name in [(manage_previews, "manage_previews"), (manage_env, "manage_env"), (allow_web, "allow_web"),
                                   (manage_data, "manage_data")]:
                if selected:
                    request[name] = True
            result = client.request("POST", LOGIN_PATH, request)
            token = result.pop("token")
            json.dump({"token": token, "poteto_url": client.url, "project_id": result["project_id"]}, out)
            out.write("\n")
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    return result | {"output": str(output)}


def logout(client_type, url, path):
    path = path.expanduser()
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("poteto_url") != url.rstrip("/"):
        raise RuntimeError("Choose a Poteto login file for this API with --file")
    client = client_type(url, value["token"])
    try:
        client.request("DELETE", LOGIN_PATH)
    except RuntimeError as error:
        if not str(error).startswith("HTTP 401:"):
            raise
    path.unlink()
    return {"revoked": True}
