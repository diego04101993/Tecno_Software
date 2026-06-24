from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.entities import Dataset, Layout, LayoutBindingField, LayoutDataBinding, LayoutRevision, User
from app.schemas.domain import (
    LayoutBindingFieldRead,
    LayoutBindingFieldSet,
    LayoutBindingValidationRequest,
    LayoutCreate,
    LayoutDataBindingCreate,
    LayoutDataBindingRead,
    LayoutDataBindingUpdate,
    LayoutRead,
    LayoutRevisionCreate,
    LayoutRevisionRead,
    LayoutRevisionUpdate,
)
from app.services.layout_bindings import build_layout_data_preview, resolve_binding_payload
from app.services.layout_editor import (
    build_editor_state_payload,
    build_layout_preview_payload,
    create_layout_revision,
    get_layout_revisions,
    publish_layout_revision,
    serialize_layout_revision,
    update_layout_revision,
)
from app.services.tenancy import (
    apply_client_filter,
    assert_client_exists,
    branch_layout_ids_query,
    can_write_branch_scope,
    can_write_client_scope,
    get_dataset_in_scope,
    get_layout_binding_in_scope,
    get_layout_in_scope,
    is_branch_scoped,
    require_branch_assignment,
    require_client_scope,
)


router = APIRouter()


def ensure_dataset_matches_layout(dataset: Dataset, layout: Layout) -> Dataset:
    if dataset.client_id != layout.client_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found for the selected layout client")
    return dataset


def ensure_layout_revision_matches_layout(revision: LayoutRevision, layout: Layout) -> LayoutRevision:
    if revision.layout_id != layout.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout revision not found")
    return revision


def validate_zone_key(layout: Layout, zone_key: str | None) -> None:
    if zone_key is None:
        return

    available_zone_keys = {str(zone.get("key")) for zone in layout.zones}
    if layout.zones and zone_key not in available_zone_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The selected zone does not exist in the layout")


@router.get("/", response_model=list[LayoutRead])
def list_layouts(
    client_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Layout]:
    query = apply_client_filter(select(Layout).order_by(Layout.created_at.desc()), Layout, current_user)
    target_client_id = require_client_scope(current_user, client_id) if client_id or current_user.client_id else None
    if target_client_id:
        query = query.where(Layout.client_id == target_client_id)
    if is_branch_scoped(current_user):
        query = query.where(Layout.id.in_(branch_layout_ids_query(require_branch_assignment(current_user))))
    return list(db.scalars(query))


@router.post("/", response_model=LayoutRead, status_code=status.HTTP_201_CREATED)
def create_layout(
    payload: LayoutCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Layout:
    if not can_write_client_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create layouts")

    client_id = require_client_scope(current_user, payload.client_id)
    assert_client_exists(db, client_id)
    layout = Layout(
        client_id=client_id,
        name=payload.name,
        template=payload.template,
        canvas_width=payload.canvas_width,
        canvas_height=payload.canvas_height,
        zones=payload.zones,
        is_default=payload.is_default,
    )
    db.add(layout)
    db.commit()
    db.refresh(layout)
    return layout


@router.get("/{layout_id}/revisions", response_model=list[LayoutRevisionRead])
def list_layout_revisions(
    layout_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LayoutRevision]:
    layout = get_layout_in_scope(db, layout_id, current_user)
    return get_layout_revisions(db, layout)


@router.post("/{layout_id}/revisions", response_model=LayoutRevisionRead, status_code=status.HTTP_201_CREATED)
def create_revision(
    layout_id: str,
    payload: LayoutRevisionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LayoutRevision:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create layout revisions")

    layout = get_layout_in_scope(db, layout_id, current_user)
    return create_layout_revision(
        db,
        layout,
        name=payload.name,
        notes=payload.notes,
        clone_from_revision_id=payload.clone_from_revision_id,
        current_user=current_user,
    )


@router.get("/{layout_id}/revisions/{revision_id}", response_model=LayoutRevisionRead)
def get_revision(
    layout_id: str,
    revision_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LayoutRevision:
    layout = get_layout_in_scope(db, layout_id, current_user)
    revision = db.get(LayoutRevision, revision_id)
    if not revision:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout revision not found")
    return ensure_layout_revision_matches_layout(revision, layout)


@router.patch("/{layout_id}/revisions/{revision_id}", response_model=LayoutRevisionRead)
def patch_revision(
    layout_id: str,
    revision_id: str,
    payload: LayoutRevisionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LayoutRevision:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update layout revisions")

    layout = get_layout_in_scope(db, layout_id, current_user)
    revision = db.get(LayoutRevision, revision_id)
    if not revision:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout revision not found")
    revision = ensure_layout_revision_matches_layout(revision, layout)
    return update_layout_revision(
        db,
        layout,
        revision,
        name=payload.name,
        notes=payload.notes,
        editor_state_json=payload.editor_state_json,
    )


@router.post("/{layout_id}/revisions/{revision_id}/publish", response_model=LayoutRevisionRead)
def publish_revision(
    layout_id: str,
    revision_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LayoutRevision:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to publish layout revisions")

    layout = get_layout_in_scope(db, layout_id, current_user)
    revision = db.get(LayoutRevision, revision_id)
    if not revision:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout revision not found")
    revision = ensure_layout_revision_matches_layout(revision, layout)
    return publish_layout_revision(db, layout, revision)


@router.get("/{layout_id}/bindings", response_model=list[LayoutDataBindingRead])
def list_layout_bindings(
    layout_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LayoutDataBinding]:
    layout = get_layout_in_scope(db, layout_id, current_user)
    return list(
        db.scalars(
            select(LayoutDataBinding)
            .where(LayoutDataBinding.layout_id == layout.id)
            .order_by(LayoutDataBinding.sort_order.asc(), LayoutDataBinding.created_at.asc())
        )
    )


@router.post("/{layout_id}/bindings", response_model=LayoutDataBindingRead, status_code=status.HTTP_201_CREATED)
def create_layout_binding(
    layout_id: str,
    payload: LayoutDataBindingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LayoutDataBinding:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to create layout bindings")

    layout = get_layout_in_scope(db, layout_id, current_user)
    validate_zone_key(layout, payload.zone_key)
    dataset = ensure_dataset_matches_layout(get_dataset_in_scope(db, payload.dataset_id, current_user), layout)

    binding = LayoutDataBinding(
        layout_id=layout.id,
        dataset_id=dataset.id,
        name=payload.name,
        preset_key=payload.preset_key,
        zone_key=payload.zone_key,
        sort_order=payload.sort_order,
        max_rows=payload.max_rows,
        options_json=payload.options_json,
        is_active=payload.is_active,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding


@router.get("/{layout_id}/bindings/{binding_id}", response_model=LayoutDataBindingRead)
def get_layout_binding(
    layout_id: str,
    binding_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LayoutDataBinding:
    layout = get_layout_in_scope(db, layout_id, current_user)
    binding = get_layout_binding_in_scope(db, binding_id, current_user)
    if binding.layout_id != layout.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout binding not found")
    return binding


@router.patch("/{layout_id}/bindings/{binding_id}", response_model=LayoutDataBindingRead)
def update_layout_binding(
    layout_id: str,
    binding_id: str,
    payload: LayoutDataBindingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LayoutDataBinding:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update layout bindings")

    layout = get_layout_in_scope(db, layout_id, current_user)
    binding = get_layout_binding_in_scope(db, binding_id, current_user)
    if binding.layout_id != layout.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout binding not found")

    if payload.dataset_id is not None:
        dataset = ensure_dataset_matches_layout(get_dataset_in_scope(db, payload.dataset_id, current_user), layout)
        binding.dataset_id = dataset.id
    if payload.name is not None:
        binding.name = payload.name
    if payload.preset_key is not None:
        binding.preset_key = payload.preset_key
    if payload.zone_key is not None:
        validate_zone_key(layout, payload.zone_key)
        binding.zone_key = payload.zone_key
    if payload.sort_order is not None:
        binding.sort_order = payload.sort_order
    if payload.max_rows is not None:
        binding.max_rows = payload.max_rows
    if payload.options_json is not None:
        binding.options_json = payload.options_json
    if payload.is_active is not None:
        binding.is_active = payload.is_active

    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding


@router.delete("/{layout_id}/bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_layout_binding(
    layout_id: str,
    binding_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete layout bindings")

    layout = get_layout_in_scope(db, layout_id, current_user)
    binding = get_layout_binding_in_scope(db, binding_id, current_user)
    if binding.layout_id != layout.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout binding not found")

    db.delete(binding)
    db.commit()


@router.get("/{layout_id}/bindings/{binding_id}/fields", response_model=list[LayoutBindingFieldRead])
def list_layout_binding_fields(
    layout_id: str,
    binding_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LayoutBindingField]:
    layout = get_layout_in_scope(db, layout_id, current_user)
    binding = get_layout_binding_in_scope(db, binding_id, current_user)
    if binding.layout_id != layout.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout binding not found")

    return list(
        db.scalars(
            select(LayoutBindingField)
            .where(LayoutBindingField.binding_id == binding.id)
            .order_by(LayoutBindingField.position_index.asc(), LayoutBindingField.created_at.asc())
        )
    )


@router.post("/{layout_id}/bindings/{binding_id}/fields", response_model=list[LayoutBindingFieldRead])
def replace_layout_binding_fields(
    layout_id: str,
    binding_id: str,
    payload: LayoutBindingFieldSet,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LayoutBindingField]:
    if not can_write_branch_scope(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to update layout field mappings")

    layout = get_layout_in_scope(db, layout_id, current_user)
    binding = get_layout_binding_in_scope(db, binding_id, current_user)
    if binding.layout_id != layout.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Layout binding not found")

    db.execute(delete(LayoutBindingField).where(LayoutBindingField.binding_id == binding.id))
    db.flush()

    created_fields: list[LayoutBindingField] = []
    for index, field in enumerate(payload.fields):
        created = LayoutBindingField(
            binding_id=binding.id,
            target_field=field.target_field,
            column_key=field.column_key,
            display_label=field.display_label,
            fallback_value=field.fallback_value,
            format_hint=field.format_hint,
            position_index=field.position_index if field.position_index is not None else index,
            is_required=field.is_required,
            options_json=field.options_json,
        )
        db.add(created)
        created_fields.append(created)

    db.commit()
    for field in created_fields:
        db.refresh(field)
    return created_fields


@router.get("/{layout_id}/data-preview")
def get_layout_data_preview(
    layout_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    layout = get_layout_in_scope(db, layout_id, current_user)
    bindings = list(
        db.scalars(
            select(LayoutDataBinding)
            .where(LayoutDataBinding.layout_id == layout.id)
            .order_by(LayoutDataBinding.sort_order.asc(), LayoutDataBinding.created_at.asc())
        )
    )
    return build_layout_data_preview(layout, bindings, db, row_limit=limit)


@router.post("/{layout_id}/bindings/validate")
def validate_layout_binding(
    layout_id: str,
    payload: LayoutBindingValidationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    layout = get_layout_in_scope(db, layout_id, current_user)
    dataset = ensure_dataset_matches_layout(get_dataset_in_scope(db, payload.dataset_id, current_user), layout)
    validate_zone_key(layout, payload.zone_key)
    return resolve_binding_payload(
        layout=layout,
        dataset=dataset,
        preset_key=payload.preset_key,
        zone_key=payload.zone_key,
        max_rows=payload.max_rows,
        field_payloads=[field.model_dump() for field in payload.fields],
        binding=None,
        db=db,
    )


@router.get("/{layout_id}/runtime-data")
def get_layout_runtime_data(
    layout_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    layout = get_layout_in_scope(db, layout_id, current_user)
    bindings = list(
        db.scalars(
            select(LayoutDataBinding)
            .where(LayoutDataBinding.layout_id == layout.id, LayoutDataBinding.is_active.is_(True))
            .order_by(LayoutDataBinding.sort_order.asc(), LayoutDataBinding.created_at.asc())
        )
    )
    payload = build_layout_data_preview(layout, bindings, db)
    payload["runtime_version"] = 1
    payload["player_ready"] = False
    return payload


@router.get("/{layout_id}/editor-state")
def get_layout_editor_state(
    layout_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    layout = get_layout_in_scope(db, layout_id, current_user)
    return build_editor_state_payload(db, layout, current_user=current_user)


@router.get("/{layout_id}/preview")
def get_layout_preview(
    layout_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    layout = get_layout_in_scope(db, layout_id, current_user)
    return build_layout_preview_payload(db, layout)
