"""Measure the image-backed /srv tree from the inspected local container."""

import hashlib
import os
from pathlib import Path
import posixpath
import re
import stat
import sys

from release_state import digest, require


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _overlaps_srv(path):
    return path == "/srv" or path.startswith("/srv/") or "/srv".startswith(path.rstrip("/") + "/")


def measure_srv_inventory(item, read):
    """Read the image-backed paths, metadata and content hashes."""
    pid = item["State"]["Pid"]
    require(sys.platform.startswith("linux") and type(pid) is int and pid > 0
            and item["State"]["Running"] is True, "R3_SRV_RUNNING_CONTAINER_REQUIRED")
    require(not any(_overlaps_srv(row["Destination"]) for row in item["Mounts"]), "R3_SRV_CONTENT_MOUNTED")
    mountinfo = Path(f"/proc/{pid}/mountinfo").read_text().splitlines()
    targets = [re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), row.split()[4]) for row in mountinfo]
    require(not any(path != "/" and _overlaps_srv(path) for path in targets), "R3_SRV_CONTENT_MOUNTED")
    changes = read("container", "diff", item["Id"]).decode().splitlines()
    require(all(re.fullmatch(r"[ACD] /[^\r\n]*", line) for line in changes), "R3_SRV_OVERLAY_DIFF_INVALID")
    require(not any(line[2:] == "/srv" or line[2:].startswith("/srv/") for line in changes),
            "R3_SRV_IMAGE_CONTENT_CHANGED")

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NOATIME", 0)
    rows = []

    def walk(fd, relative, initial):
        for name in sorted(os.listdir(fd)):
            require(name not in (".", "..") and len(rows) < 10000, "R3_SRV_ENTRY_LIMIT")
            path = posixpath.join(relative, name)
            path.encode("utf-8")
            first = os.stat(name, dir_fd=fd, follow_symlinks=False)
            meta = {"path": path, "mode": format(stat.S_IMODE(first.st_mode), "04o"),
                    "uid": first.st_uid, "gid": first.st_gid, "mtime_ns": first.st_mtime_ns}
            if stat.S_ISDIR(first.st_mode):
                rows.append({**meta, "type": "directory"})
                child = os.open(name, flags | os.O_DIRECTORY, dir_fd=fd)
                try:
                    require(_stamp(os.fstat(child)) == _stamp(first), "R3_SRV_DIRECTORY_CHANGED")
                    walk(child, path, first)
                finally:
                    os.close(child)
            elif stat.S_ISREG(first.st_mode):
                child = os.open(name, flags | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(child, "rb") as content:
                    require(_stamp(os.fstat(content.fileno())) == _stamp(first), "R3_SRV_FILE_CHANGED")
                    checksum = hashlib.sha256()
                    size = 0
                    for chunk in iter(lambda: content.read(1048576), b""):
                        checksum.update(chunk)
                        size += len(chunk)
                        require(size <= first.st_size, "R3_SRV_FILE_CHANGED")
                    require(size == first.st_size and _stamp(os.fstat(content.fileno())) == _stamp(first),
                            "R3_SRV_FILE_CHANGED")
                rows.append({**meta, "type": "regular", "size": size, "sha256": checksum.hexdigest()})
            elif stat.S_ISLNK(first.st_mode):
                target = os.readlink(name, dir_fd=fd)
                target.encode("utf-8")
                rows.append({**meta, "type": "symlink", "target": target})
            else:
                require(False, "R3_SRV_SPECIAL_FILE_UNSUPPORTED")
            require(_stamp(os.stat(name, dir_fd=fd, follow_symlinks=False)) == _stamp(first), "R3_SRV_ENTRY_CHANGED")
        require(_stamp(os.fstat(fd)) == _stamp(initial), "R3_SRV_DIRECTORY_CHANGED")

    directory = os.open(f"/proc/{pid}/root/srv", flags | os.O_DIRECTORY)
    try:
        walk(directory, "", os.fstat(directory))
    finally:
        os.close(directory)
    require(any(row["path"] == "index.html" and row["type"] == "regular" for row in rows),
            "R3_INDEX_NOT_REGULAR_OR_MISSING")
    return sorted(rows, key=lambda row: row["path"])


def measure_srv_tree(item, read):
    return digest(measure_srv_inventory(item, read))
