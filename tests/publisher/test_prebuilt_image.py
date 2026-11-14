from dataclasses import replace
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from execution import host, prebuilt_image, sandbox
from publisher.api import create_app
from publisher.cli_prebuilt import read_image
from publisher.config import Settings
from publisher.models import DeploymentInput
from publisher.store import Store

RELEASE = "a" * 24


def archive(files, mode="w:gz"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode=mode) as output:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            output.addfile(info, io.BytesIO(body))
    return data.getvalue()


LAYER = archive({"server": b"sample fixture"}, mode="w:")


def docker_image(*, arch="amd64", layer=LAYER, multiple=False):
    config = {"os": "linux", "architecture": arch,
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(LAYER).hexdigest()]}}
    entry = {"Config": "blobs/sha256/config", "Layers": ["blobs/sha256/layer"],
             "RepoTags": ["unrelated:latest", "another:tag"]}
    return archive({"manifest.json": json.dumps([entry] * (2 if multiple else 1)).encode(),
                    "blobs/sha256/config": json.dumps(config).encode(), "blobs/sha256/layer": layer,
                    "index.json": b'{"ignored":true}'})


class PrebuiltImageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "image.tar.gz"
        self.output = self.root / "canonical.tar"

    def test_only_canonical_tag_and_layers_survive(self):
        self.source.write_bytes(docker_image(layer=gzip.compress(LAYER)))
        receipt = prebuilt_image.normalize(self.source, self.output, RELEASE)
        self.assertEqual(len(LAYER), receipt["image_disk_bytes"])
        with tarfile.open(self.output) as data:
            spec, = json.load(data.extractfile("manifest.json"))
            self.assertEqual(["paas-release:" + RELEASE], spec["RepoTags"])
            self.assertEqual(LAYER, data.extractfile(spec["Layers"][0]).read())
            index = json.load(data.extractfile("index.json"))
            descriptor, = index['manifests']
            self.assertEqual('docker.io/library/paas-release:' + RELEASE,
                             descriptor['annotations']['io.containerd.image.name'])
            manifest = 'blobs/sha256/' + descriptor['digest'].split(':')[1]
            self.assertEqual(descriptor['size'], data.getmember(manifest).size)
            self.assertEqual({spec["Config"], *spec["Layers"], manifest, "manifest.json", "index.json", "oci-layout"}, set(data.getnames()))
            self.assertEqual(['sha256:' + spec['Config'].split('/')[-1], descriptor['digest']],
                             sandbox.image_identities(self.output, 'paas-release:' + RELEASE))

    def test_duplicate_layers_are_validated_but_written_once(self):
        config = {"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers",
                  "diff_ids": ["sha256:" + hashlib.sha256(LAYER).hexdigest()] * 2}}
        self.source.write_bytes(archive({"config.json": json.dumps(config).encode(), "layer.tar": LAYER,
                                        "manifest.json": b'[{"Config":"config.json","Layers":["layer.tar","layer.tar"]}]'}))
        receipt = prebuilt_image.normalize(self.source, self.output, RELEASE)
        self.assertEqual(2 * len(LAYER), receipt['image_disk_bytes'])
        with tarfile.open(self.output) as image:
            manifest, = json.load(image.extractfile('manifest.json'))
            self.assertEqual(manifest['Layers'][0], manifest['Layers'][1])
            self.assertEqual(len(image.getnames()), len(set(image.getnames())))

    def test_rejects_mismatched_platform_multiple_images_and_damaged_layers(self):
        for content, error in [
            (docker_image(arch="arm64"), "linux/amd64"),
            (docker_image(multiple=True), "exactly one"),
            (docker_image(layer=b"corrupt"), "digest"),
            (archive({"manifest.json": b'[{"Config":"../settings"}]'}), "member path"),
        ]:
            with self.subTest(error=error):
                self.source.write_bytes(content)
                with self.assertRaisesRegex(prebuilt_image.ImageError, error):
                    prebuilt_image.normalize(self.source, self.output, RELEASE)
                self.assertFalse(self.output.exists())

    def test_expanded_layers_are_bounded_even_if_outer_archive_is_small(self):
        self.source.write_bytes(docker_image(layer=gzip.compress(LAYER)))
        with patch.object(prebuilt_image, "IMAGE_LIMIT", len(LAYER) - 1):
            with self.assertRaisesRegex(prebuilt_image.ImageError, "Unpacked image layers"):
                prebuilt_image.normalize(self.source, self.output, RELEASE)

    def test_writeback_is_bounded_during_archive_output(self):
        stream = tempfile.TemporaryFile()
        self.addCleanup(stream.close)
        writer = prebuilt_image.ImageWriter(stream)
        with patch.object(prebuilt_image, "WRITEBACK_BATCH", 8), patch.object(prebuilt_image.os, "fsync") as sync:
            writer.write(b"hello")
            sync.assert_not_called()
            writer.write(b" world")
            sync.assert_called_once_with(stream.fileno())
            self.assertEqual(11, writer.tell())
            writer.write(b"!")
            writer.sync()
            self.assertEqual(2, sync.call_count)
        stream.seek(0)
        self.assertEqual(b"hello world!", stream.read())

    def test_executor_normalizes_without_either_builder(self):
        job = self.root / "builds" / RELEASE
        job.mkdir(parents=True)
        (job / "source.tar.gz").write_bytes(docker_image())
        (job / "spec.json").write_text('{"runtime":"image"}')
        (job / "status.json").write_text('{"driver":"prebuilt","state":"queued"}')
        with patch.object(host, "ROOT", self.root), patch.object(host, "admit_space"), \
             patch.object(host, "build_in_vm") as vm, patch.object(host.native_build, "build") as native:
            host.run_build(RELEASE)
        result = json.loads((job / "status.json").read_text())
        self.assertEqual(("prebuilt", "succeeded"), (result["driver"], result["state"]))
        self.assertTrue((job / "image.tar").is_file())
        vm.assert_not_called()
        native.assert_not_called()

    def test_cli_preflight_uses_runtime_config_not_source_build_settings(self):
        self.source.write_bytes(docker_image())
        config = self.root / "poteto.json"
        config.write_text('{"runtime":"node","target":"production","build_command":"npm run build","port":8080,"health_check":"/healthz"}')
        report, body = read_image(self.root, self.source, None, self.root / "credential")
        expected = {"runtime": "image", "target": "preview", "port": 8080, "health_check": "/healthz"}
        self.assertEqual(expected, report["configuration"])
        self.assertEqual(expected["target"], DeploymentInput(**expected).target)
        self.assertEqual(self.source.read_bytes(), body)

    def test_cli_storage_estimate_covers_normalized_gzip_layers(self):
        self.source.write_bytes(docker_image(layer=gzip.compress(LAYER)))
        report, _ = read_image(self.root, self.source, None, self.root / "credential")
        receipt = prebuilt_image.normalize(self.source, self.output, RELEASE)
        self.assertLess(report["unpacked_bytes"], len(LAYER))
        self.assertGreaterEqual(report["normalized_image_bytes"], receipt["image_bytes"])
        self.assertEqual(len(LAYER) + 4 * 1024**2, report["normalized_image_bytes"])

    def test_image_upload_does_not_inherit_small_source_limit(self):
        settings = Settings(self.root, upload_limit=64, unpack_limit=64)
        store = Store(settings)
        owner = store.create_account("different-limits")
        actor = store.authenticate(owner["token"])
        project = store.create_project(actor, "image")["id"]
        settings = replace(settings, prebuilt_image_projects=(project,))
        with TestClient(create_app(settings), headers={"Authorization": "Bearer " + owner["token"]}) as api:
            source = api.post(f"/v1/projects/{project}/deployments", json={"target": "preview"}).json()
            self.assertEqual(413, api.put(source["upload_url"], content=archive({"index.html": b"hello"}),
                                         headers={"Content-Type": "application/gzip"}).status_code)
            image = api.post(f"/v1/projects/{project}/deployments", json={"runtime": "image", "target": "preview"}).json()
            result = api.put(image["upload_url"], content=docker_image(),
                             headers={"Content-Type": "application/gzip"})
            self.assertEqual(202, result.status_code, result.text)

    def test_project_opt_in_preview_scope_and_redeploy(self):
        settings = Settings(self.root)
        store = Store(settings)
        owner = store.create_account("image-owner")
        actor = store.authenticate(owner["token"])
        project = store.create_project(actor, "pilot")["id"]
        path = f"/v1/projects/{project}/deployments"
        headers = {"Authorization": "Bearer " + owner["token"]}
        with TestClient(create_app(settings), headers=headers) as disabled:
            self.assertEqual(403, disabled.post(path, json={"runtime": "image"}).status_code)
        settings = replace(settings, prebuilt_image_projects=(project,))
        with TestClient(create_app(settings), headers=headers) as api:
            response = api.post("/v1/tokens", json={"name": "agent", "scopes": ["deployments:preview"],
                                                    "project_ids": [project]})
            self.assertEqual(201, response.status_code, response.text)
            token = {"Authorization": "Bearer " + response.json()["token"]}
            self.assertEqual(403, api.post(path, json={"runtime": "image"}, headers=token).status_code)
            self.assertEqual(422, api.post(path, json={"runtime": "image", "build_command": "npm run build"}).status_code)
            response = api.post(path, json={"runtime": "image", "target": "preview"}, headers=token)
            self.assertEqual(201, response.status_code, response.text)
            release = response.json()
            upload = token | {"Content-Type": "application/gzip"}
            self.assertEqual(422, api.put(release["upload_url"], content=archive({"index.html": b"not an image"}),
                                         headers=upload).status_code)
            self.assertEqual(202, api.put(release["upload_url"], content=docker_image(), headers=upload).status_code)
            with store.connection() as db:
                db.execute("UPDATE deployments SET state='ready' WHERE id=?", (release["id"],))
            response = api.post(f"/v1/deployments/{release['id']}/redeploy", json={"target": "preview"},
                                headers=token | {"Idempotency-Key": "prebuilt-test"})
            self.assertEqual(202, response.status_code, response.text)
            self.assertEqual("image", response.json()["config"]["runtime"])
