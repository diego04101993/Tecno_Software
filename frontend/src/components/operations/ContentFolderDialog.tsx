import { FormEvent, useEffect, useState } from "react";

type ContentFolderDialogProps = {
  open: boolean;
  mode: "create_root" | "create_child" | "rename";
  parentLabel?: string | null;
  initialName?: string;
  isSubmitting: boolean;
  onClose: () => void;
  onSubmit: (name: string) => Promise<void> | void;
};

const dialogCopy = {
  create_root: {
    title: "Crear carpeta",
    description: "Crea una carpeta raíz para organizar la biblioteca del studio.",
    action: "Crear carpeta",
  },
  create_child: {
    title: "Crear subcarpeta",
    description: "La nueva carpeta quedará dentro de la carpeta seleccionada.",
    action: "Crear subcarpeta",
  },
  rename: {
    title: "Renombrar carpeta",
    description: "Actualiza el nombre visible de esta carpeta en la biblioteca.",
    action: "Guardar nombre",
  },
} as const;

export function ContentFolderDialog({
  open,
  mode,
  parentLabel = null,
  initialName = "",
  isSubmitting,
  onClose,
  onSubmit,
}: ContentFolderDialogProps) {
  const [name, setName] = useState(initialName);

  useEffect(() => {
    if (!open) {
      return;
    }
    setName(initialName);
  }, [initialName, open]);

  if (!open) {
    return null;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextName = name.trim();
    if (!nextName || isSubmitting) {
      return;
    }
    await onSubmit(nextName);
  }

  const copy = dialogCopy[mode];

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/50 px-4">
      <form className="w-full max-w-md rounded-[24px] border border-slate-200 bg-white p-6 shadow-2xl" onSubmit={handleSubmit}>
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Biblioteca</p>
          <h3 className="mt-2 text-xl font-semibold text-ink">{copy.title}</h3>
          <p className="mt-2 text-sm leading-6 text-slate-600">{copy.description}</p>
          {parentLabel ? <p className="mt-3 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Padre: {parentLabel}</p> : null}
        </div>

        <div className="mt-5 space-y-2">
          <label className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500" htmlFor="content-folder-name">
            Nombre
          </label>
          <input
            id="content-folder-name"
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Ej. Promos de verano"
          />
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <button
            className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-600 transition hover:border-slate-300 hover:text-ink"
            disabled={isSubmitting}
            type="button"
            onClick={onClose}
          >
            Cancelar
          </button>
          <button
            className="rounded-full bg-ink px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
            disabled={isSubmitting || !name.trim()}
            type="submit"
          >
            {isSubmitting ? "Guardando..." : copy.action}
          </button>
        </div>
      </form>
    </div>
  );
}
