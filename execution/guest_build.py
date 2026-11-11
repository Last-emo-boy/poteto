"""Executed only in a fresh build VM. Produces one canonical Docker image."""
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
from publisher.archive import unpack

WORK = Path("/work")


def npm_command(spec, *arguments):
    command = ['npm', *arguments]
    registry = spec.get('npm_registry')
    if registry is not None:
        parsed = urlsplit(registry)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Build npm registry must be an HTTPS URL without credentials')
        command += ['--registry', registry]
    return command


def dockerfile(spec):
    runtime = spec["runtime"]
    if runtime == "static" and not spec.get("build_command"):
        return "FROM nginx:1.28-alpine\nCOPY . /usr/share/nginx/html/\n"
    lines = ["FROM node:24-alpine AS build", "WORKDIR /app", "COPY . .", "RUN " + json.dumps(npm_command(spec, 'ci'))]
    if spec.get("build_command"):
        lines.append("RUN " + json.dumps(["sh", "-c", spec["build_command"]]))
    if runtime == "static":
        output = spec.get("output_directory", ".")
        parts = Path(output).parts
        if Path(output).is_absolute() or ".." in parts or "\\" in output:
            raise ValueError("Static output directory must be inside the project")
        lines += ["FROM nginx:1.28-alpine", "COPY --from=build " + json.dumps(["/app/" + output, "/usr/share/nginx/html/"])]
    else:
        lines += ["RUN " + json.dumps(npm_command(spec, 'prune', '--omit=dev')), "FROM node:24-alpine", "WORKDIR /app", "ENV NODE_ENV=production",
                  "COPY --from=build --chown=10001:10001 /app /app", "USER 10001:10001",
                  "CMD " + json.dumps(["sh", "-c", spec.get("start_command") or "npm start"])]
    return "\n".join(lines) + "\n"


def logged(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    written = 0
    with (WORK / "build.log").open("wb") as log:
        while chunk := process.stdout.read1(8192):
            if written < 2 * 1024**2:
                part = chunk[:2 * 1024**2 - written]
                log.write(part)
                log.flush()
                written += len(part)
    return process.wait()==0


def normalize_image(spec,image):
    reference=spec.get('image_reference','')
    if not isinstance(reference,str) or not re.fullmatch(r'(?:paas-release:[a-f0-9]{24}|sha256:[a-f0-9]{64})',reference):
        raise ValueError('Invalid archived image reference')
    if not logged(['docker','load','--platform=linux/amd64','--input',str(WORK/'source.tar.gz')]):return False
    # Loading happens only in this fresh build VM. None of the original tags
    # or other images are sent to a tenant daemon.
    subprocess.run(['docker','tag',reference,image],check=True,capture_output=True)
    return True


def build_source(spec,image):
    source = WORK / "source"
    source.mkdir(mode=0o755)
    unpack(WORK / "source.tar.gz", source, max_bytes=200 * 1024**2, max_files=10000)
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
            if not source.is_relative_to(WORK / "source") or not (source / "index.html").is_file():
                raise ValueError("Static output directory must contain index.html")
        path = WORK / "Dockerfile"
        path.write_text(dockerfile(spec))
    command = ["docker", "build", "--progress=plain", "--tag", image, "--file", str(path), str(source)]
    return logged(command)


def main():
    spec=json.loads((WORK/'spec.json').read_text());image='paas-release:'+spec['id']
    ready=normalize_image(spec,image) if spec['runtime']=='image-import' else build_source(spec,image)
    if not ready:
        print(json.dumps({"state": "failed"}))
        return
    subprocess.run(["docker", "save", "--output", str(WORK / "image.tar"), image], check=True)
    print(json.dumps({"state": "succeeded", "image": image}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        (WORK / "build.log").write_text(type(error).__name__ + ": " + str(error))
        print(json.dumps({"state": "failed"}))
