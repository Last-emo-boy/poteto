"""Publish prebuilt static files without creating a tenant process or VM."""
import json
import re
import shutil

from publisher.archive import unpack

UPSTREAM = "http://poteto-static:8080"


def path_for(settings, release_id):
    if not re.fullmatch(r"[a-f0-9]{24}", release_id):
        raise ValueError("Invalid static release ID")
    return settings.root / "static-releases" / release_id


def stage(settings, deployment, archive, check):
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
        unpack(archive, pending, max_bytes=settings.unpack_limit, max_files=settings.file_limit)
        output = (pending / json.loads(deployment["config"])["output_directory"]).resolve()
        if not output.is_relative_to(pending.resolve()) or not (output / "index.html").is_file():
            raise ValueError("Static output_directory must contain index.html")
        check()
        output.replace(live)
    finally:
        if pending.exists():
            shutil.rmtree(pending)


def remove(settings, release_id):
    live = path_for(settings, release_id)
    for path in (live, live.with_name("." + live.name + ".next")):
        if path.exists():
            shutil.rmtree(path)
