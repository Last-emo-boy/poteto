"""Build and publish a portable static artifact without sending source."""
import gzip
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .archive import ArchiveError, unpack
from . import image_contract
from .cli_project import local_directory
from .cli_tools import PlanError, deployment_config, pack, plan_deployment, upload_files

UPLOAD_LIMIT = 50 * 1024 * 1024
UNPACK_LIMIT = 200 * 1024 * 1024
FILE_LIMIT = 10000


def _artifact(root, name="output.tar.gz"):
    if not root.is_dir():
        raise PlanError("missing_directory", "Deployment directory does not exist.")
    try:
        path = local_directory(root) / name
    except ValueError as error:
        raise PlanError("invalid_artifact", str(error)) from None
    if path.is_symlink() or path.resolve() != path or (path.exists() and not path.is_file()):
        raise PlanError("invalid_artifact", "The Poteto output must be a regular file.")
    return path


def _inspect(source, *, image=False):
    upload, expanded, files = ((image_contract.UPLOAD_LIMIT, image_contract.UNPACK_LIMIT, image_contract.FILE_LIMIT)
                               if image else (UPLOAD_LIMIT, UNPACK_LIMIT, FILE_LIMIT))
    if len(source) > upload:
        raise PlanError("artifact_too_large", f"Archive exceeds the server upload limit of {upload // 1024**2} MiB")
    try:
        return unpack(io.BytesIO(source), None, max_bytes=expanded,
                      max_files=files, required_file="manifest.json" if image else "index.html")
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


def package_image(directory: Path, reference: str, credential_file: Path | str):
    """Save one local Docker image; never build, pull or push on the user's behalf."""
    if not reference.strip():
        raise PlanError("missing_image_reference", "Provide a local Docker image name or ID.")
    root = directory.resolve()
    artifact = _artifact(root, "image.tar.gz")
    if artifact == Path(credential_file).expanduser().resolve():
        raise PlanError("invalid_artifact", "The output file cannot also be the credential file.")
    artifact.unlink(missing_ok=True)
    if not shutil.which("docker"):
        raise PlanError("missing_docker", "Install Docker with linux/amd64 image save support.")
    local_directory(root, create=True)
    out = tempfile.NamedTemporaryFile(dir=artifact.parent, delete=False)
    pending = Path(out.name)
    try:
        with out, subprocess.Popen(["docker", "image", "save", "--platform", "linux/amd64", "--", reference],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE) as process:
            try:
                size = 0
                with gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=3, mtime=0) as compressed:
                    while chunk := process.stdout.read(1024**2):
                        size += len(chunk)
                        if size > image_contract.ARCHIVE_LIMIT or out.tell() > image_contract.UPLOAD_LIMIT:
                            raise PlanError("artifact_too_large", "The Docker image exceeds the 128 MiB compressed / 512 MiB expanded limits.")
                        compressed.write(chunk)
                if process.wait():
                    raise PlanError("image_save_failed", "Could not save a local linux/amd64 image; see Docker output above.")
            finally:
                if process.poll() is None:
                    process.kill()
        with pending.open("rb") as source:
            data = source.read(image_contract.UPLOAD_LIMIT + 1)
        _inspect(data, image=True)
        pending.replace(artifact)
    finally:
        pending.unlink(missing_ok=True)
    return {"ok": True, "runtime": "image", "artifact": str(artifact), "upload_bytes": len(data)}


def read_image(directory: Path, artifact: Path, config: Path | None, credential_file: Path | str, *, production=False):
    root = directory.resolve()
    if not root.is_dir():
        raise PlanError("missing_directory", "Deployment directory does not exist.")
    if artifact.resolve() == Path(credential_file).expanduser().resolve():
        raise PlanError("invalid_artifact", "The artifact cannot also be the credential file.")
    if not artifact.is_file():
        raise PlanError("missing_artifact", "Choose a regular Docker image archive from poteto build --from-image.")
    with artifact.open("rb") as source:
        data = source.read(image_contract.UPLOAD_LIMIT + 1)
    stats = _inspect(data, image=True)
    if config is not None and not config.is_file():
        raise PlanError("missing_config", f"Deployment config does not exist: {config}")
    try:
        config = deployment_config(root, config)
    except ValueError:
        raise PlanError("invalid_json", "Deployment config must contain a valid JSON object.") from None
    if not isinstance(config, dict):
        raise PlanError("invalid_json", "Deployment config must contain a JSON object.")
    runtime_fields = ("port", "health_check", "sleep", "memory_mb", "cpus", "database_id", "volume_id", "migration_command")
    spec = {k: config[k] for k in runtime_fields if k in config}
    spec |= {"runtime": "image", "target": "production" if production else "preview"}
    return {"ok": True, "directory": str(root), "origin": "prebuilt-image", "runtime": "image",
            "target": spec["target"], "build_command": None, "start_command": None, "dockerfile": None,
            "public_directory": None, "artifact": str(artifact.resolve()), "upload_files": stats["files"],
            "upload_bytes": len(data), "configuration": spec}, data
