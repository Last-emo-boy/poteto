"""Credential-free directory links and deployment references for the CLI."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProjectLink:
    directory: Path
    url: str
    project_id: str


def api_origin(value):
    if not isinstance(value, str):
        raise ValueError("The project link must contain an API URL")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Project links require an HTTPS API origin (HTTP is allowed only on localhost)")
    parsed.port  # Validate a supplied port before persisting it.
    return value.rstrip("/")


def find_link(directory):
    root = directory.resolve()
    for current in (root, *root.parents):
        path = current / ".poteto" / "project.json"
        if path.exists():
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("The Poteto project link must not be a symlink")
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or not isinstance(value.get("project_id"), str)
                    or not re.fullmatch(r"[a-f0-9]{24}", value["project_id"])):
                raise ValueError("Invalid .poteto/project.json; unlink and link the directory again")
            return ProjectLink(current, api_origin(value.get("url")), value["project_id"])
        if (current / ".git").exists():
            break
    return None


def write_link(directory, url, project_id):
    root = directory.resolve()
    url = api_origin(url)
    if not root.is_dir():
        raise ValueError("Choose an existing application directory")
    if not re.fullmatch(r"[a-f0-9]{24}", project_id):
        raise ValueError("The API returned an invalid project ID")
    folder = local_directory(root, create=True)
    path = folder / "project.json"
    value = {"version": 1, "url": url, "project_id": project_id}
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, indent=2) + "\n")
    return value | {"directory": str(root)}


def local_directory(root: Path, *, create=False):
    folder = root / ".poteto"
    if folder.is_symlink() or folder.resolve() != folder:
        raise ValueError("The .poteto directory must not be a link")
    if create:
        folder.mkdir(exist_ok=True)
        ignored = folder / ".gitignore"
        if not ignored.exists() and not ignored.is_symlink():
            ignored.write_text("*\n", encoding="utf-8")
    return folder


def unlink(directory):
    root = directory.resolve()
    path = root / ".poteto" / "project.json"
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("The Poteto project link must not be a symlink")
    present = path.exists()
    path.unlink(missing_ok=True)
    return {"directory": str(root), "unlinked": present}


def deployment_reference(value):
    if "://" not in value:
        return "deployment", value
    parsed = urlsplit(value)
    match = re.fullmatch(r"(preview|app)-([a-f0-9]{24})\..+", parsed.hostname or "")
    if parsed.scheme != "https" or parsed.username is not None or not match:
        raise ValueError("Use a deployment ID or a Poteto HTTPS preview/application URL")
    return ("project" if match[1] == "app" else "deployment"), match[2]
