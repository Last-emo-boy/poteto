"""Local project setup, packaging and deployment planning for the Poteto CLI."""
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

VERSION = "0.5.0"

_PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Poteto preview</title>
<main><h1>Hello, Poteto.</h1><p>Edit this page and run <code>poteto deploy .</code>.</p></main>
</html>
"""
_NODE = """import { createServer } from 'node:http';

createServer((req, res) => {
  if (req.url === '/healthz') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    return res.end(JSON.stringify({status: 'ok'}));
  }
  if (req.url !== '/') {
    res.writeHead(404);
    return res.end('Not found');
  }
  res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'});
  res.end('<!doctype html><html lang="en"><meta charset="utf-8"><title>Poteto Node preview</title><main><h1>Hello, Poteto.</h1><p>Rendered by Node at ' + new Date().toISOString() + '</p></main></html>');
}).listen(Number(process.env.PORT || 3000), '0.0.0.0');
"""


def _json(value):
    return json.dumps(value, indent=2) + "\n"


def initialize(directory: Path, runtime: str):
    config = {"runtime": runtime, "target": "preview", "sleep": True}
    if runtime == "static":
        files = {"index.html": _PAGE}
    else:
        config.update(port=3000, health_check="/healthz")
        files = {"server.mjs": _NODE}
        if runtime == "node":
            config["start_command"] = "npm start"
            package = {"name": "poteto-app", "version": "1.0.0"}
            files["package.json"] = _json(package | {"private": True, "scripts": {"start": "node server.mjs"}})
            files["package-lock.json"] = _json(package | {
                "lockfileVersion": 3, "requires": True, "packages": {"": package},
            })
        else:
            files["Dockerfile"] = 'FROM node:24-alpine\nWORKDIR /app\nCOPY --chown=node:node server.mjs .\nUSER node\nCMD ["node","server.mjs"]\n'
    files["poteto.json"] = _json(config)
    directory.mkdir(parents=True, exist_ok=True)
    for name in (*files, "paas.json"):
        path = directory / name
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"Refusing to overwrite the existing project file: {path}")
    for name, contents in files.items():
        with (directory / name).open("x", encoding="utf-8") as out:
            out.write(contents)
    return {"directory": str(directory.resolve()), "runtime": runtime, "files": sorted(files)}


def _manifest(directory: Path, explicit: Path | None):
    if explicit is not None:
        return explicit
    # Retain the established default for projects that already have a legacy manifest.
    for name in ("paas.json", "poteto.json"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def deployment_config(directory: Path, explicit: Path | None):
    manifest = _manifest(directory, explicit)
    if manifest is not None and manifest.is_file():
        return json.loads(manifest.read_text(encoding="utf-8-sig"))
    return {"runtime": "static"}


def upload_files(root: Path, credential_file: Path | None = None):
    token_file = credential_file or os.environ.get("POTETO_TOKEN_FILE") or os.environ.get("PAAS_TOKEN_FILE")
    credential = Path(token_file).expanduser().resolve() if token_file else None
    excluded = {".git", ".poteto", ".venv", "node_modules", "__pycache__", ".DS_Store", ".ssh", ".aws", "secrets", ".codex"}
    tracked = None
    if (root / ".git").exists():
        result = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                capture_output=True)
        if result.returncode:
            raise PlanError("unreadable_git", "Git could not list uploadable files. Check git status in the upload directory.")
        tracked = {os.fsdecode(p) for p in result.stdout.split(b"\0") if p}
    files = []
    for parent, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in excluded and not (Path(parent) / d).is_symlink())
        for name in sorted(filenames):
            file = Path(parent) / name
            if name in excluded or name == ".env" or name.startswith(".env.") or file.is_symlink():
                continue
            if credential is not None and file.resolve() == credential:
                continue
            relative = file.relative_to(root).as_posix()
            if (tracked is None or relative in tracked) and file.is_file():
                files.append(relative)
    return files


def pack(directory: Path, credential_file: Path | None = None, *, files: list[str] | None = None):
    root = directory.resolve()
    if files is None:
        files = upload_files(root, credential_file)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in files:
            archive.add(root / name, arcname=name, recursive=False)
    return buffer.getvalue()


class PlanError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _read_object(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        raise PlanError("invalid_json", f"{path.name} must contain a valid JSON object.") from None
    if not isinstance(value, dict):
        raise PlanError("invalid_json", f"{path.name} must contain a JSON object.")
    return value


def _detect(root: Path, names: set[str]):
    index = "index.html" in names
    docker = "Dockerfile" in names
    if docker and not index:
        return {"runtime": "dockerfile", "target": "preview"}, "dockerfile"
    if docker:
        raise PlanError("ambiguous_runtime", "Both Dockerfile and index.html are present. Set runtime in poteto.json or upload only the built static directory.")

    package = _read_object(root / "package.json") if "package.json" in names else {}
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        raise PlanError("invalid_package", "package.json scripts must be an object.")
    if any(isinstance(package.get(field), dict) and "next" in package[field]
           for field in ("dependencies", "devDependencies")):
        if scripts.get("build") and scripts.get("start"):
            config = _preset("nextjs", package)
            config.pop("memory_mb")  # Preserve the existing auto-detected Node resource default.
            return config, "nextjs"
        if not scripts.get("start"):
            raise PlanError("missing_start_script", "Next.js needs a start script; for a static export, build first and deploy the out directory.")
    build = scripts.get("build")
    start = scripts.get("start")
    vite_config = any(name.startswith("vite.config.") and "/" not in name for name in names)
    vite = vite_config or any(isinstance(package.get(field), dict) and "vite" in package[field]
                              for field in ("dependencies", "devDependencies"))
    if vite and index:
        if build == "vite build" and not vite_config and not any(scripts.get(name) for name in ("start", "prebuild", "postbuild")):
            return {"runtime": "static", "build_command": "npm run build",
                    "output_directory": "dist", "target": "preview"}, "vite"
        raise PlanError("ambiguous_runtime", "Vite source cannot be published as static HTML. Build it and deploy its output directory, or set build_command and output_directory in poteto.json.")
    if index and (build or start):
        raise PlanError("ambiguous_runtime", "index.html is next to npm build or start scripts. Set runtime in poteto.json or upload only the built static directory.")
    if index:
        return {"runtime": "static"}, "static"
    if isinstance(start, str) and start.strip():
        config = {"runtime": "node", "start_command": "npm start", "target": "preview"}
        if isinstance(build, str) and build.strip():
            config["build_command"] = "npm run build"
        return config, "node"
    if (root / "index.html").is_file():
        raise PlanError("missing_entrypoint", "index.html is excluded from the upload. Check ignore rules or deploy the built output directory directly.")
    raise PlanError("unrecognized_project", "No supported entry point found. Deploy a folder containing index.html, an npm project with a start script, or a Dockerfile; use poteto.json for other layouts.")


def _preset(name, package):
    if not isinstance(name, str) or name not in {"nextjs", "vite"}:
        raise PlanError("invalid_preset", "preset must be nextjs or vite.")
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict) or not isinstance(scripts.get("build"), str) or not scripts["build"].strip():
        raise PlanError("missing_build_script", f"The {name} preset requires an npm build script.")
    if name == "vite":
        return {"runtime": "static", "target": "preview", "build_command": "npm run build", "output_directory": "dist"}
    if not isinstance(scripts.get("start"), str) or not scripts["start"].strip():
        raise PlanError("missing_start_script", "The nextjs preset requires an npm start script. For a static export, build first and deploy the out directory.")
    if not any(isinstance(package.get(field), dict) and "next" in package[field]
               for field in ("dependencies", "devDependencies")):
        raise PlanError("missing_next_dependency", "The nextjs preset requires Next.js in package.json.")
    return {"runtime": "node", "target": "preview", "build_command": "npm run build",
            "start_command": "npm start", "memory_mb": 512}


def _relative_path(value, key):
    if (not isinstance(value, str) or not value or len(value) > 240 or value.startswith("/")
            or "\\" in value or ".." in value.split("/") or any(ord(c) < 32 for c in value)):
        raise PlanError("invalid_path", f"{key} must be a relative path inside the upload directory.")
    return PurePosixPath(value)


@dataclass
class DeploymentPlan:
    directory: Path
    origin: str
    config: dict
    files: list[str]
    preset: str | None = None

    def summary(self):
        runtime = self.config.get("runtime", "static")
        return {
            "ok": True,
            "directory": str(self.directory),
            "origin": self.origin,
            "runtime": runtime,
            "target": self.config.get("target"),
            "build_command": self.config.get("build_command") if runtime != "dockerfile" else None,
            "start_command": (self.config.get("start_command") or "npm start") if runtime == "node" else None,
            "public_directory": self.config.get("output_directory", ".") if runtime == "static" else None,
            "dockerfile": self.config.get("dockerfile", "Dockerfile") if runtime == "dockerfile" else None,
            "upload_files": len(self.files),
        } | ({"preset": self.preset} if self.preset else {})


def plan_deployment(directory: Path, explicit: Path | None, credential_file: Path | None = None,
                    *, preview: bool = False, production: bool = False, preset: str | None = None):
    if preview and production:
        raise PlanError("invalid_target", "Choose either --preview or --prod")
    root = directory.resolve()
    if not root.is_dir():
        raise PlanError("missing_directory", "Deployment directory does not exist.")
    manifest = _manifest(root, explicit)
    if manifest is not None and not manifest.is_file():
        raise PlanError("missing_config", f"Deployment config does not exist: {manifest}")
    files = upload_files(root, credential_file)
    names = set(files)
    if manifest is not None:
        config, origin = _read_object(manifest), manifest.name
    else:
        config, origin = ({}, "preset") if preset else _detect(root, names)
    configured_preset = config.pop("preset", None)
    if configured_preset is not None and (not isinstance(configured_preset, str) or configured_preset not in {"nextjs", "vite"}):
        raise PlanError("invalid_preset", "preset must be nextjs or vite.")
    selected = preset or configured_preset
    if selected:
        if "package.json" not in names:
            raise PlanError("missing_npm_input", "Framework presets require package.json in the upload directory.")
        defaults = _preset(selected, _read_object(root / "package.json"))
        if config.get("runtime", defaults["runtime"]) != defaults["runtime"]:
            raise PlanError("invalid_runtime", f"The {selected} preset requires the {defaults['runtime']} runtime.")
        config = defaults | config
    elif origin == "nextjs":
        selected = "nextjs"
    if preview:
        config["target"] = "preview"
    elif production:
        config["target"] = "production"
    runtime = config.get("runtime", "static")
    if runtime not in ("static", "node", "dockerfile"):
        raise PlanError("invalid_runtime", "runtime must be static, node or dockerfile.")
    if "target" in config and config["target"] not in ("preview", "production"):
        raise PlanError("invalid_target", "target must be preview or production.")
    for key in ("build_command", "start_command"):
        if config.get(key) is not None and (not isinstance(config[key], str) or len(config[key]) > 2000):
            raise PlanError("invalid_command", f"{key} must be a command string of at most 2000 characters.")
    if runtime == "dockerfile":
        dockerfile = _relative_path(config.get("dockerfile", "Dockerfile"), "dockerfile")
        if dockerfile.as_posix() not in names:
            raise PlanError("missing_dockerfile", f"{dockerfile} must be included in the upload. Check the configured path and ignore rules.")
    if runtime == "node" or (runtime == "static" and config.get("build_command")):
        for name in ("package.json", "package-lock.json"):
            if name not in names:
                raise PlanError("missing_npm_input", f"npm builds require {name} in the upload directory. Include the npm manifest and lockfile, or use a Dockerfile for a different build.")
    if runtime == "static":
        output = _relative_path(config.get("output_directory", "."), "output_directory")
        if not config.get("build_command") and (output / "index.html").as_posix() not in names:
            raise PlanError("missing_entrypoint", f"{output / 'index.html'} must be included in the upload. Build first or correct output_directory and the ignore rules.")
    return DeploymentPlan(root, origin, config, files, selected)
