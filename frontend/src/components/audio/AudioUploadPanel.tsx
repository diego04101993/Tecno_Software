import { FormEvent, useRef, useState } from "react";

import { SectionCard } from "../SectionCard";
import type { AudioNormalizationStatus, AudioPlaylistKind } from "../../types/domain";

export function AudioUploadPanel({
  onSubmit,
  disabled,
}: {
  onSubmit: (payload: {
    file: File;
    name: string;
    audio_kind: AudioPlaylistKind;
    duration_seconds: number;
    normalization_status: AudioNormalizationStatus;
    target_lufs: number;
  }) => Promise<void>;
  disabled: boolean;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [form, setForm] = useState({
    name: "",
    audio_kind: "music" as AudioPlaylistKind,
    duration_seconds: 180,
    normalization_status: "pending" as AudioNormalizationStatus,
    target_lufs: -14,
  });
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file || disabled) {
      return;
    }

    setSubmitting(true);
    try {
      await onSubmit({
        file,
        ...form,
      });
      setForm({
        name: "",
        audio_kind: "music",
        duration_seconds: 180,
        normalization_status: "pending",
        target_lufs: -14,
      });
      if (fileRef.current) {
        fileRef.current.value = "";
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SectionCard title="Subir MP3" subtitle="Carga musica ambiental o spots de radio dentro de la biblioteca del cliente.">
      {disabled ? (
        <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
          Este rol puede consultar el audio, pero no cargar archivos.
        </div>
      ) : (
        <form className="grid gap-4" onSubmit={handleSubmit}>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Archivo MP3</label>
            <input ref={fileRef} type="file" accept="audio/mpeg,audio/mp3,audio/*" required />
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
            <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required />
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Clasificación</label>
              <select value={form.audio_kind} onChange={(event) => setForm({ ...form, audio_kind: event.target.value as AudioPlaylistKind })}>
                <option value="music">Música</option>
                <option value="spot">Spot</option>
              </select>
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Duracion estimada</label>
              <input
                type="number"
                min={1}
                value={form.duration_seconds}
                onChange={(event) => setForm({ ...form, duration_seconds: Number(event.target.value) })}
              />
            </div>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Estado de volumen</label>
              <select
                value={form.normalization_status}
                onChange={(event) => setForm({ ...form, normalization_status: event.target.value as AudioNormalizationStatus })}
              >
                <option value="pending">Pendiente</option>
                <option value="normalized">Normalizado</option>
                <option value="skipped">Sin normalizar</option>
              </select>
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">LUFS objetivo</label>
              <input
                type="number"
                value={form.target_lufs}
                onChange={(event) => setForm({ ...form, target_lufs: Number(event.target.value) })}
              />
            </div>
          </div>
          <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" type="submit" disabled={submitting}>
            {submitting ? "Cargando..." : "Subir audio"}
          </button>
        </form>
      )}
    </SectionCard>
  );
}
