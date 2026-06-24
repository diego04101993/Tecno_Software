from pathlib import Path
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.models.entities import ContentItem, ContentType, User
from app.schemas.domain import ContentCreate, ContentFolderMove, ContentRead
from app.services.content_cleanup import cleanup_files, delete_content_items, resolve_content_duration_seconds
from app.services.tenancy import (
    apply_client_filter,
    assert_client_exists,
    branch_content_ids_query,
    can_write_client_scope,
    get_content_folder_in_scope,
    get_content_in_scope,
    is_branch_scoped,
    require_branch_assignment,
    require_client_scope,
)


router = APIRouter()
settings = get_settings()


@router.get("/", response_model=list[ContentRead])
def list_contents(
    client_id: str | None = Query(default=None),
    folder_id: str | None = Query(default=None),
    type: ContentType | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ContentItem]:
    query = apply_client_filter(select(ContentItem).order_by(ContentItem.created_at.desc()), ContentItem, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(ContentItem.client_id == target_client_id)
    if folder_id:
        folder = get_content_folder_in_scope(db, folder_id, current_user)
        query = query.where(ContentItem.folder_id == folder.id)
    if type:
        query = query.where(ContentItem.type == type)
    if is_branch_scoped(current_user):
        query = query.where(ContentItem.id.in_(branch_content_ids_query(require_branch_assignment(current_user))))
    return list(db.scalars(query))


@router.post("/", response_model=ContentRead, status_code=status.HTTP_201_CREATED)
def create_content(
    payload: ContentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContentItem:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create content")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)
    target_folder = None
    if payload.folder_id:
        target_folder = get_content_folder_in_scope(db, payload.folder_id, current_user)
        if target_folder.client_id != client_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La carpeta no pertenece al cliente seleccionado")
    content = ContentItem(
        client_id=client_id,
        folder_id=target_folder.id if target_folder else None,
        name=payload.name,
        type=payload.type,
        file_path=payload.file_path,
        source_url=payload.source_url,
        html_content=payload.html_content,
        text_content=payload.text_content,
        duration_seconds=resolve_content_duration_seconds(payload.type, payload.duration_seconds, payload.metadata_json),
        metadata_json=payload.metadata_json,
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


@router.post("/upload", response_model=ContentRead, status_code=status.HTTP_201_CREATED)
def upload_content(
    file: UploadFile = File(...),
    name: str = Form(...),
    client_id: str | None = Form(default=None),
    folder_id: str | None = Form(default=None),
    duration_seconds: int | None = Form(default=None),
    audio_kind: str | None = Form(default=None),
    normalization_status: str | None = Form(default=None),
    target_lufs: int | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContentItem:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to upload content")

    scoped_client_id = require_client_scope(current_user, client_id)
    assert_client_exists(db, scoped_client_id)
    target_folder = None
    if folder_id:
        target_folder = get_content_folder_in_scope(db, folder_id, current_user)
        if target_folder.client_id != scoped_client_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La carpeta no pertenece al cliente seleccionado")

    content_type = ContentType.IMAGE
    if file.content_type and file.content_type.startswith("video/"):
        content_type = ContentType.VIDEO
    elif file.content_type and file.content_type.startswith("audio/"):
        content_type = ContentType.AUDIO

    client_dir = settings.MEDIA_ROOT / scoped_client_id
    client_dir.mkdir(parents=True, exist_ok=True)
    destination = client_dir / file.filename
    with destination.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    public_path = f"/media/{scoped_client_id}/{Path(file.filename).name}"
    metadata: dict[str, str | int | bool | None] = {"filename": file.filename, "content_type": file.content_type}
    if content_type == ContentType.AUDIO:
        resolved_normalization = normalization_status or "pending"
        metadata.update(
            {
                "audio_kind": audio_kind if audio_kind in {"music", "spot"} else "music",
                "normalization_status": resolved_normalization,
                "target_lufs": target_lufs if target_lufs is not None else -14,
                "volume_normalized": resolved_normalization == "normalized",
            }
        )
    resolved_duration_seconds = resolve_content_duration_seconds(content_type, duration_seconds, metadata)
    if content_type == ContentType.VIDEO:
        metadata["duration_seconds"] = resolved_duration_seconds

    content = ContentItem(
        client_id=scoped_client_id,
        folder_id=target_folder.id if target_folder else None,
        name=name,
        type=content_type,
        file_path=public_path,
        duration_seconds=resolved_duration_seconds,
        metadata_json=metadata,
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


@router.patch("/{content_id}/folder", response_model=ContentRead)
def move_content_to_folder(
    content_id: str,
    payload: ContentFolderMove,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContentItem:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to move content")

    content = get_content_in_scope(db, content_id, current_user)
    target_folder = None
    if payload.folder_id:
        target_folder = get_content_folder_in_scope(db, payload.folder_id, current_user)
        if target_folder.client_id != content.client_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La carpeta no pertenece al mismo cliente del contenido")

    content.folder_id = target_folder.id if target_folder else None
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


@router.delete("/{content_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_content(
    content_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete content")

    content = get_content_in_scope(db, content_id, current_user)

    try:
        file_paths = delete_content_items([content], db)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se pudo eliminar el contenido por dependencias activas o restricciones de integridad",
        ) from exc

    cleanup_files(file_paths)
