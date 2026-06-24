import { Palette, Ruler, SquarePen } from "lucide-react";

import { formatLayoutWidgetType } from "../lib/labels";
import type { LayoutEditorState, LayoutRegion, LayoutWidget } from "../types/domain";

type PropertiesInspectorProps = {
  state: LayoutEditorState;
  selectedWidget: LayoutWidget | null;
  selectedRegion: LayoutRegion | null;
  canManage: boolean;
  onCanvasChange: (width: number, height: number, backgroundColor: string) => void;
  onWidgetChange: (widgetId: string, patch: Partial<LayoutWidget>) => void;
  onWidgetPropsChange: (widgetId: string, patch: Record<string, unknown>) => void;
  onRegionChange: (regionId: string, patch: Partial<LayoutRegion>) => void;
  onDeleteSelected: () => void;
};

export function PropertiesInspector({
  state,
  selectedWidget,
  selectedRegion,
  canManage,
  onCanvasChange,
  onWidgetChange,
  onWidgetPropsChange,
  onRegionChange,
  onDeleteSelected,
}: PropertiesInspectorProps) {
  const canvasBackgroundColor =
    typeof state.canvas.background_color === "string" ? state.canvas.background_color : "#0f172a";

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="border-b border-slate-200 pb-5">
        <h2 className="font-display text-2xl text-ink">Inspector</h2>
        <p className="mt-1 text-sm text-slate-600">Ajusta posición, tamaño, capas, estilo, tipografía y comportamiento del elemento seleccionado.</p>
      </div>

      <div className="mt-6 space-y-5">
        {!selectedWidget && !selectedRegion ? (
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <span className="rounded-2xl bg-slate-100 p-3 text-accent">
                <Ruler className="h-5 w-5" />
              </span>
              <div>
                <p className="font-semibold text-ink">Propiedades del canvas</p>
                <p className="text-sm text-slate-500">Resolución real del layout y color base del fondo.</p>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-3">
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho</label>
                <input
                  type="number"
                  value={state.canvas.width}
                  disabled={!canManage}
                  onChange={(event) => onCanvasChange(Number(event.target.value), state.canvas.height, canvasBackgroundColor)}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Alto</label>
                <input
                  type="number"
                  value={state.canvas.height}
                  disabled={!canManage}
                  onChange={(event) => onCanvasChange(state.canvas.width, Number(event.target.value), canvasBackgroundColor)}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Fondo</label>
                <input
                  value={canvasBackgroundColor}
                  disabled={!canManage}
                  onChange={(event) => onCanvasChange(state.canvas.width, state.canvas.height, event.target.value)}
                />
              </div>
            </div>
          </div>
        ) : null}

        {selectedRegion ? (
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <span className="rounded-2xl bg-slate-100 p-3 text-accent">
                <SquarePen className="h-5 w-5" />
              </span>
              <div>
                <p className="font-semibold text-ink">Región: {selectedRegion.label}</p>
                <p className="text-sm text-slate-500">Define zonas múltiples y su compatibilidad con campañas y bindings.</p>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Etiqueta</label>
                <input
                  value={selectedRegion.label}
                  disabled={!canManage}
                  onChange={(event) => onRegionChange(selectedRegion.id, { label: event.target.value })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Key</label>
                <input
                  value={selectedRegion.key}
                  disabled={!canManage}
                  onChange={(event) => onRegionChange(selectedRegion.id, { key: event.target.value })}
                />
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-4">
              {[
                { label: "X", value: selectedRegion.x, setter: (value: number) => onRegionChange(selectedRegion.id, { x: value }) },
                { label: "Y", value: selectedRegion.y, setter: (value: number) => onRegionChange(selectedRegion.id, { y: value }) },
                { label: "Ancho", value: selectedRegion.width, setter: (value: number) => onRegionChange(selectedRegion.id, { width: value }) },
                { label: "Alto", value: selectedRegion.height, setter: (value: number) => onRegionChange(selectedRegion.id, { height: value }) },
              ].map((item) => (
                <div key={item.label}>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">{item.label}</label>
                  <input
                    type="number"
                    value={item.value}
                    disabled={!canManage}
                    onChange={(event) => item.setter(Number(event.target.value))}
                  />
                </div>
              ))}
            </div>

            {canManage ? (
              <button className="rounded-full border border-rose-200 px-5 py-3 text-sm font-semibold text-rose-700" type="button" onClick={onDeleteSelected}>
                Eliminar región
              </button>
            ) : null}
          </div>
        ) : null}

        {selectedWidget ? (
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <span className="rounded-2xl bg-slate-100 p-3 text-accent">
                <Palette className="h-5 w-5" />
              </span>
              <div>
                <p className="font-semibold text-ink">{selectedWidget.name}</p>
                <p className="text-sm text-slate-500">{formatLayoutWidgetType(selectedWidget.widget_type)}</p>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
                <input
                  value={selectedWidget.name}
                  disabled={!canManage}
                  onChange={(event) => onWidgetChange(selectedWidget.id, { name: event.target.value })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Capa</label>
                <select
                  value={selectedWidget.layer_role}
                  disabled={!canManage}
                  onChange={(event) => onWidgetChange(selectedWidget.id, { layer_role: event.target.value as LayoutWidget["layer_role"] })}
                >
                  <option value="background">Fondo</option>
                  <option value="content">Contenido</option>
                  <option value="overlay">Overlay</option>
                  <option value="branding">Logo / branding</option>
                  <option value="frame">Marco</option>
                </select>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-4">
              {[
                { label: "X", value: selectedWidget.x, setter: (value: number) => onWidgetChange(selectedWidget.id, { x: value }) },
                { label: "Y", value: selectedWidget.y, setter: (value: number) => onWidgetChange(selectedWidget.id, { y: value }) },
                { label: "Ancho", value: selectedWidget.width, setter: (value: number) => onWidgetChange(selectedWidget.id, { width: value }) },
                { label: "Alto", value: selectedWidget.height, setter: (value: number) => onWidgetChange(selectedWidget.id, { height: value }) },
              ].map((item) => (
                <div key={item.label}>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">{item.label}</label>
                  <input
                    type="number"
                    value={item.value}
                    disabled={!canManage}
                    onChange={(event) => item.setter(Number(event.target.value))}
                  />
                </div>
              ))}
            </div>

            <div className="grid gap-4 md:grid-cols-4">
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Z-index</label>
                <input
                  type="number"
                  value={selectedWidget.z_index}
                  disabled={!canManage}
                  onChange={(event) => onWidgetChange(selectedWidget.id, { z_index: Number(event.target.value) })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Opacidad</label>
                <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  value={selectedWidget.opacity}
                  disabled={!canManage}
                  onChange={(event) => onWidgetChange(selectedWidget.id, { opacity: Number(event.target.value) })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Rotación</label>
                <input
                  type="number"
                  value={selectedWidget.rotation}
                  disabled={!canManage}
                  onChange={(event) => onWidgetChange(selectedWidget.id, { rotation: Number(event.target.value) })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Duración</label>
                <input
                  type="number"
                  value={Number(selectedWidget.props_json.durationSeconds ?? 15)}
                  disabled={!canManage}
                  onChange={(event) => onWidgetPropsChange(selectedWidget.id, { durationSeconds: Number(event.target.value) })}
                />
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-3">
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Color</label>
                <input
                  value={String(selectedWidget.props_json.backgroundColor ?? "#ffffff")}
                  disabled={!canManage}
                  onChange={(event) => onWidgetPropsChange(selectedWidget.id, { backgroundColor: event.target.value })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Texto</label>
                <input
                  value={String(selectedWidget.props_json.textColor ?? "#0f172a")}
                  disabled={!canManage}
                  onChange={(event) => onWidgetPropsChange(selectedWidget.id, { textColor: event.target.value })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Fuente</label>
                <input
                  value={String(selectedWidget.props_json.fontFamily ?? "Fraunces")}
                  disabled={!canManage}
                  onChange={(event) => onWidgetPropsChange(selectedWidget.id, { fontFamily: event.target.value })}
                />
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-3">
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Tamaño fuente</label>
                <input
                  type="number"
                  value={Number(selectedWidget.props_json.fontSize ?? 24)}
                  disabled={!canManage}
                  onChange={(event) => onWidgetPropsChange(selectedWidget.id, { fontSize: Number(event.target.value) })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Alineación</label>
                <select
                  value={String(selectedWidget.props_json.textAlign ?? "left")}
                  disabled={!canManage}
                  onChange={(event) => onWidgetPropsChange(selectedWidget.id, { textAlign: event.target.value })}
                >
                  <option value="left">Izquierda</option>
                  <option value="center">Centro</option>
                  <option value="right">Derecha</option>
                </select>
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Padding</label>
                <input
                  type="number"
                  value={Number(selectedWidget.props_json.padding ?? 16)}
                  disabled={!canManage}
                  onChange={(event) => onWidgetPropsChange(selectedWidget.id, { padding: Number(event.target.value) })}
                />
              </div>
            </div>

            {selectedWidget.widget_type === "text" || selectedWidget.widget_type === "url" || selectedWidget.widget_type === "image" || selectedWidget.widget_type === "video" || selectedWidget.widget_type === "overlay_png" ? (
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">
                  {selectedWidget.widget_type === "text" ? "Contenido" : "Source URL"}
                </label>
                <textarea
                  className="min-h-[110px]"
                  value={String(
                    selectedWidget.widget_type === "text"
                      ? selectedWidget.props_json.textContent ?? ""
                      : selectedWidget.props_json.sourceUrl ?? "",
                  )}
                  disabled={!canManage}
                  onChange={(event) =>
                    onWidgetPropsChange(
                      selectedWidget.id,
                      selectedWidget.widget_type === "text"
                        ? { textContent: event.target.value }
                        : { sourceUrl: event.target.value },
                    )
                  }
                />
              </div>
            ) : null}

            {selectedWidget.widget_type === "html" ? (
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">HTML</label>
                <textarea
                  className="min-h-[120px]"
                  value={String(selectedWidget.props_json.htmlContent ?? "")}
                  disabled={!canManage}
                  onChange={(event) => onWidgetPropsChange(selectedWidget.id, { htmlContent: event.target.value })}
                />
              </div>
            ) : null}

            {canManage ? (
              <button className="rounded-full border border-rose-200 px-5 py-3 text-sm font-semibold text-rose-700" type="button" onClick={onDeleteSelected}>
                Eliminar widget
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}
