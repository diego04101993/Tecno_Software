type OperationPlaybackModeBarProps = {
  mode: "sequential" | "random";
  canEdit: boolean;
  onChange: (mode: "sequential" | "random") => void;
};

function optionClassName(active: boolean, disabled: boolean) {
  return [
    "rounded-full border px-4 py-2 text-xs font-semibold uppercase tracking-[0.14em] transition",
    active ? "border-slate-900 bg-slate-900 text-white" : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-ink",
    disabled ? "cursor-not-allowed opacity-60" : "",
  ].join(" ");
}

export function OperationPlaybackModeBar({ mode, canEdit, onChange }: OperationPlaybackModeBarProps) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
        Modo
      </span>
      <button className={optionClassName(mode === "sequential", !canEdit)} disabled={!canEdit} type="button" onClick={() => onChange("sequential")}>
        Secuencia
      </button>
      <button className={optionClassName(mode === "random", !canEdit)} disabled={!canEdit} type="button" onClick={() => onChange("random")}>
        Aleatorio
      </button>
      <span className="rounded-full bg-emerald-50 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-emerald-700">
        Loop infinito
      </span>
    </div>
  );
}
