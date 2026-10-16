from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import Client, ContentItem, User
from app.services.content_cleanup import cleanup_files, preflight_content_files
from app.services.media_storage import MediaPathError, collect_media_tree_files
from app.services.media_references import lock_media_paths, media_reference_operation


settings = get_settings()


def delete_client_cascade(client: Client, db: Session) -> None:
    try:
        # Validate DB paths AND all physical descendants before any cascade/commit.
        # No rmtree: another client's ContentItem may still reference one file.
        contents = list(db.scalars(select(ContentItem).where(ContentItem.client_id == client.id)))
        file_paths = preflight_content_files(contents)
        file_paths.extend(collect_media_tree_files(settings.MEDIA_ROOT, (client.id,)))
        file_paths.extend(collect_media_tree_files(settings.MEDIA_ROOT, ("datasets", client.id)))
        with media_reference_operation(db):
            lock_media_paths(db, file_paths)
            preflight_content_files(contents)
            db.execute(delete(User).where(User.client_id == client.id))
            db.execute(delete(Client).where(Client.id == client.id))
            db.commit()
            cleanup_files(file_paths, db)
    except MediaPathError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se pudo eliminar el cliente completo por dependencias activas o restricciones de integridad",
        ) from exc
