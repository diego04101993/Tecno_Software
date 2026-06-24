from __future__ import annotations

from pathlib import Path
import shutil

from fastapi import HTTPException, status
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import Client, User


settings = get_settings()


def _delete_directory(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        return


def cleanup_client_storage(client_id: str) -> None:
    _delete_directory(settings.MEDIA_ROOT / client_id)
    _delete_directory(settings.MEDIA_ROOT / "datasets" / client_id)


def delete_client_cascade(client: Client, db: Session) -> None:
    try:
        db.execute(delete(User).where(User.client_id == client.id))
        db.execute(delete(Client).where(Client.id == client.id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se pudo eliminar el cliente completo por dependencias activas o restricciones de integridad",
        ) from exc

    cleanup_client_storage(client.id)
