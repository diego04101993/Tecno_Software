import { FormEvent, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { SectionCard } from "../components/SectionCard";
import { TimelineBoard } from "../components/TimelineBoard";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatContentType } from "../lib/labels";
import { canAccessGlobalClients, canWriteBranchScope, canWriteClientScope } from "../lib/rbac";
import type { Campaign, Channel, Client, ContentItem, Layout, PlaylistItem, ScheduleItem } from "../types/domain";

export function CampaignsPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const [clients, setClients] = useState<Client[]>([]);
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [contents, setContents] = useState<ContentItem[]>([]);
  const [playlist, setPlaylist] = useState<PlaylistItem[]>([]);
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [selectedCampaignId, setSelectedCampaignId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [campaignForm, setCampaignForm] = useState({
    client_id: clientId ?? user?.client_id ?? "",
    layout_id: "",
    name: "",
    description: "",
    default_duration_seconds: 15,
  });
  const [scheduleForm, setScheduleForm] = useState({
    title: "",
    campaign_id: "",
    channel_id: "",
    layout_id: "",
    recurrence: "daily",
    days_of_week: "1,2,3,4,5",
    start_time: "08:00",
    end_time: "22:00",
  });
  const [assignmentChannelId, setAssignmentChannelId] = useState("");

  const canCreateCampaign = canWriteClientScope(user?.role);
  const canEditPlaylist = canWriteClientScope(user?.role);
  const canAssignCampaign = canWriteClientScope(user?.role);
  const canCreateSchedule = canWriteBranchScope(user?.role);

  async function loadData() {
    if (!token) {
      return;
    }

    const scopedClientId = clientId ?? campaignForm.client_id ?? user?.client_id ?? "";
    const layoutPath = scopedClientId ? `/layouts?client_id=${scopedClientId}` : "/layouts";
    const channelPath = scopedClientId ? `/channels?client_id=${scopedClientId}` : "/channels";
    const campaignPath = scopedClientId ? `/campaigns?client_id=${scopedClientId}` : "/campaigns";
    const contentPath = scopedClientId ? `/contents?client_id=${scopedClientId}` : "/contents";
    const schedulePath = scopedClientId ? `/schedules?client_id=${scopedClientId}` : "/schedules";

    try {
      const [clientsResponse, layoutsResponse, channelsResponse, campaignsResponse, contentsResponse, schedulesResponse] =
        await Promise.all([
          apiRequest<Client[]>("/clients", { token }),
          apiRequest<Layout[]>(layoutPath, { token }),
          apiRequest<Channel[]>(channelPath, { token }),
          apiRequest<Campaign[]>(campaignPath, { token }),
          apiRequest<ContentItem[]>(contentPath, { token }),
          apiRequest<ScheduleItem[]>(schedulePath, { token }),
        ]);

      setClients(clientsResponse);
      setLayouts(layoutsResponse);
      setChannels(channelsResponse);
      setCampaigns(campaignsResponse);
      setContents(contentsResponse);
      setSchedules(schedulesResponse);

      const nextSelectedId = selectedCampaignId ?? campaignsResponse[0]?.id ?? null;
      setSelectedCampaignId(nextSelectedId);
      setScheduleForm((current) => ({ ...current, campaign_id: nextSelectedId ?? current.campaign_id }));

      if (nextSelectedId) {
        const playlistResponse = await apiRequest<PlaylistItem[]>(`/campaigns/${nextSelectedId}/playlist`, { token });
        setPlaylist(playlistResponse);
      } else {
        setPlaylist([]);
      }
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar las campañas");
    }
  }

  useEffect(() => {
    loadData();
  }, [clientId, token]);

  useEffect(() => {
    if (!token || !selectedCampaignId) {
      return;
    }

    apiRequest<PlaylistItem[]>(`/campaigns/${selectedCampaignId}/playlist`, { token })
      .then((response) => setPlaylist(response))
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar la playlist");
      });
  }, [selectedCampaignId, token]);

  useEffect(() => {
    if (clientId || user?.client_id) {
      setCampaignForm((current) => ({ ...current, client_id: clientId ?? user?.client_id ?? current.client_id }));
    }
  }, [clientId, user?.client_id]);

  async function createCampaign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !canCreateCampaign) {
      return;
    }

    try {
      const created = await apiRequest<Campaign>("/campaigns", {
        method: "POST",
        token,
        body: {
          ...campaignForm,
          client_id: campaignForm.client_id || user?.client_id,
          layout_id: campaignForm.layout_id || null,
          is_active: true,
          loop_enabled: true,
        },
      });
      setCampaignForm((current) => ({
        ...current,
        layout_id: "",
        name: "",
        description: "",
        default_duration_seconds: 15,
      }));
      setSelectedCampaignId(created.id);
      loadData();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear la campaña");
    }
  }

  async function addContentToPlaylist(contentId: string) {
    if (!token || !selectedCampaignId || !canEditPlaylist) {
      return;
    }

    try {
      await apiRequest(`/campaigns/${selectedCampaignId}/playlist`, {
        method: "POST",
        token,
        body: {
          content_id: contentId,
          sort_order: playlist.length + 1,
          duration_seconds: contents.find((item) => item.id === contentId)?.duration_seconds ?? 15,
          zone_key: "main",
        },
      });
      const updatedPlaylist = await apiRequest<PlaylistItem[]>(`/campaigns/${selectedCampaignId}/playlist`, { token });
      setPlaylist(updatedPlaylist);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo agregar el contenido");
    }
  }

  async function createSchedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !selectedCampaignId || !canCreateSchedule) {
      return;
    }

    try {
      await apiRequest<ScheduleItem>("/schedules", {
        method: "POST",
        token,
        body: {
          client_id: campaignForm.client_id || user?.client_id,
          branch_id: user?.branch_id || null,
          campaign_id: selectedCampaignId,
          channel_id: scheduleForm.channel_id || null,
          layout_id: scheduleForm.layout_id || null,
          title: scheduleForm.title,
          recurrence: scheduleForm.recurrence,
          days_of_week: scheduleForm.days_of_week
            .split(",")
            .map((value) => Number(value.trim()))
            .filter(Boolean),
          start_time: scheduleForm.start_time,
          end_time: scheduleForm.end_time,
          is_looping: true,
          timezone: "America/Mexico_City",
        },
      });
      setScheduleForm((current) => ({
        ...current,
        title: "",
        channel_id: "",
        layout_id: "",
        days_of_week: "1,2,3,4,5",
      }));
      loadData();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo programar la campaña");
    }
  }

  async function assignCampaign() {
    if (!token || !selectedCampaignId || !assignmentChannelId || !canAssignCampaign) {
      return;
    }

    try {
      await apiRequest(`/campaigns/${selectedCampaignId}/assignments`, {
        method: "POST",
        token,
        body: { channel_id: assignmentChannelId, priority: 1 },
      });
      setAssignmentChannelId("");
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo asignar la campaña");
    }
  }

  const selectedCampaign = campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? null;
  const selectedLayout = layouts.find((layout) => layout.id === selectedCampaign?.layout_id) ?? null;
  const scopedChannels = canAccessGlobalClients(user?.role)
    ? channels.filter((channel) => !campaignForm.client_id || channel.client_id === campaignForm.client_id)
    : channels;
  const scopedLayouts = canAccessGlobalClients(user?.role)
    ? layouts.filter((layout) => !campaignForm.client_id || layout.client_id === campaignForm.client_id)
    : layouts;
  const scopedContents = canAccessGlobalClients(user?.role)
    ? contents.filter((content) => !campaignForm.client_id || content.client_id === campaignForm.client_id)
    : contents;

  return (
    <div className="space-y-6">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <div className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
        <SectionCard title="Campañas activas" subtitle="Contenido, layout y loop listos para asignarse dentro del cliente actual.">
          <div className="space-y-4">
            {campaigns.map((campaign) => (
              <button
                type="button"
                key={campaign.id}
                onClick={() => {
                  setSelectedCampaignId(campaign.id);
                  setScheduleForm((current) => ({ ...current, campaign_id: campaign.id }));
                }}
                className={`w-full rounded-[28px] border p-5 text-left transition ${
                  selectedCampaignId === campaign.id
                    ? "border-accent bg-accentSoft/70"
                    : "border-slate-200 bg-white hover:border-accent/30"
                }`}
              >
                <p className="font-display text-2xl text-ink">{campaign.name}</p>
                <p className="mt-2 text-sm text-slate-600">{campaign.description ?? "Sin descripción."}</p>
                <p className="mt-4 text-xs uppercase tracking-wide text-slate-400">
                  Loop {campaign.loop_enabled ? "activo" : "apagado"} · {campaign.default_duration_seconds}s por default
                </p>
              </button>
            ))}
          </div>
        </SectionCard>

        <SectionCard title="Crear campaña" subtitle="La campaña ya vive dentro del workspace del cliente seleccionado.">
          {!canCreateCampaign ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-5 text-sm text-slate-600">
              Tu rol puede consultar campañas y timeline, pero no crear ni modificar campañas en esta fase.
            </div>
          ) : (
            <form className="grid gap-4 md:grid-cols-2" onSubmit={createCampaign}>
              {canAccessGlobalClients(user?.role) && !clientId ? (
                <div className="md:col-span-2">
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Cliente</label>
                  <select
                    value={campaignForm.client_id}
                    onChange={(event) => setCampaignForm({ ...campaignForm, client_id: event.target.value, layout_id: "" })}
                    required
                  >
                    <option value="">Selecciona un cliente</option>
                    {clients.map((client) => (
                      <option key={client.id} value={client.id}>
                        {client.name}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}
              <div className="md:col-span-2">
                <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
                <input value={campaignForm.name} onChange={(event) => setCampaignForm({ ...campaignForm, name: event.target.value })} required />
              </div>
              <div className="md:col-span-2">
                <label className="mb-2 block text-sm font-semibold text-slate-700">Descripción</label>
                <textarea
                  rows={3}
                  value={campaignForm.description}
                  onChange={(event) => setCampaignForm({ ...campaignForm, description: event.target.value })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Layout</label>
                <select value={campaignForm.layout_id} onChange={(event) => setCampaignForm({ ...campaignForm, layout_id: event.target.value })}>
                  <option value="">Sin layout</option>
                  {scopedLayouts.map((layout) => (
                    <option key={layout.id} value={layout.id}>
                      {layout.name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Duración base</label>
                <input
                  type="number"
                  value={campaignForm.default_duration_seconds}
                  onChange={(event) => setCampaignForm({ ...campaignForm, default_duration_seconds: Number(event.target.value) })}
                />
              </div>
              <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white md:col-span-2" type="submit">
                Crear campaña
              </button>
            </form>
          )}
        </SectionCard>
      </div>

      <div className="grid gap-6 xl:grid-cols-[1fr_1fr]">
        <SectionCard
          title="Playlist visual"
          subtitle={selectedCampaign ? `Campaña seleccionada: ${selectedCampaign.name}` : "Selecciona una campaña para armar la secuencia."}
          action={
            selectedLayout ? (
              <span className="rounded-full bg-emberSoft px-4 py-2 text-xs font-semibold uppercase tracking-wide text-ember">
                Layout: {selectedLayout.name}
              </span>
            ) : null
          }
        >
          {!canEditPlaylist ? (
            <div className="mb-4 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
              Tu rol puede revisar la playlist, pero no modificarla.
            </div>
          ) : null}
          <div className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
            <div className="space-y-3">
              {playlist.map((item) => {
                const content = contents.find((contentItem) => contentItem.id === item.content_id);
                return (
                  <div key={item.id} className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
                    <p className="font-semibold text-ink">{content?.name ?? "Contenido"}</p>
                    <p className="mt-1 text-sm text-slate-500">
                      Orden {item.sort_order} · {item.duration_seconds}s · zona {item.zone_key}
                    </p>
                  </div>
                );
              })}
            </div>
            <div className="space-y-3">
              {scopedContents.map((content) => (
                <button
                  type="button"
                  key={content.id}
                  onClick={() => addContentToPlaylist(content.id)}
                  disabled={!selectedCampaignId || !canEditPlaylist}
                  className="w-full rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-left transition hover:border-accent/40 hover:bg-accentSoft/30 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <p className="font-semibold text-ink">{content.name}</p>
                  <p className="mt-1 text-sm text-slate-500">
                    {formatContentType(content.type)} · {content.duration_seconds}s
                  </p>
                </button>
              ))}
            </div>
          </div>
        </SectionCard>

        <SectionCard title="Asignación a canales" subtitle="La asignación se mantiene dentro del mismo cliente y ya no como flujo global sin contexto.">
          {!canAssignCampaign ? (
            <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
              Este rol no puede asignar campañas a canales.
            </div>
          ) : (
            <div className="grid gap-4 md:grid-cols-[1fr_auto]">
              <select value={assignmentChannelId} onChange={(event) => setAssignmentChannelId(event.target.value)} disabled={!selectedCampaignId}>
                <option value="">Selecciona un canal</option>
                {scopedChannels.map((channel) => (
                  <option key={channel.id} value={channel.id}>
                    {channel.name}
                  </option>
                ))}
              </select>
              <button className="rounded-[20px] bg-accent px-5 py-4 font-semibold text-white" type="button" onClick={assignCampaign}>
                Asignar campaña
              </button>
            </div>
          )}
        </SectionCard>
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <SectionCard title="Timeline operativo" subtitle="Vista calendario del cliente. El editor drag and drop profundo queda para una fase posterior.">
          <TimelineBoard schedules={schedules} campaigns={campaigns} selectedCampaignId={selectedCampaignId} />
        </SectionCard>

        <SectionCard title="Programar campaña" subtitle="Programación base dentro del contexto del cliente actual.">
          {!canCreateSchedule ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-5 text-sm text-slate-600">
              Este rol solo puede monitorear la programación actual.
            </div>
          ) : (
            <form className="space-y-4" onSubmit={createSchedule}>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Título del slot</label>
                <input value={scheduleForm.title} onChange={(event) => setScheduleForm({ ...scheduleForm, title: event.target.value })} required />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Canal</label>
                <select value={scheduleForm.channel_id} onChange={(event) => setScheduleForm({ ...scheduleForm, channel_id: event.target.value })}>
                  <option value="">Todos / por layout</option>
                  {scopedChannels.map((channel) => (
                    <option key={channel.id} value={channel.id}>
                      {channel.name}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Layout</label>
                <select value={scheduleForm.layout_id} onChange={(event) => setScheduleForm({ ...scheduleForm, layout_id: event.target.value })}>
                  <option value="">Usar layout de la campaña</option>
                  {scopedLayouts.map((layout) => (
                    <option key={layout.id} value={layout.id}>
                      {layout.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Inicio</label>
                  <input
                    type="time"
                    value={scheduleForm.start_time}
                    onChange={(event) => setScheduleForm({ ...scheduleForm, start_time: event.target.value })}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Fin</label>
                  <input
                    type="time"
                    value={scheduleForm.end_time}
                    onChange={(event) => setScheduleForm({ ...scheduleForm, end_time: event.target.value })}
                  />
                </div>
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Días de la semana</label>
                <input
                  value={scheduleForm.days_of_week}
                  onChange={(event) => setScheduleForm({ ...scheduleForm, days_of_week: event.target.value })}
                />
              </div>
              <button className="w-full rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" type="submit" disabled={!selectedCampaignId}>
                Guardar horario
              </button>
            </form>
          )}
        </SectionCard>
      </div>
    </div>
  );
}
