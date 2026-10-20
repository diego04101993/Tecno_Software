from __future__ import annotations

from dataclasses import dataclass
from email.utils import formatdate
from hashlib import md5
from mimetypes import guess_type
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fastapi.responses import Response, StreamingResponse

from app.services.active_media_policy import ActiveMediaPolicy, classify_active_media
from app.services.media_storage import MediaPathError, resolve_relative_media_path


DEFAULT_CHUNK_SIZE = 64 * 1024
# Response-only sandbox: preserve self-contained SVG images without allowing
# scripts, event handlers, same-origin privileges or external subresources.
SVG_CONTENT_SECURITY_POLICY = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"


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
    try:
        return resolve_relative_media_path(media_root, requested_path)
    except MediaPathError as exc:
        # Preserve GET/HEAD /media's existing public 404 contract.
        raise FileNotFoundError("Media file not found") from exc


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


def build_content_disposition(disposition: str, filename: str) -> str:
    """Serialize a presentation name only; never change the physical filename."""
    if disposition not in ("inline", "attachment"):
        raise ValueError("Content disposition must be inline or attachment")

    presentation = "".join(
        "_" if ord(char) < 32 or ord(char) == 127 or char == "\\" else char
        for char in filename
    )
    fallback = "".join(
        char if char.isascii() and (char.isalnum() or char in " ._-") else "_"
        for char in presentation
    )
    stem, separator, extension = fallback.rpartition(".")
    if not separator:
        stem = fallback
    if not any(char.isalnum() for char in stem):
        safe_extension = f".{extension}" if separator and extension.isalnum() else ""
        fallback = f"download{safe_extension}"

    encoded_filename = quote(presentation, safe="")
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded_filename}'


def build_media_headers(file_path: Path, *, stat_result: object, content_length: int, content_range: str | None = None) -> dict[str, str]:
    policy = classify_active_media(file_path.name)
    disposition = "inline"
    if policy == ActiveMediaPolicy.HTML_INERT:
        mime_type = "application/octet-stream"
        disposition = "attachment"
    elif policy == ActiveMediaPolicy.SVG_RESTRICTED:
        mime_type = "image/svg+xml"
    else:
        mime_type = guess_type(str(file_path))[0] or "application/octet-stream"
    etag_base = f"{stat_result.st_mtime}-{stat_result.st_size}"
    etag = f'"{md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
    headers = {
        "accept-ranges": "bytes",
        "content-length": str(content_length),
        "content-type": mime_type,
        "content-disposition": build_content_disposition(disposition, file_path.name),
        "last-modified": formatdate(stat_result.st_mtime, usegmt=True),
        "etag": etag,
    }
    if policy != ActiveMediaPolicy.NORMAL_MEDIA:
        headers["x-content-type-options"] = "nosniff"
    if policy == ActiveMediaPolicy.SVG_RESTRICTED:
        headers["content-security-policy"] = SVG_CONTENT_SECURITY_POLICY
        # SVGZ encoding remains unchanged; ranges still address original bytes.
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
