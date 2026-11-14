"""Validate before extracting; never delegate archive paths to tar.extractall."""
import gzip
import os
import shutil
import tarfile
import zlib
from pathlib import Path, PurePosixPath


class ArchiveError(ValueError):
    pass


class LimitedGzip:
    """Bound metadata reads and all expanded offsets, including tar extension headers."""
    def __init__(self, filename, limit):
        self.stream = gzip.open(filename, "rb")
        self.limit = limit

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def tell(self):
        return self.stream.tell()

    def read(self, size=-1):
        if size < 0 or size > 1024 * 1024:
            raise ArchiveError("Tar metadata read exceeds 1 MiB")
        if self.tell() + size > self.limit:
            raise ArchiveError("Expanded archive stream exceeds its limit")
        return self.stream.read(size)

    def seek(self, offset, whence=os.SEEK_SET):
        target = offset if whence == os.SEEK_SET else self.tell() + offset
        if whence not in {os.SEEK_SET, os.SEEK_CUR} or not 0 <= target <= self.limit:
            raise ArchiveError("Expanded archive offset exceeds its limit")
        return self.stream.seek(offset, whence)


def unpack(source, destination: Path | None, *, max_bytes, max_files, private=False, required_file=None,
           open_output=Path.open):
    count = size = 0
    found_required = required_file is None
    names = set()
    try:
        with LimitedGzip(source, max_bytes + max_files * 2048 + 1024) as compressed, tarfile.open(fileobj=compressed, mode="r:") as archive:
            for index, entry in enumerate(archive):
                if index >= max_files:
                    raise ArchiveError("Archive entry count exceeds its limit")
                name = entry.name
                parts = PurePosixPath(name).parts
                if (not name or name.startswith("/") or "\\" in name or ".." in parts
                        or len(name) > 240 or any(ord(c) < 32 for c in name)
                        or not (entry.isfile() or entry.isdir())):
                    raise ArchiveError("Archive contains an unsafe path or non-regular file")
                normalized = str(PurePosixPath(name))
                if normalized in names:
                    raise ArchiveError("Archive contains duplicate paths")
                names.add(normalized)
                if any(p == ".git" or p == ".env" or p.startswith(".env.") for p in parts):
                    raise ArchiveError("Do not upload Git internals or .env secrets; use the environment API")
                if entry.isdir():
                    if destination:
                        (destination / normalized).mkdir(parents=True, exist_ok=True)
                    continue
                count += 1
                found_required |= normalized == required_file
                size += entry.size
                if count > max_files or size > max_bytes:
                    raise ArchiveError("Unpacked project exceeds its file or byte limit")
                stream = archive.extractfile(entry)
                if stream is None:
                    raise ArchiveError("Archive file cannot be read")
                if destination:
                    target = destination / normalized
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open_output(target, "xb") as out:
                        shutil.copyfileobj(stream, out, 1024 * 1024)
                    target.chmod((0o700 if entry.mode & 0o111 else 0o600) if private else (0o755 if entry.mode & 0o111 else 0o644))
                else:
                    while stream.read(1024 * 1024):
                        pass
    except (tarfile.TarError, EOFError, OSError, zlib.error) as error:
        raise ArchiveError("Invalid or conflicting tar.gz contents") from error
    if destination:
        # A worker's restrictive umask must not make public files unreadable inside nginx.
        for directory in [destination, *destination.rglob("*")]:
            if directory.is_dir():
                directory.chmod(0o700 if private else 0o755)
    if not found_required:
        raise ArchiveError(f"Archive must contain {required_file} at its root")
    if count == 0:
        raise ArchiveError("Project archive is empty")
    return {"files": count, "unpacked_bytes": size}
