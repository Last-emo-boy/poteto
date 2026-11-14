"""Wire and storage bounds for a single prebuilt linux/amd64 image."""
import json
import re

UPLOAD_LIMIT = 128 * 1024**2
UNPACK_LIMIT = 512 * 1024**2
FILE_LIMIT = 1024
# Canonical tar metadata, configuration and padding are additional to layer bytes.
ARCHIVE_LIMIT = UNPACK_LIMIT + 4 * 1024**2

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
