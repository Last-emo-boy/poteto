"""Node-local, read-only base for fresh static source build guests."""
import json
import math
import os
import re
import stat
import time

GENERATION = re.compile(r"[a-f0-9]{24}")
NODE_IMAGE = re.compile(r"docker\.io/library/node:24-alpine@sha256:[a-f0-9]{64}")
MAX_AGE = 7 * 86400
PROFILE = "builder-profile.json"


def base_path(root, generation):
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        raise ValueError("Invalid builder template generation")
    return root / ("builder-base-" + generation + ".qcow2")


def node_image(reference):
    if not isinstance(reference, str) or not NODE_IMAGE.fullmatch(reference):
        raise ValueError("Use an official node:24-alpine image pinned to a sha256 digest")
    return reference


def load(root, *, now=None):
    path = root / PROFILE
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    base = base_path(root, value["generation"])
    node_image(value["node_image"])
    created = value["created"]
    now = time.time() if now is None else now
    if type(created) not in (int, float) or not math.isfinite(created) or not 0 < created <= now:
        raise ValueError("Invalid builder template creation time")
    if now - created >= MAX_AGE:
        return None
    try:
        info = base.lstat()
    except FileNotFoundError:
        raise ValueError("Prepared builder base is missing; regenerate or disable the profile") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("Prepared builder base must be a regular file not writable by other users")
    return value
