from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from publisher import cli
from publisher.cli_prebuilt import build_local
from publisher.cli_tools import PlanError


class LocalImageBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "app with spaces 中文"
        self.root.mkdir()
        self.credential = self.root / "credential.json"
        self.credential.write_text("synthetic secret")
        (self.root / ".env").write_text("PRIVATE=synthetic")
        self.artifact = self.root / ".poteto" / "image.tar.gz"

    def node(self):
        (self.root / "package.json").write_text(json.dumps({
            "scripts": {"build": "node compile.mjs", "start": "node server.mjs"},
            "dependencies": {"next": "fixture"},
        }))
        (self.root / "package-lock.json").write_text("{}")
        (self.root / "server.mjs").write_text("fixture")

    def package(self, root, reference, credential):
        self.assertEqual((self.root, self.credential), (root, credential))
        self.assertTrue(reference.startswith("poteto-local:"))
        self.artifact.parent.mkdir(exist_ok=True)
        with tarfile.open(self.artifact, "w:gz") as archive:
            for name, body in {
                "manifest.json": b'[{"Config":"config.json","Layers":[]}]',
                "config.json": b'{"os":"linux","architecture":"amd64","rootfs":{"type":"layers","diff_ids":[]}}',
            }.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))

    def test_node_build_uses_shared_recipe_sanitized_context_and_runtime_defaults(self):
        self.node()
        (self.root / "poteto.json").write_text('{"preset":"nextjs","database_id":"db","migration_command":"node migrate.mjs"}')
        observed = []

        def run(command, **kwargs):
            observed.append(command)
            self.assertNotIn("POTETO_TOKEN", kwargs["env"])
            self.assertNotIn("POTETO_TOKEN_FILE", kwargs["env"])
            if command[1:3] == ["buildx", "build"]:
                context = Path(command[-1])
                self.assertTrue((context / "server.mjs").is_file())
                self.assertFalse((context / ".env").exists())
                self.assertFalse((context / "credential.json").exists())
                self.assertIn("--platform", command)
                self.assertEqual("linux/amd64", command[command.index("--platform") + 1])
                recipe = Path(command[command.index("--file") + 1]).read_text()
                self.assertIn('RUN ["npm", "ci"]', recipe)
                self.assertIn("USER 10001:10001", recipe)
                self.assertIn('CMD ["sh", "-c", "npm start"]', recipe)
            return SimpleNamespace(returncode=0)

        with patch("publisher.cli_prebuilt.shutil.which", return_value="docker"), \
             patch("publisher.cli_prebuilt.subprocess.run", side_effect=run), \
             patch("publisher.cli_prebuilt.package_image", side_effect=self.package), \
             patch.dict(os.environ, {"POTETO_TOKEN": "synthetic", "POTETO_TOKEN_FILE": str(self.credential)}):
            report, data = build_local(self.root, None, self.credential)
        self.assertEqual({"runtime": "image", "target": "preview", "memory_mb": 512,
                          "database_id": "db", "migration_command": "node migrate.mjs"}, report["configuration"])
        self.assertEqual(self.artifact.read_bytes(), data)
        self.assertFalse(Path(observed[0][-1]).exists())
        self.assertEqual(["docker", "image", "rm", "--no-prune", "--", observed[0][observed[0].index("--tag") + 1]], observed[1])

    def test_web_policy_survives_local_image_packaging(self):
        self.node()
        (self.root/'poteto.json').write_text('{"runtime":"node","outbound_network":"web"}')
        with patch("publisher.cli_prebuilt.shutil.which",return_value="docker"), \
             patch("publisher.cli_prebuilt.subprocess.run",return_value=SimpleNamespace(returncode=0)), \
             patch("publisher.cli_prebuilt.package_image",side_effect=self.package):
            report,_=build_local(self.root,None,self.credential)
        self.assertEqual('web',report['configuration']['outbound_network'])
        self.assertEqual('image',report['configuration']['runtime'])

    def test_custom_dockerfile_path_is_not_shell_interpreted(self):
        (self.root / "docker files").mkdir()
        recipe = "FROM scratch\nCOPY server /server\n"
        (self.root / "docker files" / "app.Dockerfile").write_text(recipe)
        (self.root / "poteto.json").write_text('{"runtime":"dockerfile","dockerfile":"docker files/app.Dockerfile","port":8080}')
        with patch("publisher.cli_prebuilt.shutil.which", return_value="docker"), \
             patch("publisher.cli_prebuilt.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run, \
             patch("publisher.cli_prebuilt.package_image", side_effect=self.package):
            report, _ = build_local(self.root, None, self.credential, production=True)
        args = run.call_args_list[0].args[0]
        self.assertEqual(("docker files", "app.Dockerfile"), Path(args[args.index("--file") + 1]).parts[-2:])
        self.assertNotIn("shell", run.call_args_list[0].kwargs)
        self.assertEqual({"runtime": "image", "target": "production", "port": 8080}, report["configuration"])

    def test_failure_removes_stale_image_and_never_packages_or_falls_back(self):
        self.node()
        for docker, exit_code in [(None, 0), ("docker", 7)]:
            with self.subTest(docker=docker):
                self.artifact.parent.mkdir(exist_ok=True)
                self.artifact.write_bytes(b"previous image")
                with patch("publisher.cli_prebuilt.shutil.which", return_value=docker), \
                     patch("publisher.cli_prebuilt.subprocess.run", return_value=SimpleNamespace(returncode=exit_code)), \
                     patch("publisher.cli_prebuilt.package_image") as package:
                    with self.assertRaises(PlanError) as failure:
                        build_local(self.root, None, self.credential)
                self.assertEqual("missing_docker" if docker is None else "local_build_failed", failure.exception.code)
                self.assertFalse(self.artifact.exists())
                package.assert_not_called()

    def test_failed_image_export_cleans_only_the_temporary_tag(self):
        self.node()
        with patch("publisher.cli_prebuilt.shutil.which", return_value="docker"), \
             patch("publisher.cli_prebuilt.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run, \
             patch("publisher.cli_prebuilt.package_image", side_effect=PlanError("image_save_failed", "fixture")):
            with self.assertRaisesRegex(PlanError, "fixture"):
                build_local(self.root, None, self.credential)
        self.assertEqual(["docker", "image", "rm", "--no-prune", "--"], run.call_args.args[0][:-1])

    def test_image_deploy_stops_before_creation_for_both_build_paths(self):
        config = {"runtime": "image", "target": "preview", "memory_mb": 512}
        report = {"configuration": config, "target": "preview", "unpacked_bytes": 30,
                  "upload_files": 2, "normalized_image_bytes": 170}
        for flag in [("--local-build",), ("--prebuilt-image", str(self.artifact))]:
            with self.subTest(flag=flag), patch.dict(os.environ, {"POTETO_TOKEN": "synthetic", "POTETO_TOKEN_FILE": "", "PAAS_TOKEN_FILE": ""}), \
                 patch("sys.argv", ["poteto", "deploy", str(self.root), "--project", "a" * 24, *flag, "--no-wait"]), \
                 patch("publisher.cli._credential_file", return_value=None), \
                 patch("publisher.cli.build_local", return_value=(report, b"image")), \
                 patch("publisher.cli.read_image", return_value=(report, b"image")), \
                 patch.object(cli.Client, "request", side_effect=RuntimeError("HTTP 429: Artifact storage quota")) as request, \
                 redirect_stderr(io.StringIO()) as stderr, redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    cli.main()
                self.assertEqual(1, result.exception.code)
                self.assertIn("storage quota", stderr.getvalue())
                request.assert_called_once_with("POST", "/v1/projects/" + "a" * 24 + "/preflight",
                    {"configuration": config, "archive_bytes": 5, "unpacked_bytes": 30, "files": 2,
                     "normalized_image_bytes": 170})


if __name__ == "__main__":
    unittest.main()
