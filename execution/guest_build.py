"""Executed only in a fresh build VM. Produces one canonical Docker image."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
if __package__:
    from .build_source import dockerfile, npm_command, prepare_source
else:
    from build_source import dockerfile, npm_command, prepare_source

WORK = Path("/work")


def record_log(log, data):
    data = data[:max(0, 2 * 1024**2 - log.tell())]
    if data:
        log.write(data)
        log.flush()
        if os.environ.get('POTETO_STREAM_BUILD_LOG') == '1':
            sys.stderr.buffer.write(data)
            sys.stderr.buffer.flush()


def logged(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with (WORK / "build.log").open("wb") as log:
        while chunk := process.stdout.read1(8192):
            record_log(log, chunk)
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
    source, path = prepare_source(spec, WORK, WORK / 'source.tar.gz')
    output = ["--output", "type=tar,dest=" + str(WORK / "static.tar")] if spec.get("output_kind") == "static" else ["--tag", image]
    return logged(["docker", "build", "--progress=plain", *output, "--file", str(path), str(source)])


def main():
    spec=json.loads((WORK/'spec.json').read_text());image='paas-release:'+spec['id']
    ready=normalize_image(spec,image) if spec['runtime']=='image-import' else build_source(spec,image)
    if not ready:
        print(json.dumps({"state": "failed"}))
        return
    if spec.get("output_kind") == "static":
        print(json.dumps({"state": "succeeded", "output_kind": "static"}))
        return
    subprocess.run(["docker", "save", "--output", str(WORK / "image.tar"), image], check=True)
    print(json.dumps({"state": "succeeded", "image": image}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        with (WORK / "build.log").open("ab") as log:
            record_log(log, (type(error).__name__ + ": " + str(error) + "\n").encode('utf-8', errors='replace'))
        print(json.dumps({"state": "failed"}))
