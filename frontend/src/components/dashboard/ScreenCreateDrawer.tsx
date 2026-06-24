import { Columns2, Monitor, TvMinimalPlay, X } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Branch, Channel, Orientation, Videowall, VideowallNode } from "../../types/domain";
import { getVideowallCellMetrics } from "../VideowallGridPreview";
import { LedOutputMappingPanel, buildOutputMappingDraft, type ChannelOutputMappingDraft } from "./LedOutputMappingPanel";
import { SimpleVideowallMatrix } from "./SimpleVideowallMatrix";

type ScreenMode = "normal" | "expanded" | "videowall";
type VideowallFlow = "create" | "existing";
type ResolutionBasis = "monitor" | "total";

type SubmitNotice =
  | {
      tone: "success" | "warning";
      text: string;
    }
  | null;

const defaultVideowallDraft = {
  name: "",
  columns: 2,
  rows: 2,
  resolution_basis: "monitor" as ResolutionBasis,
  monitor_width: 1920,
  monitor_height: 1080,
  total_width: 3840,
  total_height: 2160,
  position_index: 1,
};

function buildExpandedOutputs(screenCount: number) {
  return Array.from({ length: screenCount }, (_, index) => ({
    output_index: index + 1,
    label: `HDMI ${index + 1}`,
    role: index === 0 ? "primary" : "extended",
  }));
}

function normalizePositiveInt(value: number, fallback: number) {
  if (!Number.isFinite(value) || value <= 0) {
    return fallback;
  }

  return Math.round(value);
}

function getFirstFreePosition(totalMonitors: number, occupiedPositions: number[]) {
  const occupiedSet = new Set(occupiedPositions);

  for (let position = 1; position <= totalMonitors; position += 1) {
    if (!occupiedSet.has(position)) {
      return position;
    }
  }

  return 1;
}

export function ScreenCreateDrawer({
  open,
  token,
  clientId,
  branch,
  onClose,
  onCreated,
}: {
  open: boolean;
  token: string | null;
  clientId: string;
  branch: Branch | null;
  onClose: () => void;
  onCreated: () => Promise<void> | void;
}) {
  const [mode, setMode] = useState<ScreenMode>("normal");
  const [name, setName] = useState("");
  const [orientation, setOrientation] = useState<Orientation>("horizontal");
  const [resolutionWidth, setResolutionWidth] = useState(1920);
  const [resolutionHeight, setResolutionHeight] = useState(1080);
  const [screenCount, setScreenCount] = useState(2);
  const [hardwareIdentifier, setHardwareIdentifier] = useState("");
  const [notes, setNotes] = useState("");
  const [outputMapping, setOutputMapping] = useState<ChannelOutputMappingDraft>(buildOutputMappingDraft(null, 1920, 1080));
  const [videowallFlow, setVideowallFlow] = useState<VideowallFlow>("create");
  const [videowallDraft, setVideowallDraft] = useState(defaultVideowallDraft);
  const [videowalls, setVideowalls] = useState<Videowall[]>([]);
  const [selectedVideowallId, setSelectedVideowallId] = useState("");
  const [existingVideowallNodes, setExistingVideowallNodes] = useState<VideowallNode[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<SubmitNotice>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (!open) {
      return;
    }

    setMode("normal");
    setName("");
    setOrientation("horizontal");
    setResolutionWidth(1920);
    setResolutionHeight(1080);
    setScreenCount(2);
    setHardwareIdentifier("");
    setNotes("");
    setOutputMapping(buildOutputMappingDraft(null, 1920, 1080));
    setVideowallFlow("create");
    setVideowallDraft(defaultVideowallDraft);
    setSelectedVideowallId("");
    setExistingVideowallNodes([]);
    setCatalogError(null);
    setError(null);
    setNotice(null);
  }, [branch?.id, open]);

  useEffect(() => {
    if (!open || !token || !clientId || !branch) {
      return;
    }

    apiRequest<Videowall[]>(`/videowalls?client_id=${clientId}&branch_id=${branch?.id ?? ""}`, { token })
      .then((response) => {
        setVideowalls(response);
        setSelectedVideowallId((current) => current || response[0]?.id || "");
        setCatalogError(null);
      })
      .catch((nextError) => {
        setVideowalls([]);
        setSelectedVideowallId("");
        setCatalogError(nextError instanceof Error ? nextError.message : "No se pudo cargar el catálogo de videowalls de esta sucursal.");
      });
  }, [branch?.id, clientId, open, token]);

  useEffect(() => {
    if (!open || !token || mode !== "videowall" || videowallFlow !== "existing" || !selectedVideowallId) {
      setExistingVideowallNodes([]);
      return;
    }

    apiRequest<VideowallNode[]>(`/videowalls/${selectedVideowallId}/nodes`, { token })
      .then((response) => {
        setExistingVideowallNodes(response);
        setCatalogError(null);
      })
      .catch((nextError) => {
        setExistingVideowallNodes([]);
        setCatalogError(nextError instanceof Error ? nextError.message : "No se pudieron cargar las posiciones ocupadas.");
      });
  }, [mode, open, selectedVideowallId, token, videowallFlow]);

  const selectedExistingVideowall = useMemo(
    () => videowalls.find((item) => item.id === selectedVideowallId) ?? null,
    [selectedVideowallId, videowalls],
  );
  const occupiedPositions = useMemo(
    () => existingVideowallNodes.map((node) => node.position_index).sort((left, right) => left - right),
    [existingVideowallNodes],
  );

  const videowallDerived = useMemo(() => {
    const usingExistingVideowall = mode === "videowall" && videowallFlow === "existing" && selectedExistingVideowall;
    const columns = usingExistingVideowall
      ? selectedExistingVideowall.columns
      : normalizePositiveInt(videowallDraft.columns, 2);
    const rows = usingExistingVideowall ? selectedExistingVideowall.rows : normalizePositiveInt(videowallDraft.rows, 2);
    const totalMonitors = columns * rows;
    const totalWidth = usingExistingVideowall
      ? selectedExistingVideowall.total_width
      : videowallDraft.resolution_basis === "monitor"
        ? normalizePositiveInt(videowallDraft.monitor_width, 1920) * columns
        : Math.max(columns, normalizePositiveInt(videowallDraft.total_width, 3840));
    const totalHeight = usingExistingVideowall
      ? selectedExistingVideowall.total_height
      : videowallDraft.resolution_basis === "monitor"
        ? normalizePositiveInt(videowallDraft.monitor_height, 1080) * rows
        : Math.max(rows, normalizePositiveInt(videowallDraft.total_height, 2160));
    const takenPositions = usingExistingVideowall ? occupiedPositions : [];
    const availablePositions = Array.from({ length: totalMonitors }, (_, index) => index + 1).filter(
      (position) => !takenPositions.includes(position),
    );
    const safePosition = availablePositions.includes(videowallDraft.position_index)
      ? videowallDraft.position_index
      : availablePositions[0] ?? 1;
    const selectedCell = getVideowallCellMetrics(columns, rows, totalWidth, totalHeight, safePosition);

    return {
      columns,
      rows,
      totalMonitors,
      totalWidth,
      totalHeight,
      configuredNodes: takenPositions.length,
      pendingNodes: Math.max(0, totalMonitors - takenPositions.length),
      selectedPosition: safePosition,
      selectedCell,
      availablePositions,
      occupiedPositions: takenPositions,
    };
  }, [mode, occupiedPositions, selectedExistingVideowall, videowallDraft, videowallFlow]);

  useEffect(() => {
    if (mode !== "videowall") {
      return;
    }

    if (videowallDraft.position_index !== videowallDerived.selectedPosition) {
      setVideowallDraft((current) => ({
        ...current,
        position_index: videowallDerived.selectedPosition,
      }));
    }
  }, [mode, videowallDerived.selectedPosition, videowallDraft.position_index]);

  if (!open || !branch) {
    return null;
  }

  const targetBranch = branch;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) {
      return;
    }

    if (mode === "videowall" && videowallFlow === "existing") {
      if (!selectedExistingVideowall) {
        setError("Selecciona un videowall existente antes de guardar la pantalla.");
        return;
      }

      if (videowallDerived.availablePositions.length === 0) {
        setError("El videowall seleccionado ya no tiene posiciones libres.");
        return;
      }
    }

    setIsSubmitting(true);
    setError(null);
    setNotice(null);

    try {
      const channelPayload = {
        client_id: clientId,
        branch_id: targetBranch.id,
        name,
        resolution_width: mode === "videowall" ? videowallDerived.selectedCell.width : resolutionWidth,
        resolution_height: mode === "videowall" ? videowallDerived.selectedCell.height : resolutionHeight,
        orientation,
        mode,
        screen_count: mode === "expanded" ? screenCount : 1,
        hardware_identifier: hardwareIdentifier || null,
        notes: notes || null,
        expanded_outputs: mode === "expanded" ? buildExpandedOutputs(screenCount) : [],
        output_mapping_json:
          mode === "videowall"
            ? {}
            : buildOutputMappingDraft(outputMapping, resolutionWidth, resolutionHeight),
      };

      const channel = await apiRequest<Channel>("/channels", {
        method: "POST",
        token,
        body: channelPayload,
      });

      let nextNotice: SubmitNotice = { tone: "success", text: "Pantalla creada correctamente." };

      if (mode === "videowall") {
        let targetVideowall = selectedExistingVideowall;

        if (videowallFlow === "create") {
          targetVideowall = await apiRequest<Videowall>("/videowalls", {
            method: "POST",
            token,
            body: {
              client_id: clientId,
              name: videowallDraft.name.trim() || `${name} Videowall`,
              columns: videowallDerived.columns,
              rows: videowallDerived.rows,
              total_width: videowallDerived.totalWidth,
              total_height: videowallDerived.totalHeight,
              sync_mode: "play_at_timestamp",
            },
          });
        }

        try {
          await apiRequest(`/videowalls/${targetVideowall?.id}/nodes`, {
            method: "POST",
            token,
            body: {
              channel_id: channel.id,
              position_index: videowallDerived.selectedPosition,
              row_index: videowallDerived.selectedCell.rowIndex,
              column_index: videowallDerived.selectedCell.columnIndex,
              x: videowallDerived.selectedCell.x,
              y: videowallDerived.selectedCell.y,
              width: videowallDerived.selectedCell.width,
              height: videowallDerived.selectedCell.height,
            },
          });
          nextNotice =
            videowallFlow === "create"
              ? {
                  tone: "success",
                  text: `Pantalla y grupo videowall creados. Nodo asignado en fila ${videowallDerived.selectedCell.rowIndex + 1}, columna ${videowallDerived.selectedCell.columnIndex + 1}.`,
                }
              : {
                  tone: "success",
                  text: `Pantalla creada y asignada al videowall existente en fila ${videowallDerived.selectedCell.rowIndex + 1}, columna ${videowallDerived.selectedCell.columnIndex + 1}.`,
                };
        } catch (nodeError) {
          nextNotice = {
            tone: "warning",
            text:
              nodeError instanceof Error
                ? `La pantalla se creó, pero la posición del videowall quedó pendiente: ${nodeError.message}`
                : "La pantalla se creó, pero la posición del videowall quedó pendiente.",
          };
        }
      } else if (mode === "expanded") {
        nextNotice = {
          tone: "success",
          text: `Grupo expandido creado con ${screenCount} salidas sobre la misma PC.`,
        };
      }

      setNotice(nextNotice);
      await onCreated();
      if (nextNotice.tone === "success") {
        onClose();
      }
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear la pantalla.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-950/45 backdrop-blur-sm">
      <button aria-label="Cerrar" className="flex-1 cursor-default" type="button" onClick={onClose} />
      <aside className="h-screen w-[min(95vw,1100px)] max-w-none overflow-x-hidden overflow-y-auto border-l border-white/10 bg-white px-6 py-6 shadow-2xl lg:px-8">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-accent">Agregar pantalla</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{targetBranch.name}</h3>
            <p className="mt-2 text-sm text-slate-600">
              Crea una pantalla normal, un grupo expandido o un nodo dentro de un videowall sin salir del dashboard.
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
        {notice ? (
          <div
            className={[
              "mt-5 rounded-[20px] px-4 py-3 text-sm",
              notice.tone === "success" ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-900",
            ].join(" ")}
          >
            {notice.text}
          </div>
        ) : null}

        <form className="mt-6 space-y-5" onSubmit={handleSubmit}>
          <div className="grid gap-3 md:grid-cols-3">
            {[
              {
                id: "normal" as const,
                title: "Normal",
                description: "Una pantalla por canal.",
                icon: Monitor,
              },
              {
                id: "expanded" as const,
                title: "Expandido",
                description: "2 o 3 salidas desde la misma PC.",
                icon: Columns2,
              },
              {
                id: "videowall" as const,
                title: "Videowall",
                description: "Nodo dentro de una matriz sincronizada.",
                icon: TvMinimalPlay,
              },
            ].map((option) => {
              const Icon = option.icon;
              const active = mode === option.id;

              return (
                <button
                  key={option.id}
                  className={[
                    "rounded-[24px] border p-4 text-left transition",
                    active ? "border-cyan-300 bg-cyan-50" : "border-slate-200 bg-white hover:border-slate-300",
                  ].join(" ")}
                  type="button"
                  onClick={() => {
                    setMode(option.id);
                    setError(null);
                    setNotice(null);
                  }}
                >
                  <span className={["inline-flex rounded-2xl p-3", active ? "bg-cyan-500 text-white" : "bg-slate-900 text-white"].join(" ")}>
                    <Icon className="h-4 w-4" />
                  </span>
                  <p className="mt-3 font-semibold text-ink">{option.title}</p>
                  <p className="mt-1 text-sm text-slate-600">{option.description}</p>
                </button>
              );
            })}
          </div>

          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div className="md:col-span-2 xl:col-span-3">
              <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
              <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Ej. Pantalla caja 1" required />
            </div>

            {mode === "expanded" ? (
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Salidas</label>
                <select value={screenCount} onChange={(event) => setScreenCount(Number(event.target.value))}>
                  <option value={2}>2 salidas</option>
                  <option value={3}>3 salidas</option>
                </select>
              </div>
            ) : null}

            {mode !== "videowall" ? (
              <>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">
                    {mode === "expanded" ? "Resolución por salida" : "Resolución ancho"}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={resolutionWidth}
                    onChange={(event) => setResolutionWidth(normalizePositiveInt(Number(event.target.value), resolutionWidth))}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">
                    {mode === "expanded" ? "Resolución alto por salida" : "Resolución alto"}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={resolutionHeight}
                    onChange={(event) => setResolutionHeight(normalizePositiveInt(Number(event.target.value), resolutionHeight))}
                  />
                </div>
              </>
            ) : null}

            {mode !== "videowall" ? (
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Orientación</label>
                <select value={orientation} onChange={(event) => setOrientation(event.target.value as Orientation)}>
                  <option value="horizontal">Horizontal</option>
                  <option value="vertical">Vertical</option>
                </select>
              </div>
            ) : null}

            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Hardware opcional</label>
              <input
                value={hardwareIdentifier}
                onChange={(event) => setHardwareIdentifier(event.target.value)}
                placeholder="Mini PC lobby"
              />
            </div>
          </div>

          {mode === "videowall" ? (
            <section className="rounded-[28px] border border-slate-200 bg-slate-50 p-5">
              <div className="flex flex-wrap gap-3">
                <button
                  className={`rounded-full px-4 py-2 text-sm font-semibold ${videowallFlow === "create" ? "bg-ink text-white" : "border border-slate-200 bg-white text-slate-700"}`}
                  type="button"
                  onClick={() => setVideowallFlow("create")}
                >
                  Nuevo grupo
                </button>
                <button
                  className={`rounded-full px-4 py-2 text-sm font-semibold ${videowallFlow === "existing" ? "bg-ink text-white" : "border border-slate-200 bg-white text-slate-700"}`}
                  type="button"
                  onClick={() => {
                    setVideowallFlow("existing");
                    setVideowallDraft((current) => ({
                      ...current,
                      position_index: getFirstFreePosition(videowallDerived.totalMonitors, occupiedPositions),
                    }));
                  }}
                >
                  Videowall existente
                </button>
              </div>

              {videowallFlow === "create" ? (
                <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  <div className="md:col-span-2 xl:col-span-3">
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre del grupo</label>
                    <input
                      value={videowallDraft.name}
                      onChange={(event) => setVideowallDraft({ ...videowallDraft, name: event.target.value })}
                      placeholder="Videowall lobby"
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Columnas</label>
                    <input
                      type="number"
                      min={1}
                      value={videowallDraft.columns}
                      onChange={(event) =>
                        setVideowallDraft((current) => ({
                          ...current,
                          columns: normalizePositiveInt(Number(event.target.value), current.columns),
                        }))
                      }
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Filas</label>
                    <input
                      type="number"
                      min={1}
                      value={videowallDraft.rows}
                      onChange={(event) =>
                        setVideowallDraft((current) => ({
                          ...current,
                          rows: normalizePositiveInt(Number(event.target.value), current.rows),
                        }))
                      }
                    />
                  </div>
                  <div className="md:col-span-2 xl:col-span-3">
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Base de resolución</label>
                    <select
                      value={videowallDraft.resolution_basis}
                      onChange={(event) =>
                        setVideowallDraft((current) => ({
                          ...current,
                          resolution_basis: event.target.value as ResolutionBasis,
                        }))
                      }
                    >
                      <option value="monitor">Por monitor</option>
                      <option value="total">Lienzo total</option>
                    </select>
                  </div>
                  {videowallDraft.resolution_basis === "monitor" ? (
                    <>
                      <div>
                        <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho por monitor</label>
                        <input
                          type="number"
                          min={1}
                          value={videowallDraft.monitor_width}
                          onChange={(event) =>
                            setVideowallDraft((current) => ({
                              ...current,
                              monitor_width: normalizePositiveInt(Number(event.target.value), current.monitor_width),
                            }))
                          }
                        />
                      </div>
                      <div>
                        <label className="mb-2 block text-sm font-semibold text-slate-700">Alto por monitor</label>
                        <input
                          type="number"
                          min={1}
                          value={videowallDraft.monitor_height}
                          onChange={(event) =>
                            setVideowallDraft((current) => ({
                              ...current,
                              monitor_height: normalizePositiveInt(Number(event.target.value), current.monitor_height),
                            }))
                          }
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      <div>
                        <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho total</label>
                        <input
                          type="number"
                          min={1}
                          value={videowallDraft.total_width}
                          onChange={(event) =>
                            setVideowallDraft((current) => ({
                              ...current,
                              total_width: normalizePositiveInt(Number(event.target.value), current.total_width),
                            }))
                          }
                        />
                      </div>
                      <div>
                        <label className="mb-2 block text-sm font-semibold text-slate-700">Alto total</label>
                        <input
                          type="number"
                          min={1}
                          value={videowallDraft.total_height}
                          onChange={(event) =>
                            setVideowallDraft((current) => ({
                              ...current,
                              total_height: normalizePositiveInt(Number(event.target.value), current.total_height),
                            }))
                          }
                        />
                      </div>
                    </>
                  )}
                </div>
              ) : (
                <div className="mt-5">
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Selecciona un grupo</label>
                  <select value={selectedVideowallId} onChange={(event) => setSelectedVideowallId(event.target.value)}>
                    {videowalls.length === 0 ? <option value="">No hay videowalls disponibles</option> : null}
                    {videowalls.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                  {selectedExistingVideowall ? (
                    <p className="mt-2 text-sm text-slate-600">
                      Matriz {selectedExistingVideowall.columns}x{selectedExistingVideowall.rows} · {selectedExistingVideowall.total_width}x
                      {selectedExistingVideowall.total_height}
                    </p>
                  ) : null}
                </div>
              )}

              {catalogError ? <div className="mt-4 rounded-[18px] bg-rose-50 px-4 py-3 text-sm text-rose-700">{catalogError}</div> : null}
              {videowallFlow === "existing" && selectedExistingVideowall && videowallDerived.availablePositions.length === 0 ? (
                <div className="mt-4 rounded-[18px] bg-amber-50 px-4 py-3 text-sm text-amber-900">
                  Este videowall ya está completo. Selecciona otro grupo o crea uno nuevo para agregar otra pantalla.
                </div>
              ) : null}

              <div className="mt-5 grid gap-5 xl:grid-cols-[minmax(280px,360px)_minmax(0,1fr)]">
                <div className="rounded-[24px] border border-slate-200 bg-white p-4">
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Preview simple</p>
                  <div className="mt-4">
                    <SimpleVideowallMatrix
                      columns={videowallDerived.columns}
                      rows={videowallDerived.rows}
                      occupiedPositions={videowallFlow === "existing" ? videowallDerived.occupiedPositions : []}
                      selectedPosition={videowallDerived.selectedPosition}
                      onSelectPosition={(position) => {
                        if (videowallFlow === "existing" && videowallDerived.occupiedPositions.includes(position)) {
                          return;
                        }

                        setVideowallDraft((current) => ({
                          ...current,
                          position_index: position,
                        }));
                      }}
                    />
                  </div>
                  <div className="mt-4 flex flex-wrap gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    <span className="rounded-full bg-emerald-50 px-3 py-1.5 text-emerald-700">{videowallDerived.configuredNodes} configurados</span>
                    <span className="rounded-full bg-amber-50 px-3 py-1.5 text-amber-700">{videowallDerived.pendingNodes} pendientes</span>
                  </div>
                </div>

                <div className="grid gap-3">
                  <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Matriz</p>
                    <p className="mt-2 font-semibold text-ink">
                      {videowallDerived.columns}x{videowallDerived.rows} · {videowallDerived.totalMonitors} monitores
                    </p>
                  </div>
                  <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Resolución total</p>
                    <p className="mt-2 font-semibold text-ink">
                      {videowallDerived.totalWidth}x{videowallDerived.totalHeight}
                    </p>
                  </div>
                  <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Nodo seleccionado</p>
                    <p className="mt-2 font-semibold text-ink">
                      Fila {videowallDerived.selectedCell.rowIndex + 1} · Columna {videowallDerived.selectedCell.columnIndex + 1}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">Posición {videowallDerived.selectedPosition}</p>
                  </div>
                  <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Recorte del nodo</p>
                    <p className="mt-2 text-sm text-slate-700">
                      x:{videowallDerived.selectedCell.x} · y:{videowallDerived.selectedCell.y} · {videowallDerived.selectedCell.width}x
                      {videowallDerived.selectedCell.height}
                    </p>
                  </div>
                </div>
              </div>
            </section>
          ) : null}

          {mode !== "videowall" ? (
            <LedOutputMappingPanel
              value={outputMapping}
              fallbackWidth={resolutionWidth}
              fallbackHeight={resolutionHeight}
              onChange={setOutputMapping}
            />
          ) : null}

          <div className="flex flex-wrap gap-3">
            <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" disabled={isSubmitting} type="submit">
              {isSubmitting ? "Guardando..." : "Crear pantalla"}
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
