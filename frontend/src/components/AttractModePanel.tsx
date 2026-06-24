import { useEffect, useState } from "react";

import type { KioskScreen, TouchExperience } from "../types/domain";

type AttractModePanelProps = {
  experience: TouchExperience | null;
  screens: KioskScreen[];
  canManage: boolean;
  onSave: (payload: {
    name: string;
    slug: string;
    description: string;
    default_idle_timeout_seconds: number;
    attract_screen_id: string | null;
    home_screen_id: string | null;
    is_active: boolean;
  }) => void;
};

export function AttractModePanel({ experience, screens, canManage, onSave }: AttractModePanelProps) {
  const [form, setForm] = useState({
    name: "",
    slug: "",
    description: "",
    default_idle_timeout_seconds: 30,
    attract_screen_id: "",
    home_screen_id: "",
    is_active: true,
  });

  useEffect(() => {
    if (!experience) {
      return;
    }

    setForm({
      name: experience.name,
      slug: experience.slug,
      description: experience.description ?? "",
      default_idle_timeout_seconds: experience.default_idle_timeout_seconds,
      attract_screen_id: experience.attract_screen_id ?? "",
      home_screen_id: experience.home_screen_id ?? "",
      is_active: experience.is_active,
    });
  }, [experience]);

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="border-b border-slate-200 pb-5">
        <h2 className="font-display text-2xl text-ink">Atract mode y tiempo de inactividad</h2>
        <p className="mt-1 text-sm text-slate-600">
          Define la pantalla de publicidad, el home del directorio y el timeout que regresara automaticamente a la experiencia inactiva.
        </p>
      </div>

      {!experience ? (
        <div className="mt-6 rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
          Crea primero una experiencia touch desde el listado principal.
        </div>
      ) : (
        <div className="mt-6 space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
              <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} disabled={!canManage} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Slug</label>
              <input value={form.slug} onChange={(event) => setForm({ ...form, slug: event.target.value })} disabled={!canManage} />
            </div>
          </div>

          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Descripción</label>
            <textarea
              className="min-h-[110px]"
              value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
              disabled={!canManage}
            />
          </div>

          <div className="grid gap-4 md:grid-cols-3">
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Timeout por defecto</label>
              <input
                type="number"
                value={form.default_idle_timeout_seconds}
                onChange={(event) => setForm({ ...form, default_idle_timeout_seconds: Number(event.target.value) })}
                disabled={!canManage}
              />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Pantalla attract</label>
              <select
                value={form.attract_screen_id}
                onChange={(event) => setForm({ ...form, attract_screen_id: event.target.value })}
                disabled={!canManage}
              >
                <option value="">Sin seleccionar</option>
                {screens.map((screen) => (
                  <option key={screen.id} value={screen.id}>
                    {screen.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Pantalla home</label>
              <select
                value={form.home_screen_id}
                onChange={(event) => setForm({ ...form, home_screen_id: event.target.value })}
                disabled={!canManage}
              >
                <option value="">Sin seleccionar</option>
                {screens.map((screen) => (
                  <option key={screen.id} value={screen.id}>
                    {screen.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <label className="flex items-center gap-3 rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4">
            <input
              className="h-5 w-5"
              type="checkbox"
              checked={form.is_active}
              onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
              disabled={!canManage}
            />
            <span className="text-sm font-semibold text-slate-700">Experiencia activa</span>
          </label>

          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-2xl bg-slate-50 px-4 py-4 text-sm text-slate-700">{screens.length} pantalla(s)</div>
            <div className="rounded-2xl bg-slate-50 px-4 py-4 text-sm text-slate-700">
              attract: {form.attract_screen_id ? "Configurado" : "Pendiente"}
            </div>
            <div className="rounded-2xl bg-slate-50 px-4 py-4 text-sm text-slate-700">
              home: {form.home_screen_id ? "Configurado" : "Pendiente"}
            </div>
          </div>

          {canManage ? (
            <button
              className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white"
              type="button"
              onClick={() =>
                onSave({
                  name: form.name,
                  slug: form.slug,
                  description: form.description,
                  default_idle_timeout_seconds: form.default_idle_timeout_seconds,
                  attract_screen_id: form.attract_screen_id || null,
                  home_screen_id: form.home_screen_id || null,
                  is_active: form.is_active,
                })
              }
            >
              Guardar experiencia
            </button>
          ) : null}
        </div>
      )}
    </section>
  );
}
