import { X } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Videowall, VideowallNode } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";
import { SimpleVideowallMatrix } from "./SimpleVideowallMatrix";

function normalizePositiveInt(value: number, fallback: number) {
  if (!Number.isFinite(value) || value <= 0) {
    return fallback;
  }

  return Math.round(value);
}

export function VideowallEditDrawer({
  open,
  token,
  videowall,
  nodes,
  onClose,
  onSaved,
}: {
  open: boolean;
  token: string | null;
  videowall: Videowall | null;
  nodes: VideowallNode[];
  onClose: () => void;
  onSaved: (videowall: Videowall) => Promise<void> | void;
}) {
  const [name, setName] = useState("");
  const [rows, setRows] = useState(1);
  const [columns, setColumns] = useState(1);
  const [totalWidth, setTotalWidth] = useState(1920);
  const [totalHeight, setTotalHeight] = useState(1080);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (!open || !videowall) {
      return;
    }

    setName(videowall.name);
    setRows(videowall.rows);
    setColumns(videowall.columns);
    setTotalWidth(videowall.total_width);
    setTotalHeight(videowall.total_height);
    setError(null);
  }, [open, videowall]);

  const previewOccupiedPositions = useMemo(() => {
    const safeTotal = Math.max(1, rows) * Math.max(1, columns);
    return nodes.map((node) => node.position_index).filter((position) => position <= safeTotal);
  }, [columns, nodes, rows]);

  if (!open || !videowall) {
    return null;
  }

  const activeVideowall = videowall;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) {
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      const response = await apiRequest<Videowall>(`/videowalls/${activeVideowall.id}`, {
        method: "PATCH",
        token,
        body: {
          name: name.trim(),
          rows: normalizePositiveInt(rows, activeVideowall.rows),
          columns: normalizePositiveInt(columns, activeVideowall.columns),
          total_width: normalizePositiveInt(totalWidth, activeVideowall.total_width),
          total_height: normalizePositiveInt(totalHeight, activeVideowall.total_height),
        },
      });

      await onSaved(response);
      onClose();
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, "No se pudo actualizar el videowall."));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[70] flex justify-end bg-slate-950/45 backdrop-blur-sm">
      <button aria-label="Cerrar" className="flex-1 cursor-default" type="button" onClick={onClose} />
      <aside className="h-screen w-[min(95vw,1100px)] max-w-none overflow-x-hidden overflow-y-auto border-l border-white/10 bg-white px-6 py-6 shadow-2xl lg:px-8">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-accent">Editar videowall</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{activeVideowall.name}</h3>
            <p className="mt-2 text-sm text-slate-600">Ajusta nombre, matriz y resolución total sin salir del dashboard.</p>
          </div>
          <button
            aria-label="Cerrar drawer"
            className="rounded-2xl border border-slate-200 p-3 text-slate-500 transition hover:border-slate-300 hover:text-ink"
            type="button"
            onClick={onClose}
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {error ? <div className="mt-5 rounded-[20px] bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

        <form className="mt-6 space-y-5" onSubmit={handleSubmit}>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div className="md:col-span-2 xl:col-span-3">
              <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
              <input value={name} onChange={(event) => setName(event.target.value)} required />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Filas</label>
              <input type="number" min={1} value={rows} onChange={(event) => setRows(Number(event.target.value) || 1)} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Columnas</label>
              <input type="number" min={1} value={columns} onChange={(event) => setColumns(Number(event.target.value) || 1)} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Resolución total ancho</label>
              <input type="number" min={1} value={totalWidth} onChange={(event) => setTotalWidth(Number(event.target.value) || 1)} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Resolución total alto</label>
              <input type="number" min={1} value={totalHeight} onChange={(event) => setTotalHeight(Number(event.target.value) || 1)} />
            </div>
          </div>

          <section className="rounded-[24px] border border-slate-200 bg-slate-50 p-5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Preview de matriz</p>
                <p className="mt-2 text-sm text-slate-600">
                  {Math.max(1, columns)}x{Math.max(1, rows)} · {Math.max(1, columns) * Math.max(1, rows)} monitores
                </p>
              </div>
              <div className="flex flex-wrap gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                <span className="rounded-full bg-emerald-50 px-3 py-1.5 text-emerald-700">{nodes.length} configurados</span>
                <span className="rounded-full bg-slate-200 px-3 py-1.5 text-slate-700">
                  {Math.max(0, Math.max(1, columns) * Math.max(1, rows) - nodes.length)} pendientes
                </span>
              </div>
            </div>
            <div className="mt-4">
              <SimpleVideowallMatrix
                columns={Math.max(1, columns)}
                rows={Math.max(1, rows)}
                occupiedPositions={previewOccupiedPositions}
                showLabels
              />
            </div>
            <p className="mt-4 text-xs text-slate-500">
              Si reduces filas o columnas y algún nodo queda fuera de rango, el backend bloqueará el cambio con un mensaje claro.
            </p>
          </section>

          <div className="flex flex-wrap gap-3">
            <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" disabled={isSubmitting} type="submit">
              {isSubmitting ? "Guardando..." : "Guardar videowall"}
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
