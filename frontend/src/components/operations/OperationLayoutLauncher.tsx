import { LayoutTemplate } from "lucide-react";

export function OperationLayoutLauncher() {
  return (
    <div className="rounded-[18px] border border-dashed border-slate-300 bg-slate-50 px-4 py-4 text-sm text-slate-600">
      <div className="flex items-start gap-3">
        <span className="rounded-xl bg-white p-2.5 text-slate-600 shadow-sm">
          <LayoutTemplate className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <p className="font-semibold text-ink">Crear layout</p>
          <p className="mt-1">
            El editor visual tipo Canva llegara en V3-E. Desde aqui podras crear menus, tablas, precios y layouts dinamicos para
            insertarlos como item de timeline.
          </p>
          <button
            className="mt-3 rounded-full border border-slate-200 bg-white px-4 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 opacity-70"
            disabled
            type="button"
          >
            Crear layout (V3-E)
          </button>
        </div>
      </div>
    </div>
  );
}
