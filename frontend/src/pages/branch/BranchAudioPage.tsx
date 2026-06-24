import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { AudioReportTable } from "../../components/audio/AudioReportTable";
import { AudioRulesPanel, createAssignmentDraft } from "../../components/audio/AudioRulesPanel";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { canWriteBranchScope } from "../../lib/rbac";
import type {
  AudioAssignment,
  AudioPlaylist,
  AudioReportSummary,
  AudioRuntimeConfig,
  Channel,
} from "../../types/domain";

export function BranchAudioPage() {
  const { token, user } = useAuth();
  const { clientId, branchId } = useParams();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [musicPlaylists, setMusicPlaylists] = useState<AudioPlaylist[]>([]);
  const [spotPlaylists, setSpotPlaylists] = useState<AudioPlaylist[]>([]);
  const [assignments, setAssignments] = useState<AudioAssignment[]>([]);
  const [report, setReport] = useState<AudioReportSummary>({ music: [], spots: [], recent_events: [] });
  const [selectedChannelId, setSelectedChannelId] = useState("");
  const [runtimeConfig, setRuntimeConfig] = useState<AudioRuntimeConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  const canManage = canWriteBranchScope(user?.role);
  const branchAssignment = useMemo(
    () => assignments.find((item) => item.branch_id === branchId && !item.channel_id) ?? null,
    [assignments, branchId],
  );
  const audioChannels = useMemo(() => channels.filter((item) => item.mode === "audio"), [channels]);
  const selectedChannelAssignment = useMemo(
    () => assignments.find((item) => item.channel_id === selectedChannelId) ?? null,
    [assignments, selectedChannelId],
  );
  const [branchDraft, setBranchDraft] = useState(createAssignmentDraft(null));
  const [channelDraft, setChannelDraft] = useState(createAssignmentDraft(null));

  async function loadData() {
    if (!token || !clientId || !branchId) {
      return;
    }

    try {
      const [channelsResponse, musicResponse, spotResponse, assignmentsResponse, reportResponse] = await Promise.all([
        apiRequest<Channel[]>(`/channels?branch_id=${branchId}`, { token }),
        apiRequest<AudioPlaylist[]>(`/audio/playlists?client_id=${clientId}&kind=music`, { token }),
        apiRequest<AudioPlaylist[]>(`/audio/playlists?client_id=${clientId}&kind=spot`, { token }),
        apiRequest<AudioAssignment[]>(`/audio/assignments?client_id=${clientId}&branch_id=${branchId}`, { token }),
        apiRequest<AudioReportSummary>(`/audio/reports/summary?client_id=${clientId}&branch_id=${branchId}`, { token }),
      ]);

      setChannels(channelsResponse);
      setMusicPlaylists(musicResponse);
      setSpotPlaylists(spotResponse);
      setAssignments(assignmentsResponse);
      setReport(reportResponse);
      setSelectedChannelId((current) => {
        const nextAudioChannels = channelsResponse.filter((item) => item.mode === "audio");
        if (current && nextAudioChannels.some((item) => item.id === current)) {
          return current;
        }
        return nextAudioChannels[0]?.id ?? "";
      });
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar la configuracion de audio");
    }
  }

  async function loadRuntimeConfig(channelId: string) {
    if (!token || !channelId) {
      setRuntimeConfig(null);
      return;
    }

    try {
      const response = await apiRequest<AudioRuntimeConfig>(`/audio/runtime-config?channel_id=${channelId}`, { token });
      setRuntimeConfig(response);
      setError(null);
    } catch (nextError) {
      setRuntimeConfig(null);
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el runtime config");
    }
  }

  async function runPageAction(action: () => Promise<void>, fallbackMessage: string) {
    try {
      await action();
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : fallbackMessage);
    }
  }

  useEffect(() => {
    void loadData();
  }, [branchId, clientId, token]);

  useEffect(() => {
    setBranchDraft(createAssignmentDraft(branchAssignment));
  }, [branchAssignment]);

  useEffect(() => {
    setChannelDraft(createAssignmentDraft(selectedChannelAssignment));
  }, [selectedChannelAssignment]);

  useEffect(() => {
    if (selectedChannelId) {
      void loadRuntimeConfig(selectedChannelId);
    } else {
      setRuntimeConfig(null);
    }
  }, [selectedChannelId, token]);

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Canales tipo audio" value={String(audioChannels.length)} hint="Overrides disponibles" tone="teal" />
        <StatCard label="Playlist música" value={String(musicPlaylists.length)} hint="Catálogo activo" />
        <StatCard label="Playlist spots" value={String(spotPlaylists.length)} hint="Radio y promociones" tone="orange" />
        <StatCard label="Eventos reportados" value={String(report.recent_events.length)} hint="Listo para el player futuro" />
      </section>

      <div className="grid gap-5 xl:grid-cols-[1fr_1fr]">
        <AudioRulesPanel
          title="Audio por sucursal"
          subtitle="Define la playlist general de musica ambiental y spots para esta sucursal."
          value={branchDraft}
          musicPlaylists={musicPlaylists}
          spotPlaylists={spotPlaylists}
          disabled={!canManage}
          onChange={setBranchDraft}
          onSubmit={async () => {
            if (!token || !clientId || !branchId) {
              return;
            }

            await runPageAction(async () => {
              await apiRequest("/audio/assignments", {
                method: "POST",
                token,
                body: {
                  client_id: clientId,
                  branch_id: branchId,
                  ...branchDraft,
                },
              });
              await loadData();
            }, "No se pudo guardar la configuracion de audio de la sucursal.");
          }}
        />

        <SectionCard title="Override por canal tipo audio" subtitle="Solo los canales en modo audio pueden recibir una asignación dedicada.">
          {audioChannels.length > 0 ? (
            <div className="space-y-5">
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Canal de audio</label>
                <select value={selectedChannelId} onChange={(event) => setSelectedChannelId(event.target.value)}>
                  <option value="">Selecciona un canal</option>
                  {audioChannels.map((channel) => (
                    <option key={channel.id} value={channel.id}>
                      {channel.name}
                    </option>
                  ))}
                </select>
              </div>
              {selectedChannelId ? (
                <AudioRulesPanel
                  title="Reglas del canal"
                  subtitle="Si guardas aquí, esta configuración tendrá prioridad sobre la de sucursal."
                  value={channelDraft}
                  musicPlaylists={musicPlaylists}
                  spotPlaylists={spotPlaylists}
                  disabled={!canManage}
                  onChange={setChannelDraft}
                  onSubmit={async () => {
                    if (!token || !clientId || !selectedChannelId) {
                      return;
                    }

                    await runPageAction(async () => {
                      await apiRequest("/audio/assignments", {
                        method: "POST",
                        token,
                        body: {
                          client_id: clientId,
                          channel_id: selectedChannelId,
                          ...channelDraft,
                        },
                      });
                      await loadData();
                      await loadRuntimeConfig(selectedChannelId);
                    }, "No se pudo guardar el override de audio del canal.");
                  }}
                />
              ) : null}
            </div>
          ) : (
            <div className="rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
              Esta sucursal todavia no tiene canales en modo audio.
            </div>
          )}
        </SectionCard>
      </div>

      <div className="grid gap-5 xl:grid-cols-[0.92fr_1.08fr]">
        <SectionCard title="Runtime config futuro" subtitle="Lectura preparada para que el media player descargue playlists y reglas de audio.">
          {runtimeConfig ? (
            <div className="space-y-4">
              <div className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Scope resuelto</p>
                <p className="mt-2 font-semibold text-ink">{runtimeConfig.assignment_scope}</p>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4">
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Música</p>
                  <p className="mt-2 font-semibold text-ink">{runtimeConfig.music_playlist?.name ?? "Sin playlist"}</p>
                </div>
                <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4">
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Spots</p>
                  <p className="mt-2 font-semibold text-ink">{runtimeConfig.spot_playlist?.name ?? "Sin playlist"}</p>
                </div>
              </div>
              <div className="rounded-[22px] border border-slate-200 bg-white px-4 py-4 text-sm text-slate-700">
                {runtimeConfig.rules.songs_between_spots} canciones - {runtimeConfig.rules.spots_per_break} spot(s) - {runtimeConfig.rules.spot_rotation_mode}
              </div>
              <pre className="overflow-auto rounded-[22px] border border-slate-200 bg-slate-950 p-4 text-xs text-slate-100">
                {JSON.stringify(runtimeConfig, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
              Selecciona un canal de audio para revisar su runtime config.
            </div>
          )}
        </SectionCard>

        <AudioReportTable
          title="Reporte base de sucursal"
          subtitle="Quedara poblado automaticamente cuando el media player reporte reproducciones."
          music={report.music}
          spots={report.spots}
          recentEvents={report.recent_events}
        />
      </div>
    </div>
  );
}
