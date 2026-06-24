import { PencilLine, PlusCircle, X } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Branch } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";

type BranchDrawerMode = "create" | "edit";

const defaultBranchForm = {
  name: "",
  code: "",
  address: "",
  timezone: "America/Mexico_City",
};

export function BranchFormDrawer({
  open,
  mode,
  token,
  clientId,
  clientName,
  branch,
  onClose,
  onSaved,
}: {
  open: boolean;
  mode: BranchDrawerMode;
  token: string | null;
  clientId: string;
  clientName: string;
  branch: Branch | null;
  onClose: () => void;
  onSaved: (branch: Branch, mode: BranchDrawerMode) => Promise<void> | void;
}) {
  const [form, setForm] = useState(defaultBranchForm);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (!open) {
      return;
    }

    if (mode === "edit" && branch) {
      setForm({
        name: branch.name,
        code: branch.code,
        address: branch.address ?? "",
        timezone: branch.timezone,
      });
    } else {
      setForm(defaultBranchForm);
    }
    setError(null);
  }, [branch, mode, open]);

  if (!open) {
    return null;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) {
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      const payload = {
        ...(mode === "create" ? { client_id: clientId } : {}),
        name: form.name.trim(),
        code: form.code.trim(),
        address: form.address.trim() || null,
        timezone: form.timezone.trim() || "America/Mexico_City",
      };
      const response = await apiRequest<Branch>(mode === "create" ? "/branches" : `/branches/${branch?.id}`, {
        method: mode === "create" ? "POST" : "PATCH",
        token,
        body: payload,
      });

      await onSaved(response, mode);
      onClose();
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, mode === "create" ? "No se pudo crear la sucursal." : "No se pudo actualizar la sucursal."));
    } finally {
      setIsSubmitting(false);
    }
  }

  const Icon = mode === "create" ? PlusCircle : PencilLine;
  const eyebrow = mode === "create" ? "Crear sucursal" : "Editar sucursal";
  const title = mode === "create" ? clientName : branch?.name ?? "Sucursal";
  const submitLabel = mode === "create" ? "Registrar sucursal" : "Guardar cambios";
  const helperText =
    mode === "create"
      ? "La sucursal aparecerá inmediatamente en el dashboard y quedará lista para agregar pantallas."
      : "Actualiza nombre, código, dirección o zona horaria sin salir del centro de infraestructura.";

  return (
    <div className="fixed inset-0 z-30 flex justify-end bg-slate-950/45 backdrop-blur-sm">
      <button aria-label="Cerrar" className="flex-1 cursor-default" type="button" onClick={onClose} />
      <aside className="h-full w-full max-w-[460px] overflow-y-auto border-l border-white/10 bg-white p-6 shadow-2xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-accent">{eyebrow}</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{title}</h3>
            <p className="mt-2 text-sm text-slate-600">{helperText}</p>
          </div>
          <button
            aria-label="Cerrar panel"
            className="rounded-2xl border border-slate-200 p-3 text-slate-500 transition hover:border-slate-300 hover:text-ink"
            type="button"
            onClick={onClose}
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {error ? <div className="mt-5 rounded-[20px] bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-4">
            <span className="inline-flex rounded-2xl bg-white p-3 text-accent shadow-sm">
              <Icon className="h-5 w-5" />
            </span>
            <p className="mt-3 text-sm text-slate-600">{helperText}</p>
          </div>

          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
            <input value={form.name} onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} required />
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Código</label>
            <input value={form.code} onChange={(event) => setForm((current) => ({ ...current, code: event.target.value }))} required />
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Direccion</label>
            <input value={form.address} onChange={(event) => setForm((current) => ({ ...current, address: event.target.value }))} />
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Zona horaria</label>
            <input value={form.timezone} onChange={(event) => setForm((current) => ({ ...current, timezone: event.target.value }))} />
          </div>

          <div className="flex flex-wrap gap-3">
            <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" disabled={isSubmitting} type="submit">
              {isSubmitting ? "Guardando..." : submitLabel}
            </button>
            <button
              className="rounded-[20px] border border-slate-200 px-5 py-4 font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink"
              type="button"
              onClick={onClose}
            >
              Cancelar
            </button>
          </div>
        </form>
      </aside>
    </div>
  );
}
