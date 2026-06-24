import { useEffect, useState } from "react";

import { formatTouchScreenKind } from "../lib/labels";
import { slugifyTouchName } from "../lib/touch";
import type { KioskScreen } from "../types/domain";

type TouchScreenNavigatorProps = {
  screens: KioskScreen[];
  selectedScreenId: string | null;
  canManage: boolean;
  onSelect: (screenId: string) => void;
  onCreate: (payload: {
    name: string;
    slug: string;
    background_url: string | null;
    attract_media_url: string | null;
    inactivity_timeout_seconds: number;
    is_attract_screen: boolean;
    screen_kind: string;
    sort_order: number;
    idle_timeout_override: number | null;
  }) => void;
  onUpdate: (
    screenId: string,
    payload: {
      name: string;
      slug: string;
      background_url: string | null;
      attract_media_url: string | null;
      inactivity_timeout_seconds: number;
      is_attract_screen: boolean;
      screen_kind: string;
      sort_order: number;
      idle_timeout_override: number | null;
    },
  ) => void;
  onDelete: (screenId: string) => void;
};

const screenKinds = ["attract", "home", "directory", "location", "custom"];

export function TouchScreenNavigator({
  screens,
  selectedScreenId,
  canManage,
  onSelect,
  onCreate,
  onUpdate,
  onDelete,
}: TouchScreenNavigatorProps) {
  const selectedScreen = screens.find((screen) => screen.id === selectedScreenId) ?? null;
  const [form, setForm] = useState({
    name: "",
    slug: "",
    background_url: "",
    attract_media_url: "",
    inactivity_timeout_seconds: 30,
    is_attract_screen: false,
    screen_kind: "custom",
    sort_order: 1,
    idle_timeout_override: "",
  });

  useEffect(() => {
    if (!selectedScreen) {
      return;
    }

    setForm({
      name: selectedScreen.name,
      slug: selectedScreen.slug,
      background_url: selectedScreen.background_url ?? "",
      attract_media_url: selectedScreen.attract_media_url ?? "",
      inactivity_timeout_seconds: selectedScreen.inactivity_timeout_seconds,
      is_attract_screen: selectedScreen.is_attract_screen,
      screen_kind: selectedScreen.screen_kind ?? "custom",
      sort_order: selectedScreen.sort_order,
      idle_timeout_override: selectedScreen.idle_timeout_override ? String(selectedScreen.idle_timeout_override) : "",
    });
  }, [selectedScreen]);

  function buildPayload() {
    return {
      name: form.name,
      slug: form.slug || slugifyTouchName(form.name),
      background_url: form.background_url || null,
      attract_media_url: form.attract_media_url || null,
      inactivity_timeout_seconds: Number(form.inactivity_timeout_seconds),
      is_attract_screen: form.is_attract_screen,
      screen_kind: form.screen_kind || "custom",
      sort_order: Number(form.sort_order),
      idle_timeout_override: form.idle_timeout_override ? Number(form.idle_timeout_override) : null,
    };
  }

  function resetForm() {
    setForm({
      name: "",
      slug: "",
      background_url: "",
      attract_media_url: "",
      inactivity_timeout_seconds: 30,
      is_attract_screen: false,
      screen_kind: "custom",
      sort_order: screens.length + 1,
      idle_timeout_override: "",
    });
  }

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="border-b border-slate-200 pb-5">
        <h2 className="font-display text-2xl text-ink">Pantallas internas</h2>
        <p className="mt-1 text-sm text-slate-600">Organiza attract, home, directorio, fichas y pantallas personalizadas dentro de la misma experiencia.</p>
      </div>

      <div className="mt-6 grid gap-5 xl:grid-cols-[0.92fr_1.08fr]">
        <div className="space-y-3">
          {screens.length > 0 ? (
            screens.map((screen) => (
              <button
                key={screen.id}
                type="button"
                onClick={() => onSelect(screen.id)}
                className={[
                  "w-full rounded-[24px] border p-4 text-left transition",
                  selectedScreenId === screen.id ? "border-accent bg-accentSoft/35" : "border-slate-200 bg-white hover:border-accent/35",
                ].join(" ")}
              >
                <p className="font-semibold text-ink">{screen.name}</p>
                <p className="mt-2 text-sm text-slate-500">
                  {formatTouchScreenKind(screen.screen_kind)} · orden {screen.sort_order} · timeout {screen.idle_timeout_override ?? screen.inactivity_timeout_seconds}s
                </p>
              </button>
            ))
          ) : (
            <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Esta experiencia aun no tiene pantallas.
            </div>
          )}
        </div>

        <div className="space-y-4">
          {!canManage ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Este rol solo puede revisar las pantallas ya configuradas.
            </div>
          ) : (
            <>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
                  <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Slug</label>
                  <input
                    value={form.slug}
                    onChange={(event) => setForm({ ...form, slug: event.target.value })}
                    placeholder={slugifyTouchName(form.name)}
                  />
                </div>
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Fondo</label>
                  <input value={form.background_url} onChange={(event) => setForm({ ...form, background_url: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Media attract</label>
                  <input value={form.attract_media_url} onChange={(event) => setForm({ ...form, attract_media_url: event.target.value })} />
                </div>
              </div>

              <div className="grid gap-4 md:grid-cols-4">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Tipo</label>
                  <select value={form.screen_kind} onChange={(event) => setForm({ ...form, screen_kind: event.target.value })}>
                    {screenKinds.map((kind) => (
                      <option key={kind} value={kind}>
                        {formatTouchScreenKind(kind)}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Orden</label>
                  <input type="number" value={form.sort_order} onChange={(event) => setForm({ ...form, sort_order: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Timeout base</label>
                  <input
                    type="number"
                    value={form.inactivity_timeout_seconds}
                    onChange={(event) => setForm({ ...form, inactivity_timeout_seconds: Number(event.target.value) })}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Override idle</label>
                  <input value={form.idle_timeout_override} onChange={(event) => setForm({ ...form, idle_timeout_override: event.target.value })} />
                </div>
              </div>

              <label className="flex items-center gap-3 rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4">
                <input
                  className="h-5 w-5"
                  type="checkbox"
                  checked={form.is_attract_screen}
                  onChange={(event) => setForm({ ...form, is_attract_screen: event.target.checked })}
                />
                <span className="text-sm font-semibold text-slate-700">Marcar como attract</span>
              </label>

              <div className="flex flex-wrap gap-3">
                <button className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white" type="button" onClick={() => onCreate(buildPayload())}>
                  Crear pantalla
                </button>
                <button
                  className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700 disabled:opacity-40"
                  type="button"
                  disabled={!selectedScreen}
                  onClick={() => {
                    if (selectedScreen) {
                      onUpdate(selectedScreen.id, buildPayload());
                    }
                  }}
                >
                  Guardar cambios
                </button>
                <button
                  className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700"
                  type="button"
                  onClick={resetForm}
                >
                  Limpiar
                </button>
                <button
                  className="rounded-full border border-rose-200 px-5 py-3 text-sm font-semibold text-rose-700 disabled:opacity-40"
                  type="button"
                  disabled={!selectedScreen}
                  onClick={() => {
                    if (selectedScreen) {
                      onDelete(selectedScreen.id);
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
