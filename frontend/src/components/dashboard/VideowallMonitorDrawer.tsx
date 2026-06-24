import { Monitor, X } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Branch, Channel, Orientation, Videowall, VideowallNode } from "../../types/domain";
import { getVideowallCellMetrics } from "../VideowallGridPreview";
import { getApiErrorMessage } from "./apiError";
import { SimpleVideowallMatrix } from "./SimpleVideowallMatrix";

export type VideowallMonitorDrawerTarget =
  | {
      mode: "create";
      preferredPosition?: number | null;
    }
  | {
      mode: "edit";
      node: VideowallNode;
      channel: Channel;
    };

function getFirstAvailablePosition(total: number, occupiedPositions: number[], currentPosition?: number | null) {
  const occupiedSet = new Set(occupiedPositions.filter((position) => position !== currentPosition));
  for (let position = 1; position <= total; position += 1) {
    if (!occupiedSet.has(position)) {
      return position;
    }
  }
  return currentPosition ?? 1;
}

export function VideowallMonitorDrawer({
  open,
  token,
  branch,
  videowall,
  nodes,
  target,
  onClose,
  onSaved,
}: {
  open: boolean;
  token: string | null;
  branch: Branch | null;
  videowall: Videowall | null;
  nodes: VideowallNode[];
  target: VideowallMonitorDrawerTarget | null;
  onClose: () => void;
  onSaved: (message: string) => Promise<void> | void;
}) {
  const [name, setName] = useState("");
  const [orientation, setOrientation] = useState<Orientation>("horizontal");
  const [hardwareIdentifier, setHardwareIdentifier] = useState("");
  const [notes, setNotes] = useState("");
  const [positionIndex, setPositionIndex] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const totalPositions = Math.max(1, (videowall?.rows ?? 1) * (videowall?.columns ?? 1));
  const occupiedPositions = useMemo(() => nodes.map((node) => node.position_index).sort((left, right) => left - right), [nodes]);
  const currentNode = target?.mode === "edit" ? target.node : null;
  const currentChannel = target?.mode === "edit" ? target.channel : null;
  const firstAvailablePosition = useMemo(
    () => getFirstAvailablePosition(totalPositions, occupiedPositions, currentNode?.position_index),
    [currentNode?.position_index, occupiedPositions, totalPositions],
  );
  const availablePositions = useMemo(
    () =>
      Array.from({ length: totalPositions }, (_, index) => index + 1).filter(
        (position) => position === currentNode?.position_index || !occupiedPositions.includes(position),
      ),
    [currentNode?.position_index, occupiedPositions, totalPositions],
  );
  const safePosition = availablePositions.includes(positionIndex)
    ? positionIndex
    : target?.mode === "edit"
      ? currentNode?.position_index ?? firstAvailablePosition
      : target?.preferredPosition && availablePositions.includes(target.preferredPosition)
        ? target.preferredPosition
        : firstAvailablePosition;
  const selectedCell = videowall
    ? getVideowallCellMetrics(videowall.columns, videowall.rows, videowall.total_width, videowall.total_height, safePosition)
    : null;

  useEffect(() => {
    if (!open || !videowall || !target) {
      return;
    }

    if (target.mode === "edit") {
      setName(target.channel.name);
      setOrientation(target.channel.orientation);
      setHardwareIdentifier(target.channel.hardware_identifier ?? "");
      setNotes(target.channel.notes ?? "");
      setPositionIndex(target.node.position_index);
    } else {
      const initialPosition =
        target.preferredPosition && availablePositions.includes(target.preferredPosition)
          ? target.preferredPosition
          : firstAvailablePosition;
      setName(`Monitor ${initialPosition}`);
      setOrientation("horizontal");
      setHardwareIdentifier("");
      setNotes("");
      setPositionIndex(initialPosition);
    }

    setError(null);
  }, [availablePositions, firstAvailablePosition, open, target, videowall]);

  if (!open || !branch || !videowall || !target || !selectedCell) {
    return null;
  }

  const activeBranch = branch;
  const activeVideowall = videowall;
  const activeTarget = target;
  const activeCell = selectedCell;
  const isEditing = activeTarget.mode === "edit";
  const matrixOccupiedPositions = isEditing
    ? occupiedPositions.filter((position) => position !== currentNode?.position_index)
    : occupiedPositions;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) {
      return;
    }

    if (!availablePositions.includes(safePosition)) {
      setError("La posición seleccionada ya no está disponible.");
      return;
    }

    setIsSubmitting(true);
    setError(null);

    let createdChannelId: string | null = null;

    try {
      if (isEditing && currentChannel && currentNode) {
        await apiRequest<Channel>(`/channels/${currentChannel.id}`, {
          method: "PATCH",
          token,
          body: {
            name: name.trim(),
            hardware_identifier: hardwareIdentifier.trim() || null,
            notes: notes.trim() || null,
          },
        });

        if (safePosition !== currentNode.position_index) {
          await apiRequest<VideowallNode>(`/videowalls/${activeVideowall.id}/nodes/${currentNode.id}`, {
            method: "PATCH",
            token,
            body: {
              position_index: safePosition,
              row_index: activeCell.rowIndex,
              column_index: activeCell.columnIndex,
            },
          });
        }

        await onSaved(`Monitor ${safePosition} actualizado correctamente.`);
      } else {
        const channel = await apiRequest<Channel>("/channels", {
          method: "POST",
          token,
          body: {
            client_id: activeVideowall.client_id,
            branch_id: activeBranch.id,
            name: name.trim(),
            resolution_width: activeCell.width,
            resolution_height: activeCell.height,
            orientation,
            mode: "videowall",
            screen_count: 1,
            hardware_identifier: hardwareIdentifier.trim() || null,
            notes: notes.trim() || null,
            expanded_outputs: [],
          },
        });
        createdChannelId = channel.id;

        await apiRequest<VideowallNode>(`/videowalls/${activeVideowall.id}/nodes`, {
          method: "POST",
          token,
          body: {
            channel_id: channel.id,
            position_index: safePosition,
            row_index: activeCell.rowIndex,
            column_index: activeCell.columnIndex,
          },
        });

        await onSaved(`Monitor ${safePosition} agregado correctamente al videowall.`);
      }

      onClose();
    } catch (nextError) {
      if (createdChannelId) {
        try {
          await apiRequest<void>(`/channels/${createdChannelId}`, {
            method: "DELETE",
            token,
          });
        } catch {
          // no-op best effort rollback
        }
      }
      setError(getApiErrorMessage(nextError, isEditing ? "No se pudo actualizar el monitor." : "No se pudo agregar el monitor."));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[80] flex justify-end bg-slate-950/45 backdrop-blur-sm">
      <button aria-label="Cerrar" className="flex-1 cursor-default" type="button" onClick={onClose} />
      <aside className="h-screen w-[min(95vw,1100px)] max-w-none overflow-x-hidden overflow-y-auto border-l border-white/10 bg-white px-6 py-6 shadow-2xl lg:px-8">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-accent">{isEditing ? "Editar monitor" : "Agregar monitor"}</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{activeVideowall.name}</h3>
            <p className="mt-2 text-sm text-slate-600">
              {isEditing
                ? "Actualiza los datos del monitor o muévelo a otra celda libre del videowall."
                : "Crea un nodo nuevo dentro del videowall con posición prellenada desde la matriz."}
            </p>
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
              <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre monitor</label>
              <input value={name} onChange={(event) => setName(event.target.value)} required />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Hardware ID opcional</label>
              <input value={hardwareIdentifier} onChange={(event) => setHardwareIdentifier(event.target.value)} placeholder="Mini PC lobby" />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Orientación</label>
              <select value={orientation} onChange={(event) => setOrientation(event.target.value as Orientation)} disabled={isEditing}>
                <option value="horizontal">Horizontal</option>
                <option value="vertical">Vertical</option>
              </select>
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Resolución ancho</label>
              <input type="number" value={activeCell.width} readOnly />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Resolución alto</label>
              <input type="number" value={activeCell.height} readOnly />
            </div>
          </div>

          <section className="rounded-[24px] border border-slate-200 bg-slate-50 p-5">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Posición del monitor</p>
                <p className="mt-2 text-sm font-semibold text-ink">
                  Fila {activeCell.rowIndex + 1} · Columna {activeCell.columnIndex + 1}
                </p>
                <p className="mt-1 text-xs text-slate-500">Monitor {safePosition}</p>
              </div>
              <div className="rounded-[18px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700">
                x:{activeCell.x} · y:{activeCell.y} · {activeCell.width}x{activeCell.height}
              </div>
            </div>

            <div className="mt-4 grid gap-5 xl:grid-cols-[minmax(260px,320px)_minmax(0,1fr)]">
              <div>
                <SimpleVideowallMatrix
                  columns={activeVideowall.columns}
                  rows={activeVideowall.rows}
                  occupiedPositions={matrixOccupiedPositions}
                  selectedPosition={safePosition}
                  showLabels
                  onSelectPosition={(position) => {
                    if (matrixOccupiedPositions.includes(position)) {
                      return;
                    }
                    setPositionIndex(position);
                  }}
                />
                <div className="mt-4 flex flex-wrap gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                  <span className="rounded-full bg-emerald-50 px-3 py-1.5 text-emerald-700">Configurado</span>
                  <span className="rounded-full bg-slate-200 px-3 py-1.5 text-slate-700">Vacío</span>
                  <span className="rounded-full bg-cyan-100 px-3 py-1.5 text-cyan-800">Seleccionado</span>
                </div>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Matriz</p>
                  <p className="mt-2 font-semibold text-ink">
                    {activeVideowall.columns}x{activeVideowall.rows}
                  </p>
                </div>
                <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Resolución total</p>
                  <p className="mt-2 font-semibold text-ink">
                    {activeVideowall.total_width}x{activeVideowall.total_height}
                  </p>
                </div>
                <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Posiciones libres</p>
                  <p className="mt-2 font-semibold text-ink">{availablePositions.length}</p>
                </div>
                <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                  <div className="flex items-center gap-2 text-slate-700">
                    <Monitor className="h-4 w-4" />
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Canal</p>
                  </div>
                  <p className="mt-2 text-sm font-semibold text-ink">{currentChannel?.channel_code ?? "Se generará al guardar"}</p>
                </div>
              </div>
            </div>
          </section>

          <div className="flex flex-wrap gap-3">
            <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" disabled={isSubmitting} type="submit">
              {isSubmitting ? "Guardando..." : isEditing ? "Guardar monitor" : "Agregar monitor"}
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
