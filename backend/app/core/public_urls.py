from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit

from fastapi import Request

from app.core.config import get_settings


settings = get_settings()


def _normalize_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None

    candidate = base_url.strip()
    if not candidate:
        return None

    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlsplit(candidate)
    if not parsed.netloc:
        return None

    path = parsed.path or "/"
    if not path.endswith("/"):
        path = f"{path}/"

    return urlunsplit((parsed.scheme or "http", parsed.netloc, path, "", ""))


def _forwarded_header_value(request: Request, key: str) -> str | None:
    forwarded = request.headers.get("forwarded")
    if not forwarded:
        return None

    first_entry = forwarded.split(",", 1)[0]
    for segment in first_entry.split(";"):
        name, separator, value = segment.strip().partition("=")
        if separator and name.lower() == key.lower():
            return value.strip().strip('"')
    return None


def build_public_base_url(request: Request) -> str:
    explicit_base_url = _normalize_base_url(settings.PUBLIC_BASE_URL)
    if explicit_base_url:
        return explicit_base_url

    forwarded_proto = request.headers.get("x-forwarded-proto") or _forwarded_header_value(request, "proto")
    forwarded_host = request.headers.get("x-forwarded-host") or _forwarded_header_value(request, "host")
    forwarded_port = request.headers.get("x-forwarded-port")

    scheme = (forwarded_proto or request.url.scheme or "http").split(",", 1)[0].strip() or "http"
    host = (forwarded_host or request.headers.get("host") or request.url.netloc).split(",", 1)[0].strip()

    if forwarded_port and host and ":" not in host:
        host = f"{host}:{forwarded_port.strip()}"

    root_path = request.scope.get("root_path", "") or "/"
    normalized_root = root_path if root_path.endswith("/") else f"{root_path}/"
    if not normalized_root.startswith("/"):
        normalized_root = f"/{normalized_root}"

    return _normalize_base_url(f"{scheme}://{host}{normalized_root}") or str(request.base_url)


def build_public_media_url(request: Request, path: str | None) -> str | None:
    if not path:
        return None
    return urljoin(build_public_base_url(request), path.lstrip("/"))


def build_absolute_public_url(base_url: str, path: str | None) -> str | None:
    if not path:
        return None
    return urljoin(base_url, path.lstrip("/"))
