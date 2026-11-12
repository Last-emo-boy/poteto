"""Normalize one Docker image for the isolated VM or gVisor runtime."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import zlib

from publisher.archive import ArchiveError, unpack

from publisher.image_contract import UPLOAD_LIMIT as TRANSPORT_LIMIT, UNPACK_LIMIT as IMAGE_LIMIT, FILE_LIMIT
METADATA_LIMIT = 1024**2
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MEMBER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*(?:/[a-zA-Z0-9][a-zA-Z0-9_.-]*)*")
MAX_LAYERS = 128


class ImageError(ValueError):
    pass


def document(path):
    if not path.is_file() or not 0 < path.stat().st_size <= METADATA_LIMIT:
        raise ImageError("Image metadata is absent or too large")
    try:
        return json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise ImageError("Invalid image metadata JSON") from None


def member(root, name):
    if (not isinstance(name, str) or len(name) > 240 or not MEMBER.fullmatch(name)
            or name == "manifest.json"):
        raise ImageError("Invalid Docker image member path")
    path = root / name
    if not path.is_file():
        raise ImageError("Docker image member is missing")
    return path


def inspect(root):
    manifest = document(root / "manifest.json")
    if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
        raise ImageError("Upload exactly one Docker image, not a multi-image or multi-platform archive")
    spec = manifest[0]
    config_path = member(root, spec.get("Config"))
    config = document(config_path)
    if not isinstance(config, dict) or (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
        raise ImageError("The image must target linux/amd64")
    rootfs = config.get("rootfs")
    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        raise ImageError("Invalid image root filesystem")
    layers = spec.get("Layers")
    digests = rootfs.get("diff_ids")
    if (not isinstance(layers, list) or not isinstance(digests, list) or
            len(layers) != len(digests) or len(layers) > MAX_LAYERS or
            any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in digests)):
        raise ImageError("Image layers do not match the image configuration")
    return config_path, [(member(root, path), digest) for path, digest in zip(layers, digests)]


def add_bytes(archive, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def add_document(archive, value, media_type):
    data = json.dumps(value, separators=(",", ":")).encode()
    descriptor = {"mediaType": media_type, "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)}
    add_bytes(archive, "blobs/sha256/" + descriptor["digest"].split(":")[1], data)
    return descriptor


class LayerReader:
    """Expose only bounded metadata reads to tarfile; never extract a layer."""
    def __init__(self, stream, size):
        self.stream = stream
        self.size = size

    def tell(self):
        return self.stream.tell()

    def seek(self, offset, whence=0):
        if whence != 0 or not 0 <= offset <= self.size:
            raise ImageError("Invalid image layer offset")
        return self.stream.seek(offset)

    def read(self, size):
        if not 0 <= size <= METADATA_LIMIT:
            raise ImageError("Image layer metadata exceeds 1 MiB")
        return self.stream.read(size)


def validate_layer(staged, size):
    staged.seek(0)
    prefix = staged.read(6)
    if (prefix.startswith((b"BZh", b"\x1f\x8b", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd"))
            or (len(prefix) >= 4 and int.from_bytes(prefix[:4], "little") & 0xfffffff0 == 0x184d2a50)):
        raise ImageError("Nested or unsupported image layer compression")
    staged.seek(0)
    try:
        with tarfile.open(fileobj=LayerReader(staged, size), mode="r:") as layer:
            logical_bytes = 0
            while entry := layer.next():
                if entry.sparse is not None or entry.size < 0:
                    raise ImageError("Sparse or invalid image layer file")
                logical_bytes += entry.size
                if logical_bytes > size:
                    raise ImageError("Image layer expands beyond its admitted size")
                layer.members.clear()
            staged.seek(layer.offset)
            while trailing := staged.read(1024**2):
                if trailing.strip(b"\0"):
                    raise ImageError("Unexpected bytes after image layer tar")
    except ImageError:
        raise
    except (tarfile.TarError, RecursionError, ValueError) as error:
        raise ImageError("Invalid uncompressed image layer tar") from error
    staged.seek(0)


def add_layer(archive, name, path, expected, limit, *, scratch):
    # Keep opaque layer bytes out of the supervisor's filesystem namespace.
    # Gzip layers are decompressed under the same aggregate limit as plain layers.
    with path.open("rb") as source:
        gzip_encoded = source.read(2) == b"\x1f\x8b"
        source.seek(0)
        digest = hashlib.sha256()
        size = 0
        with (gzip.GzipFile(fileobj=source) if gzip_encoded else source) as layer, tempfile.TemporaryFile(dir=scratch) as staged:
            while chunk := layer.read(1024**2):
                size += len(chunk)
                if size > limit:
                    raise ImageError("Unpacked image layers exceed 512 MiB")
                digest.update(chunk)
                staged.write(chunk)
            if "sha256:" + digest.hexdigest() != expected:
                raise ImageError("Image layer does not match its configuration digest")
            validate_layer(staged, size)
            if name is not None:
                info = tarfile.TarInfo(name)
                info.size = size
                archive.addfile(info, staged)
    return size


def normalize(source: Path, destination: Path, release_id: str):
    """Return size receipts; output has exactly one platform-owned tag."""
    if not re.fullmatch(r"[a-f0-9]{24}", release_id):
        raise ImageError("Invalid image release ID")
    if not 0 < source.stat().st_size <= TRANSPORT_LIMIT:
        raise ImageError("Compressed image exceeds the 128 MiB upload limit")
    image = "paas-release:" + release_id
    total = 0
    pending = destination.with_suffix(".next")
    try:
        with tempfile.TemporaryDirectory(dir=destination.parent) as workspace:
            root = Path(workspace)
            unpack(source, root, max_bytes=IMAGE_LIMIT, max_files=FILE_LIMIT, private=True)
            config, layers = inspect(root)
            config_data = config.read_bytes()
            config_digest = "sha256:" + hashlib.sha256(config_data).hexdigest()
            config_name = "blobs/sha256/" + config_digest.split(":")[1]
            names, descriptors, written = [], [], set()
            with tarfile.open(pending, mode="w") as archive:
                add_bytes(archive, config_name, config_data)
                for path, digest in layers:
                    name = "blobs/sha256/" + digest.split(":")[1]
                    size = add_layer(archive, name if digest not in written else None,
                                     path, digest, IMAGE_LIMIT - total, scratch=root)
                    total += size
                    written.add(digest)
                    names.append(name)
                    descriptors.append({"mediaType": "application/vnd.docker.image.rootfs.diff.tar",
                                        "digest": digest, "size": size})
                manifest = add_document(archive, {
                    "schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                               "digest": config_digest, "size": len(config_data)},
                    "layers": descriptors,
                }, "application/vnd.docker.distribution.manifest.v2+json")
                manifest["annotations"] = {"io.containerd.image.name": "docker.io/library/" + image,
                                           "org.opencontainers.image.ref.name": release_id}
                add_bytes(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
                add_bytes(archive, "index.json", json.dumps({"schemaVersion": 2,
                          "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [manifest]}).encode())
                add_bytes(archive, "manifest.json", json.dumps(
                    [{"Config": config_name, "Layers": names, "RepoTags": [image]}]).encode())
            pending.replace(destination)
    except (ArchiveError, EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ImageError("Invalid compressed Docker image archive") from error
    finally:
        pending.unlink(missing_ok=True)
    return {"image": image, "image_bytes": destination.stat().st_size, "image_disk_bytes": total}
