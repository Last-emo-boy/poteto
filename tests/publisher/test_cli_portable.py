"""Native client contract tests; the deployment server remains Linux-only."""
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import tarfile
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.error import URLError
from urllib.request import urlopen

from publisher import cli, cli_login


class PortableClientTests(unittest.TestCase):
    def test_wait_drains_all_log_pages_before_returning_ready(self):
        client = cli.Client('https://deploy.iflink.tech')
        ready = {'state': 'ready', 'url': 'https://example.test'}
        first = {'logs': [{'message': str(i)} for i in range(500)], 'next_cursor': 500}
        last = {'logs': [{'message': 'last log'}], 'next_cursor': 501}
        output = io.StringIO()
        interrupted = URLError(ssl.SSLEOFError(8, 'transport interrupted'))
        with patch.object(client, 'request', side_effect=[ready, first, interrupted, last]) as request, \
             patch('publisher.cli.time.sleep'), redirect_stderr(output):
            self.assertEqual(ready, client.wait('fixture', 10))
        self.assertIn('last log\nready\n', output.getvalue())
        self.assertEqual(1, output.getvalue().splitlines().count('499'))
        self.assertEqual(request.call_args_list[-2].args, request.call_args.args)
        self.assertTrue(request.call_args.args[1].endswith('?after=500'))

    def test_wait_does_not_retry_a_certificate_error(self):
        client = cli.Client('https://deploy.iflink.tech')
        failure = URLError(ssl.SSLCertVerificationError(1, 'certificate rejected'))
        with patch.object(client, 'request', side_effect=failure) as request:
            with self.assertRaises(URLError):
                client.wait('fixture', 10)
        request.assert_called_once()

    def test_directory_link_upload_inspect_and_scope_error_over_real_http(self):
        project, release = "a"*24, "b"*24
        state = {"archive": None, "config": None, "authorized": 0, "environment": {}, "creates": 0, "deny_preflight": False}
        token = "synthetic-native-client-token"
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, code, body):
                value = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(value)))
                self.end_headers()
                self.wfile.write(value)
            def authenticated(self):
                if self.headers.get("Authorization") != "Bearer "+token:
                    self.respond(401,{"error":"unauthorized"})
                    return False
                state["authorized"] += 1
                return True
            def deployment(self):
                return {"id":release,"project_id":project,"state":"queued","config":state["config"]}
            def do_GET(self):
                if not self.authenticated(): return
                if self.path == "/v1/projects/"+project+"/deployments":
                    self.respond(200,[self.deployment()] if state["archive"] else [])
                elif self.path == "/v1/deployments/"+release:
                    self.respond(200,self.deployment())
                elif self.path.startswith("/v1/projects/"+project+"/env"):
                    self.respond(200,{"keys":sorted(state["environment"])})
                else:
                    self.respond(404,{"error":"not found"})
            def do_POST(self):
                if not self.authenticated(): return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/preflight"):
                    if state["deny_preflight"]:
                        self.respond(429,{"error":"preview capacity reached"})
                        return
                    state["preflight"] = body
                    self.respond(200,{"ok":True,"target":body["configuration"]["target"]})
                    return
                if body.get("target") == "production":
                    self.respond(403,{"error":"preview token"})
                    return
                if self.path.endswith("/redeploy"):
                    state["request_id"]=self.headers.get("Idempotency-Key")
                    self.respond(202,self.deployment() | {"id":"c"*24})
                    return
                state["creates"] += 1
                state["config"] = body
                self.respond(201,self.deployment() | {"upload_url":"/v1/deployments/"+release+"/source"})
            def do_PUT(self):
                if not self.authenticated(): return
                state["archive"] = self.rfile.read(int(self.headers["Content-Length"]))
                self.respond(202,{"state":"queued"})
            def do_PATCH(self):
                if not self.authenticated(): return
                body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state["environment"].update(body.get("values",{}))
                for key in body.get("remove",[]):state["environment"].pop(key,None)
                self.respond(200,{"keys":sorted(state["environment"]),"applies_to":"new deployments"})
        server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread = Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            directory=root/"app with spaces 中文";directory.mkdir()
            (directory/"你好.html").write_text("content",encoding="utf-8")
            (directory/"index.html").write_text("hello",encoding="utf-8")
            credential=root/"login.json"
            origin="http://127.0.0.1:"+str(server.server_port)
            credential.write_text(json.dumps({"token":token,"poteto_url":origin,"project_id":project}),encoding="utf-8-sig")
            def command(*args, status=0):
                out,err=io.StringIO(),io.StringIO()
                with patch("sys.argv",["poteto",*map(str,args)]),redirect_stdout(out),redirect_stderr(err):
                    if status:
                        with self.assertRaises(SystemExit) as ex:cli.main()
                        self.assertEqual(status,ex.exception.code)
                    else:cli.main()
                self.assertNotIn(token,out.getvalue()+err.getvalue())
                return json.loads(out.getvalue()) if out.getvalue() else err.getvalue()
            env={"POTETO_TOKEN_FILE":str(credential),"PAAS_TOKEN_FILE":"","POTETO_TOKEN":"","PAAS_TOKEN":"",
                 "POTETO_URL":"","PAAS_URL":""}
            previous=Path.cwd()
            try:
                os.chdir(directory)
                with patch.dict(os.environ,env):
                    self.assertEqual(project,command("--url",origin,"link")["project_id"])
                    self.assertEqual([],command("ls"))
                    planned = command("plan",".","--remote","--preview","--json")
                    self.assertEqual("preview", planned["remote"]["target"])
                    self.assertIsNone(state["archive"])
                    command("deploy",".","--preview","--no-wait")
                    self.assertEqual("preview",state["config"]["target"])
                    with tarfile.open(fileobj=io.BytesIO(state["archive"]),mode="r:gz") as archive:
                        self.assertEqual(["index.html","你好.html"],archive.getnames())
                    self.assertEqual(release,command("inspect","https://preview-"+release+".page.iflink.tech")["id"])
                    self.assertEqual(1,len(command("ls")))
                    self.assertIn("Run poteto build",command("deploy",".","--prebuilt","--no-wait",status=1))
                    command("deploy", ".", "--local-build", "--preview", "--no-wait")
                    self.assertEqual({"runtime":"static","target":"preview"}, state["config"])
                    self.assertEqual(state["config"], state["preflight"]["configuration"])
                    self.assertEqual(len(state["archive"]), state["preflight"]["archive_bytes"])
                    creates = state["creates"]
                    state["deny_preflight"] = True
                    self.assertIn("preview capacity reached", command("deploy", ".", "--local-build", "--no-wait", status=1))
                    self.assertEqual(creates, state["creates"])
                    state["deny_preflight"] = False
                    value_file=root/"runtime value.txt";value_file.write_text("private test value",encoding="utf-8")
                    self.assertEqual(["EXAMPLE"],command("env","set","EXAMPLE","--file",value_file)["keys"])
                    self.assertEqual("private test value",state["environment"]["EXAMPLE"])
                    with patch("sys.stdin",io.StringIO("new value\r\n")):
                        self.assertEqual(["EXAMPLE"],command("env","set","EXAMPLE","--stdin")["keys"])
                    self.assertEqual("new value",state["environment"]["EXAMPLE"])
                    self.assertEqual(["EXAMPLE"],command("env","list")["keys"])
                    self.assertEqual([],command("env","rm","EXAMPLE")["keys"])
                    cloned=command("redeploy",release,"--no-wait","--request-id","native-check")
                    self.assertEqual("c"*24,cloned["id"])
                    self.assertEqual("native-check",state["request_id"])
                    self.assertIn("HTTP 403",command("deploy",".","--prod","--no-wait",status=1))
                    previous_requests = state["authorized"]
                    snapshot = Path(command("build",".")["artifact"]).read_bytes()
                    (directory/"index.html").write_text("changed source",encoding="utf-8")
                    (directory/"poteto.json").write_text('{"runtime":"node","target":"production"}',encoding="utf-8")
                    prebuilt = command("plan",".","--prebuilt","--json")
                    self.assertEqual(("preview", 2, None), (prebuilt["target"], prebuilt["upload_files"], prebuilt["build_command"]))
                    self.assertEqual(previous_requests, state["authorized"])
                    command("deploy",".","--prebuilt","--no-wait")
                    self.assertEqual({"runtime":"static","target":"preview"},state["config"])
                    self.assertEqual(snapshot,state["archive"])
                    self.assertIn("HTTP 403",command("deploy",".","--prebuilt","--prod","--no-wait",status=1))
                    self.assertTrue(command("unlink")["unlinked"])
            finally:
                os.chdir(previous)
            self.assertGreaterEqual(state["authorized"],7)

    def test_browser_callback_binds_only_loopback_and_checks_state(self):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1",0))
            port=reserve.getsockname()[1]
        redirect=f"http://127.0.0.1:{port}/callback"
        worker=[]
        callbacks=[]
        def open_browser(url):
            parsed=parse_qs(urlsplit(url).query)
            self.assertEqual("S256",parsed["code_challenge_method"][0])
            def complete():
                from urllib.error import HTTPError
                for state in ["wrong",parsed["state"][0]]:
                    try:
                        with urlopen(redirect+"?"+urlencode({"state":state,"code":"native-test-code"}),timeout=3) as response:
                            callbacks.append(response.status)
                    except HTTPError as error:
                        callbacks.append(error.code)
                        error.close()
            t=Thread(target=complete,daemon=True);worker.append(t);t.start()
            return True
        with patch.multiple(cli_login,CALLBACK_PORT=port,REDIRECT_URI=redirect),patch.object(cli_login.webbrowser,"open",side_effect=open_browser):
            result=cli_login.authorize("test-client",timeout=10)
        worker[0].join(timeout=3)
        self.assertEqual([400,200],callbacks)
        self.assertEqual("native-test-code",result["code"])
        self.assertGreaterEqual(len(result["verifier"]),43)


class ArchiveCliTests(unittest.TestCase):
    def command(self, arguments):
        client=Mock(url='https://deploy.iflink.tech')
        client.request.return_value={'id':'a'*24}
        client.import_bundle.return_value={'id':'a'*24}
        output=io.StringIO()
        with patch.dict(os.environ,{'POTETO_TOKEN':'synthetic-cli-secret','POTETO_URL':'https://deploy.iflink.tech'}), \
             patch.object(cli,'_credential_file',return_value=None),patch.object(cli,'find_link',return_value=None), \
             patch.object(cli,'Client',return_value=client),patch('sys.argv',['poteto',*arguments]),redirect_stdout(output):
            cli.main()
        self.assertNotIn('synthetic-cli-secret',output.getvalue())
        return client

    def test_preview_export_resolves_url_to_the_release_identifier(self):
        release='b'*24
        client=self.command(['export','create','c'*24,'--recipient','age1'+'a'*58,
                             '--deployment','https://preview-'+release+'.page.iflink.tech','--no-wait'])
        self.assertEqual(release,client.request.call_args.args[2]['deployment_id'])

    def test_preview_import_is_explicit_and_legacy_default_stays_omitted(self):
        args=['import','create','c'*24,'backup.age','--identity','identity.txt','--no-wait']
        client=self.command(args+['--preview'])
        self.assertEqual('preview',client.import_bundle.call_args.kwargs['target'])
        client=self.command(args)
        self.assertNotIn('target',client.import_bundle.call_args.kwargs)

    def test_failed_import_cleanup_requires_explicit_data_destruction(self):
        operation='c'*24
        client=self.command(['import','delete',operation,'--destroy-data','--no-wait'])
        self.assertEqual(('DELETE','/v1/imports/'+operation+'?destroy_data=true'),client.request.call_args.args)
        client=self.command(['import','delete',operation])
        self.assertEqual(('DELETE','/v1/imports/'+operation),client.request.call_args.args)


if __name__ == '__main__':
    unittest.main()
