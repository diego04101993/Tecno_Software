import { Trash2 } from "lucide-react";
import { FormEvent, useMemo, useState } from "react";

import { SectionCard } from "../SectionCard";
import { formatAudioNormalizationStatus, formatAudioPlaylistKind } from "../../lib/labels";
import { resolveAudioAssetPath } from "../../lib/audio";
import type { AudioLibraryItem, AudioPlaylist, AudioPlaylistItem, AudioPlaylistKind } from "../../types/domain";

export function AudioPlaylistEditor({
  library,
  playlists,
  selectedPlaylistId,
  onSelectPlaylist,
  playlistItems,
  canManage,
  onCreatePlaylist,
  onAddItem,
  onDeleteItem,
}: {
  library: AudioLibraryItem[];
  playlists: AudioPlaylist[];
  selectedPlaylistId: string;
  onSelectPlaylist: (playlistId: string) => void;
  playlistItems: AudioPlaylistItem[];
  canManage: boolean;
  onCreatePlaylist: (payload: { name: string; kind: AudioPlaylistKind; description: string }) => Promise<void>;
  onAddItem: (contentId: string) => Promise<void>;
  onDeleteItem: (itemId: string) => Promise<void>;
}) {
  const [form, setForm] = useState({
    name: "",
    kind: "music" as AudioPlaylistKind,
    description: "",
  });
  const [selectedContentId, setSelectedContentId] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const selectedPlaylist = useMemo(
    () => playlists.find((item) => item.id === selectedPlaylistId) ?? null,
    [playlists, selectedPlaylistId],
  );

  const filteredLibrary = useMemo(() => {
    if (!selectedPlaylist) {
      return [];
    }
    return library.filter((item) => item.audio_kind === selectedPlaylist.kind);
  }, [library, selectedPlaylist]);

  async function handleCreatePlaylist(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canManage) {
      return;
    }

    setSubmitting(true);
    try {
      await onCreatePlaylist(form);
      setForm({
        name: "",
        kind: "music",
        description: "",
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SectionCard title="Playlists de audio" subtitle="Organiza musica y spots en listas separadas para su reproduccion futura.">
      <div className="grid gap-5 xl:grid-cols-[0.92fr_1.08fr]">
        <div className="space-y-4">
          <form className="grid gap-4 rounded-[24px] border border-slate-200 bg-slate-50 p-4" onSubmit={handleCreatePlaylist}>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Nueva playlist</label>
              <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required />
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Tipo</label>
                <select value={form.kind} onChange={(event) => setForm({ ...form, kind: event.target.value as AudioPlaylistKind })}>
                  <option value="music">Música</option>
                  <option value="spot">Spot</option>
                </select>
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Descripción</label>
                <input value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} />
              </div>
            </div>
            <button className="rounded-[18px] bg-ink px-4 py-3 text-sm font-semibold text-white" type="submit" disabled={!canManage || submitting}>
              {submitting ? "Guardando..." : "Crear playlist"}
            </button>
          </form>

          <div className="space-y-3">
            {playlists.map((playlist) => (
              <button
                key={playlist.id}
                className={[
                  "w-full rounded-[22px] border px-4 py-4 text-left transition",
                  selectedPlaylistId === playlist.id
                    ? "border-cyan-300 bg-cyan-50 shadow-[0_0_0_1px_rgba(6,182,212,0.18)]"
                    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50",
                ].join(" ")}
                type="button"
                onClick={() => onSelectPlaylist(playlist.id)}
              >
                <p className="font-semibold text-ink">{playlist.name}</p>
                <p className="mt-1 text-sm text-slate-500">{formatAudioPlaylistKind(playlist.kind)}</p>
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-4">
          {selectedPlaylist ? (
            <>
              <div className="rounded-[24px] border border-slate-200 bg-white p-4">
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Playlist activa</p>
                <p className="mt-2 text-xl font-semibold text-ink">{selectedPlaylist.name}</p>
                <p className="mt-1 text-sm text-slate-500">
                  {formatAudioPlaylistKind(selectedPlaylist.kind)} - {selectedPlaylist.description ?? "Sin descripcion"}
                </p>
              </div>

              <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-4">
                <div className="grid gap-3 md:grid-cols-[1fr_auto]">
                  <select value={selectedContentId} onChange={(event) => setSelectedContentId(event.target.value)}>
                    <option value="">Selecciona un audio para agregar</option>
                    {filteredLibrary.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                  <button
                    className="rounded-[18px] bg-ink px-4 py-3 text-sm font-semibold text-white"
                    type="button"
                    disabled={!canManage || !selectedContentId}
                    onClick={async () => {
                      await onAddItem(selectedContentId);
                      setSelectedContentId("");
                    }}
                  >
                    Agregar
                  </button>
                </div>
              </div>

              <div className="space-y-3">
                {playlistItems.map((item) => {
                  const assetPath = resolveAudioAssetPath(item.content?.file_path ?? null);
                  return (
                    <article key={item.id} className="rounded-[24px] border border-slate-200 bg-white p-4">
                      <div className="flex items-start justify-between gap-4">
                        <div className="min-w-0">
                          <p className="truncate font-semibold text-ink">{item.content?.name ?? "Audio sin archivo"}</p>
                          <p className="mt-1 text-sm text-slate-500">
                            Orden {item.sort_order} - {item.content?.duration_seconds ?? 0}s - {formatAudioNormalizationStatus(item.content?.normalization_status)}
                          </p>
                        </div>
                        {canManage ? (
                          <button
                            className="rounded-full border border-rose-200 p-2 text-rose-600 transition hover:bg-rose-50"
                            type="button"
                            onClick={() => onDeleteItem(item.id)}
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        ) : null}
                      </div>
                      {assetPath ? <audio className="mt-4 w-full" controls src={assetPath} preload="none" /> : null}
                    </article>
                  );
                })}
                {playlistItems.length === 0 ? (
                  <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
                    Esta playlist todavia no tiene audios cargados.
                  </div>
                ) : null}
              </div>
            </>
          ) : (
            <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
              Selecciona una playlist para editar su contenido.
            </div>
          )}
        </div>
      </div>
    </SectionCard>
  );
}
