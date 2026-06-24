import { formatDynamicValue } from "../lib/layoutBindings";
import type { Layout, LayoutBindingPreviewResult, LayoutPreviewZone } from "../types/domain";

function zoneTableLines(binding: LayoutBindingPreviewResult) {
  return binding.rows.slice(0, 3).map((row, rowIndex) =>
    binding.headers
      .slice(0, 2)
      .map((header) => `${header.label}: ${formatDynamicValue(row[header.target_field], header.format_hint)}`)
      .join(" · ") || `Fila ${rowIndex + 1}`,
  );
}

export function BoundLayoutPreview({
  layout,
  zones,
  bindings,
}: {
  layout: Layout;
  zones: LayoutPreviewZone[];
  bindings: LayoutBindingPreviewResult[];
}) {
  const canvasWidth = Math.max(1, layout.canvas_width);
  const canvasHeight = Math.max(1, layout.canvas_height);

  return (
    <div className="overflow-hidden rounded-[28px] border border-slate-200 bg-slate-950 p-4 shadow-card">
      <div className="mb-4 flex items-center justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-slate-400">Bound layout preview</p>
          <p className="mt-2 text-sm text-slate-300">Simula como el dataset alimentara visualmente las zonas del layout seleccionado.</p>
        </div>
        <span className="rounded-full border border-white/10 bg-white/10 px-3 py-1 text-xs font-semibold text-slate-100">
          {layout.canvas_width}x{layout.canvas_height}
        </span>
      </div>

      <div
        className="relative overflow-hidden rounded-[24px] border border-white/10 bg-[radial-gradient(circle_at_top,rgba(255,255,255,0.12),transparent_36%),linear-gradient(160deg,#0f172a,#111827)]"
        style={{ aspectRatio: `${canvasWidth} / ${canvasHeight}` }}
      >
        {zones.map((zone) => {
          const bound = bindings.find((binding) => binding.zone?.key === zone.key) ?? null;
          const lines = bound ? zoneTableLines(bound) : [];
          return (
            <div
              key={zone.key}
              className="absolute overflow-hidden rounded-[22px] border border-white/10 bg-white/6 p-3"
              style={{
                left: `${(zone.x / canvasWidth) * 100}%`,
                top: `${(zone.y / canvasHeight) * 100}%`,
                width: `${(zone.width / canvasWidth) * 100}%`,
                height: `${(zone.height / canvasHeight) * 100}%`,
              }}
            >
              <div className="flex h-full flex-col rounded-[18px] border border-white/10 bg-black/20 p-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] uppercase tracking-[0.22em] text-slate-400">{zone.label}</p>
                    <p className="mt-1 text-sm font-semibold text-white">{bound?.binding?.name ?? "Zona libre"}</p>
                  </div>
                  {bound ? (
                    <span className="rounded-full border border-emerald-400/20 bg-emerald-400/12 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-emerald-100">
                      Dataset activo
                    </span>
                  ) : null}
                </div>

                <div className="mt-4 flex-1 rounded-[16px] border border-white/8 bg-white/6 p-3">
                  {bound ? (
                    <div className="space-y-2 text-xs text-slate-200">
                      {lines.length > 0 ? (
                        lines.map((line) => (
                          <div key={line} className="rounded-xl border border-white/8 bg-black/20 px-3 py-2">
                            {line}
                          </div>
                        ))
                      ) : (
                        <div className="rounded-xl border border-dashed border-white/10 px-3 py-3 text-slate-300">
                          Binding valido sin filas de ejemplo.
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="flex h-full items-center justify-center rounded-xl border border-dashed border-white/10 text-xs uppercase tracking-[0.18em] text-slate-500">
                      Sin binding
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
