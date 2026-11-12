#!/usr/bin/env python3
"""Portable publishing client. Uses Python's standard library only."""
import argparse
import getpass
import http.client
import json
import os
import re
from pathlib import Path
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import warnings

if __package__:
    from .cli_login import default_token_file, login, logout
    from .cli_tools import VERSION, PlanError, deployment_config, initialize, pack, plan_deployment
    from .cli_project import deployment_reference, find_link, unlink, write_link
    from .login_contract import DEFAULT_URL, LOGIN_PATH
    from .transfer_wire import BUNDLE_LIMIT, CHUNK, FORMAT, BoundedZipInput
else:
    from cli_login import default_token_file, login, logout
    from cli_tools import VERSION, PlanError, deployment_config, initialize, pack, plan_deployment
    from cli_project import deployment_reference, find_link, unlink, write_link
    from login_contract import DEFAULT_URL, LOGIN_PATH
    from transfer_wire import BUNDLE_LIMIT, CHUNK, FORMAT, BoundedZipInput


class Client:
    def __init__(self, url, token=None):
        self.url, self.token = url.rstrip("/"), token

    def request(self, method, path, body=None, raw=False, *, idempotency_key=None, timeout=180):
        headers = {"User-Agent": "Poteto/" + VERSION}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if raw:
            headers["Content-Type"] = raw if isinstance(raw,str) else "application/gzip"
        elif body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, body, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            try:
                with error:
                    message = json.loads(error.read())
            except (ValueError, UnicodeDecodeError):
                message = {"error": f"HTTP {error.code}"}
            raise RuntimeError(f"HTTP {error.code}: {json.dumps(message, ensure_ascii=False)}") from None

    def wait(self, release_id, timeout):
        deadline = time.monotonic() + timeout
        expired = f"Timed out waiting; deployment {release_id} continues server-side"

        def observe(path):
            interrupted = False
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    return self.request('GET', path, timeout=min(30, remaining))
                except (OSError, http.client.HTTPException) as error:
                    cause = error.reason if isinstance(error, urllib.error.URLError) else error
                    if isinstance(cause, ssl.SSLCertVerificationError):
                        raise
                    if not interrupted:
                        print(f'Connection interrupted; still watching deployment {release_id}', file=sys.stderr, flush=True)
                        interrupted = True
                    time.sleep(min(2, max(0, deadline - time.monotonic())))
            raise RuntimeError(expired)

        previous = None
        cursor = 0
        while time.monotonic() < deadline:
            release = observe('/v1/deployments/' + release_id)
            while True:
                logs = observe(f'/v1/deployments/{release_id}/logs?after={cursor}')
                for line in logs["logs"]:
                    print(line["message"], file=sys.stderr, flush=True)
                if len(logs["logs"]) < 500:
                    cursor = logs["next_cursor"]
                    break
                if logs["next_cursor"] <= cursor:
                    raise RuntimeError('Build log cursor did not advance')
                cursor = logs["next_cursor"]
            if release["state"] != previous:
                print(release["state"], file=sys.stderr)
                previous = release["state"]
            if release["state"] in {"ready", "failed", "cancelled"}:
                if release["state"] != "ready":
                    raise RuntimeError(release["error"] or "Deployment did not become ready")
                return release
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise RuntimeError(expired)

    def wait_job(self, job_id, timeout=900):
        deadline=time.monotonic()+timeout
        while True:
            job=self.request('GET','/v1/jobs/'+job_id)
            if job['state']=='completed':return job
            if job['state']=='failed':raise RuntimeError('Operation '+job_id+' failed: '+(job['error'] or 'No error details returned'))
            if job['state']=='cancelled':raise RuntimeError('Operation '+job_id+' was cancelled')
            if time.monotonic()>deadline:raise RuntimeError('Operation '+job_id+' continues server-side; observe that same job')
            time.sleep(1)

    def download_export(self,operation,destination):
        import re
        if not re.fullmatch(r'[a-f0-9]{24}',operation):raise ValueError('Invalid export ID')
        status=self.request('GET','/v1/exports/'+operation)
        if status['state']!='completed' or status['size']<=0:raise RuntimeError('Export is not ready')
        if destination.exists():raise RuntimeError('Refusing to overwrite an existing export file')
        partial=destination.with_name(destination.name+'.'+operation+'.part')
        offset=partial.stat().st_size if partial.exists() else 0
        total=status['size']
        if offset>total:raise RuntimeError('Partial file is larger than the export')
        try:
            with open(partial,'ab',opener=lambda p,f:os.open(p,f,0o600)) as out:
                while offset<total:
                    size=min(8*1024**2,total-offset)
                    path=f'/v1/exports/{operation}/data?offset={offset}&length={size}'
                    request=urllib.request.Request(self.url+path,headers={'Authorization':'Bearer '+self.token,'User-Agent':'OVH-PaaS-CLI/0.1'})
                    with urllib.request.urlopen(request,timeout=180) as response:
                        if response.headers.get('X-Paas-Offset')!=str(offset) or response.headers.get('X-Paas-Size')!=str(total):
                            raise RuntimeError('Export response does not match this artifact and offset')
                        chunk=response.read(size+1)
                    if len(chunk)!=size:raise RuntimeError('Export chunk was incomplete')
                    out.write(chunk);offset+=size
        except (OSError,RuntimeError) as error:
            raise RuntimeError('Export download interrupted; repeat the same download to resume: '+str(error)) from None
        # Install atomically without replacing a file created by another process.
        os.link(partial,destination);partial.unlink()
        return {'id':operation,'bytes':total,'output':str(destination)}

    def import_bundle(self,*,project=None,operation=None,bundle,identity,age,wait=True):
        import re
        import tempfile
        import urllib.parse
        import zipfile
        parsed=urllib.parse.urlparse(self.url)
        if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','::1','localhost'}):
            raise ValueError('Project import requires HTTPS outside localhost')
        if operation and not re.fullmatch(r'[a-f0-9]{24}',operation):raise ValueError('Invalid import ID')
        with tempfile.TemporaryFile() as plain:
            process=subprocess.Popen([age,'-d','-i',str(identity),str(bundle)],stdout=subprocess.PIPE)
            size=0
            try:
                while chunk:=process.stdout.read(1024**2):
                    size+=len(chunk)
                    if size>BUNDLE_LIMIT:raise ValueError('Project bundle exceeds 14 GiB')
                    plain.write(chunk)
                if process.wait():raise RuntimeError('age did not authenticate the complete project bundle')
            finally:
                process.stdout.close()
                if process.poll() is None:process.kill();process.wait()
            plain.flush();plain.seek(0)
            try:
                with zipfile.ZipFile(BoundedZipInput(plain)) as archive:
                    info=archive.getinfo('manifest.json')
                    if not 0<info.file_size<=256*1024:raise ValueError('Invalid manifest size')
                    manifest=json.loads(archive.read(info))
                bundle_id=manifest['bundle_id'];kinds=sorted({r['kind'] for r in manifest['resources']})
                if manifest['format']!=FORMAT or not re.fullmatch(r'[a-f0-9]{24}',bundle_id) or not set(kinds)<={'database','volume'}:
                    raise ValueError('Unsupported bundle')
            except (zipfile.BadZipFile,KeyError,TypeError,ValueError):raise ValueError('Invalid authenticated project bundle') from None
            status=(self.request('GET','/v1/imports/'+operation) if operation else
                    self.request('POST','/v1/projects/'+project+'/imports',{'bundle_id':bundle_id,'size':size,'kinds':kinds}))
            operation=status['id'];path='/v1/imports/'+operation
            if status['bundle_id']!=bundle_id or status['size']!=size:raise RuntimeError('This bundle does not match the resumable import')
            if status['state']=='uploading':
                print('Import operation '+operation,file=sys.stderr)
                offset=status['received'];plain.seek(offset)
                try:
                    while offset<size:
                        length=min(CHUNK,size-offset);chunk=plain.read(length)
                        if len(chunk)!=length:raise RuntimeError('Authenticated bundle ended early')
                        next_status=self.request('PUT',path+'/part?offset='+str(offset),chunk,raw='application/octet-stream')
                        if next_status['received']!=offset+length:raise RuntimeError('Import did not commit the expected offset')
                        offset+=length
                    status=self.request('POST',path+'/submit')
                except (RuntimeError,OSError) as error:
                    raise RuntimeError('Import '+operation+' interrupted; use import resume with the same bundle: '+str(error)) from None
        if wait and status['state'] in {'queued','running'}:
            self.wait_job(operation,timeout=1800);status=self.request('GET',path)
        return status


def _credential_file(url):
    token_file = os.environ.get("POTETO_TOKEN_FILE") or os.environ.get("PAAS_TOKEN_FILE")
    if token_file:
        return Path(token_file).expanduser()
    default = default_token_file()
    if not (os.environ.get("POTETO_TOKEN") or os.environ.get("PAAS_TOKEN")) and default.is_file():
        if url.rstrip("/") == DEFAULT_URL:
            return default
        stored = json.loads(default.read_text(encoding="utf-8-sig"))
        if isinstance(stored, dict) and stored.get("poteto_url") == url.rstrip("/"):
            return default
    return None


def _show_plan(args, credential_file):
    try:
        report = plan_deployment(args.directory, args.config, credential_file, preview=args.preview,
                                 production=args.prod, preset=args.preset).summary()
    except (PlanError, OSError) as error:
        if args.json:
            code = error.code if isinstance(error, PlanError) else "unreadable_project"
            print(json.dumps({"ok": False, "error": {"code": code, "message": str(error)}}, ensure_ascii=False))
            raise SystemExit(1) from None
        raise
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    print(f"{report['runtime']} ({report['origin']}) · {report['directory']}")
    print(f"Target: {report['target'] or 'deployment default (use --preview to select preview)'}")
    for field, label in (("build_command", "Build"), ("start_command", "Start"),
                         ("public_directory", "Public directory"), ("dockerfile", "Dockerfile")):
        if report[field] is not None:
            print(f"{label}: {report[field]}")
    print(f"Files to upload: {report['upload_files']}")


def _environment_value(args):
    if args.file:
        with args.file.open(encoding="utf-8-sig", newline="") as source:
            value = source.read(16385)
    elif args.stdin:
        value = sys.stdin.read(16387)
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    elif sys.stdin.isatty():
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass(args.name + ": ")
            except getpass.GetPassWarning:
                raise RuntimeError("Hidden input is unavailable; use --stdin or --file") from None
    else:
        raise RuntimeError("Use --stdin or --file to set a value without an interactive terminal")
    if len(value) > 16384 or "\x00" in value:
        raise ValueError("Variable values must contain at most 16384 characters and no NUL")
    return value


def main(prog="poteto"):
    if sys.platform == "win32":
        for output in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(output, "reconfigure"):
                output.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--version", action="version", version=f"{prog} {VERSION}")
    parser.add_argument("--url", help="API origin (overrides environment and directory link)")
    commands = parser.add_subparsers(dest="command", required=True)
    sign_in = commands.add_parser("login", help="Sign in with IF.Link in your browser")
    sign_in.add_argument("--output", type=Path, default=default_token_file())
    sign_out = commands.add_parser("logout", help="Revoke the credential issued by IF.Link login")
    sign_out.add_argument("--file", type=Path, default=default_token_file())
    init = commands.add_parser("init", help="Start a static, Node or Dockerfile preview project")
    init.add_argument("directory", type=Path, nargs="?", default=Path("."))
    init.add_argument("--runtime", choices=["static", "node", "dockerfile"], default="static")
    if prog != "paas":
        link = commands.add_parser("link", help="Remember an existing project for this directory; stores no credentials")
        link.add_argument("directory", type=Path, nargs="?", default=Path("."))
        link.add_argument("--project", help="Project ID (defaults to the project from poteto login)")
        unlink_command = commands.add_parser("unlink", help="Remove only this directory's local project link")
        unlink_command.add_argument("directory", type=Path, nargs="?", default=Path("."))
        plan = commands.add_parser("plan", help="Inspect a deployment locally without logging in or uploading")
        plan.add_argument("directory", type=Path, nargs="?", default=Path("."))
        plan.add_argument("--config", type=Path, help="Deployment configuration JSON")
        plan.add_argument("--preset", choices=["nextjs", "vite"], help="Apply a framework's build and runtime defaults")
        target = plan.add_mutually_exclusive_group()
        target.add_argument("--preview", action="store_true")
        target.add_argument("--prod", action="store_true", help="Plan an explicit production deployment")
        plan.add_argument("--json", action="store_true", help="Output a machine-readable result, including validation errors")
    commands.add_parser("doctor", help="Check API reachability and the current credential without printing it")
    commands.add_parser("whoami")
    tokens = commands.add_parser("token")
    token_actions = tokens.add_subparsers(dest="token_action", required=True)
    preview_token = token_actions.add_parser("create-preview", help="Issue a project-scoped publishing credential")
    preview_token.add_argument("--project", required=True)
    preview_token.add_argument("--name", required=True)
    preview_token.add_argument("--expires-in-days", type=int, default=30)
    preview_token.add_argument("--output", type=Path, required=True)
    revoke = token_actions.add_parser("revoke")
    revoke.add_argument("token_id")
    job=commands.add_parser('job');job.add_argument('job_id')
    projects = commands.add_parser("projects")
    projects.add_argument("--create")
    project=commands.add_parser('project')
    actions=project.add_subparsers(dest='project_action',required=True)
    rename=actions.add_parser('rename');rename.add_argument('project');rename.add_argument('name')
    status_project=actions.add_parser('status');status_project.add_argument('project',nargs='?')
    delete_project=actions.add_parser('delete');delete_project.add_argument('project')
    delete_project.add_argument('--destroy-data',action='store_true',help='Also destroy all project databases and stored files')
    delete_project.add_argument('--no-wait',action='store_true')
    exports=commands.add_parser('export')
    actions=exports.add_subparsers(dest='export_action',required=True)
    export_create=actions.add_parser('create');export_create.add_argument('project')
    export_create.add_argument('--recipient',required=True,help='Public age X25519 recipient, never a private identity')
    export_create.add_argument('--quiesce',action='store_true',help='Allow a bounded pause of apps while copying databases and files')
    export_create.add_argument('--no-wait',action='store_true')
    for action in ['status','download','delete']:
        entry=actions.add_parser(action);entry.add_argument('export_id')
        if action=='download':entry.add_argument('--output',type=Path,required=True)
    imports=commands.add_parser('import');actions=imports.add_subparsers(dest='import_action',required=True)
    for action in ['create','resume','status','retry','delete']:
        entry=actions.add_parser(action);entry.add_argument('target',help='Destination project ID' if action=='create' else 'Import ID')
        if action in {'create','resume'}:
            entry.add_argument('bundle',type=Path);entry.add_argument('--identity',type=Path,required=True)
            entry.add_argument('--age',default=os.environ.get('PAAS_AGE','age'));entry.add_argument('--no-wait',action='store_true')
    deploy = commands.add_parser("deploy")
    deploy.add_argument("directory", type=Path, nargs="?", default=Path("."))
    deploy.add_argument("--project", help="Project ID (defaults to the project from poteto login)")
    deploy.add_argument("--config", type=Path, help="Deployment configuration JSON")
    if prog != "paas":
        deploy.add_argument("--preset", choices=["nextjs", "vite"])
    target = deploy.add_mutually_exclusive_group()
    target.add_argument("--preview", action="store_true")
    target.add_argument("--prod", action="store_true", help="Explicitly deploy to production")
    deploy.add_argument("--no-wait", action="store_true")
    deploy.add_argument("--url-only", action="store_true", help="Print only the ready HTTPS URL to stdout")
    deploy.add_argument("--timeout", type=int, default=900)
    redeploy = commands.add_parser("redeploy", help="Rebuild archived source with current runtime variables; defaults to preview")
    redeploy.add_argument("deployment", help="Deployment ID or Poteto URL")
    target = redeploy.add_mutually_exclusive_group()
    target.add_argument("--prod", action="store_true")
    target.add_argument("--preview", action="store_true")
    redeploy.add_argument("--request-id", help="Reuse this ID to safely resume the same request", default=None)
    redeploy.add_argument("--no-wait", action="store_true")
    redeploy.add_argument("--url-only", action="store_true")
    redeploy.add_argument("--timeout", type=int, default=900)
    deployments = commands.add_parser("deployments", aliases=["ls"], help="List releases in the current project")
    deployments.add_argument("--project", help="Project ID (defaults to the project from poteto login)")
    status = commands.add_parser("status", aliases=["inspect"], help="Inspect a deployment ID or Poteto URL")
    status.add_argument("deployment")
    logs = commands.add_parser("logs")
    logs.add_argument("deployment")
    logs.add_argument('--runtime',action='store_true',help='Read a bounded runtime stdout/stderr snapshot without waking the app')
    logs.add_argument("--follow", action="store_true", help="Follow build logs until the release is ready")
    logs.add_argument("--timeout", type=int, default=900, help="Maximum wait when using --follow")
    for name in ['retry', 'cancel', 'delete']:
        command = commands.add_parser(name)
        command.add_argument('deployment')
    for name in ["promote", "rollback"]:
        command = commands.add_parser(name)
        command.add_argument("deployment")
        command.add_argument("--project", help="Project ID (defaults to the directory link or login)")
    env = commands.add_parser("env")
    env.add_argument("env_action", nargs="?", choices=["list", "ls", "set", "rm"], help="Omit for the legacy JSON replacement interface")
    env.add_argument("name", nargs="?")
    env.add_argument("--project", help="Project ID (defaults to the directory link or login)")
    env.add_argument("--target", choices=["production", "preview"], help="New subcommands default to preview; legacy env defaults to production")
    env.add_argument("--file", type=Path, help="Value file for env set, or a JSON object for legacy env replacement")
    env.add_argument("--stdin", action="store_true", help="Read one variable value from stdin; never place secrets in arguments")
    for name in ['database','volume']:
        data=commands.add_parser(name)
        actions=data.add_subparsers(dest='data_action',required=True)
        create=actions.add_parser('create')
        create.add_argument('--project',help='Project ID (defaults to the directory link or login)')
        create.add_argument('--stage',choices=['production','preview'],default='production')
        listing=actions.add_parser('list');listing.add_argument('--project',help='Project ID (defaults to the directory link or login)')
        retry=actions.add_parser('retry');retry.add_argument('resource')
        delete=actions.add_parser('delete');delete.add_argument('resource')
        delete.add_argument('--destroy-data',action='store_true',required=True,help='Explicitly destroy the stored data, after all bound releases are removed')
    args = parser.parse_args()
    args.command = {"ls": "deployments", "inspect": "status"}.get(args.command, args.command)
    if args.command in {"deploy", "redeploy"} and args.no_wait and args.url_only:
        parser.error("--url-only requires waiting for the release")
    if args.command == "env":
        args.target = args.target or ("preview" if args.env_action else "production")
        if args.env_action in {"set", "rm"}:
            if not args.name or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", args.name):
                parser.error("env set/rm requires a valid uppercase variable name")
            if args.name.startswith("PAAS_") or args.name in {"PORT", "DATABASE_URL"}:
                parser.error("Platform-managed variables cannot be changed")
        elif args.name:
            parser.error("A variable name is only used with env set/rm")
        if args.stdin and (args.env_action != "set" or args.file):
            parser.error("--stdin is only used with env set and cannot be combined with --file")
        if args.env_action in {"list", "ls", "rm"} and args.file:
            parser.error("--file is only used for env set or legacy env replacement")
    if args.command == "logs" and args.runtime and args.follow:
        parser.error("--follow is for build logs, not runtime snapshots")
    try:
        if args.command == "unlink":
            print(json.dumps(unlink(args.directory), indent=2))
            return
        if args.command == "init":
            print(json.dumps(initialize(args.directory, args.runtime), indent=2, ensure_ascii=False))
            return
        linked = find_link(getattr(args, "directory", Path("."))) if prog != "paas" else None
        explicit_url = args.url or os.environ.get("POTETO_URL") or os.environ.get("PAAS_URL")
        args.url = explicit_url or (linked.url if linked else DEFAULT_URL)
        if args.command == "login":
            print(json.dumps(login(Client(args.url), args.output), indent=2, ensure_ascii=False))
            return
        if args.command == "logout":
            print(json.dumps(logout(Client, args.url, args.file), indent=2))
            return
        if args.command == "plan":
            token_file = os.environ.get("POTETO_TOKEN_FILE") or os.environ.get("PAAS_TOKEN_FILE") or default_token_file()
            _show_plan(args, token_file)
            return
        token_file = _credential_file(args.url)
        token = os.environ.get("POTETO_TOKEN") or os.environ.get("PAAS_TOKEN")
        signed_in_project = None
        bound_credential = False
        if token_file:
            value = token_file.read_text(encoding="utf-8-sig").strip()
            stored = json.loads(value) if value.startswith("{") else {"token": value}
            if "poteto_url" in stored:
                if stored["poteto_url"] != args.url.rstrip("/"):
                    raise ValueError("This Poteto login file belongs to a different API")
                bound_credential = True
                signed_in_project = stored.get("project_id")
            token = stored["token"]
        if token and linked and not explicit_url and linked.url != DEFAULT_URL and not bound_credential:
            raise ValueError("A directory link cannot choose a custom API for a manual token; confirm it with --url or POTETO_URL")
        if args.command == "doctor":
            client = Client(args.url, token)
            report = {"version": VERSION, "api": client.url, "python": ".".join(map(str, sys.version_info[:3])),
                      "credential": "file" if token_file else "environment" if token else "missing"}
            if token:
                account = client.request("GET", "/v1/me")
                report.update({"project_ids": account["project_ids"], "scopes": account["scopes"]})
            else:
                try:
                    client.request("GET", LOGIN_PATH)
                except RuntimeError as error:
                    if not str(error).startswith(("HTTP 401:", "HTTP 404:")):
                        raise
                    report["login"] = "manual token required"
                else:
                    report["login"] = "IF.Link available; run poteto login"
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return
        if not token:
            parser.error("Run poteto login or set POTETO_TOKEN_FILE or POTETO_TOKEN (legacy PAAS_* variables also work)")
        client = Client(args.url, token)
        linked_project = linked.project_id if linked and linked.url == client.url else None
        if hasattr(args, "project"):
            args.project = args.project or linked_project or signed_in_project
            if args.project is None and args.command not in {"deploy", "deployments", "link"}:
                parser.error("Choose a project with --project, poteto link, or poteto login")
        if hasattr(args, "deployment"):
            kind, reference = deployment_reference(args.deployment)
            if kind == "project":
                reference = client.request("GET", "/v1/projects/" + reference)["current_deployment"]
                if not reference:
                    raise ValueError("This project has no production deployment")
            args.deployment = reference
        if args.command == "whoami":
            result = client.request("GET", "/v1/me")
        elif args.command == "link":
            if not args.project:
                parser.error("link requires --project PROJECT_ID when using a manual token")
            # Preview credentials can list their own releases without gaining
            # broader project-management access.
            client.request("GET", "/v1/projects/" + args.project + "/deployments")
            result = write_link(args.directory, client.url, args.project)
        elif args.command == "token":
            if args.token_action == "create-preview":
                with open(args.output,"x",opener=lambda path,flags:os.open(path,flags,0o600)) as out:
                    try:
                        result=client.request("POST","/v1/tokens",{"name":args.name,"scopes":["deployments:preview"],
                            "project_ids":[args.project],"expires_in_days":args.expires_in_days})
                        json.dump({"token":result.pop("token")},out);out.write("\n")
                    except BaseException:
                        args.output.unlink(missing_ok=True)
                        raise
                result["output"]=str(args.output)
            else:
                client.request("DELETE","/v1/tokens/"+args.token_id)
                result={"id":args.token_id,"revoked":True}
        elif args.command=='job':
            result=client.request('GET','/v1/jobs/'+args.job_id)
        elif args.command == "projects":
            result = client.request("POST", "/v1/projects", {"name": args.create}) if args.create else client.request("GET", "/v1/projects")
        elif args.command=='project':
            path='/v1/projects/'+args.project
            if args.project_action=='rename':result=client.request('PATCH',path,{'name':args.name})
            elif args.project_action=='status':result=client.request('GET',path)
            else:
                result=client.request('DELETE',path+('?destroy_data=true' if args.destroy_data else ''))
                if not args.no_wait:result=client.wait_job(result['job_id'])
        elif args.command=='export':
            if args.export_action=='create':
                result=client.request('POST','/v1/projects/'+args.project+'/exports',{'recipient':args.recipient,'quiesce':args.quiesce})
                if not args.no_wait:
                    client.wait_job(result['id']);result=client.request('GET','/v1/exports/'+result['id'])
            elif args.export_action=='download':result=client.download_export(args.export_id,args.output)
            else:result=client.request('DELETE' if args.export_action=='delete' else 'GET','/v1/exports/'+args.export_id)
        elif args.command=='import':
            if args.import_action in {'create','resume'}:
                target={'project':args.target} if args.import_action=='create' else {'operation':args.target}
                result=client.import_bundle(**target,bundle=args.bundle,identity=args.identity,age=args.age,wait=not args.no_wait)
            else:
                path='/v1/imports/'+args.target
                result=client.request('POST',path+'/retry') if args.import_action=='retry' else client.request('DELETE' if args.import_action=='delete' else 'GET',path)
        elif args.command == "deploy":
            project_id = args.project
            if not project_id:
                parser.error("deploy requires --project PROJECT_ID when using a manual token")
            if prog == "paas":
                if not args.directory.is_dir():
                    raise RuntimeError("Deployment directory does not exist")
                config = deployment_config(args.directory, args.config)
                source = pack(args.directory, token_file)
            else:
                prepared = plan_deployment(args.directory, args.config, token_file, preview=args.preview,
                                           production=args.prod, preset=args.preset)
                config = prepared.config
                source = pack(prepared.directory, files=prepared.files)
            if args.prod:
                config["target"] = "production"
            elif args.preview or ((signed_in_project or linked_project) and "target" not in config):
                config["target"] = "preview"
            if len(source) > 50 * 1024 * 1024:
                raise RuntimeError("Archive exceeds the server upload limit of 50 MiB")
            result = client.request("POST", f"/v1/projects/{project_id}/deployments", config)
            try:
                client.request("PUT", result["upload_url"], source, raw=True)
            except RuntimeError:
                print(f"Upload failed for deployment {result['id']}; inspect its status before retrying", file=sys.stderr)
                raise
            if not args.no_wait:
                result = client.wait(result["id"], args.timeout)
            else:
                result = client.request("GET", "/v1/deployments/" + result["id"])
            if args.url_only:
                print(result["production_url"] if config.get("target", "production") == "production" else result["url"])
                return
        elif args.command == "redeploy":
            key = args.request_id or uuid.uuid4().hex
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", key):
                parser.error("--request-id must contain 1 to 64 letters, digits, dots, underscores or hyphens")
            print("Redeploy request ID: " + key + " (reuse --request-id to resume)", file=sys.stderr)
            result = client.request("POST", "/v1/deployments/" + args.deployment + "/redeploy",
                                    {"target": "production" if args.prod else "preview"}, idempotency_key=key)
            if not args.no_wait:
                result = client.wait(result["id"], args.timeout)
            if args.url_only:
                print(result["production_url"] if args.prod else result["url"])
                return
        elif args.command == "deployments":
            project_id = args.project
            if not project_id:
                parser.error("deployments requires --project PROJECT_ID when using a manual token")
            result = client.request("GET", f"/v1/projects/{project_id}/deployments")
        elif args.command == "logs" and args.follow:
            result = client.wait(args.deployment, args.timeout)
        elif args.command=='logs' and args.runtime:
            path='/v1/deployments/'+args.deployment+'/runtime-logs'
            result=client.request('GET',path)
            deadline=time.monotonic()+45
            while result['state'] in {'queued','running'}:
                if time.monotonic()>deadline:
                    raise RuntimeError('Runtime logs are still being collected; repeat this command to observe the same operation')
                time.sleep(1)
                result=client.request('GET',path)
            if result['error']:raise RuntimeError(result['error'])
        elif args.command in {"status", "logs"}:
            path = "/v1/deployments/" + args.deployment + ("/logs" if args.command == "logs" else "")
            result = client.request("GET", path)
        elif args.command in {'retry', 'cancel', 'delete'}:
            path = '/v1/deployments/' + args.deployment
            result = client.request('DELETE', path) if args.command == 'delete' else client.request('POST', path + '/' + args.command)
        elif args.command in {"promote", "rollback"}:
            result = client.request("POST", f"/v1/projects/{args.project}/{args.command}", {"deployment_id": args.deployment})
        elif args.command == "env":
            path=f"/v1/projects/{args.project}/env"+("?target=preview" if args.target=="preview" else "")
            if args.env_action == "set":
                value = _environment_value(args)
                result = client.request("PATCH", path, {"values": {args.name: value}})
            elif args.env_action == "rm":
                result = client.request("PATCH", path, {"remove": [args.name]})
            else:
                result = client.request("PUT", path, {"values": json.loads(args.file.read_text(encoding="utf-8-sig"))}) if args.file else client.request("GET", path)
        elif args.command in {'database','volume'}:
            if args.data_action=='create':
                result=client.request('POST',f'/v1/projects/{args.project}/'+('databases' if args.command=='database' else 'volumes'),{'stage':args.stage})
            elif args.data_action=='list':
                result=[r for r in client.request('GET',f'/v1/projects/{args.project}/data') if r['kind']==args.command]
            elif args.data_action=='retry':
                result=client.request('POST','/v1/data/'+args.resource+'/retry')
            else:
                result=client.request('DELETE','/v1/data/'+args.resource+'?destroy_data=true')
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (RuntimeError, OSError, ValueError, KeyboardInterrupt) as error:
        print("Cancelled" if isinstance(error, KeyboardInterrupt) else str(error), file=sys.stderr)
        raise SystemExit(1) from None


def legacy_main():
    main("paas")


if __name__ == "__main__":
    main("poteto" if __package__ else "paas")
