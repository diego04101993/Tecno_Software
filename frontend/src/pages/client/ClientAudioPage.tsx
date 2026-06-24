import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { AudioPlaylistEditor } from "../../components/audio/AudioPlaylistEditor";
import { AudioReportTable } from "../../components/audio/AudioReportTable";
import { AudioUploadPanel } from "../../components/audio/AudioUploadPanel";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { canWriteClientScope } from "../../lib/rbac";
import type {
  AudioLibraryItem,
  AudioPlaylist,
  AudioPlaylistItem,
  AudioReportSummary,
} from "../../types/domain";

export function ClientAudioPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const [library, setLibrary] = useState<AudioLibraryItem[]>([]);
  const [musicPlaylists, setMusicPlaylists] = useState<AudioPlaylist[]>([]);
  const [spotPlaylists, setSpotPlaylists] = useState<AudioPlaylist[]>([]);
  const [selectedPlaylistId, setSelectedPlaylistId] = useState("");
  const [playlistItems, setPlaylistItems] = useState<AudioPlaylistItem[]>([]);
  const [report, setReport] = useState<AudioReportSummary>({ music: [], spots: [], recent_events: [] });
  const [error, setError] = useState<string | null>(null);

  const allPlaylists = useMemo(() => [...musicPlaylists, ...spotPlaylists], [musicPlaylists, spotPlaylists]);
  const canManage = canWriteClientScope(user?.role);
  const selectedPlaylist = useMemo(
    () => allPlaylists.find((item) => item.id === selectedPlaylistId) ?? null,
    [allPlaylists, selectedPlaylistId],
  );

  async function loadData() {
    if (!token || !clientId) {
      return;
    }

    try {
      const [libraryResponse, musicResponse, spotResponse, reportResponse] = await Promise.all([
        apiRequest<AudioLibraryItem[]>(`/audio/library?client_id=${clientId}`, { token }),
        apiRequest<AudioPlaylist[]>(`/audio/playlists?client_id=${clientId}&kind=music`, { token }),
        apiRequest<AudioPlaylist[]>(`/audio/playlists?client_id=${clientId}&kind=spot`, { token }),
        apiRequest<AudioReportSummary>(`/audio/reports/summary?client_id=${clientId}`, { token }),
      ]);

      setLibrary(libraryResponse);
      setMusicPlaylists(musicResponse);
      setSpotPlaylists(spotResponse);
      setReport(reportResponse);
      setSelectedPlaylistId((current) => {
        const nextPlaylists = [...musicResponse, ...spotResponse];
        if (current && nextPlaylists.some((item) => item.id === current)) {
          return current;
        }
        return nextPlaylists[0]?.id ?? "";
      });
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el modulo de audio");
    }
  }

  async function loadPlaylistItems(playlistId: string) {
    if (!token || !playlistId) {
      setPlaylistItems([]);
      return;
    }

    try {
      const response = await apiRequest<AudioPlaylistItem[]>(`/audio/playlists/${playlistId}/items`, { token });
      setPlaylistItems(response);
      setError(null);
    } catch (nextError) {
      setPlaylistItems([]);
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar la playlist seleccionada");
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
  }, [clientId, token]);

  useEffect(() => {
    if (selectedPlaylistId) {
      void loadPlaylistItems(selectedPlaylistId);
    } else {
      setPlaylistItems([]);
    }
  }, [selectedPlaylistId, token]);

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Audios" value={String(library.length)} hint="MP3 en biblioteca" tone="teal" />
        <StatCard label="Playlists de musica" value={String(musicPlaylists.length)} hint="Ambientacion continua" />
        <StatCard label="Playlists de spots" value={String(spotPlaylists.length)} hint="Radio y promos" tone="orange" />
        <StatCard label="Eventos reportados" value={String(report.recent_events.length)} hint="Aún vacío hasta conectar player" />
      </section>

      <div className="grid gap-5 2xl:grid-cols-[0.86fr_1.14fr]">
        <AudioUploadPanel
          disabled={!canManage}
          onSubmit={async (payload) => {
            if (!token || !clientId) {
              return;
            }

            await runPageAction(async () => {
              const data = new FormData();
              data.append("file", payload.file);
              data.append("name", payload.name);
              data.append("client_id", clientId);
              data.append("duration_seconds", String(payload.duration_seconds));
              data.append("audio_kind", payload.audio_kind);
              data.append("normalization_status", payload.normalization_status);
              data.append("target_lufs", String(payload.target_lufs));
              await apiRequest("/contents/upload", { method: "POST", token, formData: data });
              await loadData();
            }, "No se pudo subir el audio.");
          }}
        />

        <AudioPlaylistEditor
          library={library}
          playlists={allPlaylists}
          selectedPlaylistId={selectedPlaylistId}
          onSelectPlaylist={setSelectedPlaylistId}
          playlistItems={playlistItems}
          canManage={canManage}
          onCreatePlaylist={async (payload) => {
            if (!token || !clientId) {
              return;
            }

            await runPageAction(async () => {
              await apiRequest<AudioPlaylist>("/audio/playlists", {
                method: "POST",
                token,
                body: {
                  client_id: clientId,
                  ...payload,
                },
              });
              await loadData();
            }, "No se pudo crear la playlist.");
          }}
          onAddItem={async (contentId) => {
            if (!token || !selectedPlaylist) {
              return;
            }

            await runPageAction(async () => {
              await apiRequest(`/audio/playlists/${selectedPlaylist.id}/items`, {
                method: "POST",
                token,
                body: {
                  content_id: contentId,
                  sort_order: playlistItems.length + 1,
                  is_enabled: true,
                },
              });
              await loadPlaylistItems(selectedPlaylist.id);
            }, "No se pudo agregar el audio a la playlist.");
          }}
          onDeleteItem={async (itemId) => {
            if (!token || !selectedPlaylist) {
              return;
            }

            await runPageAction(async () => {
              await apiRequest(`/audio/playlists/${selectedPlaylist.id}/items/${itemId}`, {
                method: "DELETE",
                token,
              });
              await loadPlaylistItems(selectedPlaylist.id);
            }, "No se pudo quitar el audio de la playlist.");
          }}
        />
      </div>

      <div className="grid gap-5 2xl:grid-cols-[0.95fr_1.05fr]">
        <SectionCard title="Biblioteca por tipo" subtitle="Separa musica ambiental y spots desde el mismo repositorio de contenidos.">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-3">
              <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Música</p>
              {library.filter((item) => item.audio_kind === "music").map((item) => (
                <article key={item.id} className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
                  <p className="font-semibold text-ink">{item.name}</p>
                  <p className="mt-1 text-sm text-slate-500">{item.duration_seconds}s</p>
                </article>
              ))}
            </div>
            <div className="space-y-3">
              <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Spots</p>
              {library.filter((item) => item.audio_kind === "spot").map((item) => (
                <article key={item.id} className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
                  <p className="font-semibold text-ink">{item.name}</p>
                  <p className="mt-1 text-sm text-slate-500">{item.duration_seconds}s</p>
                </article>
              ))}
            </div>
          </div>
        </SectionCard>

        <AudioReportTable
          title="Reporte base"
          subtitle="Quedara poblado cuando el media player comience a enviar reproducciones de audio."
          music={report.music}
          spots={report.spots}
          recentEvents={report.recent_events}
        />
      </div>
    </div>
  );
}
