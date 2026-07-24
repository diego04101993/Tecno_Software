from __future__ import annotations

from dataclasses import dataclass
from email.utils import formatdate
from hashlib import md5
from mimetypes import guess_type
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import quote, unquote

from fastapi.responses import Response, StreamingResponse


DEFAULT_CHUNK_SIZE = 64 * 1024


class MediaRangeError(ValueError):
    """Raised when the incoming Range header cannot be satisfied."""


@dataclass(frozen=True)
class MediaByteRange:
    start: int
    end: int
    total_size: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total_size}"


def resolve_media_path(media_root: Path, requested_path: str) -> Path:
    root = media_root.resolve()
    decoded_path = unquote(requested_path or "").replace("\\", "/").strip()
    pure_path = PurePosixPath(decoded_path)

    if not decoded_path or decoded_path.startswith("/") or pure_path.is_absolute():
        raise FileNotFoundError("Invalid media path")

    parts = [part for part in pure_path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise FileNotFoundError("Invalid media path")

    candidate = root.joinpath(*parts)
    try:
        resolved_candidate = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError("Media file not found") from exc

    try:
        resolved_candidate.relative_to(root)
    except ValueError as exc:
        raise FileNotFoundError("Media path escapes root") from exc

    if resolved_candidate.is_dir():
        raise FileNotFoundError("Media path points to a directory")

    return resolved_candidate


def parse_range_header(range_header: str, total_size: int) -> MediaByteRange:
    if total_size < 1:
        raise MediaRangeError("Range not satisfiable for empty file")
    if not range_header.startswith("bytes="):
        raise MediaRangeError("Only bytes ranges are supported")

    range_spec = range_header.removeprefix("bytes=").strip()
    if not range_spec or "," in range_spec or "-" not in range_spec:
        raise MediaRangeError("Only a single byte range is supported")

    start_text, end_text = [value.strip() for value in range_spec.split("-", 1)]

    if not start_text:
        if not end_text.isdigit():
            raise MediaRangeError("Invalid suffix range")
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise MediaRangeError("Invalid suffix range length")
        if suffix_length >= total_size:
            start = 0
        else:
            start = total_size - suffix_length
        end = total_size - 1
        return MediaByteRange(start=start, end=end, total_size=total_size)

    if not start_text.isdigit():
        raise MediaRangeError("Invalid range start")

    start = int(start_text)
    if start >= total_size:
        raise MediaRangeError("Range start exceeds file size")

    if not end_text:
        end = total_size - 1
    else:
        if not end_text.isdigit():
            raise MediaRangeError("Invalid range end")
        end = min(int(end_text), total_size - 1)
        if end < start:
            raise MediaRangeError("Range end precedes start")

    return MediaByteRange(start=start, end=end, total_size=total_size)


def iter_file_chunks(file_path: Path, start: int, end: int, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
    remaining = end - start + 1
    with file_path.open("rb") as source:
        source.seek(start)
        while remaining > 0:
            chunk = source.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def build_media_headers(file_path: Path, *, stat_result: object, content_length: int, content_range: str | None = None) -> dict[str, str]:
    mime_type = guess_type(str(file_path))[0] or "application/octet-stream"
    etag_base = f"{stat_result.st_mtime}-{stat_result.st_size}"
    etag = f'"{md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
    filename = file_path.name
    encoded_filename = quote(filename)

    headers = {
        "accept-ranges": "bytes",
        "content-length": str(content_length),
        "content-type": mime_type,
        "content-disposition": f'inline; filename="{filename}"; filename*=UTF-8\'\'{encoded_filename}',
        "last-modified": formatdate(stat_result.st_mtime, usegmt=True),
        "etag": etag,
    }
    if content_range:
        headers["content-range"] = content_range
    return headers


def build_range_not_satisfiable_response(file_path: Path, total_size: int) -> Response:
    stat_result = file_path.stat()
    headers = build_media_headers(file_path, stat_result=stat_result, content_length=0)
    headers["content-range"] = f"bytes */{total_size}"
    return Response(status_code=416, headers=headers)


def build_media_response(file_path: Path, method: str, range_header: str | None) -> Response:
    stat_result = file_path.stat()
    total_size = stat_result.st_size

    if range_header:
        byte_range = parse_range_header(range_header, total_size)
        headers = build_media_headers(
            file_path,
            stat_result=stat_result,
            content_length=byte_range.length,
            content_range=byte_range.content_range,
        )
        if method == "HEAD":
            return Response(status_code=206, headers=headers)
        return StreamingResponse(
            iter_file_chunks(file_path, byte_range.start, byte_range.end),
            status_code=206,
            headers=headers,
            media_type=headers["content-type"],
        )

    headers = build_media_headers(file_path, stat_result=stat_result, content_length=total_size)
    if method == "HEAD":
        return Response(status_code=200, headers=headers)
    return StreamingResponse(
        iter_file_chunks(file_path, 0, max(total_size - 1, 0)),
        status_code=200,
        headers=headers,
        media_type=headers["content-type"],
    )
