"""Separate literal DB paths, already-decoded HTTP paths and new upload names.

No database, application startup or network I/O. Neither historical paths nor
ASGI request paths are URL-decoded here. Only new filenames use strict rules.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import tempfile
import unicodedata
from typing import BinaryIO
from urllib.parse import unquote
from uuid import uuid4


class MediaPathError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)  # Never include an untrusted filename or physical path.


def _literal_relative_parts(value: str) -> tuple[str, ...]:
    # Preserve %, spaces, reserved POSIX names and trailing dots literally.
    # Backslashes/drive notation are rejected, never interpreted as aliases.
    # Inspect raw components BEFORE pathlib can normalize empty / dot segments.
    parts = tuple(value.split("/"))
    if (not value or PurePosixPath(value).is_absolute() or "\\" in value
            or any(ord(char) < 32 for char in value)
            or any(part in ("", ".", "..") or PureWindowsPath(part).drive for part in parts)):
        raise MediaPathError("PATH_OUTSIDE_MEDIA_ROOT")
    if os.name == "nt":
        # Linux historical names are valid on Linux. On Windows do not let its
        # ADS / device-name / trailing-dot aliases select a DIFFERENT file.
        if any(":" in part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in parts):
            raise MediaPathError("INVALID_FILENAME")
    return parts


def contained_media_path(media_root: Path, candidate: Path) -> Path:
    """Resolve symlinks and enforce component-based containment, even if missing."""
    try:
        root = media_root.resolve(strict=True)
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except ValueError as exc:
        raise MediaPathError("PATH_OUTSIDE_MEDIA_ROOT") from exc
    except (OSError, RuntimeError) as exc:
        raise MediaPathError("FILE_NOT_FOUND") from exc
    return resolved


def validate_media_file(media_root: Path, candidate: Path, *, must_exist: bool = True) -> Path:
    resolved = contained_media_path(media_root, candidate)
    try:
        mode = resolved.stat().st_mode
    except FileNotFoundError as exc:
        if not must_exist:
            return resolved
        raise MediaPathError("FILE_NOT_FOUND") from exc
    except OSError as exc:
        raise MediaPathError("FILE_NOT_FOUND") from exc
    if stat.S_ISDIR(mode):
        raise MediaPathError("PATH_IS_DIRECTORY")
    if not stat.S_ISREG(mode):
        raise MediaPathError("INVALID_FILENAME")
    return resolved


def resolve_relative_media_path(media_root: Path, requested_path: str, *, must_exist: bool = True) -> Path:
    """HTTP_REQUEST_PATH: Starlette/ASGI already decoded the URL exactly once."""
    candidate = media_root.resolve().joinpath(*_literal_relative_parts(requested_path))
    return validate_media_file(media_root, candidate, must_exist=must_exist)


def resolve_stored_media_path(media_root: Path, stored_path: str | None, *, must_exist: bool = True) -> Path | None:
    if stored_path is None:
        return None  # Text, HTML and remote URL content do not own a local file.
    # The upload-generated form is /media/... . Declarative creation and the
    # existing URL builder also accept media/... . Never accept physical paths.
    prefix = next((prefix for prefix in ("/media/", "media/") if stored_path.startswith(prefix)), None)
    if prefix is None:
        raise MediaPathError("PATH_OUTSIDE_MEDIA_ROOT")
    candidate = media_root.resolve().joinpath(*_literal_relative_parts(stored_path[len(prefix):]))
    return validate_media_file(media_root, candidate, must_exist=must_exist)


def media_relative_path(media_root: Path, physical_path: Path) -> str:
    return contained_media_path(media_root, physical_path).relative_to(media_root.resolve()).as_posix()


def media_public_path(media_root: Path, physical_path: Path) -> str:
    return "/media/" + media_relative_path(media_root, physical_path)


def _reject_directory_redirects(media_root: Path, candidate: Path) -> None:
    root = media_root.resolve()
    try:
        parts = candidate.absolute().relative_to(root).parts
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink() or (current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
                raise MediaPathError("PATH_OUTSIDE_MEDIA_ROOT")
    except ValueError as exc:
        raise MediaPathError("PATH_OUTSIDE_MEDIA_ROOT") from exc
    except OSError as exc:
        raise MediaPathError("FILE_NOT_FOUND") from exc


def normalize_upload_filename(filename: str | None) -> str:
    display_name = unicodedata.normalize("NFC", filename or "").strip()
    try:
        parts = _literal_relative_parts(display_name)
    except MediaPathError as exc:
        raise MediaPathError("INVALID_FILENAME") from exc
    if (len(parts) != 1 or "/" in display_name or "\\" in display_name
            or display_name in (".", "..") or len(display_name) > 255
            or display_name.endswith((".", " ")) or PureWindowsPath(display_name).is_reserved()
            or any(ord(char) < 32 or char in ':<>"|?*' for char in display_name)
            or unquote(display_name) != display_name):
        raise MediaPathError("INVALID_FILENAME")
    return display_name


_MIME_EXTENSIONS = {"video/mp4": ".mp4", "video/webm": ".webm", "image/jpeg": ".jpg",
                    "image/png": ".png", "audio/mpeg": ".mp3", "audio/wav": ".wav"}


@dataclass(frozen=True)
class StoredUpload:
    physical_path: Path
    filename: str
    size: int


def write_media_upload(media_root: Path, directory_parts: tuple[str, ...], filename: str | None,
                       content_type: str | None, source: BinaryIO) -> StoredUpload:
    """Write completely, then atomically link to a unique, never-overwritten name.

    Same-directory hard-link promotion is atomic and fails if the target exists
    on POSIX and NTFS. Unsupported filesystems fail closed (no overwrite fallback).
    A failed DB commit may leave a *complete* orphan; it must never delete an
    existing file, nor leave a committed row pointing at a partial upload.
    """
    display_name = normalize_upload_filename(filename)
    for part in directory_parts:
        if normalize_upload_filename(part) != part:
            raise MediaPathError("INVALID_FILENAME")
    if not directory_parts:
        raise MediaPathError("INVALID_FILENAME")
    suffix = Path(display_name).suffix.lower()
    if suffix and not re.fullmatch(r"\.[a-z0-9_-]{1,16}", suffix):
        raise MediaPathError("INVALID_FILENAME")
    if not suffix:
        suffix = _MIME_EXTENSIONS.get((content_type or "").lower(), ".bin")
    directory = media_root.resolve().joinpath(*directory_parts)
    _reject_directory_redirects(media_root, directory)
    directory = contained_media_path(media_root, directory)
    temporary: Path | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _reject_directory_redirects(media_root, directory)
        directory = contained_media_path(media_root, directory)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".upload-", suffix=".tmp", dir=directory, delete=False) as buffer:
            temporary = Path(buffer.name)
            created_stat = os.fstat(buffer.fileno())
            temporary_identity = (created_stat.st_dev, created_stat.st_ino)
            shutil.copyfileobj(source, buffer)
            buffer.flush()
            os.fsync(buffer.fileno())
            size = buffer.tell()
        if size == 0:
            raise MediaPathError("UPLOAD_WRITE_FAILED")
        validate_media_file(media_root, temporary)
        for _ in range(10):
            destination = contained_media_path(media_root, directory / f"{uuid4().hex}{suffix}")
            try:
                os.link(temporary, destination)  # Never os.replace()/open('wb') on the final name.
                return StoredUpload(destination, display_name, size)
            except FileExistsError:
                continue
        raise MediaPathError("UPLOAD_WRITE_FAILED")
    except MediaPathError:
        raise
    except (OSError, ValueError) as exc:
        raise MediaPathError("UPLOAD_WRITE_FAILED") from exc
    finally:
        if temporary is not None:
            # Only the temporary created by this operation; never remove a final file.
            try:
                _reject_directory_redirects(media_root, temporary.parent)
                validate_media_file(media_root, temporary)
                current_stat = temporary.lstat()
                if stat.S_ISLNK(current_stat.st_mode) or (current_stat.st_dev, current_stat.st_ino) != temporary_identity:
                    raise MediaPathError("UPLOAD_WRITE_FAILED")
                temporary.unlink()
            except OSError as exc:
                raise MediaPathError("UPLOAD_WRITE_FAILED") from exc


def collect_media_tree_files(media_root: Path, directory_parts: tuple[str, ...]) -> list[Path]:
    """Preflight a client tree without rmtree or following directory symlinks."""
    if not directory_parts:
        raise MediaPathError("INVALID_FILENAME")
    for part in directory_parts:
        if normalize_upload_filename(part) != part:
            raise MediaPathError("INVALID_FILENAME")
    directory = media_root.resolve().joinpath(*directory_parts)
    _reject_directory_redirects(media_root, directory)
    directory = contained_media_path(media_root, directory)
    if not directory.exists():
        return []
    files: list[Path] = []

    def visit(current: Path):
        contained_media_path(media_root, current)
        if current.is_symlink() or (getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
            raise MediaPathError("PATH_OUTSIDE_MEDIA_ROOT")
        if current.is_dir():
            for child in current.iterdir():
                visit(child)
        else:
            files.append(validate_media_file(media_root, current))

    # Check the lexical directory as well, before resolve could hide a link.
    try:
        visit(media_root.joinpath(*directory_parts))
    except OSError as exc:
        raise MediaPathError("FILE_NOT_FOUND") from exc
    return files
