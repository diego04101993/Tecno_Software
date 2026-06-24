import { FormEvent, useRef, useState } from "react";
import { UploadCloud } from "lucide-react";

type QuickContentUploadPanelProps = {
  disabled?: boolean;
  onUpload: (payload: { file: File; name: string; durationSeconds: number }) => Promise<void>;
};

export function QuickContentUploadPanel({ disabled = false, onUpload }: QuickContentUploadPanelProps) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [name, setName] = useState("");
  const [durationSeconds, setDurationSeconds] = useState(15);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file || disabled || submitting) {
      return;
    }

    setSubmitting(true);
    try {
      await onUpload({
        file,
        name: name.trim() || file.name.replace(/\.[^.]+$/, ""),
        durationSeconds,
      });
      setName("");
      setDurationSeconds(15);
      if (fileRef.current) {
        fileRef.current.value = "";
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3" onSubmit={handleSubmit}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="rounded-xl bg-accentSoft p-2 text-accent">
            <UploadCloud className="h-4 w-4" />
          </span>
          <div>
            <p className="text-sm font-semibold text-ink">Subida rapida</p>
            <p className="text-xs text-slate-500">Carga una pieza y dejala lista en la biblioteca.</p>
          </div>
        </div>
        <span className="rounded-full bg-white px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500 shadow-sm">
          Media
        </span>
      </div>

      <div className="mt-3 grid gap-2">
        <input ref={fileRef} type="file" accept="image/*,video/*" disabled={disabled || submitting} required />
        <div className="grid gap-2 xl:grid-cols-[minmax(0,1fr)_88px_auto]">
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            disabled={disabled || submitting}
            placeholder="Nombre visible"
          />
          <input
            type="number"
            min={1}
            value={durationSeconds}
            onChange={(event) => setDurationSeconds(Number(event.target.value) || 1)}
            disabled={disabled || submitting}
            placeholder="Seg"
          />
          <button
            className="rounded-[14px] bg-ink px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
            disabled={disabled || submitting}
            type="submit"
          >
            {submitting ? "Subiendo..." : "Subir"}
          </button>
        </div>
      </div>
    </form>
  );
}
