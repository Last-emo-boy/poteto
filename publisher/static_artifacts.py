"""Publish prebuilt static files without creating a tenant process or VM."""
import json
import re
import shutil

from publisher.archive import unpack

UPSTREAM = "http://poteto-static:8080"
BUILT_LIMIT = 50 * 1024**2


def path_for(settings, release_id):
    if not re.fullmatch(r"[a-f0-9]{24}", release_id):
        raise ValueError("Invalid static release ID")
    return settings.root / "static-releases" / release_id


def built_archive(settings, release_id):
    return settings.root / "built-artifacts" / (path_for(settings, release_id).name + ".tar.gz")


def stage(settings, deployment, archive, check, *, built=False, before_publish=None):
    live = path_for(settings, deployment["id"])
    if (live / "index.html").is_file():
        return
    live.parent.mkdir(mode=0o755, exist_ok=True)
    live.parent.chmod(0o755)
    pending = live.with_name("." + live.name + ".next")
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir()
    try:
        stats = unpack(archive, pending, max_bytes=settings.unpack_limit, max_files=settings.file_limit)
        output = pending.resolve() if built else (pending / json.loads(deployment["config"])["output_directory"]).resolve()
        if not output.is_relative_to(pending.resolve()) or not (output / "index.html").is_file():
            raise ValueError("Static output_directory must contain index.html")
        check()
        if before_publish is not None:
            before_publish(stats)
        output.replace(live)
    finally:
        if pending.exists():
            shutil.rmtree(pending)


def remove(settings, release_id):
    live = path_for(settings, release_id)
    for path in (live, live.with_name("." + live.name + ".next")):
        if path.exists():
            shutil.rmtree(path)
    archive = built_archive(settings, release_id)
    archive.unlink(missing_ok=True)
    archive.with_suffix(".next").unlink(missing_ok=True)
