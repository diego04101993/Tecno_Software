import { AlertTriangle } from "lucide-react";

type DeleteConfirmDialogProps = {
  open: boolean;
  title: string;
  description: string;
  errorMessage?: string | null;
  confirmLabel?: string;
  cancelLabel?: string;
  isDeleting?: boolean;
  busyLabel?: string;
  confirmDisabled?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
};

export function DeleteConfirmDialog({
  open,
  title,
  description,
  errorMessage = null,
  confirmLabel = "Eliminar",
  cancelLabel = "Cancelar",
  isDeleting = false,
  busyLabel = "Eliminando...",
  confirmDisabled = false,
  onCancel,
  onConfirm,
}: DeleteConfirmDialogProps) {
  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-slate-950/55 px-4 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-[28px] border border-rose-200 bg-white p-6 shadow-2xl">
        <div className="flex items-start gap-4">
          <span className="rounded-2xl bg-rose-50 p-3 text-rose-600">
            <AlertTriangle className="h-5 w-5" />
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="text-lg font-semibold text-ink">{title}</h3>
            <p className="mt-2 text-sm leading-6 text-slate-600">{description}</p>
            {errorMessage ? <div className="mt-4 rounded-2xl bg-amber-50 px-4 py-3 text-sm text-amber-900">{errorMessage}</div> : null}
          </div>
        </div>

        <div className="mt-6 flex flex-wrap justify-end gap-3">
          <button
            className="rounded-full border border-slate-200 px-4 py-2.5 text-sm font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink"
            type="button"
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            className="rounded-full bg-rose-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-rose-700 disabled:cursor-not-allowed disabled:opacity-60"
            disabled={isDeleting || confirmDisabled}
            type="button"
            onClick={onConfirm}
          >
            {isDeleting ? busyLabel : confirmDisabled ? "Preparando..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
