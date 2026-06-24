import type {
  AudioNormalizationStatus,
  AudioPlaylistKind,
  ChannelMode,
  ContentType,
  DataSourceStatus,
  DataSourceType,
  DatasetColumnType,
  DatasetImportStatus,
  DatasetStatus,
  LayoutBindingPreset,
  LayoutRevisionStatus,
  LayoutTemplate,
  LayoutWidgetType,
  Orientation,
  UserRole,
} from "../types/domain";

const userRoleLabels: Record<UserRole, string> = {
  super_admin: "Superadministrador",
  staff_admin: "Staff admin",
  staff_operator: "Staff operator",
  client: "Administrador de cliente",
  client_admin: "Administrador de cliente",
  client_operator: "Operador de cliente",
  branch_manager: "Gerente de sucursal",
  operator: "Operador",
};

const channelModeLabels: Record<ChannelMode, string> = {
  normal: "Normal",
  expanded: "Expandido",
  videowall: "Videowall",
  audio: "Audio",
  touch: "Touch / kiosco",
};

const orientationLabels: Record<Orientation, string> = {
  horizontal: "Horizontal",
  vertical: "Vertical",
};

const contentTypeLabels: Record<ContentType, string> = {
  image: "Imagen",
  video: "Video",
  url: "URL",
  html: "HTML",
  text: "Texto dinámico",
  audio: "Audio MP3",
};

const layoutTemplateLabels: Record<LayoutTemplate, string> = {
  single: "Pantalla única",
  two_columns: "Dos columnas",
  four_grid: "Cuadrícula 2x2",
  custom: "Personalizado",
};

const audioPlaylistKindLabels: Record<AudioPlaylistKind, string> = {
  music: "Música",
  spot: "Spot",
};

const audioNormalizationLabels: Record<AudioNormalizationStatus, string> = {
  pending: "Pendiente",
  normalized: "Normalizado",
  skipped: "Sin normalizar",
};

const dataSourceTypeLabels: Record<DataSourceType, string> = {
  file_upload: "Archivo",
  google_sheets: "Google Sheets",
  api: "API",
};

const dataSourceStatusLabels: Record<DataSourceStatus, string> = {
  active: "Activo",
  paused: "Pausado",
  error: "Error",
};

const datasetStatusLabels: Record<DatasetStatus, string> = {
  ready: "Listo",
  processing: "Procesando",
  error: "Con error",
};

const datasetImportStatusLabels: Record<DatasetImportStatus, string> = {
  pending: "Pendiente",
  completed: "Completado",
  failed: "Fallido",
};

const datasetColumnTypeLabels: Record<DatasetColumnType, string> = {
  text: "Texto",
  number: "Número",
  datetime: "Fecha/Hora",
  boolean: "Booleano",
  empty: "Vacío",
};

const layoutBindingPresetLabels: Record<LayoutBindingPreset, string> = {
  autobuses: "Autobuses",
  aeropuerto: "Aeropuerto",
  menu: "Menú",
  turnero: "Turnero",
};

const layoutRevisionStatusLabels: Record<LayoutRevisionStatus, string> = {
  draft: "Borrador",
  published: "Publicado",
  archived: "Archivado",
};

const layoutWidgetTypeLabels: Record<LayoutWidgetType, string> = {
  image: "Imagen",
  video: "Video",
  text: "Texto",
  clock: "Reloj",
  url: "URL",
  html: "HTML",
  dataset_table: "Tabla dinámica",
  overlay_png: "Overlay PNG",
  rss: "RSS",
  weather: "Clima",
  social_feed: "Redes sociales",
  api_feed: "API",
  world_clock: "Reloj mundial",
};

const touchScreenKindLabels: Record<string, string> = {
  attract: "Atract",
  home: "Inicio",
  directory: "Directorio",
  location: "Ficha de ubicacion",
  custom: "Personalizada",
};

export function formatUserRole(role: string | null | undefined) {
  if (!role) {
    return "Sin rol";
  }

  return userRoleLabels[role as UserRole] ?? role;
}

export function formatClientStatus(status: string | null | undefined) {
  if (!status) {
    return "Sin estado";
  }

  const normalized = status.toLowerCase();
  if (normalized === "active") {
    return "Activo";
  }
  if (normalized === "inactive") {
    return "Inactivo";
  }
  if (normalized === "suspended") {
    return "Suspendido";
  }

  return normalized
    .split("_")
    .filter(Boolean)
    .map((token) => token.charAt(0).toUpperCase() + token.slice(1))
    .join(" ");
}

export function formatUserStatus(status: string | null | undefined) {
  if (!status) {
    return "Sin estado";
  }

  if (status === "active") {
    return "Activo";
  }

  if (status === "suspended") {
    return "Suspendido";
  }

  return status;
}

export function formatChannelMode(mode: ChannelMode | string | null | undefined) {
  if (!mode) {
    return "Sin modo";
  }

  return channelModeLabels[mode as ChannelMode] ?? mode;
}

export function formatOrientation(orientation: Orientation | null | undefined) {
  return orientation ? orientationLabels[orientation] : "Sin orientación";
}

export function formatContentType(type: ContentType | null | undefined) {
  return type ? contentTypeLabels[type] : "Sin tipo";
}

export function formatLayoutTemplate(template: LayoutTemplate | null | undefined) {
  return template ? layoutTemplateLabels[template] : "Sin plantilla";
}

export function formatAudioPlaylistKind(kind: AudioPlaylistKind | null | undefined) {
  return kind ? audioPlaylistKindLabels[kind] : "Sin clasificación";
}

export function formatAudioNormalizationStatus(status: AudioNormalizationStatus | null | undefined) {
  return status ? audioNormalizationLabels[status] : "Sin estado";
}

export function formatDataSourceType(type: DataSourceType | null | undefined) {
  return type ? dataSourceTypeLabels[type] : "Sin origen";
}

export function formatDataSourceStatus(status: DataSourceStatus | null | undefined) {
  return status ? dataSourceStatusLabels[status] : "Sin estado";
}

export function formatDatasetStatus(status: DatasetStatus | null | undefined) {
  return status ? datasetStatusLabels[status] : "Sin estado";
}

export function formatDatasetImportStatus(status: DatasetImportStatus | null | undefined) {
  return status ? datasetImportStatusLabels[status] : "Sin estado";
}

export function formatDatasetColumnType(type: DatasetColumnType | null | undefined) {
  return type ? datasetColumnTypeLabels[type] : "Sin tipo";
}

export function formatLayoutBindingPreset(preset: LayoutBindingPreset | null | undefined) {
  return preset ? layoutBindingPresetLabels[preset] : "Sin preset";
}

export function formatLayoutRevisionStatus(status: LayoutRevisionStatus | null | undefined) {
  return status ? layoutRevisionStatusLabels[status] : "Sin estado";
}

export function formatLayoutWidgetType(widgetType: LayoutWidgetType | null | undefined) {
  return widgetType ? layoutWidgetTypeLabels[widgetType] : "Sin widget";
}

export function formatTouchScreenKind(kind: string | null | undefined) {
  if (!kind) {
    return "Sin tipo";
  }

  return touchScreenKindLabels[kind] ?? kind;
}

export function formatHeartbeatState(lastPingAt: string | null | undefined) {
  if (!lastPingAt) {
    return {
      label: "Sin heartbeat",
      tone: "rose" as const,
      stale: true,
    };
  }

  const timestamp = new Date(lastPingAt);
  const diffMs = Math.max(0, Date.now() - timestamp.getTime());
  const diffSeconds = Math.floor(diffMs / 1000);
  const stale = diffSeconds > 90;

  return {
    label: formatHeartbeatAge(diffSeconds),
    tone: stale ? ("rose" as const) : ("emerald" as const),
    stale,
  };
}

export function formatHeartbeatAge(ageSeconds: number | null | undefined) {
  if (ageSeconds === null || ageSeconds === undefined || !Number.isFinite(ageSeconds)) {
    return "Sin registro";
  }

  const safeSeconds = Math.max(0, Math.floor(ageSeconds));
  if (safeSeconds < 60) {
    return `Hace ${safeSeconds} segundo${safeSeconds === 1 ? "" : "s"}`;
  }

  const minutes = Math.floor(safeSeconds / 60);
  if (minutes < 60) {
    return `Hace ${minutes} minuto${minutes === 1 ? "" : "s"}`;
  }

  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `Hace ${hours} hora${hours === 1 ? "" : "s"}`;
  }

  const days = Math.floor(hours / 24);
  return `Hace ${days} dia${days === 1 ? "" : "s"}`;
}

export function formatChannelHeartbeatState(channel: {
  last_heartbeat_at?: string | null;
  last_ping_at?: string | null;
  heartbeat_age_seconds?: number | null;
}) {
  const ageSeconds = channel.heartbeat_age_seconds;
  const lastHeartbeatAt = channel.last_heartbeat_at ?? channel.last_ping_at ?? null;

  if (ageSeconds !== null && ageSeconds !== undefined) {
    return {
      label: formatHeartbeatAge(ageSeconds),
      tone: ageSeconds > 90 ? ("rose" as const) : ("emerald" as const),
      stale: ageSeconds > 90,
    };
  }

  return formatHeartbeatState(lastHeartbeatAt);
}
