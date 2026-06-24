import { MonitorSmartphone, Route, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { AttractModePanel } from "../components/AttractModePanel";
import { SectionCard } from "../components/SectionCard";
import { StatCard } from "../components/StatCard";
import { TouchHotspotInspector } from "../components/TouchHotspotInspector";
import { TouchLocationPanel } from "../components/TouchLocationPanel";
import { TouchScreenCanvas } from "../components/TouchScreenCanvas";
import { TouchScreenNavigator } from "../components/TouchScreenNavigator";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { canWriteClientScope } from "../lib/rbac";
import {
  createDefaultHotspotDraft,
  getTouchAttractScreen,
  getTouchHomeScreen,
  parseHotspot,
  resolveTouchAction,
  resolveTouchTimeout,
  serializeHotspotDraft,
} from "../lib/touch";
import { buildClientTouchPath } from "../lib/workspace";
import type {
  Branch,
  Channel,
  KioskScreen,
  TouchExperience,
  TouchExperienceAssignment,
  TouchLocation,
  TouchMap,
  TouchRuntimeConfig,
} from "../types/domain";

export function TouchExperienceBuilderPage() {
  const { token, user } = useAuth();
  const { clientId, experienceId } = useParams();
  const [experience, setExperience] = useState<TouchExperience | null>(null);
  const [screens, setScreens] = useState<KioskScreen[]>([]);
  const [locations, setLocations] = useState<TouchLocation[]>([]);
  const [maps, setMaps] = useState<TouchMap[]>([]);
  const [assignments, setAssignments] = useState<TouchExperienceAssignment[]>([]);
  const [runtime, setRuntime] = useState<TouchRuntimeConfig | null>(null);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [selectedScreenId, setSelectedScreenId] = useState<string | null>(null);
  const [selectedHotspotId, setSelectedHotspotId] = useState<string | null>(null);
  const [hotspotDraft, setHotspotDraft] = useState(createDefaultHotspotDraft());
  const [previewScreenId, setPreviewScreenId] = useState<string | null>(null);
  const [previewLocation, setPreviewLocation] = useState<TouchLocation | null>(null);
  const [previewNote, setPreviewNote] = useState<string | null>(null);
  const [assignmentForm, setAssignmentForm] = useState({
    branch_id: "",
    channel_id: "",
    sort_order: 1,
    is_active: true,
  });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const canManage = canWriteClientScope(user?.role);
  const selectedRuntimeScreen = runtime?.screens.find((screen) => screen.id === selectedScreenId) ?? null;
  const selectedButtons = selectedRuntimeScreen?.buttons ?? [];
  const previewScreen = useMemo(() => {
    if (!runtime) {
      return null;
    }
    return runtime.screens.find((screen) => screen.id === previewScreenId) ?? getTouchAttractScreen(runtime) ?? getTouchHomeScreen(runtime) ?? runtime.screens[0] ?? null;
  }, [previewScreenId, runtime]);
  const previewTimeout = runtime ? resolveTouchTimeout(runtime, previewScreen) : 30;
  const totalHotspots = useMemo(() => runtime?.screens.reduce((sum, screen) => sum + screen.buttons.length, 0) ?? 0, [runtime]);

  async function loadData() {
    if (!token || !clientId || !experienceId) {
      return;
    }

    setLoading(true);
    try {
      const [experienceResponse, screensResponse, locationsResponse, mapsResponse, assignmentsResponse, runtimeResponse, branchesResponse, channelsResponse] =
        await Promise.all([
          apiRequest<TouchExperience>(`/touch/experiences/${experienceId}`, { token }),
          apiRequest<KioskScreen[]>(`/touch/experiences/${experienceId}/screens`, { token }),
          apiRequest<TouchLocation[]>(`/touch/experiences/${experienceId}/locations`, { token }),
          apiRequest<TouchMap[]>(`/touch/experiences/${experienceId}/maps`, { token }),
          apiRequest<TouchExperienceAssignment[]>(`/touch/experiences/${experienceId}/assignments`, { token }),
          apiRequest<TouchRuntimeConfig>(`/touch/experiences/${experienceId}/runtime-config`, { token }),
          apiRequest<Branch[]>(`/branches?client_id=${clientId}`, { token }),
          apiRequest<Channel[]>(`/channels?client_id=${clientId}`, { token }),
        ]);

      setExperience(experienceResponse);
      setScreens(screensResponse);
      setLocations(locationsResponse);
      setMaps(mapsResponse);
      setAssignments(assignmentsResponse);
      setRuntime(runtimeResponse);
      setBranches(branchesResponse);
      setChannels(channelsResponse);

      const nextSelectedScreenId =
        screensResponse.find((screen) => screen.id === selectedScreenId)?.id ??
        runtimeResponse.experience.home_screen_id ??
        runtimeResponse.experience.attract_screen_id ??
        screensResponse[0]?.id ??
        null;
      setSelectedScreenId(nextSelectedScreenId);

      const nextPreviewScreenId =
        runtimeResponse.screens.find((screen) => screen.id === previewScreenId)?.id ??
        getTouchAttractScreen(runtimeResponse)?.id ??
        getTouchHomeScreen(runtimeResponse)?.id ??
        runtimeResponse.screens[0]?.id ??
        null;
      setPreviewScreenId(nextPreviewScreenId);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el builder touch");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadData();
  }, [clientId, experienceId, token]);

  useEffect(() => {
    if (!runtime || !selectedHotspotId) {
      return;
    }

    const currentButton = runtime.screens.flatMap((screen) => screen.buttons).find((button) => button.id === selectedHotspotId) ?? null;
    if (currentButton) {
      setHotspotDraft(parseHotspot(currentButton));
    }
  }, [runtime, selectedHotspotId]);

  useEffect(() => {
    if (!runtime || !previewScreen) {
      return;
    }

    const timeout = Math.max(3, previewTimeout);
    const timer = window.setTimeout(() => {
      const attract = getTouchAttractScreen(runtime);
      setPreviewScreenId(attract?.id ?? previewScreen.id);
      setPreviewLocation(null);
      setPreviewNote("La simulación regresó automáticamente al attract mode por inactividad.");
    }, timeout * 1000);

    return () => window.clearTimeout(timer);
  }, [previewScreen?.id, previewTimeout, runtime]);

  async function runAction(action: () => Promise<unknown>, fallbackMessage: string) {
    try {
      await action();
      setError(null);
      await loadData();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : fallbackMessage);
    }
  }

  async function saveExperience(payload: {
    name: string;
    slug: string;
    description: string;
    default_idle_timeout_seconds: number;
    attract_screen_id: string | null;
    home_screen_id: string | null;
    is_active: boolean;
  }) {
    if (!token || !experienceId) {
      return;
    }

    await runAction(
      () =>
        apiRequest(`/touch/experiences/${experienceId}`, {
          method: "PATCH",
          token,
          body: {
            ...payload,
            description: payload.description || null,
          },
        }),
      "No se pudo guardar la experiencia touch.",
    );
  }

  function resetHotspotDraft() {
    setSelectedHotspotId(null);
    setHotspotDraft({
      ...createDefaultHotspotDraft(),
      sort_order: selectedButtons.length + 1,
    });
  }

  function handlePreviewActivate(buttonId: string) {
    if (!runtime || !previewScreen) {
      return;
    }

    const button = runtime.screens.flatMap((screen) => screen.buttons).find((item) => item.id === buttonId);
    if (!button) {
      return;
    }

    const result = resolveTouchAction({ button, runtime, currentScreen: previewScreen });
    if (result.nextScreenId) {
      setPreviewScreenId(result.nextScreenId);
    }
    setPreviewLocation(result.location ?? null);
    setPreviewNote(result.note ?? null);
  }

  return (
    <div className="space-y-6">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <div className="flex flex-col gap-4 rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur lg:flex-row lg:items-center lg:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Touch Experience Builder</p>
          <h1 className="mt-2 font-display text-4xl text-ink">{experience?.name ?? "Experiencia touch"}</h1>
          <p className="mt-3 max-w-3xl text-sm text-slate-600">
            Construye attract mode, pantallas internas, hotspots, locations, mapas y asignaciones del runtime touch sin tocar aún el player real.
          </p>
        </div>
        <Link
          className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
          to={buildClientTouchPath(clientId ?? "")}
        >
          Volver a experiencias
        </Link>
      </div>

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Pantallas" value={String(screens.length)} hint="Atract, home, directorio y fichas" tone="teal" />
        <StatCard label="Hotspots" value={String(totalHotspots)} hint="Nodos interactivos sobre imagen" />
        <StatCard label="Locations" value={String(locations.length)} hint="Tiendas, POIs y categorias" tone="orange" />
        <StatCard label="Assignments" value={String(assignments.length)} hint="Sucursales y canales enlazados" />
      </section>

      <div className="grid gap-5 xl:grid-cols-[0.9fr_1.1fr]">
        <AttractModePanel experience={experience} screens={screens} canManage={canManage} onSave={saveExperience} />
        <TouchScreenNavigator
          screens={screens}
          selectedScreenId={selectedScreenId}
          canManage={canManage}
          onSelect={(screenId) => {
            setSelectedScreenId(screenId);
            resetHotspotDraft();
          }}
          onCreate={async (payload) => {
            if (!token || !experienceId) {
              return;
            }

            await runAction(
              () =>
                apiRequest(`/touch/experiences/${experienceId}/screens`, {
                  method: "POST",
                  token,
                  body: payload,
                }),
              "No se pudo crear la pantalla touch.",
            );
          }}
          onUpdate={async (screenId, payload) => {
            if (!token || !experienceId) {
              return;
            }

            await runAction(
              () =>
                apiRequest(`/touch/experiences/${experienceId}/screens/${screenId}`, {
                  method: "PATCH",
                  token,
                  body: payload,
                }),
              "No se pudo actualizar la pantalla touch.",
            );
          }}
          onDelete={async (screenId) => {
            if (!token || !experienceId) {
              return;
            }

            await runAction(
              () =>
                apiRequest(`/touch/experiences/${experienceId}/screens/${screenId}`, {
                  method: "DELETE",
                  token,
                }),
              "No se pudo eliminar la pantalla touch.",
            );
          }}
        />
      </div>

      <div className="grid gap-5 xl:grid-cols-[1.02fr_0.98fr]">
        <TouchScreenCanvas
          screen={selectedRuntimeScreen}
          buttons={selectedButtons}
          activeHotspotId={selectedHotspotId}
          onSelectHotspot={(buttonId) => {
            setSelectedHotspotId(buttonId);
            const button = selectedButtons.find((item) => item.id === buttonId);
            if (button) {
              setHotspotDraft(parseHotspot(button));
            }
          }}
          title="Canvas del builder"
          subtitle="Selecciona la pantalla y ajusta hotspots sobre la imagen. La misma configuracion alimenta el runtime touch."
          mode="builder"
        />
        <TouchHotspotInspector
          buttons={selectedButtons}
          screens={screens}
          locations={locations}
          selectedHotspotId={selectedHotspotId}
          draft={hotspotDraft}
          canManage={canManage}
          onSelectHotspot={(buttonId) => {
            if (!buttonId) {
              resetHotspotDraft();
              return;
            }
            setSelectedHotspotId(buttonId);
          }}
          onChange={setHotspotDraft}
          onCreate={async () => {
            if (!token || !experienceId || !selectedScreenId) {
              return;
            }

            await runAction(
              () =>
                apiRequest(`/touch/experiences/${experienceId}/screens/${selectedScreenId}/buttons`, {
                  method: "POST",
                  token,
                  body: {
                    label: hotspotDraft.label,
                    x: hotspotDraft.x,
                    y: hotspotDraft.y,
                    width: hotspotDraft.width,
                    height: hotspotDraft.height,
                    sort_order: hotspotDraft.sort_order,
                    is_hotspot: true,
                    style_json: {
                      backgroundColor: hotspotDraft.backgroundColor,
                      textColor: hotspotDraft.textColor,
                      opacity: hotspotDraft.opacity,
                    },
                    ...serializeHotspotDraft(hotspotDraft),
                  },
                }),
              "No se pudo crear el hotspot.",
            );
            resetHotspotDraft();
          }}
          onUpdate={async () => {
            if (!token || !experienceId || !selectedScreenId || !selectedHotspotId) {
              return;
            }

            await runAction(
              () =>
                apiRequest(`/touch/experiences/${experienceId}/screens/${selectedScreenId}/buttons/${selectedHotspotId}`, {
                  method: "PATCH",
                  token,
                  body: {
                    label: hotspotDraft.label,
                    x: hotspotDraft.x,
                    y: hotspotDraft.y,
                    width: hotspotDraft.width,
                    height: hotspotDraft.height,
                    sort_order: hotspotDraft.sort_order,
                    is_hotspot: true,
                    style_json: {
                      backgroundColor: hotspotDraft.backgroundColor,
                      textColor: hotspotDraft.textColor,
                      opacity: hotspotDraft.opacity,
                    },
                    ...serializeHotspotDraft(hotspotDraft),
                  },
                }),
              "No se pudo actualizar el hotspot.",
            );
          }}
          onDelete={async (buttonId) => {
            if (!token || !experienceId || !selectedScreenId) {
              return;
            }

            await runAction(
              () =>
                apiRequest(`/touch/experiences/${experienceId}/screens/${selectedScreenId}/buttons/${buttonId}`, {
                  method: "DELETE",
                  token,
                }),
              "No se pudo eliminar el hotspot.",
            );
            resetHotspotDraft();
          }}
        />
      </div>

      <TouchLocationPanel
        locations={locations}
        maps={maps}
        canManage={canManage}
        onCreateLocation={async (payload) => {
          if (!token || !experienceId) {
            return;
          }

          await runAction(
            () =>
              apiRequest(`/touch/experiences/${experienceId}/locations`, {
                method: "POST",
                token,
                body: payload,
              }),
            "No se pudo crear la location.",
          );
        }}
        onUpdateLocation={async (locationId, payload) => {
          if (!token || !experienceId) {
            return;
          }

          await runAction(
            () =>
              apiRequest(`/touch/experiences/${experienceId}/locations/${locationId}`, {
                method: "PATCH",
                token,
                body: payload,
              }),
            "No se pudo actualizar la location.",
          );
        }}
        onDeleteLocation={async (locationId) => {
          if (!token || !experienceId) {
            return;
          }

          await runAction(
            () =>
              apiRequest(`/touch/experiences/${experienceId}/locations/${locationId}`, {
                method: "DELETE",
                token,
              }),
            "No se pudo eliminar la location.",
          );
        }}
        onCreateMap={async (payload) => {
          if (!token || !experienceId) {
            return;
          }

          await runAction(
            () =>
              apiRequest(`/touch/experiences/${experienceId}/maps`, {
                method: "POST",
                token,
                body: payload,
              }),
            "No se pudo crear el mapa.",
          );
        }}
        onUpdateMap={async (mapId, payload) => {
          if (!token || !experienceId) {
            return;
          }

          await runAction(
            () =>
              apiRequest(`/touch/experiences/${experienceId}/maps/${mapId}`, {
                method: "PATCH",
                token,
                body: payload,
              }),
            "No se pudo actualizar el mapa.",
          );
        }}
        onDeleteMap={async (mapId) => {
          if (!token || !experienceId) {
            return;
          }

          await runAction(
            () =>
              apiRequest(`/touch/experiences/${experienceId}/maps/${mapId}`, {
                method: "DELETE",
                token,
              }),
            "No se pudo eliminar el mapa.",
          );
        }}
      />

      <div className="grid gap-5 xl:grid-cols-[0.95fr_1.05fr]">
        <SectionCard
          title="Assignments y runtime"
          subtitle="Asigna la experiencia a una sucursal o a un canal especifico. La prioridad futura puede resolverse desde el player touch."
        >
          {!canManage ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Este rol solo puede revisar assignments y runtime de la experiencia.
            </div>
          ) : (
            <div className="space-y-4">
              <div className="grid gap-4 md:grid-cols-3">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Sucursal</label>
                  <select
                    value={assignmentForm.branch_id}
                    onChange={(event) => setAssignmentForm({ ...assignmentForm, branch_id: event.target.value })}
                  >
                    <option value="">Sin sucursal</option>
                    {branches.map((branch) => (
                      <option key={branch.id} value={branch.id}>
                        {branch.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Canal</label>
                  <select
                    value={assignmentForm.channel_id}
                    onChange={(event) => setAssignmentForm({ ...assignmentForm, channel_id: event.target.value })}
                  >
                    <option value="">Sin canal</option>
                    {channels
                      .filter((channel) => !assignmentForm.branch_id || channel.branch_id === assignmentForm.branch_id)
                      .map((channel) => (
                        <option key={channel.id} value={channel.id}>
                          {channel.name}
                        </option>
                      ))}
                  </select>
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Orden</label>
                  <input
                    type="number"
                    value={assignmentForm.sort_order}
                    onChange={(event) => setAssignmentForm({ ...assignmentForm, sort_order: Number(event.target.value) })}
                  />
                </div>
              </div>

              <label className="flex items-center gap-3 rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4">
                <input
                  className="h-5 w-5"
                  type="checkbox"
                  checked={assignmentForm.is_active}
                  onChange={(event) => setAssignmentForm({ ...assignmentForm, is_active: event.target.checked })}
                />
                <span className="text-sm font-semibold text-slate-700">Assignment activo</span>
              </label>

              <button
                className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white"
                type="button"
                onClick={async () => {
                  if (!token || !experienceId) {
                    return;
                  }

                  await runAction(
                    () =>
                      apiRequest(`/touch/experiences/${experienceId}/assignments`, {
                        method: "POST",
                        token,
                        body: {
                          branch_id: assignmentForm.branch_id || null,
                          channel_id: assignmentForm.channel_id || null,
                          sort_order: assignmentForm.sort_order,
                          is_active: assignmentForm.is_active,
                        },
                      }),
                    "No se pudo crear el assignment.",
                  );
                }}
              >
                Crear assignment
              </button>

              <div className="space-y-3">
                {assignments.length > 0 ? (
                  assignments.map((assignment) => {
                    const branch = branches.find((item) => item.id === assignment.branch_id);
                    const channel = channels.find((item) => item.id === assignment.channel_id);
                    return (
                      <article key={assignment.id} className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                          <div>
                            <p className="font-semibold text-ink">{branch?.name ?? "Sin sucursal"} · {channel?.name ?? "Sin canal"}</p>
                            <p className="mt-1 text-sm text-slate-500">
                              orden {assignment.sort_order} · {assignment.is_active ? "Activo" : "Pausado"}
                            </p>
                          </div>
                          {canManage ? (
                            <button
                              className="rounded-full border border-rose-200 px-4 py-2 text-sm font-semibold text-rose-700"
                              type="button"
                              onClick={async () => {
                                if (!token || !experienceId) {
                                  return;
                                }

                                await runAction(
                                  () =>
                                    apiRequest(`/touch/experiences/${experienceId}/assignments/${assignment.id}`, {
                                      method: "DELETE",
                                      token,
                                    }),
                                  "No se pudo eliminar el assignment.",
                                );
                              }}
                            >
                              Eliminar
                            </button>
                          ) : null}
                        </div>
                      </article>
                    );
                  })
                ) : (
                  <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                    Todavía no hay assignments activos para esta experiencia.
                  </div>
                )}
              </div>
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Preview interactivo"
          subtitle="Simula el comportamiento touch: attract mode, home, hotspots, ficha de tienda y regreso automatico por inactividad."
        >
          <div className="space-y-4">
            <div className="flex flex-wrap gap-3">
              <button
                className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700"
                type="button"
                onClick={() => {
                  const attract = runtime ? getTouchAttractScreen(runtime) : null;
                  setPreviewScreenId(attract?.id ?? runtime?.screens[0]?.id ?? null);
                  setPreviewLocation(null);
                  setPreviewNote("Preview reiniciado en attract mode.");
                }}
              >
                Ir a attract
              </button>
              <button
                className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700"
                type="button"
                onClick={() => {
                  const home = runtime ? getTouchHomeScreen(runtime) : null;
                  setPreviewScreenId(home?.id ?? runtime?.screens[0]?.id ?? null);
                  setPreviewLocation(null);
                  setPreviewNote("Preview movido a home.");
                }}
              >
                Ir a home
              </button>
              <div className="rounded-full bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-700">
                Timeout activo: {previewTimeout}s
              </div>
            </div>

            <TouchScreenCanvas
              screen={previewScreen}
              buttons={previewScreen?.buttons ?? []}
              mode="preview"
              title="Player touch simulado"
              subtitle="Haz clic en los hotspots para recorrer la experiencia. Si no interactuas, el preview vuelve solo al attract mode."
              onActivateHotspot={(button) => handlePreviewActivate(button.id)}
              location={previewLocation}
              maps={maps}
              note={previewNote}
            />

            <div className="grid gap-4 md:grid-cols-3">
              <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                <div className="flex items-center gap-3">
                  <span className="rounded-2xl bg-white p-3 text-accent">
                    <Sparkles className="h-5 w-5" />
                  </span>
                  <div>
                    <p className="font-semibold text-ink">Attract mode</p>
                    <p className="text-sm text-slate-500">{runtime?.experience.attract_screen_id ? "Configurado" : "Pendiente"}</p>
                  </div>
                </div>
              </article>
              <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                <div className="flex items-center gap-3">
                  <span className="rounded-2xl bg-white p-3 text-accent">
                    <MonitorSmartphone className="h-5 w-5" />
                  </span>
                  <div>
                    <p className="font-semibold text-ink">Pantalla activa</p>
                    <p className="text-sm text-slate-500">{previewScreen?.name ?? "Sin pantalla"}</p>
                  </div>
                </div>
              </article>
              <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                <div className="flex items-center gap-3">
                  <span className="rounded-2xl bg-white p-3 text-accent">
                    <Route className="h-5 w-5" />
                  </span>
                  <div>
                    <p className="font-semibold text-ink">Runtime touch</p>
                    <p className="text-sm text-slate-500">
                      v{runtime?.touch_runtime_version ?? 0} · player_ready {runtime?.player_ready ? "true" : "false"}
                    </p>
                  </div>
                </div>
              </article>
            </div>
          </div>
        </SectionCard>
      </div>

      <SectionCard
        title="Estado del runtime"
        subtitle="Resumen del payload que consumira el futuro player touch: pantallas, hotspots, locations, maps y assignments."
      >
        <div className="grid gap-4 md:grid-cols-4">
          <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
            pantallas runtime: {runtime?.screens.length ?? 0}
          </div>
          <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
            hotspots runtime: {totalHotspots}
          </div>
          <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
            locations runtime: {runtime?.locations.length ?? 0}
          </div>
          <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
            assignments runtime: {runtime?.assignments.length ?? 0}
          </div>
        </div>
      </SectionCard>

      {loading ? <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-4 text-sm text-slate-600">Actualizando builder touch...</div> : null}
    </div>
  );
}
