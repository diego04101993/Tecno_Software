from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from types import SimpleNamespace

from app.services.tenant_mutations import write_guard, MutationIntent
from app.services.tenant_ownership import RowRef
from app.services.tenant_relations import conflict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ContentItem, Dataset, Layout, LayoutDataBinding, LayoutRevision, LayoutRevisionStatus, User
from app.core.config import get_settings
from app.services.media_references import content_file_owned, read_payload_media_owned, media_reference_owners, MediaReadContext
from app.services.tenancy import safe_tenant_resource
from app.services.layout_bindings import build_layout_data_preview, normalize_layout_zones


SUPPORTED_WIDGET_TYPES = [
    "image",
    "video",
    "text",
    "clock",
    "url",
    "html",
    "dataset_table",
    "overlay_png",
]

FUTURE_WIDGET_TYPES = [
    "rss",
    "weather",
    "social_feed",
    "api_feed",
    "world_clock",
]

LAYER_ROLES = [
    "background",
    "content",
    "overlay",
    "branding",
    "frame",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_region_id(index: int) -> str:
    return f"region_{index + 1}"


def _new_widget_id() -> str:
    return f"widget_{uuid4().hex[:12]}"


def _clamp_int(value: object, fallback: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(minimum, parsed)


def _coerce_region(region: dict, layout: Layout, index: int) -> dict:
    region_id = str(region.get("id") or region.get("key") or _new_region_id(index)).strip() or _new_region_id(index)
    width = _clamp_int(region.get("width"), layout.canvas_width, minimum=120)
    height = _clamp_int(region.get("height"), layout.canvas_height, minimum=120)
    return {
        "id": region_id,
        "key": str(region.get("key") or region_id).strip() or region_id,
        "label": str(region.get("label") or region.get("name") or f"Region {index + 1}").strip() or f"Region {index + 1}",
        "x": _clamp_int(region.get("x"), 0),
        "y": _clamp_int(region.get("y"), 0),
        "width": width,
        "height": height,
        "z_index": _clamp_int(region.get("z_index"), index + 1, minimum=0),
        "layer_role": str(region.get("layer_role") or "content"),
        "visible": bool(region.get("visible", True)),
        "locked": bool(region.get("locked", False)),
    }


def _default_widget_props(widget_type: str) -> dict:
    base: dict[str, object] = {
        "backgroundColor": "#ffffff",
        "textColor": "#0f172a",
        "fontFamily": "Fraunces",
        "fontSize": 24,
        "fontWeight": 600,
        "textAlign": "left",
        "borderRadius": 16,
        "padding": 16,
        "durationSeconds": 15,
        "showHeaders": True,
        "headerBackgroundColor": "#0f172a",
        "headerTextColor": "#ffffff",
        "rowBackgroundColor": "#ffffff",
        "rowAltBackgroundColor": "#f8fafc",
        "headerHeight": 48,
        "rowHeight": 42,
        "futureScrollMode": "paged",
        "futurePaginationMode": "paged",
        "futureAutoScroll": False,
        "futurePageSize": 8,
    }
    if widget_type in {"image", "video", "overlay_png"}:
        base["fit"] = "cover"
    if widget_type == "clock":
        base["timezone"] = "America/Mexico_City"
        base["format"] = "HH:mm"
    if widget_type == "dataset_table":
        base["showHeaders"] = True
        base["headerConfig"] = {"uppercase": True}
    return base


def _coerce_widget(widget: dict, layout: Layout, region_ids: set[str], index: int) -> dict:
    widget_type = str(widget.get("widget_type") or widget.get("type") or "text")
    width_default = 420 if widget_type == "dataset_table" else 360
    height_default = 280 if widget_type == "dataset_table" else 180
    region_id = str(widget.get("region_id") or next(iter(region_ids), "main"))
    if region_id not in region_ids:
        region_id = next(iter(region_ids), "main")

    normalized_props = _default_widget_props(widget_type)
    raw_props = widget.get("props_json") if isinstance(widget.get("props_json"), dict) else {}
    normalized_props.update(raw_props)

    binding_ref = widget.get("binding_ref") if isinstance(widget.get("binding_ref"), dict) else {}
    normalized_binding_ref = {
        "binding_id": binding_ref.get("binding_id"),
        "dataset_id": binding_ref.get("dataset_id"),
    }

    return {
        "id": str(widget.get("id") or _new_widget_id()),
        "widget_type": widget_type,
        "name": str(widget.get("name") or widget_type.replace("_", " ").title()),
        "region_id": region_id,
        "layer_role": str(widget.get("layer_role") or ("overlay" if widget_type == "overlay_png" else "content")),
        "x": _clamp_int(widget.get("x"), 40),
        "y": _clamp_int(widget.get("y"), 40),
        "width": _clamp_int(widget.get("width"), width_default, minimum=40),
        "height": _clamp_int(widget.get("height"), height_default, minimum=40),
        "z_index": _clamp_int(widget.get("z_index"), index + 1, minimum=0),
        "opacity": max(0.0, min(1.0, float(widget.get("opacity", 1) or 1))),
        "rotation": float(widget.get("rotation", 0) or 0),
        "visible": bool(widget.get("visible", True)),
        "locked": bool(widget.get("locked", False)),
        "props_json": normalized_props,
        "binding_ref": normalized_binding_ref,
    }


def build_initial_editor_state(layout: Layout) -> dict:
    normalized_zones = normalize_layout_zones(layout)
    regions = []
    for index, zone in enumerate(normalized_zones):
        regions.append(
            {
                "id": zone["key"],
                "key": zone["key"],
                "label": zone["label"],
                "x": zone["x"],
                "y": zone["y"],
                "width": zone["width"],
                "height": zone["height"],
                "z_index": index + 1,
                "layer_role": "content",
                "visible": True,
                "locked": False,
            }
        )

    return {
        "version": 1,
        "canvas": {
            "width": layout.canvas_width,
            "height": layout.canvas_height,
            "background_color": "#0f172a",
        },
        "guides": {
            "show_grid": True,
            "show_safe_area": True,
            "snap_threshold": 12,
        },
        "supported_widget_types": list(SUPPORTED_WIDGET_TYPES),
        "future_widget_types": list(FUTURE_WIDGET_TYPES),
        "layer_roles": list(LAYER_ROLES),
        "regions": regions,
        "widgets": [],
        "settings": {
            "multiple_regions_enabled": True,
            "multiple_layers_enabled": True,
            "preserve_campaign_compatibility": True,
            "preserve_bindings_compatibility": True,
            "preserve_runtime_compatibility": True,
        },
    }


def normalize_editor_state(editor_state: dict | None, layout: Layout) -> dict:
    base_state = build_initial_editor_state(layout)
    raw_state = deepcopy(editor_state or {})

    raw_canvas = raw_state.get("canvas") if isinstance(raw_state.get("canvas"), dict) else {}
    base_state["canvas"] = {
        "width": _clamp_int(raw_canvas.get("width"), layout.canvas_width, minimum=240),
        "height": _clamp_int(raw_canvas.get("height"), layout.canvas_height, minimum=240),
        "background_color": str(raw_canvas.get("background_color") or "#0f172a"),
    }

    raw_guides = raw_state.get("guides") if isinstance(raw_state.get("guides"), dict) else {}
    base_state["guides"] = {
        "show_grid": bool(raw_guides.get("show_grid", True)),
        "show_safe_area": bool(raw_guides.get("show_safe_area", True)),
        "snap_threshold": _clamp_int(raw_guides.get("snap_threshold"), 12, minimum=4),
    }

    raw_regions = raw_state.get("regions") if isinstance(raw_state.get("regions"), list) else base_state["regions"]
    regions = [_coerce_region(region, layout, index) for index, region in enumerate(raw_regions)] or base_state["regions"]
    region_ids = {region["id"] for region in regions}

    raw_widgets = raw_state.get("widgets") if isinstance(raw_state.get("widgets"), list) else []
    widgets = [_coerce_widget(widget, layout, region_ids, index) for index, widget in enumerate(raw_widgets)]

    raw_settings = raw_state.get("settings") if isinstance(raw_state.get("settings"), dict) else {}
    base_state["settings"] = {
        "multiple_regions_enabled": bool(raw_settings.get("multiple_regions_enabled", True)),
        "multiple_layers_enabled": bool(raw_settings.get("multiple_layers_enabled", True)),
        "preserve_campaign_compatibility": True,
        "preserve_bindings_compatibility": True,
        "preserve_runtime_compatibility": True,
    }

    base_state["regions"] = regions
    base_state["widgets"] = sorted(widgets, key=lambda item: (item["z_index"], item["id"]))
    return base_state


def serialize_layout_revision(revision: LayoutRevision) -> dict:
    return {
        "id": revision.id,
        "created_at": revision.created_at.isoformat() if revision.created_at else None,
        "updated_at": revision.updated_at.isoformat() if revision.updated_at else None,
        "layout_id": revision.layout_id,
        "revision_number": revision.revision_number,
        "name": revision.name,
        "status": revision.status,
        "notes": revision.notes,
        "editor_state_json": revision.editor_state_json,
        "preview_state_json": revision.preview_state_json,
        "published_at": revision.published_at.isoformat() if revision.published_at else None,
        "created_by_user_id": revision.created_by_user_id,
        "is_current_draft": revision.is_current_draft,
        "is_current_published": revision.is_current_published,
    }


def get_layout_revisions(db: Session, layout: Layout) -> list[LayoutRevision]:
    return list(
        db.scalars(
            select(LayoutRevision)
            .where(LayoutRevision.layout_id == layout.id)
            .order_by(LayoutRevision.revision_number.desc(), LayoutRevision.created_at.desc())
        )
    )


def get_current_draft_revision(db: Session, layout: Layout) -> LayoutRevision | None:
    return db.scalar(
        select(LayoutRevision)
        .where(LayoutRevision.layout_id == layout.id, LayoutRevision.is_current_draft.is_(True))
        .order_by(LayoutRevision.revision_number.desc())
    )


def get_current_published_revision(db: Session, layout: Layout) -> LayoutRevision | None:
    return db.scalar(
        select(LayoutRevision)
        .where(LayoutRevision.layout_id == layout.id, LayoutRevision.is_current_published.is_(True))
        .order_by(LayoutRevision.revision_number.desc())
    )


def get_next_revision_number(db: Session, layout: Layout) -> int:
    revisions = get_layout_revisions(db, layout)
    return (max((revision.revision_number for revision in revisions), default=0) + 1) if revisions else 1


def _widget_is_tenant_safe(value, layout: Layout, db: Session, *, references=None, media_checked=False) -> bool:
    if not media_checked and not read_payload_media_owned(db, layout.client_id, value, references=references,
                                                       context=MediaReadContext.WIDGET):
        return False
    if isinstance(value, dict):
        for key, child in value.items():
            model = {"content_id": ContentItem, "dataset_id": Dataset, "layout_id": Layout}.get(key)
            if model and child:
                if not isinstance(child, str):
                    return False
                resource = safe_tenant_resource(db, model, child, layout.client_id)
                if resource is None or (model is ContentItem and not content_file_owned(db, resource)):
                    return False
            if not _widget_is_tenant_safe(child, layout, db, references=references, media_checked=True):
                return False
    elif isinstance(value, list):
        return all(_widget_is_tenant_safe(child, layout, db, references=references, media_checked=True) for child in value)
    return True


def _build_safe_layout_states(layout: Layout, editor_state: dict, db: Session) -> tuple[dict, dict]:
    from app.services.tenant_queries import readable_query
    normalized = normalize_editor_state(editor_state, layout)
    active_bindings = list(
        db.scalars(
            readable_query(select(LayoutDataBinding), LayoutDataBinding, db=db, client_id=layout.client_id)
            .where(LayoutDataBinding.layout_id == layout.id, LayoutDataBinding.is_active.is_(True))
            .order_by(LayoutDataBinding.sort_order.asc(), LayoutDataBinding.created_at.asc())
        )
    )
    binding_preview = build_layout_data_preview(layout, active_bindings, db)
    binding_preview_map = {
        payload["binding"]["id"]: payload
        for payload in binding_preview["bindings"]
        if payload.get("binding") and payload["binding"].get("id")
    }

    editor_widgets = []
    preview_widgets = []
    references = media_reference_owners(db, get_settings().MEDIA_ROOT)
    for widget in normalized["widgets"]:
        if not _widget_is_tenant_safe(widget, layout, db, references=references):
            continue
        binding_id = widget.get("binding_ref", {}).get("binding_id")
        if binding_id and binding_id not in binding_preview_map:
            continue
        # Normalization copied the stored JSON. Keep this validated instance in
        # both responses; an ID shared with another widget grants no trust.
        editor_widgets.append(widget)
        widget_preview = deepcopy(widget)
        if widget["widget_type"] == "dataset_table" and binding_id and binding_id in binding_preview_map:
            widget_preview["binding_preview"] = binding_preview_map[binding_id]
        preview_widgets.append(widget_preview)

    normalized["widgets"] = editor_widgets
    return normalized, {
        "version": 1,
        "canvas": normalized["canvas"],
        "guides": normalized["guides"],
        "regions": normalized["regions"],
        "widgets": preview_widgets,
        "bindings": binding_preview["bindings"],
        "generated_at": now_utc().isoformat(),
        "player_ready": False,
    }


def build_layout_preview_state(layout: Layout, editor_state: dict, db: Session) -> dict:
    _, preview = _build_safe_layout_states(layout, editor_state, db)
    return preview


def sync_layout_from_editor_state(layout: Layout, editor_state: dict) -> None:
    normalized = normalize_editor_state(editor_state, layout)
    layout.canvas_width = normalized["canvas"]["width"]
    layout.canvas_height = normalized["canvas"]["height"]
    layout.zones = [
        {
            "key": region["key"],
            "label": region["label"],
            "x": region["x"],
            "y": region["y"],
            "width": region["width"],
            "height": region["height"],
        }
        for region in normalized["regions"]
    ]


def _clone_editor_state_from_best_source(db: Session, layout: Layout, clone_from_revision_id: str | None = None) -> dict:
    if clone_from_revision_id:
        source = db.get(LayoutRevision, clone_from_revision_id)
        if source and source.layout_id == layout.id:
            return normalize_editor_state(source.editor_state_json, layout)

    current_draft = get_current_draft_revision(db, layout)
    if current_draft:
        return normalize_editor_state(current_draft.editor_state_json, layout)

    current_published = get_current_published_revision(db, layout)
    if current_published:
        return normalize_editor_state(current_published.editor_state_json, layout)

    return build_initial_editor_state(layout)


def _revision_batch(db, layout, current_user, operation, revisions, patches, *, new_values=None, layout_values=None):
    """Prepare all revision flags before the first persistent assignment."""
    if current_user is None:
        raise conflict()
    expected_ids = {item.id for item in revisions}
    intents = [MutationIntent("UPDATE", RowRef("Layout", layout.id), tuple((layout_values or {}).items()))]
    intents.extend(MutationIntent("UPDATE", RowRef("LayoutRevision", item.id),
                                  tuple(patches.get(item.id, {}).items())) for item in revisions)
    if new_values is not None:
        intents.append(MutationIntent("CREATE", RowRef("LayoutRevision", new_values["id"]),
                                      tuple((key, value) for key, value in new_values.items() if key != "id")))
    with write_guard(db, actor=current_user, client_id=layout.client_id, operation=operation, intents=tuple(intents)):
        if {item.id for item in get_layout_revisions(db, layout)} != expected_ids:
            raise conflict()
        for item in revisions:
            for key, value in patches.get(item.id, {}).items():
                setattr(item, key, value)
        for key, value in (layout_values or {}).items():
            setattr(layout, key, value)
        created = None
        if new_values is not None:
            created = LayoutRevision(**new_values)
            db.add(created)
        db.commit()
        if created is not None:
            db.refresh(created)
        return created


def ensure_layout_draft_revision(db: Session, layout: Layout, current_user: User | None = None) -> LayoutRevision:
    current_draft = get_current_draft_revision(db, layout)
    if current_draft:
        if not current_draft.preview_state_json:
            update_layout_revision(db, layout, current_draft, current_user=current_user)
        return current_draft
    return create_layout_revision(db, layout, name=f"{layout.name} Draft",
                                  notes="Draft inicial generado desde el layout activo.",
                                  clone_from_revision_id=None, current_user=current_user)


def create_layout_revision(
    db: Session, layout: Layout, *, name: str | None, notes: str | None,
    clone_from_revision_id: str | None, current_user: User | None = None,
) -> LayoutRevision:
    if clone_from_revision_id:
        source = db.get(LayoutRevision, clone_from_revision_id)
        if source is None or source.layout_id != layout.id:
            raise conflict()
    revisions = get_layout_revisions(db, layout)
    editor_state = _clone_editor_state_from_best_source(db, layout, clone_from_revision_id=clone_from_revision_id)
    values = dict(
        id=str(uuid4()), layout_id=layout.id, revision_number=get_next_revision_number(db, layout),
        name=name or f"{layout.name} Draft", status=LayoutRevisionStatus.DRAFT.value, notes=notes,
        editor_state_json=editor_state, preview_state_json=build_layout_preview_state(layout, editor_state, db),
        created_by_user_id=current_user.id if current_user else None,
        is_current_draft=True, is_current_published=False,
    )
    patches = {item.id: {"is_current_draft": False} for item in revisions if item.is_current_draft}
    return _revision_batch(db, layout, current_user, "create_revision", revisions, patches, new_values=values)


def update_layout_revision(
    db: Session, layout: Layout, revision: LayoutRevision, *, name: str | None = None,
    notes: str | None = None, editor_state_json: dict | None = None, current_user: User | None = None,
) -> LayoutRevision:
    values = {}
    if name is not None:
        values["name"] = name
    if notes is not None:
        values["notes"] = notes
    editor = revision.editor_state_json
    if editor_state_json is not None:
        editor = normalize_editor_state(editor_state_json, layout)
        values["editor_state_json"] = editor
    values["preview_state_json"] = build_layout_preview_state(layout, editor, db)
    values["status"] = LayoutRevisionStatus.DRAFT.value if not revision.is_current_published else LayoutRevisionStatus.PUBLISHED.value
    _revision_batch(db, layout, current_user, "patch_revision", get_layout_revisions(db, layout), {revision.id: values})
    db.refresh(revision)
    return revision


def publish_layout_revision(db: Session, layout: Layout, revision: LayoutRevision, *, current_user: User | None = None) -> LayoutRevision:
    editor = normalize_editor_state(revision.editor_state_json, layout)
    preview = build_layout_preview_state(layout, editor, db)
    revisions = get_layout_revisions(db, layout)
    patches = {}
    for item in revisions:
        values = {"is_current_published": False}
        if item.id == revision.id:
            values.update(is_current_draft=False, status=LayoutRevisionStatus.PUBLISHED.value,
                          published_at=now_utc(), is_current_published=True,
                          editor_state_json=editor, preview_state_json=preview)
        elif item.status == LayoutRevisionStatus.PUBLISHED.value:
            values["status"] = LayoutRevisionStatus.ARCHIVED.value
        patches[item.id] = values
    # The existing normalizer writes only into this detached plain value object.
    proposed = SimpleNamespace(canvas_width=layout.canvas_width, canvas_height=layout.canvas_height, zones=deepcopy(layout.zones))
    sync_layout_from_editor_state(proposed, editor)
    layout_values = dict(canvas_width=proposed.canvas_width, canvas_height=proposed.canvas_height, zones=proposed.zones)
    _revision_batch(db, layout, current_user, "publish_revision", revisions, patches, layout_values=layout_values)
    db.refresh(revision)
    db.refresh(layout)
    return revision


def safe_revision_payload(revision: LayoutRevision, layout: Layout, db: Session) -> dict:
    payload = serialize_layout_revision(revision)
    from app.services.tenant_ownership import TenantGraph, OwnerState, RowRef
    if revision.created_by_user_id:
        owner = TenantGraph(db).resolve_owner(RowRef("User", revision.created_by_user_id))
        if not (owner.state == OwnerState.GLOBAL_PRINCIPAL or
                (owner.state == OwnerState.OWNED and owner.client_id == layout.client_id)):
            payload["created_by_user_id"] = None
    editor, preview = _build_safe_layout_states(layout, revision.editor_state_json, db)
    payload["preview_state_json"] = preview
    payload["editor_state_json"] = editor
    return payload


def _read_only_revision(db: Session, layout: Layout) -> LayoutRevision:
    current = get_current_draft_revision(db, layout) or get_current_published_revision(db, layout)
    if current:
        return current
    state = build_initial_editor_state(layout)
    # Never attached to the session; no commit, flush or timestamp changes on GET.
    return LayoutRevision(layout_id=layout.id, revision_number=0, name=layout.name,
                          status=LayoutRevisionStatus.DRAFT.value, editor_state_json=state,
                          preview_state_json=build_layout_preview_state(layout, state, db),
                          is_current_draft=False, is_current_published=False)


def build_editor_state_payload(db: Session, layout: Layout, current_user: User | None = None, *, persist: bool = True) -> dict:
    from app.services.tenant_queries import readable_query
    active_revision = (ensure_layout_draft_revision(db, layout, current_user=current_user)
                       if persist else _read_only_revision(db, layout))
    revisions = [safe_revision_payload(revision, layout, db) for revision in get_layout_revisions(db, layout)]
    bindings_payload = build_layout_data_preview(
        layout,
        list(
            db.scalars(
                readable_query(select(LayoutDataBinding), LayoutDataBinding, db=db, client_id=layout.client_id)
                .where(LayoutDataBinding.layout_id == layout.id)
                .order_by(LayoutDataBinding.sort_order.asc(), LayoutDataBinding.created_at.asc())
            )
        ),
        db,
    )
    return {
        "layout": {
            "id": layout.id,
            "created_at": layout.created_at.isoformat() if layout.created_at else None,
            "updated_at": layout.updated_at.isoformat() if layout.updated_at else None,
            "client_id": layout.client_id,
            "name": layout.name,
            "template": layout.template.value,
            "canvas_width": layout.canvas_width,
            "canvas_height": layout.canvas_height,
            "zones": layout.zones,
            "is_default": layout.is_default,
        },
        "active_revision": safe_revision_payload(active_revision, layout, db),
        "revisions": revisions,
        "supported_widget_types": list(SUPPORTED_WIDGET_TYPES),
        "future_widget_types": list(FUTURE_WIDGET_TYPES),
        "layer_roles": list(LAYER_ROLES),
        "binding_preview": bindings_payload,
        "editor_generated_at": now_utc().isoformat(),
        "player_ready": False,
    }


def build_layout_preview_payload(db: Session, layout: Layout, *, persist: bool = True) -> dict:
    active_revision = get_current_draft_revision(db, layout) or get_current_published_revision(db, layout)
    if not active_revision:
        active_revision = ensure_layout_draft_revision(db, layout) if persist else _read_only_revision(db, layout)
    preview_state = build_layout_preview_state(layout, active_revision.editor_state_json, db)
    return {
        "layout": {
            "id": layout.id,
            "client_id": layout.client_id,
            "name": layout.name,
            "template": layout.template.value,
            "canvas_width": layout.canvas_width,
            "canvas_height": layout.canvas_height,
            "zones": layout.zones,
            "is_default": layout.is_default,
        },
        "revision": safe_revision_payload(active_revision, layout, db),
        "preview": preview_state,
        "preview_generated_at": now_utc().isoformat(),
        "player_ready": False,
    }
