from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from publisher import cli
from publisher.api import create_app
from publisher.cli_login import default_token_file
from publisher.cli_project import api_origin, deployment_reference, find_link, unlink, write_link
from publisher.cli_tools import pack, plan_deployment
from publisher.config import Settings
from publisher.store import Store


class ProjectCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "my app 中文"
        self.directory.mkdir()
        (self.directory / "index.html").write_text("<h1>测试</h1>", encoding="utf-8")
        settings = Settings(self.root / "control", new_app_domain="page.iflink.tech")
        store = Store(settings)
        owner = store.authenticate(store.create_account("owner")["token"])
        self.project = store.create_project(owner, "first")["id"]
        self.other = store.create_project(owner, "second")["id"]
        self.url = "https://deploy.example.test"
        self.api = TestClient(create_app(settings))
        self.addCleanup(self.api.close)
        with store.connection() as db:
            self.secret = store.mint_token(db, owner, name="preview", scopes=["deployments:preview"],
                project_ids=[self.project], expires=9999999999)["token"]
        self.credential = self.root / "credential.json"
        self.credential.write_text(json.dumps({"token": self.secret, "poteto_url": self.url,
                                               "project_id": self.project}), encoding="utf-8-sig")
        environment = patch.dict(os.environ, {"PAAS_URL": "", "POTETO_URL": "", "PAAS_TOKEN": "",
            "POTETO_TOKEN": "", "PAAS_TOKEN_FILE": "", "POTETO_TOKEN_FILE": str(self.credential)})
        environment.start()
        self.addCleanup(environment.stop)
        api = self.api
        class LocalClient(cli.Client):
            def request(self, method, path, body=None, raw=False):
                headers = {"Authorization": "Bearer " + self.token} if self.token else {}
                if raw:
                    response = api.request(method, path, content=body, headers=headers | {"Content-Type": "application/gzip"})
                else:
                    response = api.request(method, path, json=body, headers=headers)
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
                return response.json() if response.content else None
        client = patch.object(cli, "Client", LocalClient)
        client.start()
        self.addCleanup(client.stop)
        self.cwd = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, self.cwd)

    def command(self, *args, status=0):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["poteto", *map(str,args)]), redirect_stdout(out), redirect_stderr(err):
            if status:
                with self.assertRaises(SystemExit) as raised:
                    cli.main()
                self.assertEqual(status, raised.exception.code)
            else:
                cli.main()
        self.assertNotIn(self.secret, out.getvalue() + err.getvalue())
        return (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def link(self):
        return self.command("--url", self.url, "link")[0]

    def test_preview_token_can_link_then_deploy_and_list_from_nested_directory(self):
        linked = self.link()
        self.assertEqual(self.project, linked["project_id"])
        self.assertNotIn("token", (self.directory/".poteto/project.json").read_text())
        child = self.directory / "dist"
        child.mkdir()
        (child/"index.html").write_text("child", encoding="utf-8")
        os.chdir(child)
        self.assertEqual([], self.command("ls")[0])
        release = self.command("deploy", ".", "--no-wait")[0]
        self.assertEqual("preview", release["config"]["target"])
        self.assertEqual(self.project, release["project_id"])
        with tarfile.open(fileobj=io.BytesIO(pack(self.directory)), mode="r:gz") as archive:
            self.assertFalse(any(".poteto" in name for name in archive.getnames()))

    def test_link_preserves_existing_binding_and_unlink_does_not_touch_remote_project(self):
        self.link()
        before = (self.directory/".poteto/project.json").read_bytes()
        self.command("--url",self.url,"link",status=1)
        self.assertEqual(before,(self.directory/".poteto/project.json").read_bytes())
        (self.directory/".poteto/notes.txt").write_text("keep",encoding="utf-8")
        self.assertTrue(self.command("unlink")[0]["unlinked"])
        self.assertFalse(self.command("unlink")[0]["unlinked"])
        self.assertTrue((self.directory/".poteto/notes.txt").exists())
        self.assertEqual([],self.command("--url",self.url,"deployments")[0])

    def test_link_denies_another_project_without_leaving_local_state(self):
        self.command("--url",self.url,"link","--project",self.other,status=1)
        self.assertFalse((self.directory/".poteto/project.json").exists())

    def test_explicit_endpoint_does_not_reuse_link_or_send_wrong_login_token(self):
        self.link()
        _, error = self.command("--url","https://other.example.test","ls",status=1)
        self.assertIn("different API",error)
        with patch.dict(os.environ,{"POTETO_TOKEN_FILE":"","POTETO_TOKEN":self.secret}):
            _, error = self.command("--url","https://other.example.test","ls",status=2)
        self.assertIn("--project",error)

    def test_production_is_explicit_and_preview_token_is_still_denied(self):
        self.link()
        production,_ = self.command("plan",".","--prod","--json")
        self.assertEqual("production",production["target"])
        _,error=self.command("deploy",".","--prod","--no-wait",status=1)
        self.assertIn("HTTP 403",error)
        self.command("plan",".","--preview","--prod",status=2)

    def test_inspect_accepts_preview_url_and_logs_keep_the_existing_id_contract(self):
        self.link()
        release=self.command("deploy",".","--no-wait")[0]
        url="https://preview-"+release["id"]+".page.iflink.tech/path?q=x"
        self.assertEqual(release["id"],self.command("inspect",url)[0]["id"])
        self.assertEqual(release["id"],self.command("status",release["id"])[0]["id"])
        self.assertIn("logs",self.command("logs",url)[0])

    def test_bom_config_and_unicode_source_are_portable(self):
        (self.directory/"poteto.json").write_text('{"runtime":"static","target":"preview"}',encoding="utf-8-sig")
        (self.directory/"你好.txt").write_text("hello",encoding="utf-8")
        self.assertIn("你好.txt",plan_deployment(self.directory,None).files)
        report,_ = self.command("plan",".","--json")
        self.assertEqual("preview",report["target"])

    def test_offline_plan_does_not_read_even_a_corrupt_default_credential(self):
        self.link()
        self.credential.write_text("{broken",encoding="utf-8")
        with patch.dict(os.environ,{"POTETO_TOKEN_FILE":""}),patch.object(cli,"default_token_file",return_value=self.credential):
            self.assertTrue(self.command("plan",".","--json")[0]["ok"])


class LocalLinkTests(unittest.TestCase):
    def test_link_stops_at_nested_git_boundary_and_unlink_is_local(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            write_link(root,"https://api.example.test","a"*24)
            nested=root/"other";nested.mkdir();(nested/".git").write_text("gitdir: other")
            self.assertIsNone(find_link(nested))
            self.assertFalse(unlink(nested)["unlinked"])
            self.assertEqual("a"*24,find_link(root).project_id)

    def test_invalid_links_are_clear_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/".poteto").mkdir()
            for value in [[],{"version":1,"project_id":None},{"version":1,"project_id":"a"*24,"url":None}]:
                (root/".poteto/project.json").write_text(json.dumps(value))
                with self.assertRaises(ValueError):find_link(root)
        for value in ["http://public.example","https://user:password@api.test","https://api.test/path"]:
            with self.assertRaises(ValueError):api_origin(value)
        self.assertEqual("http://127.0.0.1:8000",api_origin("http://127.0.0.1:8000/"))

    def test_references_use_ids_without_requesting_the_supplied_host(self):
        self.assertEqual(("deployment","a"*24),deployment_reference("https://preview-"+ "a"*24+".page.iflink.tech"))
        self.assertEqual(("project","a"*24),deployment_reference("https://app-"+ "a"*24+".page.iflink.tech"))
        for value in ["https://unrelated.example","http://preview-"+ "a"*24+".page.iflink.tech"]:
            with self.assertRaises(ValueError):deployment_reference(value)
        self.assertEqual(("deployment","old-id"),deployment_reference("old-id"))

    def test_windows_credential_location_keeps_an_existing_legacy_login(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch("publisher.cli_login.Path.home",return_value=root),patch("sys.platform","win32"), \
                    patch.dict(os.environ,{"LOCALAPPDATA":str(root/"Local")}):
                self.assertEqual(root/"Local/Poteto/token.json",default_token_file())
                legacy=root/".config/poteto/token.json";legacy.parent.mkdir(parents=True);legacy.write_text("{}")
                self.assertEqual(legacy,default_token_file())


if __name__ == "__main__":
    unittest.main()
