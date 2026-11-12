"""Validate source and resolve one Docker build context for either driver."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from publisher.archive import unpack


def npm_command(spec, *arguments):
    command = ['npm', *arguments]
    registry = spec.get('npm_registry')
    if registry is not None:
        parsed = urlsplit(registry)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Build npm registry must be an HTTPS URL without credentials')
        command += ['--registry', registry]
    return command


def dockerfile(spec, *, cached=False):
    runtime = spec["runtime"]
    if runtime == "static" and not spec.get("build_command"):
        return "FROM nginx:1.28-alpine\nCOPY . /usr/share/nginx/html/\n"
    run = "RUN --mount=type=cache,target=/root/.npm,sharing=locked " if cached else "RUN "
    lines = ["FROM node:24-alpine AS build", "WORKDIR /app", "COPY . .", run + json.dumps(npm_command(spec, 'ci'))]
    if spec.get("build_command"):
        lines.append("RUN " + json.dumps(["sh", "-c", spec["build_command"]]))
    if runtime == "static":
        output = spec.get("output_directory", ".")
        parts = Path(output).parts
        if Path(output).is_absolute() or ".." in parts or "\\" in output:
            raise ValueError("Static output directory must be inside the project")
        lines += ["FROM nginx:1.28-alpine", "COPY --from=build " + json.dumps(["/app/" + output, "/usr/share/nginx/html/"])]
    else:
        lines += [run + json.dumps(npm_command(spec, 'prune', '--omit=dev')), "FROM node:24-alpine", "WORKDIR /app", "ENV NODE_ENV=production",
                  "COPY --from=build --chown=10001:10001 /app /app", "USER 10001:10001",
                  "CMD " + json.dumps(["sh", "-c", spec.get("start_command") or "npm start"])]
    return "\n".join(lines) + "\n"


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
