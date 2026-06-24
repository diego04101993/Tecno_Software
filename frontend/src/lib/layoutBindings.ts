import type { DatasetColumn, Layout, LayoutBindingField, LayoutBindingPreset, LayoutBindingPresetInfo, LayoutPreviewZone } from "../types/domain";

type ExistingBindingFieldLike = Pick<
  LayoutBindingField,
  "target_field" | "column_key" | "display_label" | "fallback_value" | "format_hint" | "position_index" | "is_required" | "options_json"
>;

export const layoutBindingPresets: LayoutBindingPresetInfo[] = [
  {
    key: "autobuses",
    label: "Tarifario de autobuses",
    description: "Salidas, horarios, precios y puertas para terminales o centrales.",
    default_max_rows: 8,
    fields: [
      { key: "destino", label: "Destino", required: true, format_hint: "text" },
      { key: "hora", label: "Hora", required: true, format_hint: "time" },
      { key: "precio", label: "Precio", required: true, format_hint: "currency" },
      { key: "puerta", label: "Puerta", required: true, format_hint: "text" },
      { key: "servicio", label: "Servicio", required: false, format_hint: "text" },
    ],
  },
  {
    key: "aeropuerto",
    label: "Salidas de aeropuerto",
    description: "Vuelos, destinos, estado y puerta para pantallas de salidas.",
    default_max_rows: 8,
    fields: [
      { key: "vuelo", label: "Vuelo", required: true, format_hint: "text" },
      { key: "destino", label: "Destino", required: true, format_hint: "text" },
      { key: "hora", label: "Hora", required: true, format_hint: "time" },
      { key: "puerta", label: "Puerta", required: true, format_hint: "text" },
      { key: "estado", label: "Estado", required: false, format_hint: "status" },
    ],
  },
  {
    key: "menu",
    label: "Menú dinámico",
    description: "Productos, descripcion y precios para menuboards o promos.",
    default_max_rows: 10,
    fields: [
      { key: "producto", label: "Producto", required: true, format_hint: "text" },
      { key: "descripcion", label: "Descripción", required: false, format_hint: "text" },
      { key: "precio", label: "Precio", required: true, format_hint: "currency" },
      { key: "categoria", label: "Categoría", required: false, format_hint: "text" },
    ],
  },
  {
    key: "turnero",
    label: "Turnero",
    description: "Turno, servicio, modulo y estado para bancos o filas operativas.",
    default_max_rows: 12,
    fields: [
      { key: "turno", label: "Turno", required: true, format_hint: "text" },
      { key: "servicio", label: "Servicio", required: true, format_hint: "text" },
      { key: "modulo", label: "Modulo", required: false, format_hint: "text" },
      { key: "estado", label: "Estado", required: false, format_hint: "status" },
    ],
  },
];

export function getLayoutBindingPreset(presetKey: LayoutBindingPreset) {
  return layoutBindingPresets.find((preset) => preset.key === presetKey) ?? layoutBindingPresets[0];
}

function normalizeToken(value: string) {
  return value
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

export function suggestColumnKey(columns: DatasetColumn[], targetField: string) {
  const normalizedTarget = normalizeToken(targetField);
  const exactMatch = columns.find((column) => normalizeToken(column.column_key) === normalizedTarget);
  if (exactMatch) {
    return exactMatch.column_key;
  }

  const sourceMatch = columns.find((column) => normalizeToken(column.source_name) === normalizedTarget);
  if (sourceMatch) {
    return sourceMatch.column_key;
  }

  return (
    columns.find((column) => normalizeToken(column.display_name).includes(normalizedTarget) || normalizedTarget.includes(normalizeToken(column.display_name)))
      ?.column_key ?? null
  );
}

export function buildSuggestedBindingFields(
  presetKey: LayoutBindingPreset,
  columns: DatasetColumn[],
  existingFields?: ExistingBindingFieldLike[],
) {
  const preset = getLayoutBindingPreset(presetKey);
  const existingByTarget = new Map(existingFields?.map((field) => [field.target_field, field]) ?? []);

  return preset.fields.map((field, index) => {
    const existing = existingByTarget.get(field.key);
    return {
      target_field: field.key,
      column_key: existing?.column_key ?? suggestColumnKey(columns, field.key),
      display_label: existing?.display_label ?? field.label,
      fallback_value: existing?.fallback_value ?? "",
      format_hint: existing?.format_hint ?? field.format_hint ?? null,
      position_index: existing?.position_index ?? index,
      is_required: existing?.is_required ?? field.required,
      options_json: existing?.options_json ?? {},
    };
  });
}

export function normalizeLayoutZones(layout: Layout): LayoutPreviewZone[] {
  if (Array.isArray(layout.zones) && layout.zones.length > 0) {
    return layout.zones.map((zone, index) => {
      const key = typeof zone.key === "string" ? zone.key : `zone_${index + 1}`;
      return {
        key,
        label: key === "main" ? "Principal" : key.replace(/[_-]+/g, " ").replace(/\b\w/g, (match) => match.toUpperCase()),
        x: Number(zone.x ?? 0),
        y: Number(zone.y ?? 0),
        width: Number(zone.width ?? layout.canvas_width),
        height: Number(zone.height ?? layout.canvas_height),
      };
    });
  }

  return [
    {
      key: "main",
      label: "Principal",
      x: 0,
      y: 0,
      width: layout.canvas_width,
      height: layout.canvas_height,
    },
  ];
}

export function formatDynamicValue(value: unknown, formatHint: string | null | undefined) {
  if (value === null || value === undefined || value === "") {
    return "—";
  }

  if (formatHint === "currency" && typeof value === "number") {
    return new Intl.NumberFormat("es-MX", { style: "currency", currency: "MXN", maximumFractionDigits: 0 }).format(value);
  }

  return String(value);
}
