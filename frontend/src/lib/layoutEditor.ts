import type {
  LayoutEditorState,
  LayoutRegion,
  LayoutWidget,
  LayoutWidgetType,
} from "../types/domain";

export type SelectionTarget =
  | { kind: "canvas" }
  | { kind: "region"; id: string }
  | { kind: "widget"; id: string };

export type LayoutGuideSet = {
  vertical: number[];
  horizontal: number[];
};

export const canvasPresets = [
  { label: "16:9 Horizontal", width: 1920, height: 1080 },
  { label: "9:16 Vertical", width: 1080, height: 1920 },
  { label: "Personalizada", width: 1280, height: 720 },
];

export function createDefaultRegion(state: LayoutEditorState): LayoutRegion {
  return {
    id: `region_${Date.now()}`,
    key: `region_${state.regions.length + 1}`,
    label: `Región ${state.regions.length + 1}`,
    x: 40,
    y: 40,
    width: Math.max(320, Math.round(state.canvas.width * 0.46)),
    height: Math.max(220, Math.round(state.canvas.height * 0.3)),
    z_index: state.regions.length + 1,
    layer_role: "content",
    visible: true,
    locked: false,
  };
}

export function createDefaultWidget(widgetType: LayoutWidgetType, state: LayoutEditorState, regionId?: string | null): LayoutWidget {
  const defaultRegionId = regionId ?? state.regions[0]?.id ?? "main";
  const id = `widget_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`;
  const isTable = widgetType === "dataset_table";
  const isOverlay = widgetType === "overlay_png";

  return {
    id,
    widget_type: widgetType,
    name: widgetType.replace(/_/g, " "),
    region_id: defaultRegionId,
    layer_role: isOverlay ? "overlay" : widgetType === "image" || widgetType === "video" ? "background" : "content",
    x: 60,
    y: 60,
    width: isTable ? 640 : 360,
    height: isTable ? 360 : widgetType === "text" ? 140 : 220,
    z_index: state.widgets.length + 1,
    opacity: 1,
    rotation: 0,
    visible: true,
    locked: false,
    props_json: {
      title: widgetType === "dataset_table" ? "Tabla dinámica" : "Nuevo widget",
      backgroundColor: widgetType === "overlay_png" ? "transparent" : "#ffffff",
      textColor: "#0f172a",
      fontFamily: "Fraunces",
      fontSize: widgetType === "text" ? 28 : 22,
      fontWeight: 600,
      textAlign: "left",
      padding: 16,
      borderRadius: 16,
      sourceUrl: "",
      textContent: widgetType === "text" ? "Texto de ejemplo" : "",
      htmlContent: "",
      showHeaders: true,
      headerConfig: {
        uppercase: true,
        backgroundColor: "#0f172a",
        textColor: "#ffffff",
      },
      futureScrollMode: "paged",
      futurePaginationMode: "paged",
      futureAutoScroll: false,
      futurePageSize: 8,
    },
    binding_ref: {
      binding_id: null,
      dataset_id: null,
    },
  };
}

export function updateWidget(state: LayoutEditorState, widgetId: string, updater: (widget: LayoutWidget) => LayoutWidget): LayoutEditorState {
  return {
    ...state,
    widgets: state.widgets.map((widget) => (widget.id === widgetId ? updater(widget) : widget)),
  };
}

export function updateRegion(state: LayoutEditorState, regionId: string, updater: (region: LayoutRegion) => LayoutRegion): LayoutEditorState {
  return {
    ...state,
    regions: state.regions.map((region) => (region.id === regionId ? updater(region) : region)),
  };
}

export function removeWidget(state: LayoutEditorState, widgetId: string): LayoutEditorState {
  return {
    ...state,
    widgets: state.widgets.filter((widget) => widget.id !== widgetId),
  };
}

export function removeRegion(state: LayoutEditorState, regionId: string): LayoutEditorState {
  const nextRegions = state.regions.filter((region) => region.id !== regionId);
  const fallbackRegionId = nextRegions[0]?.id ?? "main";
  return {
    ...state,
    regions: nextRegions,
    widgets: state.widgets.map((widget) => (widget.region_id === regionId ? { ...widget, region_id: fallbackRegionId } : widget)),
  };
}

export function clampWithinCanvas(state: LayoutEditorState, bounds: { x: number; y: number; width: number; height: number }) {
  const width = Math.min(bounds.width, state.canvas.width);
  const height = Math.min(bounds.height, state.canvas.height);
  return {
    x: Math.max(0, Math.min(bounds.x, state.canvas.width - width)),
    y: Math.max(0, Math.min(bounds.y, state.canvas.height - height)),
    width: Math.max(40, width),
    height: Math.max(40, height),
  };
}

export function computeGuides(state: LayoutEditorState, currentId: string | null, kind: "widget" | "region"): LayoutGuideSet {
  const others =
    kind === "widget"
      ? state.widgets.filter((widget) => widget.id !== currentId)
      : state.regions.filter((region) => region.id !== currentId);

  const vertical = [0, Math.round(state.canvas.width / 2), state.canvas.width];
  const horizontal = [0, Math.round(state.canvas.height / 2), state.canvas.height];

  for (const item of others) {
    vertical.push(item.x, item.x + Math.round(item.width / 2), item.x + item.width);
    horizontal.push(item.y, item.y + Math.round(item.height / 2), item.y + item.height);
  }

  return { vertical, horizontal };
}

export function applySnapping(value: number, guides: number[], threshold: number) {
  let snapped = value;
  let active: number | null = null;
  for (const guide of guides) {
    if (Math.abs(guide - value) <= threshold) {
      snapped = guide;
      active = guide;
      break;
    }
  }
  return { value: snapped, active };
}

export function sortByLayer<T extends { z_index: number }>(items: T[]) {
  return [...items].sort((a, b) => a.z_index - b.z_index);
}

export function detectCanvasPreset(width: number, height: number) {
  if (width === 1920 && height === 1080) {
    return "16:9 Horizontal";
  }
  if (width === 1080 && height === 1920) {
    return "9:16 Vertical";
  }
  return "Personalizada";
}
