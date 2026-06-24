import type { KioskButton, KioskScreen, TouchLocation } from "../types/domain";
import type { TouchHotspotDraft, TouchHotspotIntent } from "../lib/touch";

type TouchHotspotInspectorProps = {
  buttons: KioskButton[];
  screens: KioskScreen[];
  locations: TouchLocation[];
  selectedHotspotId: string | null;
  draft: TouchHotspotDraft;
  canManage: boolean;
  onSelectHotspot: (buttonId: string | null) => void;
  onChange: (next: TouchHotspotDraft) => void;
  onCreate: () => void;
  onUpdate: () => void;
  onDelete: (buttonId: string) => void;
};

const hotspotIntents: Array<{ value: TouchHotspotIntent; label: string; description: string }> = [
  { value: "screen", label: "Ir a pantalla", description: "Navega a otra pantalla interna de la experiencia." },
  { value: "location", label: "Abrir tienda", description: "Muestra la ficha informativa de una location o POI." },
  { value: "home", label: "Volver a inicio", description: "Regresa a la pantalla principal del directorio." },
  { value: "url", label: "Abrir URL", description: "Deja preparada una salida externa para futuro player." },
];

export function TouchHotspotInspector({
  buttons,
  screens,
  locations,
  selectedHotspotId,
  draft,
  canManage,
  onSelectHotspot,
  onChange,
  onCreate,
  onUpdate,
  onDelete,
}: TouchHotspotInspectorProps) {
  const selectedButton = buttons.find((button) => button.id === selectedHotspotId) ?? null;

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="flex flex-col gap-3 border-b border-slate-200 pb-5">
        <div>
          <h2 className="font-display text-2xl text-ink">Hotspots</h2>
          <p className="mt-1 text-sm text-slate-600">Gestiona botones sobre la imagen: navegación, ficha de tienda o regreso a home.</p>
        </div>
      </div>

      <div className="mt-6 grid gap-5 xl:grid-cols-[0.9fr_1.1fr]">
        <div className="space-y-3">
          {buttons.length > 0 ? (
            buttons.map((button) => (
              <button
                key={button.id}
                type="button"
                onClick={() => onSelectHotspot(button.id)}
                className={[
                  "w-full rounded-[24px] border p-4 text-left transition",
                  selectedHotspotId === button.id ? "border-accent bg-accentSoft/35" : "border-slate-200 bg-white hover:border-accent/40",
                ].join(" ")}
              >
                <p className="font-semibold text-ink">{button.label}</p>
                <p className="mt-2 text-sm text-slate-500">
                  {button.action_type} · {button.width}x{button.height} · ({button.x}, {button.y})
                </p>
              </button>
            ))
          ) : (
            <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Esta pantalla aun no tiene hotspots.
            </div>
          )}
        </div>

        <div className="space-y-4">
          {!canManage ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Este rol solo puede revisar hotspots y navegar el preview de la experiencia.
            </div>
          ) : (
            <>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Etiqueta</label>
                  <input value={draft.label} onChange={(event) => onChange({ ...draft, label: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Orden</label>
                  <input
                    type="number"
                    value={draft.sort_order}
                    onChange={(event) => onChange({ ...draft, sort_order: Number(event.target.value) })}
                  />
                </div>
              </div>

              <div className="grid gap-4 md:grid-cols-4">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">X</label>
                  <input type="number" value={draft.x} onChange={(event) => onChange({ ...draft, x: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Y</label>
                  <input type="number" value={draft.y} onChange={(event) => onChange({ ...draft, y: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho</label>
                  <input type="number" value={draft.width} onChange={(event) => onChange({ ...draft, width: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Alto</label>
                  <input type="number" value={draft.height} onChange={(event) => onChange({ ...draft, height: Number(event.target.value) })} />
                </div>
              </div>

              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Accion</label>
                <div className="grid gap-3 md:grid-cols-2">
                  {hotspotIntents.map((intent) => (
                    <button
                      key={intent.value}
                      type="button"
                      onClick={() => onChange({ ...draft, intent: intent.value })}
                      className={[
                        "rounded-[22px] border p-4 text-left transition",
                        draft.intent === intent.value ? "border-accent bg-accentSoft/35" : "border-slate-200 bg-white hover:border-accent/35",
                      ].join(" ")}
                    >
                      <p className="font-semibold text-ink">{intent.label}</p>
                      <p className="mt-2 text-sm text-slate-600">{intent.description}</p>
                    </button>
                  ))}
                </div>
              </div>

              {draft.intent === "screen" ? (
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Pantalla destino</label>
                  <select value={draft.target_screen_id} onChange={(event) => onChange({ ...draft, target_screen_id: event.target.value })}>
                    <option value="">Selecciona una pantalla</option>
                    {screens.map((screen) => (
                      <option key={screen.id} value={screen.id}>
                        {screen.name}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}

              {draft.intent === "location" ? (
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Location / tienda</label>
                  <select value={draft.location_id} onChange={(event) => onChange({ ...draft, location_id: event.target.value })}>
                    <option value="">Selecciona una tienda</option>
                    {locations.map((location) => (
                      <option key={location.id} value={location.id}>
                        {location.name}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}

              {draft.intent === "url" ? (
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">URL destino</label>
                  <input value={draft.action_value} onChange={(event) => onChange({ ...draft, action_value: event.target.value })} />
                </div>
              ) : null}

              <div className="grid gap-4 md:grid-cols-3">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Fondo</label>
                  <input value={draft.backgroundColor} onChange={(event) => onChange({ ...draft, backgroundColor: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Texto</label>
                  <input value={draft.textColor} onChange={(event) => onChange({ ...draft, textColor: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Opacidad</label>
                  <input
                    type="number"
                    min="0"
                    max="1"
                    step="0.05"
                    value={draft.opacity}
                    onChange={(event) => onChange({ ...draft, opacity: Number(event.target.value) })}
                  />
                </div>
              </div>

              <div className="flex flex-wrap gap-3">
                <button className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white" type="button" onClick={onCreate}>
                  Crear hotspot
                </button>
                <button
                  className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700 disabled:opacity-40"
                  type="button"
                  disabled={!selectedButton}
                  onClick={onUpdate}
                >
                  Guardar cambios
                </button>
                <button
                  className="rounded-full border border-rose-200 px-5 py-3 text-sm font-semibold text-rose-700 disabled:opacity-40"
                  type="button"
                  disabled={!selectedButton}
                  onClick={() => {
                    if (selectedButton) {
                      onDelete(selectedButton.id);
                    }
                  }}
                >
                  Eliminar
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
