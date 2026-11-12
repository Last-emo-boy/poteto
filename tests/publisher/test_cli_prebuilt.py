import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from publisher.cli_prebuilt import build_static, read_prebuilt
from publisher.cli_tools import PlanError


class LocalStaticBuildTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.credential = self.root / "credential"
        self.output = self.root / "dist"
        self.artifact = self.root / ".poteto" / "output.tar.gz"
        (self.root / "index.html").write_text("raw source", encoding="utf-8")
        (self.root / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8")
        (self.root / "package-lock.json").write_text("{}", encoding="utf-8")
        (self.root / "poteto.json").write_text('{"preset":"vite","target":"production"}', encoding="utf-8")

    def build(self):
        return build_static(self.root, None, self.credential)

    def test_packages_only_output_and_does_not_require_source_to_publish(self):
        def run(directory, command):
            self.assertEqual((self.root, "npm run build"), (directory, command))
            self.output.mkdir()
            (self.output / "index.html").write_text("compiled", encoding="utf-8")
            (self.output / ".env").write_text("synthetic secret", encoding="utf-8")
            self.credential.write_text("synthetic token", encoding="utf-8")

        self.credential = self.output / self.credential.name
        with patch("publisher.cli_prebuilt._run_build", side_effect=run):
            result = self.build()
        (self.root / "poteto.json").unlink()
        report, data = read_prebuilt(self.root)
        self.assertEqual(str(self.artifact), result["artifact"])
        self.assertEqual(("preview", 1, len(data)), (report["target"], report["upload_files"], report["upload_bytes"]))
        self.assertEqual("production", read_prebuilt(self.root, production=True)[0]["target"])
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            self.assertEqual(["index.html"], archive.getnames())
            self.assertEqual(b"compiled", archive.extractfile("index.html").read())

    def test_failed_build_cannot_leave_a_publishable_stale_artifact(self):
        self.artifact.parent.mkdir()
        self.artifact.write_bytes(b"previous output")
        with patch("publisher.cli_prebuilt._run_build", side_effect=PlanError("local_build_failed", "failed")):
            with self.assertRaisesRegex(PlanError, "failed"):
                self.build()
        with self.assertRaisesRegex(PlanError, "Run poteto build"):
            read_prebuilt(self.root)

    def test_build_rejects_source_root_as_generated_output(self):
        (self.root / "poteto.json").write_text('{"preset":"vite","output_directory":"."}', encoding="utf-8")
        with patch("publisher.cli_prebuilt._run_build") as run:
            with self.assertRaisesRegex(PlanError, "separate from source"):
                self.build()
            run.assert_not_called()

    def test_missing_index_in_transferred_artifact_is_rejected(self):
        self.artifact.parent.mkdir()
        with tarfile.open(self.artifact, mode="w:gz") as archive:
            archive.add(self.root / "index.html", arcname="dist/index.html")
        with self.assertRaisesRegex(PlanError, "index.html at its root"):
            read_prebuilt(self.root)
