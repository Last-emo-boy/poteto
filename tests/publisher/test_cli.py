from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from publisher import cli
from publisher.cli import pack
from publisher.cli_tools import VERSION, PlanError, deployment_config, initialize, plan_deployment


class CLITests(unittest.TestCase):
    def invoke(self, *args, prog="poteto", status=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", [prog, *map(str, args)]), redirect_stdout(stdout), redirect_stderr(stderr):
            if status:
                with self.assertRaises(SystemExit) as error:
                    cli.main(prog)
                self.assertEqual(status, error.exception.code)
            else:
                cli.main(prog)
        return stdout.getvalue(), stderr.getvalue()

    def test_shell_entry_point_loads_import_client_outside_the_checkout(self):
        root = Path(__file__).resolve().parents[2]
        env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.check_output([root/'scripts/paas','import','resume','--help'],cwd=cwd,env=env,text=True)
        self.assertIn('--identity',result)

    def test_pack_excludes_secrets_links_and_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text("hello")
            (root / ".env").write_text("SECRET=private")
            (root / "secrets").mkdir()
            (root / "secrets/key").write_text("private")
            (root / "node_modules").mkdir()
            (root / "node_modules/dependency.js").write_text("ignored")
            (root / "link").symlink_to("/etc/passwd")
            with tarfile.open(fileobj=io.BytesIO(pack(root)), mode="r:gz") as tar:
                self.assertEqual(["index.html"], tar.getnames())

    def test_pack_respects_gitignored_files_at_project_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("private-key.pem\n")
            (root / "private-key.pem").write_text("do not upload")
            (root / "index.html").write_text("hello")
            with tarfile.open(fileobj=io.BytesIO(pack(root)), mode="r:gz") as tar:
                self.assertEqual([".gitignore", "index.html"], tar.getnames())

    def test_pack_excludes_the_active_token_file_even_when_tracked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "index.html").write_text("hello")
            (root / "credentials").mkdir()
            token = root / "credentials/deploy.json"
            token.write_text('{"token":"paas_test_only"}')
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            for path in (str(token), os.path.relpath(token)):
                with self.subTest(path=path), patch.dict(os.environ, {"PAAS_TOKEN_FILE": path}):
                    with tarfile.open(fileobj=io.BytesIO(pack(root)), mode="r:gz") as tar:
                        self.assertEqual(["index.html"], tar.getnames())

    def test_pack_does_not_exclude_a_source_file_with_the_token_basename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"
            root.mkdir()
            token = Path(temp) / "token.json"
            token.write_text('{"token":"paas_test_only"}')
            (root / "token.json").write_text('{"application_data":true}')
            with patch.dict(os.environ, {"PAAS_TOKEN_FILE": str(token)}):
                with tarfile.open(fileobj=io.BytesIO(pack(root)), mode="r:gz") as tar:
                    self.assertEqual(["token.json"], tar.getnames())

    def test_scaffold_and_legacy_manifest_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for runtime, expected in [
                ("static", {"index.html", "poteto.json"}),
                ("node", {"package.json", "package-lock.json", "server.mjs", "poteto.json"}),
                ("dockerfile", {"Dockerfile", "server.mjs", "poteto.json"}),
            ]:
                path = root / runtime
                self.assertEqual(expected, set(initialize(path, runtime)["files"]))
                self.assertEqual({"runtime": runtime, "target": "preview", "sleep": True}.items(),
                                 {k: deployment_config(path, None)[k] for k in ("runtime", "target", "sleep")}.items())
                with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                    initialize(path, runtime)
            path = root / "static"
            (path / "paas.json").write_text('{"runtime":"node"}')
            self.assertEqual({"runtime": "node"}, deployment_config(path, None))
            self.assertEqual("static", deployment_config(path, path / "poteto.json")["runtime"])

    def test_offline_static_plan_does_not_read_credentials_or_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text("hello")
            (root / ".env").write_text("PRIVATE=value")
            token = root / "bad-token.json"
            token.write_text("{not valid JSON")
            with patch.dict(os.environ, {"POTETO_TOKEN_FILE": str(token)}), patch.object(cli, "Client") as client:
                out, err = self.invoke("plan", root, "--preview", "--json")
                client.assert_not_called()
            report = json.loads(out)
            self.assertEqual("", err)
            self.assertEqual(("static", ".", "preview", 1),
                             tuple(report[key] for key in ("runtime", "public_directory", "target", "upload_files")))
            self.assertEqual(str(root.resolve()), report["directory"])

    def test_static_output_is_selected_without_changing_the_upload_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dist = root / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("generated")
            (root / "poteto.json").write_text('{"output_directory":"./dist","target":"production"}')
            (root / "paas.json").write_text('{"output_directory":"dist","target":"production"}')
            old = plan_deployment(root, None)
            override = plan_deployment(root, root / "poteto.json", preview=True)
            self.assertEqual(("paas.json", "production"), (old.origin, old.summary()["target"]))
            self.assertEqual(("poteto.json", "./dist", "preview"),
                             (override.origin, override.summary()["public_directory"], override.summary()["target"]))
            self.assertIn("dist/index.html", override.files)
            self.assertEqual(["index.html"], plan_deployment(dist, None).files)

    def test_detect_node_and_dockerfile_without_moving_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"scripts":{"start":"node app.js","build":"node compile.js"}}')
            with self.assertRaises(PlanError) as error:
                plan_deployment(root, None)
            self.assertEqual("missing_npm_input", error.exception.code)
            (root / "package-lock.json").write_text("{}")
            node = plan_deployment(root, None).summary()
            self.assertEqual(("node", "npm run build", "npm start", "preview"),
                             tuple(node[k] for k in ("runtime", "build_command", "start_command", "target")))
            self.assertIsNone(node["public_directory"])
            (root / "Dockerfile").write_text("FROM scratch")
            docker = plan_deployment(root, None).summary()
            self.assertEqual(("dockerfile", "Dockerfile", "preview"),
                             tuple(docker[k] for k in ("runtime", "dockerfile", "target")))

    def test_ambiguous_vite_is_not_published_and_legacy_still_uses_its_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text('<script type="module" src="/src.jsx"></script>')
            (root / "package.json").write_text('{"scripts":{"dev":"vite"},"devDependencies":{"vite":"1.0.0"}}')
            with patch.dict(os.environ, {"PAAS_TOKEN": "test-only", "POTETO_TOKEN_FILE": "", "PAAS_TOKEN_FILE": ""}), \
                    patch.object(cli, "Client") as client:
                out, err = self.invoke("plan", root, "--json", status=1)
                self.assertEqual("", err)
                problem = json.loads(out)["error"]
                self.assertEqual("ambiguous_runtime", problem["code"])
                self.assertIn("output directory", problem["message"])
                _, err = self.invoke("deploy", root, "--project", "project", status=1)
                self.assertIn(problem["message"], err)
                client.return_value.request.assert_not_called()
                api = client.return_value
                api.request.side_effect = [{"id": "release", "upload_url": "/upload"}, None]
                api.wait.return_value = {"state": "ready"}
                self.invoke("deploy", root, "--project", "project", prog="paas")
                api.request.assert_any_call("POST", "/v1/projects/project/deployments", {"runtime": "static"})

    def test_unambiguous_vite_plan_matches_uploaded_spec_and_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text("source")
            (root / "package.json").write_text('{"scripts":{"build":"vite build"},"devDependencies":{"vite":"1.0.0"}}')
            (root / "package-lock.json").write_text("{}")
            with patch.dict(os.environ, {"PAAS_TOKEN": "test-only", "POTETO_TOKEN_FILE": "", "PAAS_TOKEN_FILE": ""}), \
                    patch.object(cli, "Client") as client:
                out, _ = self.invoke("plan", root, "--json")
                plan = json.loads(out)
                self.assertEqual(("vite", "static", "dist"), (plan["origin"], plan["runtime"], plan["public_directory"]))
                api = client.return_value
                api.request.side_effect = [{"id": "release", "upload_url": "/upload"}, None]
                api.wait.return_value = {"url": "https://preview.test"}
                out, _ = self.invoke("deploy", root, "--project", "project", "--url-only")
                self.assertEqual("https://preview.test", out.strip())
                spec = api.request.call_args_list[0].args[2]
                self.assertEqual({"runtime": "static", "build_command": "npm run build",
                                  "output_directory": "dist", "target": "preview"}, spec)
                with tarfile.open(fileobj=io.BytesIO(api.request.call_args_list[1].args[2]), mode="r:gz") as archive:
                    self.assertEqual(["index.html", "package-lock.json", "package.json"], archive.getnames())
            package = root / "package.json"
            original = package.read_text()
            package.write_text(original.replace('"vite build"', '"vite build","postbuild":"mv dist public-site"'))
            with self.assertRaises(PlanError) as error:
                plan_deployment(root, None)
            self.assertEqual("ambiguous_runtime", error.exception.code)
            package.write_text(original)
            (root / "vite.config.ts").write_text("export default { build: { outDir: 'public-site' } }")
            with self.assertRaises(PlanError) as error:
                plan_deployment(root, None)
            self.assertEqual("ambiguous_runtime", error.exception.code)
            (root / "poteto.json").write_text('{"build_command":"npm run build","output_directory":"public-site"}')
            self.assertEqual("public-site", plan_deployment(root, None).summary()["public_directory"])

    def test_plan_rejects_ignored_inputs_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("dist/\n")
            (root / "dist").mkdir()
            (root / "dist" / "index.html").write_text("built")
            manifest = root / "poteto.json"
            for value, code in [
                ('{"output_directory":"dist"}', "missing_entrypoint"),
                ('{"output_directory":"../dist"}', "invalid_path"),
                ('["runtime","static"]', "invalid_json"),
            ]:
                with self.subTest(code=code):
                    manifest.write_text(value)
                    out, _ = self.invoke("plan", root, "--json", status=1)
                    self.assertEqual(code, json.loads(out)["error"]["code"])
            out, _ = self.invoke("plan", root, "--config", root / "missing.json", "--json", status=1)
            self.assertEqual("missing_config", json.loads(out)["error"]["code"])
            self.assertEqual(["index.html"], plan_deployment(root / "dist", None).files)

    def test_url_only_and_diagnostics_do_not_print_bearer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize(root, "static")
            project = "1" * 24
            credential = root / "token.json"
            secret = "paas_test_only_not_printed"
            credential.write_text(json.dumps({"poteto_url": "https://deploy.w33d.xyz", "project_id": project, "token": secret}))
            url = "https://preview-test.poteto.best"
            requests = []
            class TestClient:
                def __init__(self, api, token=None):
                    self.url = api
                def request(self, method, path, body=None, raw=False):
                    requests.append((method, path, body))
                    if path == "/v1/me":
                        return {"project_ids": [project], "scopes": ["deployments:preview"]}
                    if path.endswith("/deployments") and method == "GET":
                        return [{"url": url}]
                    if path.endswith("/deployments"):
                        return {"id": "release", "upload_url": "/upload"}
                def wait(self, deployment, timeout):
                    return {"id": deployment, "url": url}
            def command(*args):
                out = io.StringIO()
                with patch("sys.argv", ["poteto", *args]), redirect_stdout(out):
                    cli.main()
                self.assertNotIn(secret, out.getvalue())
                return out.getvalue()
            with (patch.dict(os.environ, {"POTETO_TOKEN_FILE": str(credential), "PAAS_URL": "https://deploy.w33d.xyz"}),
                  patch("publisher.cli.Client", TestClient)):
                self.assertEqual(url, command("deploy", str(root), "--url-only").strip())
                self.assertEqual("preview", requests[0][2]["target"])
                self.assertEqual([project], json.loads(command("doctor"))["project_ids"])
                self.assertEqual([{"url": url}], json.loads(command("deployments")))
                self.assertEqual(url, json.loads(command("logs", "release", "--follow"))["url"])

    def test_build_and_install_standalone_cli(self):
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dist = root / "dist"
            subprocess.run([repo/"scripts/build-poteto-dist", dist, "--url", "https://download.test"],
                           check=True, capture_output=True)
            archive = dist / f"poteto-{VERSION}.pyz"
            skill = Path("skills/poteto-deploy/SKILL.md")
            self.assertEqual((repo / skill).read_text().replace(
                "https://poteto.iflink.tech", "https://download.test"), (dist / skill).read_text())
            self.assertIn("/" + skill.as_posix(), (dist / "index.html").read_text())
            with zipfile.ZipFile(archive) as packed:
                self.assertNotIn("publisher/api.py", packed.namelist())
                self.assertNotIn("publisher/oidc_login.py", packed.namelist())
                self.assertFalse(any("secrets" in name for name in packed.namelist()))
            fake_bin = root / "tools"
            fake_bin.mkdir()
            curl = fake_bin / "curl"
            curl.write_text('#!/bin/sh\n[ "$POTETO_TEST_CURL_FAILURE" != 1 ] || exit 22\n'
                            'last=\nfor v do last=$v; done\ncp "$POTETO_TEST_ARCHIVE" "$last"\n')
            curl.chmod(0o755)
            install = root / "bin"
            env = os.environ | {"PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                                "POTETO_TEST_ARCHIVE": str(archive), "POTETO_TEST_CURL_FAILURE": "0",
                                "POTETO_INSTALL_DIR": str(install)}
            subprocess.run(["sh", dist/"install.sh"], cwd=root, env=env, check=True, capture_output=True)
            installed = install / "poteto"
            self.assertEqual(f"poteto {VERSION}", subprocess.check_output([installed, "--version"], text=True, cwd=root).strip())
            report = subprocess.check_output([installed, "plan", dist, "--preview", "--json"], cwd=root, env=env)
            self.assertEqual("static", json.loads(report)["runtime"])
            failed = subprocess.run(["sh", dist/"install.sh"], cwd=root,
                                    env=env | {"POTETO_TEST_CURL_FAILURE": "1"}, capture_output=True)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(archive.read_bytes(), installed.read_bytes())

    def test_distribution_can_embed_api_origin_without_changing_source_defaults(self):
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            subprocess.run([repo/"scripts/build-poteto-dist", root/"dist", "--url", "https://install.test",
                            "--api-url", "https://api.test"], check=True, capture_output=True)
            archive=root/"dist"/f"poteto-{VERSION}.pyz"
            code="import sys; sys.path.insert(0,sys.argv[1]); from publisher.login_contract import DEFAULT_URL; print(DEFAULT_URL)"
            result=subprocess.check_output(["python3","-c",code,str(archive)],cwd=root,text=True)
            self.assertEqual("https://api.test",result.strip())
            self.assertIn('DEFAULT_URL = "https://deploy.w33d.xyz"', (repo/"publisher/login_contract.py").read_text())


if __name__ == "__main__":
    unittest.main()
