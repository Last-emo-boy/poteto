import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import builder_profile, build_source, host


GENERATION = "a" * 24
IMAGE = "docker.io/library/node:24-alpine@sha256:" + "c" * 64
BOOT = "01234567-abcd-abcd-abcd-1234567890ab"


class PreparedBuilderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_profile_lifetime_and_backing_file_validation(self):
        self.assertIsNone(builder_profile.load(self.root, now=3000))
        p = self.root / builder_profile.PROFILE
        value = {"generation": GENERATION, "node_image": IMAGE, "created": 1000}
        p.write_text(json.dumps(value))
        self.assertIsNone(builder_profile.load(self.root, now=1000 + builder_profile.MAX_AGE))
        with self.assertRaisesRegex(ValueError, "base is missing"):
            builder_profile.load(self.root, now=3000)
        disk = builder_profile.base_path(self.root, GENERATION)
        disk.write_bytes(b"prepared")
        disk.chmod(0o644)
        self.assertEqual(value, builder_profile.load(self.root, now=3000))
        disk.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "not writable"):
            builder_profile.load(self.root, now=3000)
        with self.assertRaisesRegex(ValueError, "generation"):
            builder_profile.base_path(self.root, "../runtime0")

    def test_digest_is_only_used_for_generated_static_output(self):
        spec = {"runtime": "static", "output_kind": "static", "output_directory": "dist",
                "build_command": "npm run build", "builder_node_image": IMAGE}
        self.assertIn("FROM " + IMAGE + " AS build", build_source.dockerfile(spec))
        with self.assertRaisesRegex(ValueError, "Invalid prepared"):
            build_source.dockerfile(spec | {"builder_node_image": "node:24-alpine\nRUN whoami"})
        with self.assertRaisesRegex(ValueError, "Invalid prepared"):
            build_source.dockerfile(spec | {"runtime": "node", "output_kind": "image"})
        self.assertIn("FROM node:24-alpine AS build", build_source.dockerfile({"runtime": "node"}))

    def test_guest_setup_transfers_one_bounded_outer_archive_with_opaque_source(self):
        source = self.root / "source.tar.gz"
        source.write_bytes(b"opaque upload")
        spec = {"runtime": "static", "build_command": "npm run build", "output_kind": "static"}

        def receive(command, *, stdin, **kwargs):
            self.assertEqual("22099", command[1])
            self.assertEqual("build-fixture", command[2])
            with tarfile.open(fileobj=stdin, mode="r:") as bundle:
                self.assertEqual(sorted([
                    "opt/paas-execution/guest_build.py", "opt/paas-execution/build_source.py",
                    "opt/paas-execution/publisher/archive.py", "opt/paas-execution/publisher/__init__.py",
                    "work/spec.json", "work/source.tar.gz",
                ]), sorted(bundle.getnames()))
                self.assertEqual(source.read_bytes(), bundle.extractfile("work/source.tar.gz").read())
                self.assertEqual(spec, json.load(bundle.extractfile("work/spec.json")))
            return Mock(stdout=(BOOT + "\n").encode())

        with patch.object(host.subprocess, "run", side_effect=receive) as transport:
            self.assertEqual(BOOT, host.provision_builder("build-fixture", self.root, spec))
            transport.assert_called_once()

    def test_seal_uses_pristine_base_and_stops_docker_before_snapshot(self):
        (self.root / "root.qcow2").write_bytes(b"pristine overlay")
        def command(args, **kwargs):
            if args[0] == "qemu-img":
                Path(args[-1]).write_bytes(b"flattened builder")
            return Mock(stdout=b"success\n")

        with (patch.object(host, "ROOT", self.root), patch.object(host, "admit_space"),
              patch.object(host, "unit_state", return_value="inactive"),
              patch.object(host, "reset_disk", return_value=self.root) as reset,
              patch.object(host, "systemctl"), patch.object(host, "wait_ready"),
              patch.object(host, "ssh", return_value=Mock(stderr=b"prepared")) as remote,
              patch.object(host.subprocess, "run", side_effect=command),
              patch.object(host.secrets, "token_hex", return_value=GENERATION)):
            host.prepare_builder_base(IMAGE)
            reset.assert_called_once_with("builder")
            self.assertIn("--network none", remote.call_args_list[0].args[2])
            self.assertIn(IMAGE.encode(), remote.call_args_list[0].kwargs["data"])
            self.assertIn("disable --now docker.service docker.socket", remote.call_args_list[1].args[2])
            self.assertIn("--machine-id", remote.call_args_list[1].args[2])
        self.assertEqual(IMAGE, builder_profile.load(self.root)["node_image"])
        self.assertEqual(b"flattened builder", builder_profile.base_path(self.root, GENERATION).read_bytes())
