import { Hand, MapPinned, MonitorSmartphone, Sparkles } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { SectionCard } from "../components/SectionCard";
import { StatCard } from "../components/StatCard";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { canWriteClientScope } from "../lib/rbac";
import { buildTouchExperiencePath } from "../lib/workspace";
import { slugifyTouchName } from "../lib/touch";
import type { TouchExperience } from "../types/domain";

export function TouchExperiencesPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const navigate = useNavigate();
  const [experiences, setExperiences] = useState<TouchExperience[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    name: "",
    slug: "",
    description: "",
    default_idle_timeout_seconds: 45,
    is_active: true,
  });

  const canManage = canWriteClientScope(user?.role);
  const activeExperiences = useMemo(() => experiences.filter((item) => item.is_active), [experiences]);

  async function loadData() {
    if (!token || !clientId) {
      return;
    }

    try {
      const response = await apiRequest<TouchExperience[]>(`/touch/experiences?client_id=${clientId}`, { token });
      setExperiences(response);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el modulo touch");
    }
  }

  useEffect(() => {
    void loadData();
  }, [clientId, token]);

  async function createExperience(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !clientId || !canManage) {
      return;
    }

    try {
      const created = await apiRequest<TouchExperience>("/touch/experiences", {
        method: "POST",
        token,
        body: {
          client_id: clientId,
          name: form.name,
          slug: form.slug || slugifyTouchName(form.name),
          description: form.description || null,
          default_idle_timeout_seconds: form.default_idle_timeout_seconds,
          is_active: form.is_active,
        },
      });
      setForm({
        name: "",
        slug: "",
        description: "",
        default_idle_timeout_seconds: 45,
        is_active: true,
      });
      await loadData();
      navigate(buildTouchExperiencePath(clientId, created.id));
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear la experiencia touch");
    }
  }

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Experiencias" value={String(experiences.length)} hint="Directorios, kioskos y menus touch" tone="teal" />
        <StatCard label="Activas" value={String(activeExperiences.length)} hint="Listas para asignación" />
        <StatCard label="Attract mode" value={String(experiences.filter((item) => Boolean(item.attract_screen_id)).length)} hint="Con pantalla de reposo" tone="orange" />
        <StatCard label="Home definido" value={String(experiences.filter((item) => Boolean(item.home_screen_id)).length)} hint="Flujo inicial del directorio" />
      </section>

      <div className="grid gap-5 xl:grid-cols-[0.95fr_1.05fr]">
        <SectionCard
          title="Crear experiencia touch"
          subtitle="Define la experiencia interactiva base. El builder siguiente te permitira agregar pantallas, hotspots, tiendas y attract mode."
        >
          {!canManage ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Este rol solo puede consultar experiencias touch existentes.
            </div>
          ) : (
            <form className="space-y-4" onSubmit={createExperience}>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
                  <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required />
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

              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Descripción</label>
                <textarea
                  className="min-h-[120px]"
                  value={form.description}
                  onChange={(event) => setForm({ ...form, description: event.target.value })}
                  placeholder="Ejemplo: Directorio de plaza comercial con attract mode, tiendas y hotspots por categoria."
                />
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Timeout por defecto</label>
                  <input
                    type="number"
                    value={form.default_idle_timeout_seconds}
                    onChange={(event) => setForm({ ...form, default_idle_timeout_seconds: Number(event.target.value) })}
                  />
                </div>
                <label className="flex items-center gap-3 rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4">
                  <input
                    className="h-5 w-5"
                    type="checkbox"
                    checked={form.is_active}
                    onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
                  />
                  <span className="text-sm font-semibold text-slate-700">Experiencia activa</span>
                </label>
              </div>

              <button className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white" type="submit">
                Crear experiencia
              </button>
            </form>
          )}
        </SectionCard>

        <SectionCard
          title="Experiencias del cliente"
          subtitle="Cada experiencia queda lista para asignarse a sucursales o canales y generar un runtime touch consistente."
        >
          <div className="space-y-4">
            {experiences.length > 0 ? (
              experiences.map((experience) => (
                <article key={experience.id} className="rounded-[26px] border border-slate-200 bg-white p-5">
                  <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
                    <div className="min-w-0">
                      <p className="truncate font-display text-2xl text-ink">{experience.name}</p>
                      <p className="mt-2 text-sm text-slate-500">
                        {experience.slug} · timeout {experience.default_idle_timeout_seconds}s · {experience.is_active ? "Activa" : "Pausada"}
                      </p>
                      <p className="mt-3 text-sm text-slate-700">{experience.description ?? "Sin descripcion operativa"}</p>
                    </div>
                    <Link
                      className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
                      to={buildTouchExperiencePath(clientId ?? "", experience.id)}
                    >
                      Abrir builder
                    </Link>
                  </div>

                  <div className="mt-4 grid gap-3 md:grid-cols-3">
                    <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      attract: {experience.attract_screen_id ? "Configurado" : "Pendiente"}
                    </div>
                    <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      home: {experience.home_screen_id ? "Configurado" : "Pendiente"}
                    </div>
                    <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      runtime: preparado para player futuro
                    </div>
                  </div>
                </article>
              ))
            ) : (
              <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                Todavía no existen experiencias touch para este cliente.
              </div>
            )}
          </div>
        </SectionCard>
      </div>

      <section className="grid gap-4 xl:grid-cols-3">
        {[
          {
            title: "Attract mode y regreso automatico",
            description: "Define publicidad de reposo, timeout por defecto y la pantalla home que se mostrara al primer toque.",
            icon: Sparkles,
          },
          {
            title: "Hotspots sobre imagen",
            description: "Cada boton puede navegar a otra pantalla, abrir una location o regresar al inicio del directorio.",
            icon: Hand,
          },
          {
            title: "Locations y mapas",
            description: "Carga tiendas, categorias, pisos, locales y mapas base listos para futuro player touch.",
            icon: MapPinned,
          },
        ].map((item) => {
          const Icon = item.icon;
          return (
            <article key={item.title} className="rounded-[28px] border border-white/70 bg-card/90 p-5 shadow-card backdrop-blur">
              <div className="flex items-start gap-4">
                <span className="rounded-2xl bg-accentSoft p-3 text-accent">
                  <Icon className="h-5 w-5" />
                </span>
                <div>
                  <p className="font-semibold text-ink">{item.title}</p>
                  <p className="mt-2 text-sm text-slate-600">{item.description}</p>
                </div>
              </div>
            </article>
          );
        })}
      </section>

      <SectionCard
        title="Ruta del modulo"
        subtitle="Touch vive dentro de Kiosko, separado de layouts, para que el futuro player reciba un runtime interactivo consistente."
      >
        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded-[26px] border border-slate-200 bg-slate-50 px-5 py-5">
            <div className="flex items-start gap-4">
              <span className="rounded-2xl bg-white p-3 text-accent">
                <MonitorSmartphone className="h-5 w-5" />
              </span>
              <div>
                <p className="font-semibold text-ink">Kiosko legado</p>
                <p className="mt-2 text-sm text-slate-600">Sigue disponible para pantallas simples con hotspots clásicos.</p>
              </div>
            </div>
          </div>
          <div className="rounded-[26px] border border-slate-200 bg-slate-50 px-5 py-5">
            <div className="flex items-start gap-4">
              <span className="rounded-2xl bg-white p-3 text-accent">
                <Hand className="h-5 w-5" />
              </span>
              <div>
                <p className="font-semibold text-ink">Touch Experience Core</p>
                <p className="mt-2 text-sm text-slate-600">Nuevo builder con attract mode, pantallas internas, locations y runtime touch.</p>
              </div>
            </div>
          </div>
        </div>
      </SectionCard>
    </div>
  );
}
