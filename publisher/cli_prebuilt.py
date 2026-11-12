"""Build and publish a portable static artifact without sending source."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .archive import ArchiveError, unpack
from .cli_project import local_directory
from .cli_tools import PlanError, pack, plan_deployment, upload_files

UPLOAD_LIMIT = 50 * 1024 * 1024
UNPACK_LIMIT = 200 * 1024 * 1024
FILE_LIMIT = 10000


def _artifact(root):
    if not root.is_dir():
        raise PlanError("missing_directory", "Deployment directory does not exist.")
    try:
        path = local_directory(root) / "output.tar.gz"
    except ValueError as error:
        raise PlanError("invalid_artifact", str(error)) from None
    if path.is_symlink() or path.resolve() != path or (path.exists() and not path.is_file()):
        raise PlanError("invalid_artifact", "The Poteto output must be a regular file.")
    return path


def _inspect(source):
    if len(source) > UPLOAD_LIMIT:
        raise PlanError("artifact_too_large", "Archive exceeds the server upload limit of 50 MiB")
    try:
        return unpack(io.BytesIO(source), None, max_bytes=UNPACK_LIMIT,
                      max_files=FILE_LIMIT, required_file="index.html")
    except ArchiveError as error:
        raise PlanError("invalid_artifact", str(error)) from None


def _run_build(directory, command):
    if not shutil.which("npm"):
        raise PlanError("missing_npm", "Install Node.js and npm before running poteto build.")
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {"POTETO_TOKEN", "POTETO_TOKEN_FILE", "PAAS_TOKEN", "PAAS_TOKEN_FILE"}}
    for step, script in (("Install", "npm ci --include=dev"), ("Build", command)):
        result = subprocess.run(script, shell=True, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                stdout=sys.stderr)
        if result.returncode:
            raise PlanError("local_build_failed", f"{step} exited with status {result.returncode}; see build output above.")


def build_static(directory: Path, config: Path | None, credential_file: Path | str, *, preset=None):
    root = directory.resolve()
    artifact = _artifact(root)
    # A failed build must not leave an earlier artifact eligible for publishing.
    if Path(credential_file).expanduser().resolve() == artifact:
        raise PlanError("invalid_artifact", "The output file cannot also be the credential file.")
    artifact.unlink(missing_ok=True)
    plan = plan_deployment(root, config, credential_file, preset=preset)
    if plan.config.get("runtime", "static") != "static":
        raise PlanError("unsupported_prebuilt_runtime",
                        "Local prebuild currently supports static sites. For Node or Dockerfile, use source deployment.")
    command = plan.config.get("build_command")
    output = root / plan.config.get("output_directory", ".")
    public = output.resolve()
    if (not public.is_relative_to(root) or public.is_relative_to(artifact.parent)
            or public != output or (command and public == root)):
        raise PlanError("invalid_output_directory",
                        "Use a real output directory inside the project (e.g. dist), separate from source when building.")
    if command:
        _run_build(root, command)
    if not public.is_dir() or public != output.resolve():
        raise PlanError("missing_output_directory", "The build did not create the configured output directory.")
    files = plan.files if public == root else upload_files(public, credential_file)
    if "index.html" not in files:
        raise PlanError("missing_entrypoint", "The static output must contain an uploadable index.html.")
    paths = [public / name for name in files]
    if any(not path.resolve().is_relative_to(public) for path in paths):
        raise PlanError("invalid_output_directory", "Static output files must stay inside the output directory.")
    if len(files) > FILE_LIMIT or sum(path.stat().st_size for path in paths) > UNPACK_LIMIT:
        raise PlanError("artifact_too_large", "Static output exceeds 10,000 files or 200 MiB before compression.")
    source = pack(public, files=files)
    stats = _inspect(source)
    local_directory(root, create=True)
    out = tempfile.NamedTemporaryFile(dir=artifact.parent, delete=False)
    temporary = Path(out.name)
    try:
        with out:
            out.write(source)
        temporary.replace(artifact)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "runtime": "static", "artifact": str(artifact), "upload_files": stats["files"],
            "upload_bytes": len(source)}


def read_prebuilt(directory: Path, *, production=False):
    root = directory.resolve()
    artifact = _artifact(root)
    try:
        with artifact.open("rb") as source:
            data = source.read(UPLOAD_LIMIT + 1)
    except FileNotFoundError:
        raise PlanError("missing_artifact", "No prebuilt artifact. Run poteto build in this directory first.") from None
    stats = _inspect(data)
    report = {"ok": True, "directory": str(root), "origin": "prebuilt", "runtime": "static",
              "target": "production" if production else "preview",
              "build_command": None, "start_command": None, "public_directory": ".", "dockerfile": None,
              "artifact": str(artifact), "upload_files": stats["files"], "upload_bytes": len(data)}
    return report, data
