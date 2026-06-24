import { Blocks, Clock3, Code2, Globe2, ImageIcon, MonitorPlay, SquareStack, Table2, Type } from "lucide-react";

import { formatLayoutWidgetType } from "../lib/labels";
import type { LayoutWidgetType } from "../types/domain";

type WidgetLibraryPanelProps = {
  supportedWidgets: LayoutWidgetType[];
  futureWidgets: LayoutWidgetType[];
  canManage: boolean;
  onAddWidget: (widgetType: LayoutWidgetType) => void;
  onAddRegion: () => void;
};

const widgetIcons: Record<string, typeof Blocks> = {
  image: ImageIcon,
  video: MonitorPlay,
  text: Type,
  clock: Clock3,
  url: Globe2,
  html: Code2,
  dataset_table: Table2,
  overlay_png: SquareStack,
  rss: Blocks,
  weather: Blocks,
  social_feed: Blocks,
  api_feed: Blocks,
  world_clock: Blocks,
};

export function WidgetLibraryPanel({ supportedWidgets, futureWidgets, canManage, onAddWidget, onAddRegion }: WidgetLibraryPanelProps) {
  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="border-b border-slate-200 pb-5">
        <h2 className="font-display text-2xl text-ink">Biblioteca visual</h2>
        <p className="mt-1 text-sm text-slate-600">Agrega widgets, overlays PNG y nuevas regiones sobre el canvas con estructura preparada para capas y runtime futuro.</p>
      </div>

      <div className="mt-6 space-y-4">
        <button
          type="button"
          disabled={!canManage}
          onClick={onAddRegion}
          className="w-full rounded-[24px] border border-accent/30 bg-accentSoft/35 px-4 py-4 text-left text-sm font-semibold text-ink transition disabled:opacity-50"
        >
          Nueva región
        </button>

        <div className="grid gap-3 md:grid-cols-2">
          {supportedWidgets.map((widgetType) => {
            const Icon = widgetIcons[widgetType] ?? Blocks;
            return (
              <button
                key={widgetType}
                type="button"
                disabled={!canManage}
                onClick={() => onAddWidget(widgetType)}
                className="rounded-[24px] border border-slate-200 bg-white px-4 py-4 text-left transition hover:border-accent/35 disabled:opacity-50"
              >
                <div className="flex items-start gap-3">
                  <span className="rounded-2xl bg-slate-100 p-3 text-accent">
                    <Icon className="h-5 w-5" />
                  </span>
                  <div>
                    <p className="font-semibold text-ink">{formatLayoutWidgetType(widgetType)}</p>
                    <p className="mt-2 text-sm text-slate-500">
                      {widgetType === "overlay_png"
                        ? "Perfecto para marcos, logos, QR y branding encima del contenido."
                        : widgetType === "dataset_table"
                          ? "Usa datasets y bindings existentes; preparado para scroll y paginación futura."
                          : "Widget listo para canvas, preview y runtime futuro."}
                    </p>
                  </div>
                </div>
              </button>
            );
          })}
        </div>

        <div className="rounded-[26px] border border-slate-200 bg-slate-50 p-4">
          <p className="text-sm font-semibold text-ink">Widgets futuros preparados</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {futureWidgets.map((widgetType) => (
              <span key={widgetType} className="rounded-full bg-white px-3 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-600">
                {formatLayoutWidgetType(widgetType)}
              </span>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
