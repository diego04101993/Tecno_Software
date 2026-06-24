import { SectionCard } from "../SectionCard";
import type {
  AudioAssignment,
  AudioNormalizationStatus,
  AudioPlaylist,
  AudioSpotRotationMode,
} from "../../types/domain";

type AssignmentDraft = {
  music_playlist_id: string;
  spot_playlist_id: string;
  songs_between_spots: number;
  spots_per_break: number;
  spot_rotation_mode: AudioSpotRotationMode;
  avoid_consecutive_spots: boolean;
  volume_normalization_enabled: boolean;
  volume_normalization_status: AudioNormalizationStatus;
  target_lufs: number;
};

export function createAssignmentDraft(assignment: AudioAssignment | null): AssignmentDraft {
  return {
    music_playlist_id: assignment?.music_playlist_id ?? "",
    spot_playlist_id: assignment?.spot_playlist_id ?? "",
    songs_between_spots: assignment?.songs_between_spots ?? 3,
    spots_per_break: assignment?.spots_per_break ?? 1,
    spot_rotation_mode: assignment?.spot_rotation_mode ?? "sequential",
    avoid_consecutive_spots: assignment?.avoid_consecutive_spots ?? true,
    volume_normalization_enabled: assignment?.volume_normalization_enabled ?? false,
    volume_normalization_status: assignment?.volume_normalization_status ?? "pending",
    target_lufs: assignment?.target_lufs ?? -14,
  };
}

export function AudioRulesPanel({
  title,
  subtitle,
  value,
  musicPlaylists,
  spotPlaylists,
  disabled,
  onChange,
  onSubmit,
}: {
  title: string;
  subtitle: string;
  value: AssignmentDraft;
  musicPlaylists: AudioPlaylist[];
  spotPlaylists: AudioPlaylist[];
  disabled: boolean;
  onChange: (next: AssignmentDraft) => void;
  onSubmit: () => Promise<void>;
}) {
  return (
    <SectionCard title={title} subtitle={subtitle}>
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Playlist de musica</label>
          <select value={value.music_playlist_id} onChange={(event) => onChange({ ...value, music_playlist_id: event.target.value })}>
            <option value="">Sin playlist</option>
            {musicPlaylists.map((playlist) => (
              <option key={playlist.id} value={playlist.id}>
                {playlist.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Playlist de spots</label>
          <select value={value.spot_playlist_id} onChange={(event) => onChange({ ...value, spot_playlist_id: event.target.value })}>
            <option value="">Sin playlist</option>
            {spotPlaylists.map((playlist) => (
              <option key={playlist.id} value={playlist.id}>
                {playlist.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Cada X canciones</label>
          <input
            type="number"
            min={1}
            value={value.songs_between_spots}
            onChange={(event) => onChange({ ...value, songs_between_spots: Number(event.target.value) })}
          />
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Reproducir Y spots</label>
          <input
            type="number"
            min={1}
            value={value.spots_per_break}
            onChange={(event) => onChange({ ...value, spots_per_break: Number(event.target.value) })}
          />
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Rotacion de spots</label>
          <select
            value={value.spot_rotation_mode}
            onChange={(event) => onChange({ ...value, spot_rotation_mode: event.target.value as AudioSpotRotationMode })}
          >
            <option value="sequential">En orden</option>
            <option value="random">Aleatorio</option>
          </select>
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Estado de volumen</label>
          <select
            value={value.volume_normalization_status}
            onChange={(event) => onChange({ ...value, volume_normalization_status: event.target.value as AudioNormalizationStatus })}
          >
            <option value="pending">Pendiente</option>
            <option value="normalized">Normalizado</option>
            <option value="skipped">Sin normalizar</option>
          </select>
        </div>
        <label className="flex items-center gap-3 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={value.avoid_consecutive_spots}
            onChange={(event) => onChange({ ...value, avoid_consecutive_spots: event.target.checked })}
          />
          Evitar el mismo spot seguido
        </label>
        <label className="flex items-center gap-3 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={value.volume_normalization_enabled}
            onChange={(event) => onChange({ ...value, volume_normalization_enabled: event.target.checked })}
          />
          Preparar normalizacion de volumen
        </label>
        <div className="md:col-span-2">
          <label className="mb-2 block text-sm font-semibold text-slate-700">LUFS objetivo</label>
          <input
            type="number"
            value={value.target_lufs}
            onChange={(event) => onChange({ ...value, target_lufs: Number(event.target.value) })}
          />
        </div>
      </div>
      <button
        className="mt-5 rounded-[20px] bg-ink px-5 py-4 font-semibold text-white"
        type="button"
        disabled={disabled}
        onClick={() => void onSubmit()}
      >
        Guardar asignación de audio
      </button>
    </SectionCard>
  );
}
