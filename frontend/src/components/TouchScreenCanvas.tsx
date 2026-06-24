import type { KioskButton, KioskScreen, TouchLocation, TouchMap } from "../types/domain";
import { formatTouchScreenKind } from "../lib/labels";

type TouchScreenCanvasProps = {
  screen: KioskScreen | null;
  buttons: KioskButton[];
  activeHotspotId?: string | null;
  onSelectHotspot?: (buttonId: string) => void;
  onActivateHotspot?: (button: KioskButton) => void;
  mode?: "builder" | "preview";
  title?: string;
  subtitle?: string;
  location?: TouchLocation | null;
  maps?: TouchMap[];
  note?: string | null;
};

export function TouchScreenCanvas({
  screen,
  buttons,
  activeHotspotId = null,
  onSelectHotspot,
  onActivateHotspot,
  mode = "builder",
  title = "Canvas touch",
  subtitle = "Visualiza la pantalla activa, sus hotspots y el estado interactivo.",
  location = null,
  maps = [],
  note = null,
}: TouchScreenCanvasProps) {
  if (!screen) {
    return (
      <div className="rounded-[32px] border border-dashed border-slate-300 bg-slate-50 px-6 py-12 text-center text-sm text-slate-500">
        Crea o selecciona una pantalla para comenzar a construir la experiencia touch.
      </div>
    );
  }

  const matchingMap = location
    ? maps.find((item) => item.floor_zone && item.floor_zone === location.floor_zone) ?? maps[0] ?? null
    : null;

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="flex flex-col gap-3 border-b border-slate-200 pb-5 md:flex-row md:items-center md:justify-between">
        <div>
          <h2 className="font-display text-2xl text-ink">{title}</h2>
          <p className="mt-1 text-sm text-slate-600">{subtitle}</p>
        </div>
        <div className="rounded-full bg-slate-100 px-4 py-2 text-xs font-semibold uppercase tracking-[0.18em] text-slate-600">
          {formatTouchScreenKind(screen.screen_kind)} · {buttons.length} hotspot(s)
        </div>
      </div>

      <div className="mt-6 grid gap-5 xl:grid-cols-[0.92fr_1.08fr]">
        <div className="rounded-[32px] border border-slate-200 bg-slate-950/90 p-5">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="font-display text-2xl text-white">{screen.name}</p>
              <p className="text-sm text-slate-300">
                slug: {screen.slug} · timeout base {screen.inactivity_timeout_seconds}s
              </p>
            </div>
            {screen.is_attract_screen ? (
              <span className="rounded-full bg-white/10 px-4 py-2 text-xs font-semibold uppercase tracking-[0.18em] text-amber-200">
                Atract mode
              </span>
            ) : null}
          </div>

          <div
            className="relative mx-auto aspect-[9/16] max-w-md overflow-hidden rounded-[30px] border border-white/10 bg-cover bg-center shadow-2xl"
            style={{
              backgroundImage: screen.background_url
                ? `linear-gradient(rgba(15, 23, 42, 0.18), rgba(15, 23, 42, 0.18)), url(${screen.background_url})`
                : "linear-gradient(160deg, #0f172a 0%, #0f766e 55%, #f59e0b 100%)",
            }}
          >
            <div className="absolute inset-x-0 top-0 flex items-center justify-between bg-gradient-to-b from-slate-950/55 to-transparent px-5 py-4 text-white">
              <div>
                <p className="text-xs uppercase tracking-[0.24em] text-white/70">{formatTouchScreenKind(screen.screen_kind)}</p>
                <p className="mt-1 text-lg font-semibold">{screen.name}</p>
              </div>
              <div className="rounded-full bg-white/10 px-3 py-2 text-xs font-semibold backdrop-blur">
                {mode === "preview" ? "Preview" : "Builder"}
              </div>
            </div>

            {screen.attract_media_url ? (
              <div className="absolute inset-x-6 bottom-6 rounded-[24px] border border-white/15 bg-slate-950/55 px-4 py-3 text-xs text-white/80 backdrop-blur">
                Media attract: {screen.attract_media_url}
              </div>
            ) : null}

            {buttons.map((button) => {
              const style = button.style_json ?? {};
              const backgroundColor = typeof style.backgroundColor === "string" ? style.backgroundColor : "#ffffff";
              const textColor = typeof style.textColor === "string" ? style.textColor : "#0f172a";
              const opacity = typeof style.opacity === "number" ? style.opacity : 0.82;
              const isActive = activeHotspotId === button.id;

              return (
                <button
                  key={button.id}
                  type="button"
                  onClick={() => {
                    if (mode === "preview") {
                      onActivateHotspot?.(button);
                      return;
                    }
                    onSelectHotspot?.(button.id);
                  }}
                  className={[
                    "absolute overflow-hidden rounded-[22px] border px-3 py-2 text-left shadow-xl backdrop-blur transition",
                    mode === "preview" ? "hover:scale-[1.02]" : "hover:border-accent",
                    isActive ? "border-accent ring-2 ring-accent/50" : "border-white/65",
                  ].join(" ")}
                  style={{
                    left: `${button.x}px`,
                    top: `${button.y}px`,
                    width: `${button.width}px`,
                    height: `${button.height}px`,
                    backgroundColor,
                    color: textColor,
                    opacity,
                  }}
                >
                  <p className="truncate text-sm font-semibold">{button.label}</p>
                  <p className="mt-1 text-[11px] uppercase tracking-[0.18em] opacity-80">{button.action_type}</p>
                </button>
              );
            })}
          </div>
        </div>

        <div className="space-y-4">
          <article className="rounded-[28px] border border-slate-200 bg-slate-50 px-5 py-5">
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Resumen operativo</p>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <div className="rounded-2xl bg-white px-4 py-4 text-sm text-slate-700">Tipo: {formatTouchScreenKind(screen.screen_kind)}</div>
              <div className="rounded-2xl bg-white px-4 py-4 text-sm text-slate-700">
                Timeout: {screen.idle_timeout_override ?? screen.inactivity_timeout_seconds}s
              </div>
              <div className="rounded-2xl bg-white px-4 py-4 text-sm text-slate-700">Hotspots: {buttons.length}</div>
              <div className="rounded-2xl bg-white px-4 py-4 text-sm text-slate-700">
                Fondo: {screen.background_url ? "Configurado" : "Gradiente por defecto"}
              </div>
            </div>
          </article>

          {location ? (
            <article className="rounded-[28px] border border-accent/20 bg-accentSoft/35 px-5 py-5">
              <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Ficha de ubicacion</p>
              <p className="mt-3 font-display text-3xl text-ink">{location.name}</p>
              <p className="mt-2 text-sm text-slate-600">
                {location.category ?? "Sin categoria"} · {location.floor_zone ?? "Zona pendiente"} · {location.suite ?? "Local pendiente"}
              </p>
              <p className="mt-3 text-sm text-slate-700">{location.description ?? "Agrega una descripcion para explicar como llegar o que ofrece la tienda."}</p>
              {matchingMap?.background_url ? (
                <div className="mt-4 overflow-hidden rounded-[24px] border border-slate-200 bg-white">
                  <div
                    className="aspect-[16/9] bg-cover bg-center"
                    style={{ backgroundImage: `linear-gradient(rgba(15, 23, 42, 0.12), rgba(15, 23, 42, 0.12)), url(${matchingMap.background_url})` }}
                  />
                  <div className="border-t border-slate-200 px-4 py-3 text-sm text-slate-600">
                    Mapa asociado: {matchingMap.name} · {matchingMap.floor_zone ?? "Sin piso/zona"}
                  </div>
                </div>
              ) : null}
            </article>
          ) : null}

          {note ? (
            <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4 text-sm text-slate-700">{note}</div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
