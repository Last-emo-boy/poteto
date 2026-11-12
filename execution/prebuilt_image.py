"""Normalize a single Docker-exported image before loading it into a tenant VM."""
import gzip
import hashlib
import json
from pathlib import Path
import re
import tarfile
import tempfile
import zlib

from publisher.archive import ArchiveError, unpack

TRANSPORT_LIMIT = 50 * 1024**2
IMAGE_LIMIT = 200 * 1024**2
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
    import io
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


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
                    raise ImageError("Unpacked image layers exceed 200 MiB")
                digest.update(chunk)
                staged.write(chunk)
            if "sha256:" + digest.hexdigest() != expected:
                raise ImageError("Image layer does not match its configuration digest")
            info = tarfile.TarInfo(name)
            info.size = size
            staged.seek(0)
            archive.addfile(info, staged)
    return size


def normalize(source: Path, destination: Path, release_id: str):
    """Return size receipts; output has exactly one platform-owned tag."""
    if not re.fullmatch(r"[a-f0-9]{24}", release_id):
        raise ImageError("Invalid image release ID")
    if not 0 < source.stat().st_size <= TRANSPORT_LIMIT:
        raise ImageError("Compressed image exceeds the 50 MiB upload limit")
    image = "paas-release:" + release_id
    total = 0
    pending = destination.with_suffix(".next")
    try:
        with tempfile.TemporaryDirectory(dir=destination.parent) as workspace:
            root = Path(workspace)
            unpack(source, root, max_bytes=IMAGE_LIMIT, max_files=1024, private=True)
            config, layers = inspect(root)
            config_data = config.read_bytes()
            config_name = hashlib.sha256(config_data).hexdigest() + ".json"
            names = []
            with tarfile.open(pending, mode="w") as archive:
                add_bytes(archive, config_name, config_data)
                for index, (path, digest) in enumerate(layers):
                    name = f"layers/{index}/layer.tar"
                    total += add_layer(archive, name, path, digest, IMAGE_LIMIT - total, scratch=root)
                    names.append(name)
                add_bytes(archive, "manifest.json", json.dumps(
                    [{"Config": config_name, "Layers": names, "RepoTags": [image]}]).encode())
            pending.replace(destination)
    except (ArchiveError, EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ImageError("Invalid compressed Docker image archive") from error
    finally:
        pending.unlink(missing_ok=True)
    return {"image": image, "image_bytes": destination.stat().st_size, "image_disk_bytes": total}
