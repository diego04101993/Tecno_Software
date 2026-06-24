export type CampaignPublishSummary = {
  total: number;
  published: number;
  existing: number;
  errors: Array<{ targetLabel: string; message: string }>;
};

export function CampaignPublishResultSummary({
  summary,
}: {
  summary: CampaignPublishSummary | null;
}) {
  if (!summary) {
    return (
      <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
        El resumen aparecerá aquí después de publicar la campaña en las pantallas seleccionadas.
      </div>
    );
  }

  return (
    <div className="space-y-3 rounded-[24px] border border-slate-200 bg-white px-4 py-4">
      <div>
        <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Resumen de publicación</p>
        <p className="mt-1 text-sm text-slate-600">{summary.total} intento(s) procesado(s).</p>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-[18px] border border-emerald-200 bg-emerald-50 px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-emerald-700">Publicados</p>
          <p className="mt-2 text-xl font-semibold text-emerald-900">{summary.published}</p>
        </div>
        <div className="rounded-[18px] border border-amber-200 bg-amber-50 px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-amber-700">Ya estaban</p>
          <p className="mt-2 text-xl font-semibold text-amber-900">{summary.existing}</p>
        </div>
        <div className="rounded-[18px] border border-rose-200 bg-rose-50 px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-rose-700">Errores</p>
          <p className="mt-2 text-xl font-semibold text-rose-900">{summary.errors.length}</p>
        </div>
      </div>

      {summary.errors.length > 0 ? (
        <div className="rounded-[18px] border border-rose-200 bg-rose-50 px-4 py-4 text-sm text-rose-800">
          <p className="font-semibold">Pantallas con error</p>
          <div className="mt-2 space-y-2">
            {summary.errors.slice(0, 6).map((error) => (
              <div key={`${error.targetLabel}:${error.message}`}>
                <p className="font-semibold">{error.targetLabel}</p>
                <p className="mt-0.5 text-xs text-rose-700">{error.message}</p>
              </div>
            ))}
            {summary.errors.length > 6 ? <p className="text-xs text-rose-700">Y {summary.errors.length - 6} error(es) mas.</p> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
