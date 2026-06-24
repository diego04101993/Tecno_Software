export function ExpandedOutputsPreview({ screenCount }: { screenCount: number }) {
  const outputs = Array.from({ length: screenCount }, (_, index) => index + 1);

  return (
    <div className="rounded-[28px] border border-slate-200 bg-slate-50/90 p-4">
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Preview extendido</p>
          <p className="mt-2 text-sm text-slate-700">Una sola PC extiende el escritorio en varias salidas HDMI.</p>
        </div>
        <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-semibold text-slate-600">
          {screenCount} salida(s)
        </span>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-3">
        {outputs.map((output, index) => (
          <div
            key={output}
            className="relative overflow-hidden rounded-[22px] border border-slate-300 bg-gradient-to-br from-slate-900 via-slate-800 to-slate-700 p-4 text-white shadow-sm"
          >
            <div className="absolute inset-y-0 right-0 w-10 bg-white/5" />
            <p className="text-[11px] uppercase tracking-[0.22em] text-slate-300">Salida {output}</p>
            <p className="mt-3 text-lg font-semibold">HDMI {output}</p>
            <p className="mt-2 text-xs text-slate-300">
              {index === 0 ? "Monitor principal" : "Escritorio extendido"}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
