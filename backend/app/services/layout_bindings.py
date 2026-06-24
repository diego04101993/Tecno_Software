from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Dataset, DatasetColumn, DatasetImport, DatasetRow, Layout, LayoutBindingField, LayoutBindingPreset, LayoutDataBinding


LAYOUT_BINDING_PRESETS: dict[LayoutBindingPreset, dict] = {
    LayoutBindingPreset.AUTOBUSES: {
        "label": "Tarifario de autobuses",
        "description": "Ideal para salidas, horarios, precios y puertas de embarque.",
        "fields": [
            {"key": "destino", "label": "Destino", "required": True, "format_hint": "text"},
            {"key": "hora", "label": "Hora", "required": True, "format_hint": "time"},
            {"key": "precio", "label": "Precio", "required": True, "format_hint": "currency"},
            {"key": "puerta", "label": "Puerta", "required": True, "format_hint": "text"},
            {"key": "servicio", "label": "Servicio", "required": False, "format_hint": "text"},
        ],
        "default_max_rows": 8,
    },
    LayoutBindingPreset.AEROPUERTO: {
        "label": "Salidas de aeropuerto",
        "description": "Muestra vuelo, destino, hora, puerta y estado de salida.",
        "fields": [
            {"key": "vuelo", "label": "Vuelo", "required": True, "format_hint": "text"},
            {"key": "destino", "label": "Destino", "required": True, "format_hint": "text"},
            {"key": "hora", "label": "Hora", "required": True, "format_hint": "time"},
            {"key": "puerta", "label": "Puerta", "required": True, "format_hint": "text"},
            {"key": "estado", "label": "Estado", "required": False, "format_hint": "status"},
        ],
        "default_max_rows": 8,
    },
    LayoutBindingPreset.MENU: {
        "label": "Menú dinámico",
        "description": "Perfecto para menuboards con producto, descripcion, categoria y precio.",
        "fields": [
            {"key": "producto", "label": "Producto", "required": True, "format_hint": "text"},
            {"key": "descripcion", "label": "Descripción", "required": False, "format_hint": "text"},
            {"key": "precio", "label": "Precio", "required": True, "format_hint": "currency"},
            {"key": "categoria", "label": "Categoría", "required": False, "format_hint": "text"},
        ],
        "default_max_rows": 10,
    },
    LayoutBindingPreset.TURNERO: {
        "label": "Turnero / banco",
        "description": "Prepara colas operativas con turno, servicio, modulo y estado.",
        "fields": [
            {"key": "turno", "label": "Turno", "required": True, "format_hint": "text"},
            {"key": "servicio", "label": "Servicio", "required": True, "format_hint": "text"},
            {"key": "modulo", "label": "Modulo", "required": False, "format_hint": "text"},
            {"key": "estado", "label": "Estado", "required": False, "format_hint": "status"},
        ],
        "default_max_rows": 12,
    },
}


def slugify_token(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")


def title_case_token(value: str) -> str:
    return value.replace("_", " ").strip().title() or "Principal"


def normalize_layout_zones(layout: Layout) -> list[dict]:
    if layout.zones:
        normalized: list[dict] = []
        for index, zone in enumerate(layout.zones):
            key = str(zone.get("key") or f"zone_{index + 1}").strip()
            normalized.append(
                {
                    "key": key,
                    "label": title_case_token(key),
                    "x": int(zone.get("x", 0) or 0),
                    "y": int(zone.get("y", 0) or 0),
                    "width": int(zone.get("width", layout.canvas_width) or layout.canvas_width),
                    "height": int(zone.get("height", layout.canvas_height) or layout.canvas_height),
                }
            )
        return normalized

    return [
        {
            "key": "main",
            "label": "Principal",
            "x": 0,
            "y": 0,
            "width": layout.canvas_width,
            "height": layout.canvas_height,
        }
    ]


def get_preset_definition(preset_key: LayoutBindingPreset | str) -> dict:
    key = preset_key if isinstance(preset_key, LayoutBindingPreset) else LayoutBindingPreset(preset_key)
    preset = LAYOUT_BINDING_PRESETS[key]
    return {
        "key": key.value,
        "label": preset["label"],
        "description": preset["description"],
        "default_max_rows": preset["default_max_rows"],
        "fields": [field.copy() for field in preset["fields"]],
    }


def get_dataset_snapshot(db: Session, dataset: Dataset) -> tuple[DatasetImport | None, list[DatasetColumn], list[DatasetRow]]:
    if not dataset.current_import_id:
        return None, [], []

    import_record = db.get(DatasetImport, dataset.current_import_id)
    if not import_record:
        return None, [], []

    columns = list(
        db.scalars(
            select(DatasetColumn)
            .where(DatasetColumn.import_id == import_record.id)
            .order_by(DatasetColumn.position_index.asc())
        )
    )
    rows = list(
        db.scalars(
            select(DatasetRow)
            .where(DatasetRow.import_id == import_record.id)
            .order_by(DatasetRow.row_index.asc())
        )
    )
    return import_record, columns, rows


def serialize_binding(binding: LayoutDataBinding) -> dict:
    return {
        "id": binding.id,
        "created_at": binding.created_at.isoformat() if binding.created_at else None,
        "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
        "layout_id": binding.layout_id,
        "dataset_id": binding.dataset_id,
        "name": binding.name,
        "preset_key": binding.preset_key.value,
        "zone_key": binding.zone_key,
        "sort_order": binding.sort_order,
        "max_rows": binding.max_rows,
        "options_json": binding.options_json,
        "is_active": binding.is_active,
    }


def serialize_field(field: LayoutBindingField) -> dict:
    return {
        "id": field.id,
        "created_at": field.created_at.isoformat() if field.created_at else None,
        "updated_at": field.updated_at.isoformat() if field.updated_at else None,
        "binding_id": field.binding_id,
        "target_field": field.target_field,
        "column_key": field.column_key,
        "display_label": field.display_label,
        "fallback_value": field.fallback_value,
        "format_hint": field.format_hint,
        "position_index": field.position_index,
        "is_required": field.is_required,
        "options_json": field.options_json,
    }


def serialize_dataset(dataset: Dataset | None) -> dict | None:
    if not dataset:
        return None

    return {
        "id": dataset.id,
        "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
        "updated_at": dataset.updated_at.isoformat() if dataset.updated_at else None,
        "client_id": dataset.client_id,
        "data_source_id": dataset.data_source_id,
        "name": dataset.name,
        "slug": dataset.slug,
        "description": dataset.description,
        "status": dataset.status.value,
        "current_import_id": dataset.current_import_id,
        "row_count": dataset.row_count,
        "column_count": dataset.column_count,
    }


def serialize_import(import_record: DatasetImport | None) -> dict | None:
    if not import_record:
        return None

    return {
        "id": import_record.id,
        "created_at": import_record.created_at.isoformat() if import_record.created_at else None,
        "updated_at": import_record.updated_at.isoformat() if import_record.updated_at else None,
        "dataset_id": import_record.dataset_id,
        "source_filename": import_record.source_filename,
        "source_mime_type": import_record.source_mime_type,
        "storage_path": import_record.storage_path,
        "import_status": import_record.import_status.value,
        "detected_sheet_name": import_record.detected_sheet_name,
        "row_count": import_record.row_count,
        "column_count": import_record.column_count,
        "imported_at": import_record.imported_at.isoformat() if import_record.imported_at else None,
        "summary_json": import_record.summary_json,
    }


def build_field_summary(
    preset: dict,
    field_payloads: Sequence[dict],
    columns: Sequence[DatasetColumn],
) -> tuple[list[dict], list[str], list[str], list[str]]:
    by_target = {payload["target_field"]: payload for payload in field_payloads}
    columns_by_key = {column.column_key: column for column in columns}
    mapped_fields: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    missing_required_fields: list[str] = []

    for position_index, definition in enumerate(preset["fields"]):
        payload = by_target.get(definition["key"], {})
        column_key = payload.get("column_key")
        column = columns_by_key.get(column_key) if column_key else None
        fallback_value = payload.get("fallback_value")
        resolved = bool(column or fallback_value)
        label = payload.get("display_label") or definition["label"]
        format_hint = payload.get("format_hint") or definition.get("format_hint")

        if definition["required"] and not resolved:
            missing_required_fields.append(definition["key"])
            errors.append(f"Falta mapear el campo obligatorio '{definition['label']}'.")
        if column_key and not column:
            errors.append(f"La columna '{column_key}' ya no existe en el dataset actual.")

        mapped_fields.append(
            {
                "target_field": definition["key"],
                "label": label,
                "required": definition["required"],
                "column_key": column_key,
                "column_label": column.display_name if column else None,
                "data_type": column.data_type.value if column else None,
                "fallback_value": fallback_value,
                "format_hint": format_hint,
                "position_index": payload.get("position_index", position_index),
                "resolved": resolved,
                "options_json": payload.get("options_json", {}),
            }
        )

    return mapped_fields, errors, warnings, missing_required_fields


def build_preview_rows(rows: Sequence[DatasetRow], mapped_fields: Sequence[dict], row_limit: int) -> list[dict]:
    preview_rows: list[dict] = []
    for row in rows[: max(1, row_limit)]:
        resolved = {"_row_index": row.row_index}
        row_data = row.row_data_json or {}
        for field in mapped_fields:
            value = row_data.get(field["column_key"]) if field["column_key"] else None
            if value in (None, "") and field["fallback_value"] not in (None, ""):
                value = field["fallback_value"]
            resolved[field["target_field"]] = value
        preview_rows.append(resolved)
    return preview_rows


def resolve_binding_payload(
    *,
    layout: Layout,
    dataset: Dataset,
    preset_key: LayoutBindingPreset,
    zone_key: str | None,
    max_rows: int,
    field_payloads: Sequence[dict],
    binding: LayoutDataBinding | None,
    db: Session,
) -> dict:
    zones = normalize_layout_zones(layout)
    selected_zone = next((zone for zone in zones if zone["key"] == zone_key), zones[0] if zones else None)
    preset = get_preset_definition(preset_key)
    import_record, columns, rows = get_dataset_snapshot(db, dataset)

    warnings: list[str] = []
    errors: list[str] = []
    if zone_key and not any(zone["key"] == zone_key for zone in zones):
        errors.append("La zona seleccionada ya no existe en el layout.")
    if not import_record:
        warnings.append("El dataset aun no tiene una importacion vigente para previsualizar.")

    mapped_fields, field_errors, field_warnings, missing_required_fields = build_field_summary(preset, field_payloads, columns)
    errors.extend(field_errors)
    warnings.extend(field_warnings)
    preview_rows = build_preview_rows(rows, mapped_fields, max_rows) if import_record else []

    return {
        "binding": serialize_binding(binding) if binding else None,
        "preset": preset,
        "dataset": serialize_dataset(dataset),
        "current_import": serialize_import(import_record),
        "zone": selected_zone,
        "fields": list(field_payloads),
        "mapped_fields": mapped_fields,
        "headers": [
            {
                "key": field["target_field"],
                "label": field["label"],
                "target_field": field["target_field"],
                "column_key": field["column_key"],
                "required": field["required"],
                "format_hint": field["format_hint"],
            }
            for field in mapped_fields
        ],
        "rows": preview_rows,
        "validation": {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "missing_required_fields": missing_required_fields,
        },
    }


def build_layout_data_preview(layout: Layout, bindings: Sequence[LayoutDataBinding], db: Session, row_limit: int | None = None) -> dict:
    zones = normalize_layout_zones(layout)
    serialized_bindings: list[dict] = []
    for binding in sorted(bindings, key=lambda item: (item.sort_order, item.created_at or datetime.now(timezone.utc))):
        dataset = binding.dataset
        field_payloads = [
            {
                "target_field": field.target_field,
                "column_key": field.column_key,
                "display_label": field.display_label,
                "fallback_value": field.fallback_value,
                "format_hint": field.format_hint,
                "position_index": field.position_index,
                "is_required": field.is_required,
                "options_json": field.options_json,
            }
            for field in sorted(binding.fields, key=lambda item: (item.position_index, item.created_at or datetime.now(timezone.utc)))
        ]
        payload = resolve_binding_payload(
            layout=layout,
            dataset=dataset,
            preset_key=binding.preset_key,
            zone_key=binding.zone_key,
            max_rows=row_limit or binding.max_rows,
            field_payloads=field_payloads,
            binding=binding,
            db=db,
        )
        payload["fields"] = [serialize_field(field) for field in sorted(binding.fields, key=lambda item: (item.position_index, item.created_at or datetime.now(timezone.utc)))]
        serialized_bindings.append(payload)

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
        "zones": zones,
        "bindings": serialized_bindings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
