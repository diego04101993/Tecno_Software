import { Hand, MonitorSmartphone, Sparkles, TvMinimalPlay } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { KioskPreview } from "../components/KioskPreview";
import { SectionCard } from "../components/SectionCard";
import { StatCard } from "../components/StatCard";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { canAccessGlobalClients, canWriteClientScope } from "../lib/rbac";
import { buildClientTouchPath } from "../lib/workspace";
import type { Client, KioskButton, KioskScreen, TouchExperience } from "../types/domain";

export function KioskPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const [clients, setClients] = useState<Client[]>([]);
  const [screens, setScreens] = useState<KioskScreen[]>([]);
  const [buttons, setButtons] = useState<KioskButton[]>([]);
  const [experiences, setExperiences] = useState<TouchExperience[]>([]);
  const [selectedScreenId, setSelectedScreenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [screenForm, setScreenForm] = useState({
    client_id: clientId ?? user?.client_id ?? "",
    name: "",
    slug: "",
    background_url: "",
    attract_media_url: "",
    inactivity_timeout_seconds: 30,
    is_attract_screen: false,
  });
  const [buttonForm, setButtonForm] = useState({
    label: "",
    x: 24,
    y: 24,
    width: 240,
    height: 72,
    action_type: "navigate_menu",
    action_value: "",
    target_screen_id: "",
    sort_order: 1,
  });

  const canManageKiosk = canWriteClientScope(user?.role);
  const selectedScreen = screens.find((screen) => screen.id === selectedScreenId) ?? null;
  const activeExperiences = useMemo(() => experiences.filter((item) => item.is_active), [experiences]);

  async function loadData(nextSelectedId?: string | null) {
    if (!token) {
      return;
    }

    const screenPath = clientId ? `/kiosk/screens?client_id=${clientId}` : "/kiosk/screens";
    const touchPath = clientId ? `/touch/experiences?client_id=${clientId}` : "/touch/experiences";

    try {
      const [clientsResponse, screensResponse, experiencesResponse] = await Promise.all([
        apiRequest<Client[]>("/clients", { token }),
        apiRequest<KioskScreen[]>(screenPath, { token }),
        apiRequest<TouchExperience[]>(touchPath, { token }),
      ]);
      setClients(clientsResponse);
      setScreens(screensResponse);
      setExperiences(experiencesResponse);

      const selected = nextSelectedId ?? selectedScreenId ?? screensResponse[0]?.id ?? null;
      setSelectedScreenId(selected);
      if (selected) {
        const buttonsResponse = await apiRequest<KioskButton[]>(`/kiosk/screens/${selected}/buttons`, { token });
        setButtons(buttonsResponse);
      } else {
        setButtons([]);
      }
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el modulo de kiosko");
    }
  }

  useEffect(() => {
    void loadData();
  }, [clientId, token]);

  useEffect(() => {
    if (!selectedScreenId || !token) {
      return;
    }
    apiRequest<KioskButton[]>(`/kiosk/screens/${selectedScreenId}/buttons`, { token })
      .then((response) => setButtons(response))
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar los hotspots");
      });
  }, [selectedScreenId, token]);

  useEffect(() => {
    if (clientId || user?.client_id) {
      setScreenForm((current) => ({ ...current, client_id: clientId ?? user?.client_id ?? current.client_id }));
    }
  }, [clientId, user?.client_id]);

  async function createScreen(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !canManageKiosk) {
      return;
    }

    try {
      const created = await apiRequest<KioskScreen>("/kiosk/screens", {
        method: "POST",
        token,
        body: {
          ...screenForm,
          client_id: screenForm.client_id || user?.client_id,
          background_url: screenForm.background_url || null,
          attract_media_url: screenForm.attract_media_url || null,
        },
      });
      setScreenForm((current) => ({
        ...current,
        name: "",
        slug: "",
        background_url: "",
        attract_media_url: "",
      }));
      await loadData(created.id);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear la pantalla heredada");
    }
  }

  async function createButton(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !selectedScreenId || !canManageKiosk) {
      return;
    }

    try {
      await apiRequest<KioskButton>(`/kiosk/screens/${selectedScreenId}/buttons`, {
        method: "POST",
        token,
        body: {
          ...buttonForm,
          action_value: buttonForm.action_value || null,
          target_screen_id: buttonForm.target_screen_id || null,
          style_json: {},
          action_payload_json: {},
          is_hotspot: true,
        },
      });
      setButtonForm((current) => ({
        ...current,
        label: "",
        action_value: "",
        target_screen_id: "",
      }));
      await loadData(selectedScreenId);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear el hotspot heredado");
    }
  }

  return (
    <div className="space-y-6">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Touch experiences" value={String(experiences.length)} hint="Nuevo builder interactivo" tone="teal" />
        <StatCard label="Touch activas" value={String(activeExperiences.length)} hint="Listas para asignación" />
        <StatCard label="Pantallas heredadas" value={String(screens.length)} hint="Kiosko clásico por pantalla" tone="orange" />
        <StatCard label="Hotspots heredados" value={String(buttons.length)} hint="Solo de la pantalla seleccionada" />
      </section>

      <div className="grid gap-5 2xl:grid-cols-[1.1fr_0.9fr]">
        <SectionCard
          title="Touch Experience Core"
          subtitle="Nuevo modulo para directorios, attract mode, hotspots por pantalla, locations y runtime touch preparado para el futuro player."
        >
          <div className="space-y-4">
            <div className="grid gap-4 md:grid-cols-3">
              {[
                {
                  title: "Attract mode",
                  description: "Publicidad de reposo y regreso automatico tras inactividad.",
                  icon: Sparkles,
                },
                {
                  title: "Hotspots sobre imagen",
                  description: "Navega entre pantallas, abre tiendas o vuelve a home.",
                  icon: Hand,
                },
                {
                  title: "Directorio por locations",
                  description: "Carga tiendas, categorias, pisos, locales y mapas base.",
                  icon: MonitorSmartphone,
                },
              ].map((item) => {
                const Icon = item.icon;
                return (
                  <article key={item.title} className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                    <div className="flex items-start gap-4">
                      <span className="rounded-2xl bg-white p-3 text-accent">
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
            </div>

            <div className="flex flex-wrap gap-3">
              <Link className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white" to={buildClientTouchPath(clientId ?? "")}>
                Abrir modulo touch
              </Link>
              <div className="rounded-full bg-slate-100 px-4 py-3 text-sm font-semibold text-slate-700">
                {activeExperiences.length} experiencia(s) activa(s)
              </div>
            </div>
          </div>
        </SectionCard>

        <SectionCard
          title="Kiosko heredado"
          subtitle="Se mantiene compatible para pantallas simples. Las experiencias touch avanzadas ya viven separadas dentro de Kiosko / Touch."
        >
          <div className="grid gap-4 md:grid-cols-2">
            <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
              <div className="flex items-start gap-4">
                <span className="rounded-2xl bg-white p-3 text-accent">
                  <TvMinimalPlay className="h-5 w-5" />
                </span>
                <div>
                  <p className="font-semibold text-ink">Pantallas simples</p>
                  <p className="mt-2 text-sm text-slate-600">Editor rapido para un fondo, botones y modo attract tradicional.</p>
                </div>
              </div>
            </article>
            <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
              <div className="flex items-start gap-4">
                <span className="rounded-2xl bg-white p-3 text-accent">
                  <Hand className="h-5 w-5" />
                </span>
                <div>
                  <p className="font-semibold text-ink">Hotspots básicos</p>
                  <p className="mt-2 text-sm text-slate-600">Util para kioskos heredados mientras el player touch nuevo se termina de conectar.</p>
                </div>
              </div>
            </article>
          </div>
        </SectionCard>
      </div>

      <div className="grid gap-5 2xl:grid-cols-[1.08fr_0.92fr]">
        <SectionCard title="Pantallas heredadas" subtitle="Solo se listan pantallas de kiosko clásico; las touch ya no se mezclan aquí.">
          <div className="grid gap-4 2xl:grid-cols-2">
            {screens.length > 0 ? (
              screens.map((screen) => (
                <button
                  type="button"
                  key={screen.id}
                  onClick={() => setSelectedScreenId(screen.id)}
                  className={`w-full rounded-[28px] border p-5 text-left transition ${
                    selectedScreenId === screen.id ? "border-accent bg-accentSoft/70" : "border-slate-200 bg-white hover:border-accent/30"
                  }`}
                >
                  <p className="font-display text-2xl text-ink">{screen.name}</p>
                  <p className="mt-2 text-sm text-slate-600">
                    {screen.slug} · timeout {screen.inactivity_timeout_seconds}s
                  </p>
                </button>
              ))
            ) : (
              <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                No hay pantallas heredadas para este cliente.
              </div>
            )}
          </div>
        </SectionCard>

        <SectionCard title="Crear pantalla heredada" subtitle="Se conserva el flujo clásico para una sola pantalla con hotspots simples.">
          {!canManageKiosk ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-5 text-sm text-slate-600">
              Este rol solo puede consultar las pantallas de kiosko disponibles.
            </div>
          ) : (
            <form className="grid gap-4 xl:grid-cols-2" onSubmit={createScreen}>
              {canAccessGlobalClients(user?.role) && !clientId ? (
                <div className="md:col-span-2">
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Cliente</label>
                  <select value={screenForm.client_id} onChange={(event) => setScreenForm({ ...screenForm, client_id: event.target.value })} required>
                    <option value="">Selecciona un cliente</option>
                    {clients.map((client) => (
                      <option key={client.id} value={client.id}>
                        {client.name}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
                <input value={screenForm.name} onChange={(event) => setScreenForm({ ...screenForm, name: event.target.value })} required />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Slug</label>
                <input value={screenForm.slug} onChange={(event) => setScreenForm({ ...screenForm, slug: event.target.value })} required />
              </div>
              <div className="md:col-span-2">
                <label className="mb-2 block text-sm font-semibold text-slate-700">Background URL</label>
                <input value={screenForm.background_url} onChange={(event) => setScreenForm({ ...screenForm, background_url: event.target.value })} />
              </div>
              <div className="md:col-span-2">
                <label className="mb-2 block text-sm font-semibold text-slate-700">Media attract URL</label>
                <input value={screenForm.attract_media_url} onChange={(event) => setScreenForm({ ...screenForm, attract_media_url: event.target.value })} />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Timeout de inactividad</label>
                <input
                  type="number"
                  value={screenForm.inactivity_timeout_seconds}
                  onChange={(event) => setScreenForm({ ...screenForm, inactivity_timeout_seconds: Number(event.target.value) })}
                />
              </div>
              <label className="flex items-center gap-3 rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4">
                <input
                  className="h-5 w-5"
                  type="checkbox"
                  checked={screenForm.is_attract_screen}
                  onChange={(event) => setScreenForm({ ...screenForm, is_attract_screen: event.target.checked })}
                />
                <span className="text-sm font-semibold text-slate-700">Es pantalla attract</span>
              </label>
              <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white md:col-span-2" type="submit">
                Guardar pantalla
              </button>
            </form>
          )}
        </SectionCard>
      </div>

      <div className="grid gap-5 2xl:grid-cols-[1.16fr_0.84fr]">
        <KioskPreview screen={selectedScreen} buttons={buttons} />

        <SectionCard title="Hotspots heredados" subtitle="Se conservan para kiosko clásico mientras el runtime touch nuevo madura.">
          {!canManageKiosk ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-5 text-sm text-slate-600">
              Este rol no puede crear ni modificar hotspots heredados.
            </div>
          ) : (
            <form className="space-y-4" onSubmit={createButton}>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Etiqueta</label>
                <input value={buttonForm.label} onChange={(event) => setButtonForm({ ...buttonForm, label: event.target.value })} required />
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">X</label>
                  <input type="number" value={buttonForm.x} onChange={(event) => setButtonForm({ ...buttonForm, x: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Y</label>
                  <input type="number" value={buttonForm.y} onChange={(event) => setButtonForm({ ...buttonForm, y: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho</label>
                  <input type="number" value={buttonForm.width} onChange={(event) => setButtonForm({ ...buttonForm, width: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Alto</label>
                  <input type="number" value={buttonForm.height} onChange={(event) => setButtonForm({ ...buttonForm, height: Number(event.target.value) })} />
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Accion</label>
                  <select value={buttonForm.action_type} onChange={(event) => setButtonForm({ ...buttonForm, action_type: event.target.value })}>
                    <option value="navigate_menu">Navigate menu</option>
                    <option value="switch_screen">Switch screen</option>
                    <option value="open_url">Open URL</option>
                    <option value="play_video">Play video</option>
                  </select>
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Orden</label>
                  <input
                    type="number"
                    value={buttonForm.sort_order}
                    onChange={(event) => setButtonForm({ ...buttonForm, sort_order: Number(event.target.value) })}
                  />
                </div>
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Action value</label>
                <input value={buttonForm.action_value} onChange={(event) => setButtonForm({ ...buttonForm, action_value: event.target.value })} />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Target screen ID</label>
                <input value={buttonForm.target_screen_id} onChange={(event) => setButtonForm({ ...buttonForm, target_screen_id: event.target.value })} />
              </div>
              <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" type="submit" disabled={!selectedScreenId}>
                Guardar hotspot
              </button>
            </form>
          )}
        </SectionCard>
      </div>
    </div>
  );
}
