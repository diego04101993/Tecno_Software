from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_settings
from app.services.media_streaming import (
    MediaRangeError,
    build_media_response,
    build_range_not_satisfiable_response,
    resolve_media_path,
)


router = APIRouter(prefix="/media", tags=["media"])
settings = get_settings()


@router.api_route("/{media_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_media(media_path: str, request: Request):
    try:
        file_path = resolve_media_path(settings.MEDIA_ROOT, media_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Archivo no encontrado") from exc

    range_header = request.headers.get("range")
    try:
        return build_media_response(file_path, request.method, range_header)
    except MediaRangeError:
        return build_range_not_satisfiable_response(file_path, file_path.stat().st_size)
