"""Validate source and resolve one Docker build context for either driver."""
from pathlib import Path
from publisher.archive import unpack
from publisher.build_recipe import dockerfile, npm_command


def prepare_source(spec, work, archive, *, cached=False):
    source = work / "source"
    source.mkdir(mode=0o755)
    unpack(archive, source, max_bytes=200 * 1024**2, max_files=10000)
    if spec["runtime"] == "dockerfile":
        path = (source / spec.get("dockerfile", "Dockerfile")).resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("Dockerfile must exist inside the uploaded project")
    else:
        if spec["runtime"] == "node" or spec.get("build_command"):
            if not (source / "package-lock.json").is_file():
                raise ValueError("Node source builds require package-lock.json for npm ci")
        if spec["runtime"] == "static" and not spec.get("build_command"):
            source = (source / spec.get("output_directory", ".")).resolve()
            if not source.is_relative_to(work / "source") or not (source / "index.html").is_file():
                raise ValueError("Static output directory must contain index.html")
        path = work / "Dockerfile"
        path.write_text(dockerfile(spec, cached=cached), encoding="utf-8")
    return source, path
