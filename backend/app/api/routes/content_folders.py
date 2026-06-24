from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.entities import ContentFolder, ContentItem, User
from app.schemas.domain import ContentFolderCreate, ContentFolderDeleteImpactRead, ContentFolderRead, ContentFolderUpdate
from app.services.content_cleanup import build_folder_delete_impact, cleanup_files, delete_folder_tree
from app.services.tenancy import (
    apply_client_filter,
    assert_client_exists,
    can_write_client_scope,
    get_content_folder_in_scope,
    require_client_scope,
)


router = APIRouter()


def _ensure_folder_name_available(
    db: Session,
    *,
    client_id: str,
    parent_id: str | None,
    name: str,
    exclude_folder_id: str | None = None,
) -> None:
    name = name.strip()
    query = select(ContentFolder.id).where(
        ContentFolder.client_id == client_id,
        ContentFolder.name == name,
    )
    if parent_id is None:
        query = query.where(ContentFolder.parent_id.is_(None))
    else:
        query = query.where(ContentFolder.parent_id == parent_id)
    if exclude_folder_id:
        query = query.where(ContentFolder.id != exclude_folder_id)

    if db.scalar(query.limit(1)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una carpeta con ese nombre en este nivel")


@router.get("/content-folders", response_model=list[ContentFolderRead])
def list_content_folders(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ContentFolder]:
    query = apply_client_filter(
        select(ContentFolder).order_by(ContentFolder.sort_order.asc(), ContentFolder.created_at.asc()),
        ContentFolder,
        current_user,
    )
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(ContentFolder.client_id == target_client_id)
    return list(db.scalars(query))


@router.get("/content-folders/{folder_id}/delete-impact", response_model=ContentFolderDeleteImpactRead)
def get_content_folder_delete_impact(
    folder_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    folder = get_content_folder_in_scope(db, folder_id, current_user)
    return build_folder_delete_impact(folder, db)


@router.post("/content-folders", response_model=ContentFolderRead, status_code=status.HTTP_201_CREATED)
def create_content_folder(
    payload: ContentFolderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContentFolder:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create content folders")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)

    parent_id = None
    if payload.parent_id:
        parent = get_content_folder_in_scope(db, payload.parent_id, current_user)
        if parent.client_id != client_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La carpeta padre pertenece a otro cliente")
        parent_id = parent.id

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre de la carpeta es obligatorio")

    _ensure_folder_name_available(db, client_id=client_id, parent_id=parent_id, name=name)

    folder = ContentFolder(
        client_id=client_id,
        parent_id=parent_id,
        name=name,
        sort_order=payload.sort_order,
        is_active=True,
    )
    db.add(folder)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una carpeta con ese nombre en este nivel") from exc
    db.refresh(folder)
    return folder


@router.patch("/content-folders/{folder_id}", response_model=ContentFolderRead)
def rename_content_folder(
    folder_id: str,
    payload: ContentFolderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContentFolder:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update content folders")

    folder = get_content_folder_in_scope(db, folder_id, current_user)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El nombre de la carpeta es obligatorio")

    _ensure_folder_name_available(
        db,
        client_id=folder.client_id,
        parent_id=folder.parent_id,
        name=name,
        exclude_folder_id=folder.id,
    )

    folder.name = name
    db.add(folder)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe una carpeta con ese nombre en este nivel") from exc
    db.refresh(folder)
    return folder


@router.delete("/content-folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_content_folder(
    folder_id: str,
    cascade: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete content folders")

    folder = get_content_folder_in_scope(db, folder_id, current_user)
    if cascade:
        delete_result = delete_folder_tree(folder, db)
        db.commit()
        cleanup_files(delete_result["file_paths"])
        return

    if db.scalar(select(ContentFolder.id).where(ContentFolder.parent_id == folder.id).limit(1)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La carpeta todavía tiene subcarpetas")
    if db.scalar(select(ContentItem.id).where(ContentItem.folder_id == folder.id).limit(1)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La carpeta todavía tiene contenidos")

    db.delete(folder)
    db.commit()
