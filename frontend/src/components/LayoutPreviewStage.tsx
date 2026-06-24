import { Clock3, ImageIcon, MonitorPlay, Table2, Type, Globe2, Code2 } from "lucide-react";

import { formatLayoutWidgetType } from "../lib/labels";
import { sortByLayer } from "../lib/layoutEditor";
import type { LayoutEditorState, LayoutWidget } from "../types/domain";

type PreviewWidget = LayoutWidget & {
  binding_preview?: {
    headers: Array<{ key: string; label: string }>;
    rows: Array<Record<string, unknown>>;
  };
};

type LayoutPreviewStageProps = {
  state: LayoutEditorState;
  widgets?: PreviewWidget[];
  scale?: number;
  showRegions?: boolean;
};

function WidgetPlaceholder({ widget }: { widget: PreviewWidget }) {
  const props = widget.props_json ?? {};
  const backgroundColor = typeof props.backgroundColor === "string" ? props.backgroundColor : "#ffffff";
  const textColor = typeof props.textColor === "string" ? props.textColor : "#0f172a";
  const borderRadius = typeof props.borderRadius === "number" ? props.borderRadius : 16;
  const padding = typeof props.padding === "number" ? props.padding : 16;
  const sourceUrl = typeof props.sourceUrl === "string" ? props.sourceUrl : "";
  const textContent = typeof props.textContent === "string" ? props.textContent : "";
  const htmlContent = typeof props.htmlContent === "string" ? props.htmlContent : "";
  const fontSize = typeof props.fontSize === "number" ? props.fontSize : 22;
  const fontWeight = typeof props.fontWeight === "number" ? props.fontWeight : 600;
  const textAlign = typeof props.textAlign === "string" ? props.textAlign : "left";

  const commonClassName = "h-full w-full overflow-hidden";
  const commonStyle = {
    backgroundColor,
    color: textColor,
    borderRadius: `${borderRadius}px`,
    padding: `${padding}px`,
    fontSize: `${fontSize}px`,
    fontWeight,
    textAlign: textAlign as "left" | "center" | "right",
  };

  if (widget.widget_type === "image" || widget.widget_type === "overlay_png") {
    return (
      <div className={`${commonClassName} border border-slate-200 bg-slate-100`} style={commonStyle}>
        {sourceUrl ? (
          <div
            className="h-full w-full bg-cover bg-center"
            style={{ backgroundImage: `url(${sourceUrl})`, borderRadius: `${Math.max(borderRadius - 6, 0)}px` }}
          />
        ) : (
          <div className="flex h-full items-center justify-center gap-3 text-sm text-slate-500">
            <ImageIcon className="h-5 w-5" />
            {widget.widget_type === "overlay_png" ? "Overlay PNG" : "Imagen"}
          </div>
        )}
      </div>
    );
  }

  if (widget.widget_type === "video") {
    return (
      <div className={`${commonClassName} border border-slate-200 bg-slate-950 text-white`} style={commonStyle}>
        <div className="flex h-full items-center justify-center gap-3 bg-gradient-to-br from-slate-900 via-slate-800 to-slate-700">
          <MonitorPlay className="h-6 w-6" />
          <span className="text-base font-semibold">{sourceUrl ? "Video enlazado" : "Widget de video"}</span>
        </div>
      </div>
    );
  }

  if (widget.widget_type === "text") {
    return (
      <div className={`${commonClassName} border border-slate-200`} style={commonStyle}>
        <div className="flex h-full items-start gap-3">
          <Type className="mt-1 h-5 w-5 opacity-60" />
          <div className="line-clamp-5">{textContent || "Texto de ejemplo para campañas, mensajes u operación."}</div>
        </div>
      </div>
    );
  }

  if (widget.widget_type === "clock") {
    return (
      <div className={`${commonClassName} border border-slate-200`} style={commonStyle}>
        <div className="flex h-full items-center justify-center gap-4">
          <Clock3 className="h-7 w-7 opacity-70" />
          <div>
            <div className="text-3xl font-bold">{new Date().toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit" })}</div>
            <div className="mt-1 text-sm opacity-70">{new Date().toLocaleDateString("es-MX")}</div>
          </div>
        </div>
      </div>
    );
  }

  if (widget.widget_type === "url") {
    return (
      <div className={`${commonClassName} border border-slate-200`} style={commonStyle}>
        <div className="flex h-full items-center justify-center gap-3">
          <Globe2 className="h-5 w-5 opacity-70" />
          <span className="truncate text-base">{sourceUrl || "URL de sitio o dashboard"}</span>
        </div>
      </div>
    );
  }

  if (widget.widget_type === "html") {
    return (
      <div className={`${commonClassName} border border-slate-200`} style={commonStyle}>
        <div className="flex h-full items-start gap-3">
          <Code2 className="mt-1 h-5 w-5 opacity-70" />
          <pre className="line-clamp-6 whitespace-pre-wrap text-sm">{htmlContent || "<div>HTML básico de preview</div>"}</pre>
        </div>
      </div>
    );
  }

  if (widget.widget_type === "dataset_table") {
    const headers = widget.binding_preview?.headers ?? [];
    const rows = widget.binding_preview?.rows ?? [];
    return (
      <div className={`${commonClassName} border border-slate-200 bg-white`} style={{ ...commonStyle, padding: 0 }}>
        <div className="flex items-center gap-3 border-b border-slate-200 bg-slate-950 px-4 py-3 text-white">
          <Table2 className="h-5 w-5" />
          <span className="font-semibold">{widget.name || "Tabla dinámica"}</span>
        </div>
        <div className="overflow-hidden p-3">
          {headers.length > 0 ? (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-slate-600">
                  {headers.slice(0, 5).map((header) => (
                    <th key={header.key} className="px-2 py-2 font-semibold">
                      {header.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 5).map((row, index) => (
                  <tr key={`${widget.id}-${index}`} className="border-b border-slate-100 text-slate-700 last:border-b-0">
                    {headers.slice(0, 5).map((header) => (
                      <td key={`${widget.id}-${index}-${header.key}`} className="px-2 py-2">
                        {String(row[header.key] ?? "-")}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="flex h-full min-h-[120px] items-center justify-center gap-3 text-sm text-slate-500">
              <Table2 className="h-5 w-5" />
              Selecciona un binding para mostrar la tabla dinámica.
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={`${commonClassName} border border-dashed border-slate-300 bg-slate-50`} style={commonStyle}>
      <div className="flex h-full items-center justify-center text-sm text-slate-500">{formatLayoutWidgetType(widget.widget_type)}</div>
    </div>
  );
}

export function LayoutPreviewStage({ state, widgets, scale = 1, showRegions = false }: LayoutPreviewStageProps) {
  const renderWidgets = sortByLayer((widgets as PreviewWidget[] | undefined) ?? (state.widgets as PreviewWidget[]));

  return (
    <div className="overflow-auto rounded-[28px] border border-slate-200 bg-slate-100 p-4">
      <div
        className="relative mx-auto origin-top-left overflow-hidden rounded-[24px] border border-slate-200 bg-slate-950 shadow-2xl"
        style={{
          width: `${state.canvas.width * scale}px`,
          height: `${state.canvas.height * scale}px`,
          backgroundColor: state.canvas.background_color,
        }}
      >
        {showRegions
          ? state.regions.map((region) => (
              <div
                key={region.id}
                className="pointer-events-none absolute rounded-[18px] border border-dashed border-white/45 bg-white/5"
                style={{
                  left: `${region.x * scale}px`,
                  top: `${region.y * scale}px`,
                  width: `${region.width * scale}px`,
                  height: `${region.height * scale}px`,
                }}
              >
                <div className="rounded-br-2xl bg-slate-950/70 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-white">
                  {region.label}
                </div>
              </div>
            ))
          : null}

        {renderWidgets.map((widget) => (
          <div
            key={widget.id}
            className="absolute overflow-hidden"
            style={{
              left: `${widget.x * scale}px`,
              top: `${widget.y * scale}px`,
              width: `${widget.width * scale}px`,
              height: `${widget.height * scale}px`,
              opacity: widget.opacity,
              transform: `rotate(${widget.rotation}deg)`,
              zIndex: widget.z_index,
            }}
          >
            <WidgetPlaceholder widget={widget} />
          </div>
        ))}
      </div>
    </div>
  );
}
