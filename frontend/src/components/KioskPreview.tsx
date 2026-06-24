import type { KioskButton, KioskScreen } from "../types/domain";

export function KioskPreview({ screen, buttons }: { screen: KioskScreen | null; buttons: KioskButton[] }) {
  if (!screen) {
    return (
      <div className="rounded-[28px] border border-dashed border-slate-300 bg-slate-50 p-8 text-center text-sm text-slate-500">
        Elige una pantalla de kiosko para revisar hotspots y modo attract.
      </div>
    );
  }

  return (
    <div className="rounded-[36px] border border-slate-200 bg-card p-5 shadow-card">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <p className="font-display text-2xl text-ink">{screen.name}</p>
          <p className="text-sm text-slate-600">
            Timeout {screen.inactivity_timeout_seconds}s · {screen.is_attract_screen ? "Attract" : "Interactivo"}
          </p>
        </div>
      </div>
      <div
        className="relative mx-auto aspect-[9/16] max-w-sm overflow-hidden rounded-[32px] border border-slate-200 bg-cover bg-center"
        style={{
          backgroundImage: screen.background_url
            ? `linear-gradient(rgba(15, 23, 42, 0.18), rgba(15, 23, 42, 0.18)), url(${screen.background_url})`
            : "linear-gradient(135deg, #0f766e, #f97316)",
        }}
      >
        {buttons.map((button) => (
          <div
            key={button.id}
            className="absolute rounded-2xl border border-white/60 bg-white/70 px-3 py-2 shadow-lg backdrop-blur"
            style={{
              left: `${button.x}px`,
              top: `${button.y}px`,
              width: `${button.width}px`,
              height: `${button.height}px`,
            }}
          >
            <p className="text-sm font-semibold text-ink">{button.label}</p>
            <p className="mt-1 text-[11px] uppercase tracking-wide text-slate-500">{button.action_type}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
