from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.models.entities import ContentFolder, ContentItem, ContentType, User
from app.schemas.domain import ContentCreate, ContentFolderMove, ContentRead
from app.services.active_media_policy import ActiveMediaPolicy, classify_active_media
from app.services.content_cleanup import cleanup_files, delete_content_items, resolve_content_duration_seconds
from app.services.media_storage import MediaPathError, media_public_path, normalize_upload_filename, write_media_upload, resolve_stored_media_path
from app.services.media_references import media_reference_operation, save_content_reference, readable_media_ids
from app.services.tenancy import (
    optional_relation_id,
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


from app.services.tenant_mutations import delete_guard, write_guard, MutationIntent
from app.services.tenant_ownership import RowRef


from app.services.tenant_responses import read_snapshot
from app.services.tenant_queries import readable_query

router = APIRouter()
settings = get_settings()


def readable_content_ids(db: Session) -> list[str]:
    """Resolve filesystem ownership once, then constrain SQL before pagination.

    Canonical paths (including aliases) cannot be safely compared as SQL strings.
    This bounded-to-the-request allowlist does not load or modify ORM entities.
    """
    return readable_media_ids(db, "ContentItem")


def safe_content_read(content: ContentItem, db: Session) -> ContentRead:
    folder_id = optional_relation_id(db, ContentFolder, content.folder_id, content.client_id)
    return ContentRead.model_validate(content).model_copy(deep=True, update={"folder_id": folder_id})


@router.get("/", response_model=list[ContentRead])
@read_snapshot(response_model=list[ContentRead])
def list_contents(
    client_id: str | None = Query(default=None),
    folder_id: str | None = Query(default=None),
    type: ContentType | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ContentItem]:
    query = apply_client_filter(readable_query(select(ContentItem), ContentItem, db=db).where(
        ContentItem.id.in_(readable_content_ids(db))
    ).order_by(ContentItem.created_at.desc()), ContentItem, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(ContentItem.client_id == target_client_id)
    if folder_id:
        folder = get_content_folder_in_scope(db, folder_id, current_user)
        query = query.where(ContentItem.folder_id == folder.id, ContentItem.client_id == folder.client_id)
    if type:
        query = query.where(ContentItem.type == type)
    if is_branch_scoped(current_user):
        query = query.where(ContentItem.id.in_(branch_content_ids_query(require_branch_assignment(current_user))))
    return [safe_content_read(content, db) for content in db.scalars(query)]


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
    values = dict(
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
    ref = RowRef("ContentItem", str(uuid4()))
    intent = MutationIntent("CREATE", ref, tuple(values.items()))
    with write_guard(db, actor=current_user, client_id=client_id, operation="create_content", intents=(intent,)):
        content = ContentItem(id=ref.identity, **values)
        try:
            save_content_reference(content, db)
        except MediaPathError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=exc.code) from exc
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

    # Use the same normalized name as storage, including trimmed whitespace.
    try:
        filename = normalize_upload_filename(file.filename)
    except MediaPathError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    declared_mime = (file.content_type or "").split(";", 1)[0].strip().lower()
    if classify_active_media(filename) == ActiveMediaPolicy.HTML_INERT or declared_mime in {"text/html", "application/xhtml+xml"}:
        raise HTTPException(status_code=415, detail="ACTIVE_HTML_UPLOAD_NOT_ALLOWED")

    content_type = ContentType.IMAGE
    if file.content_type and file.content_type.startswith("video/"):
        content_type = ContentType.VIDEO
    elif file.content_type and file.content_type.startswith("audio/"):
        content_type = ContentType.AUDIO

    # Validate actor, folder and tenant before storage can create a file.
    # The final path is generated by storage; only StoredUpload may prove it.
    ref = RowRef("ContentItem", str(uuid4()))
    proposed = dict(client_id=scoped_client_id, folder_id=target_folder.id if target_folder else None,
                    name=name, type=content_type, file_path=None)
    intent = MutationIntent("CREATE", ref, tuple(proposed.items()))
    with write_guard(db, actor=current_user, client_id=scoped_client_id,
                     operation="upload_content", intents=(intent,)):
        try:
            uploaded = write_media_upload(settings.MEDIA_ROOT, (scoped_client_id,), file.filename, file.content_type, file.file)
            public_path = media_public_path(settings.MEDIA_ROOT, uploaded.physical_path)
        except MediaPathError as exc:
            raise HTTPException(status_code=400, detail=exc.code) from exc
        metadata: dict[str, str | int | bool | None] = {"filename": uploaded.filename, "content_type": file.content_type}
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
            id=ref.identity,
            client_id=scoped_client_id,
            folder_id=target_folder.id if target_folder else None,
            name=name,
            type=content_type,
            file_path=public_path,
            duration_seconds=resolved_duration_seconds,
            metadata_json=metadata,
        )
        try:
            save_content_reference(content, db, uploaded=uploaded)
        except MediaPathError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except SQLAlchemyError as exc:
            db.rollback()
            # Keep the completed orphan; never remove a file after an ambiguous commit.
            raise HTTPException(status_code=500, detail="UPLOAD_WRITE_FAILED") from exc
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

    folder_id = target_folder.id if target_folder else None
    intent = MutationIntent("UPDATE", RowRef("ContentItem", content.id), (("folder_id", folder_id),))
    with write_guard(db, actor=current_user, client_id=content.client_id,
                     operation="move_content_to_folder", intents=(intent,)):
        content.folder_id = folder_id
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

    with delete_guard(db, (content,), actor=current_user, operation="delete_content"):
        try:
            with media_reference_operation(db):
                file_paths = delete_content_items([content], db)
                db.commit()
                cleanup_files(file_paths, db)
        except MediaPathError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=exc.code) from exc
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No se pudo eliminar el contenido por dependencias activas o restricciones de integridad",
            ) from exc
