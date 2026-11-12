"""Shared Node/static image recipe for local and hosted builders."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


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
    base = spec.get("builder_node_image", "node:24-alpine")
    if base != "node:24-alpine" and (spec.get("output_kind") != "static" or not isinstance(base, str) or not re.fullmatch(
            r"docker\.io/library/node:24-alpine@sha256:[a-f0-9]{64}", base)):
        raise ValueError("Invalid prepared builder image")
    lines = ["FROM " + base + " AS build", "WORKDIR /app", "COPY . .", run + json.dumps(npm_command(spec, 'ci'))]
    if spec.get("build_command"):
        lines.append("RUN " + json.dumps(["sh", "-c", spec["build_command"]]))
    if runtime == "static":
        output = spec.get("output_directory", ".")
        parts = Path(output).parts
        if Path(output).is_absolute() or ".." in parts or "\\" in output:
            raise ValueError("Static output directory must be inside the project")
        final, destination = ("scratch", "/") if spec.get("output_kind") == "static" else ("nginx:1.28-alpine", "/usr/share/nginx/html/")
        lines += ["FROM " + final, "COPY --from=build " + json.dumps(["/app/" + output, destination])]
    else:
        lines += [run + json.dumps(npm_command(spec, 'prune', '--omit=dev')), "FROM node:24-alpine", "WORKDIR /app", "ENV NODE_ENV=production",
                  "COPY --from=build --chown=10001:10001 /app /app", "USER 10001:10001",
                  "CMD " + json.dumps(["sh", "-c", spec.get("start_command") or "npm start"])]
    return "\n".join(lines) + "\n"


