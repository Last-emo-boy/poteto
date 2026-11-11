"""Authenticate before reading bodies; bound JSON requests independently of source uploads."""
import asyncio
import re
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from publisher.login_contract import LOGIN_PATH
from publisher.store import Problem


class ControlLimits:
    def __init__(self, app, store, allow_cli_login=False):
        self.app, self.store = app, store
        self.allow_cli_login = allow_cli_login

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/"):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        try:
            public_login = (self.allow_cli_login and scope["path"] == LOGIN_PATH
                            and scope["method"] in {"GET", "POST"})
            if not public_login:
                authorization = headers.get("authorization", "")
                scheme, _, token = authorization.partition(" ")
                if scheme.lower() != "bearer" or not token or len(token) > 200:
                    raise Problem(401, "A platform Bearer token is required")
                actor = await run_in_threadpool(self.store.authenticate, token)
                scope.setdefault("state", {})["actor"] = actor
            if scope['method'] not in {'GET','HEAD','OPTIONS'} and (self.store.settings.root/'maintenance.json').exists():
                raise Problem(503,'Publishing is temporarily in maintenance; existing applications are managed separately')
            # Source bodies are streamed with their own byte limit, lease and deadline.
            is_source = scope["method"] == "PUT" and scope["path"].endswith("/source")
            is_import_part = scope['method']=='PUT' and bool(re.fullmatch(r'/v1/imports/[a-f0-9]{24}/part',scope['path']))
            if scope["method"] in {"POST", "PUT", "PATCH"} and not (is_source or is_import_part):
                try:
                    length = int(headers.get("content-length", "0"))
                except ValueError:
                    raise Problem(400, "Invalid Content-Length") from None
                if length > 65536:
                    raise Problem(413, "JSON request exceeds 64 KiB")
                parts, total = [], 0
                async with asyncio.timeout(20):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        part = message.get("body", b"")
                        total += len(part)
                        if total > 65536:
                            raise Problem(413, "JSON request exceeds 64 KiB")
                        parts.append(part)
                        if not message.get("more_body", False):
                            break
                body = b"".join(parts)
                original_receive = receive
                sent = False

                async def replay():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await original_receive()
                receive = replay
        except (Problem, TimeoutError) as error:
            status = error.status if isinstance(error, Problem) else 408
            message = error.message if isinstance(error, Problem) else "Request body timed out"
            extra = {"Cache-Control": "no-store"}
            if status == 401:
                extra["WWW-Authenticate"] = "Bearer"
            if status == 429:
                extra["Retry-After"] = "60"
            if status == 503:
                extra['Retry-After']='30'
            return await JSONResponse({"error": message}, status_code=status, headers=extra)(scope, receive, send)
        await self.app(scope, receive, send)
