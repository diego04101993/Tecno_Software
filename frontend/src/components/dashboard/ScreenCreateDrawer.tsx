import { Columns2, Monitor, TvMinimalPlay, X } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Branch, Channel, DeviceMode, Orientation, Videowall, VideowallNode, VideowallRenderMode } from "../../types/domain";
import { getVideowallCellMetrics } from "../VideowallGridPreview";
import { DeviceOutputsPreview, getSuggestedCanvas } from "./DeviceOutputsPreview";
import { LedOutputMappingPanel, buildOutputMappingDraft, type ChannelOutputMappingDraft } from "./LedOutputMappingPanel";
import { ContentRotationField, type ContentRotation } from "../ContentRotationField";
import { SimpleVideowallMatrix } from "./SimpleVideowallMatrix";

type ScreenMode = DeviceMode | "videowall";
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
  render_mode: "multi-node" as VideowallRenderMode,
  columns: 2,
  rows: 2,
  resolution_basis: "monitor" as ResolutionBasis,
  monitor_width: 1920,
  monitor_height: 1080,
  total_width: 3840,
  total_height: 2160,
  output_width: 1920,
  output_height: 1080,
  position_index: 1,
};

function isMultiOutputMode(mode: ScreenMode) {
  return mode === "mirror" || mode === "extended" || mode === "independent";
}

function getLegacyMode(mode: ScreenMode): Channel["mode"] {
  if (mode === "videowall") {
    return "videowall";
  }
  if (mode === "single") {
    return "normal";
  }
  return "expanded";
}

function getOutputCount(mode: ScreenMode, screenCount: number) {
  if (!isMultiOutputMode(mode)) {
    return 1;
  }
  return Math.max(2, screenCount);
}

function getDeviceModeLabel(mode: DeviceMode) {
  switch (mode) {
    case "mirror":
      return "Mirror";
    case "extended":
      return "Extended";
    case "independent":
      return "Independent";
    default:
      return "Single";
  }
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
  const [mode, setMode] = useState<ScreenMode>("single");
  const [name, setName] = useState("");
  const [orientation, setOrientation] = useState<Orientation>("horizontal");
  const [rotation, setRotation] = useState<ContentRotation>(0);
  const [resolutionWidth, setResolutionWidth] = useState(1920);
  const [resolutionHeight, setResolutionHeight] = useState(1080);
  const [screenCount, setScreenCount] = useState(2);
  const [canvasWidth, setCanvasWidth] = useState(1920);
  const [canvasHeight, setCanvasHeight] = useState(1080);
  const [isCanvasCustom, setIsCanvasCustom] = useState(false);
  const [hardwareIdentifier, setHardwareIdentifier] = useState("");
  const [notes, setNotes] = useState("");
  const [outputMapping, setOutputMapping] = useState<ChannelOutputMappingDraft>(buildOutputMappingDraft(null, 1920, 1080));
  const [outputMappingError, setOutputMappingError] = useState<string | null>(null);
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

    setMode("single");
    setName("");
    setOrientation("horizontal");
    setRotation(0);
    setResolutionWidth(1920);
    setResolutionHeight(1080);
    setScreenCount(2);
    setCanvasWidth(1920);
    setCanvasHeight(1080);
    setIsCanvasCustom(false);
    setHardwareIdentifier("");
    setNotes("");
    setOutputMapping(buildOutputMappingDraft(null, 1920, 1080));
    setOutputMappingError(null);
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
        setSelectedVideowallId((current) => current || response.find((item) => item.render_mode === "multi-node")?.id || "");
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
    () => videowalls.find((item) => item.id === selectedVideowallId && item.render_mode === "multi-node") ?? null,
    [selectedVideowallId, videowalls],
  );
  const occupiedPositions = useMemo(
    () => existingVideowallNodes.map((node) => node.position_index).sort((left, right) => left - right),
    [existingVideowallNodes],
  );
  const effectiveOutputCount = useMemo(() => getOutputCount(mode, screenCount), [mode, screenCount]);
  const suggestedCanvas = useMemo(
    () => getSuggestedCanvas(mode === "videowall" ? "single" : mode, resolutionWidth, resolutionHeight, effectiveOutputCount),
    [effectiveOutputCount, mode, resolutionHeight, resolutionWidth],
  );

  useEffect(() => {
    if (mode === "videowall") {
      return;
    }

    if (mode === "single") {
      setCanvasWidth(resolutionWidth);
      setCanvasHeight(resolutionHeight);
      setIsCanvasCustom(false);
      return;
    }

    if (!isCanvasCustom) {
      setCanvasWidth(suggestedCanvas.width);
      setCanvasHeight(suggestedCanvas.height);
    }
  }, [isCanvasCustom, mode, resolutionHeight, resolutionWidth, suggestedCanvas.height, suggestedCanvas.width]);

  const videowallDerived = useMemo(() => {
    const usingExistingVideowall = mode === "videowall" && videowallFlow === "existing" && selectedExistingVideowall;
    const renderMode = usingExistingVideowall ? selectedExistingVideowall.render_mode : videowallDraft.render_mode;
    const isHardwareSingleInput = renderMode === "hardware-single-input";
    const columns = usingExistingVideowall
      ? selectedExistingVideowall.columns
      : normalizePositiveInt(videowallDraft.columns, 2);
    const rows = usingExistingVideowall ? selectedExistingVideowall.rows : normalizePositiveInt(videowallDraft.rows, 2);
    const totalMonitors = columns * rows;
    const totalWidth = usingExistingVideowall
      ? selectedExistingVideowall.total_width
      : !isHardwareSingleInput && videowallDraft.resolution_basis === "monitor"
        ? normalizePositiveInt(videowallDraft.monitor_width, 1920) * columns
        : Math.max(columns, normalizePositiveInt(videowallDraft.total_width, 3840));
    const totalHeight = usingExistingVideowall
      ? selectedExistingVideowall.total_height
      : !isHardwareSingleInput && videowallDraft.resolution_basis === "monitor"
        ? normalizePositiveInt(videowallDraft.monitor_height, 1080) * rows
        : Math.max(rows, normalizePositiveInt(videowallDraft.total_height, 2160));
    const outputWidth = usingExistingVideowall
      ? selectedExistingVideowall.output_width
      : normalizePositiveInt(videowallDraft.output_width, 1920);
    const outputHeight = usingExistingVideowall
      ? selectedExistingVideowall.output_height
      : normalizePositiveInt(videowallDraft.output_height, 1080);
    const takenPositions = usingExistingVideowall ? occupiedPositions : [];
    const availablePositions = Array.from({ length: totalMonitors }, (_, index) => index + 1).filter(
      (position) => !takenPositions.includes(position),
    );
    const safePosition = availablePositions.includes(videowallDraft.position_index)
      ? videowallDraft.position_index
      : availablePositions[0] ?? 1;
    const selectedCell = isHardwareSingleInput ? null : getVideowallCellMetrics(columns, rows, totalWidth, totalHeight, safePosition);

    return {
      renderMode,
      isHardwareSingleInput,
      columns,
      rows,
      totalMonitors,
      totalWidth,
      totalHeight,
      outputWidth,
      outputHeight,
      configuredNodes: takenPositions.length,
      pendingNodes: Math.max(0, totalMonitors - takenPositions.length),
      selectedPosition: safePosition,
      selectedCell,
      availablePositions,
      occupiedPositions: takenPositions,
    };
  }, [mode, occupiedPositions, selectedExistingVideowall, videowallDraft, videowallFlow]);

  useEffect(() => {
    if (mode !== "videowall" || videowallDerived.isHardwareSingleInput) {
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
  const compatibleExistingVideowalls = videowalls.filter((item) => item.render_mode === "multi-node");

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) {
      return;
    }

    if (mode !== "videowall" && outputMappingError) {
      setError(outputMappingError);
      return;
    }

    if (mode === "videowall" && !videowallDerived.isHardwareSingleInput && videowallFlow === "existing") {
      if (!selectedExistingVideowall) {
        setError("Selecciona un videowall existente antes de guardar la pantalla.");
        return;
      }

      if (videowallDerived.availablePositions.length === 0) {
        setError("El videowall seleccionado ya no tiene posiciones libres.");
        return;
      }
    }

    if (mode === "videowall" && !videowallDerived.isHardwareSingleInput && !videowallDerived.selectedCell) {
      setError("No se pudo calcular la posición del nodo seleccionado.");
      return;
    }

    setIsSubmitting(true);
    setError(null);
    setNotice(null);

    try {
      const legacyMode = getLegacyMode(mode);
      const deviceMode = mode === "videowall" ? "single" : mode;
      const channelPayload = {
        client_id: clientId,
        branch_id: targetBranch.id,
        name,
        resolution_width:
          legacyMode === "videowall"
            ? videowallDerived.isHardwareSingleInput
              ? videowallDerived.outputWidth
              : (videowallDerived.selectedCell?.width ?? resolutionWidth)
            : resolutionWidth,
        resolution_height:
          legacyMode === "videowall"
            ? videowallDerived.isHardwareSingleInput
              ? videowallDerived.outputHeight
              : (videowallDerived.selectedCell?.height ?? resolutionHeight)
            : resolutionHeight,
        orientation,
        rotation: mode === "single" ? rotation : undefined,
        mode: legacyMode,
        device_mode: deviceMode,
        screen_count: effectiveOutputCount,
        canvas_width:
          legacyMode === "videowall"
            ? (videowallDerived.isHardwareSingleInput ? videowallDerived.outputWidth : (videowallDerived.selectedCell?.width ?? resolutionWidth))
            : canvasWidth,
        canvas_height:
          legacyMode === "videowall"
            ? (videowallDerived.isHardwareSingleInput ? videowallDerived.outputHeight : (videowallDerived.selectedCell?.height ?? resolutionHeight))
            : canvasHeight,
        hardware_identifier: hardwareIdentifier || null,
        notes: notes || null,
        expanded_outputs: [],
        output_mapping_json:
          legacyMode === "videowall"
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
        if (videowallDerived.isHardwareSingleInput) {
          await apiRequest<Videowall>("/videowalls", {
            method: "POST",
            token,
            body: {
              client_id: clientId,
              name: videowallDraft.name.trim() || `${name} Videowall`,
              render_mode: "hardware-single-input",
              columns: videowallDerived.columns,
              rows: videowallDerived.rows,
              total_width: videowallDerived.totalWidth,
              total_height: videowallDerived.totalHeight,
              output_width: videowallDerived.outputWidth,
              output_height: videowallDerived.outputHeight,
              primary_channel_id: channel.id,
              sync_mode: "play_at_timestamp",
            },
          });

          nextNotice = {
            tone: "success",
            text: `Pantalla y videowall HDMI creados. ${name} quedó como canal principal de la salida ${videowallDerived.outputWidth}x${videowallDerived.outputHeight}.`,
          };
        } else {
          let targetVideowall = selectedExistingVideowall;

          if (videowallFlow === "create") {
            targetVideowall = await apiRequest<Videowall>("/videowalls", {
              method: "POST",
              token,
              body: {
                client_id: clientId,
                name: videowallDraft.name.trim() || `${name} Videowall`,
                render_mode: "multi-node",
                columns: videowallDerived.columns,
                rows: videowallDerived.rows,
                total_width: videowallDerived.totalWidth,
                total_height: videowallDerived.totalHeight,
                output_width: videowallDerived.selectedCell?.width ?? videowallDerived.outputWidth,
                output_height: videowallDerived.selectedCell?.height ?? videowallDerived.outputHeight,
                sync_mode: "play_at_timestamp",
              },
            });
          }

          const selectedCell = videowallDerived.selectedCell;

          try {
          await apiRequest(`/videowalls/${targetVideowall?.id}/nodes`, {
            method: "POST",
            token,
            body: {
              channel_id: channel.id,
              position_index: videowallDerived.selectedPosition,
              row_index: selectedCell?.rowIndex,
              column_index: selectedCell?.columnIndex,
              x: selectedCell?.x,
              y: selectedCell?.y,
              width: selectedCell?.width,
              height: selectedCell?.height,
            },
          });
          nextNotice =
            videowallFlow === "create"
              ? {
                  tone: "success",
                  text: `Pantalla y grupo videowall creados. Nodo asignado en fila ${(selectedCell?.rowIndex ?? 0) + 1}, columna ${(selectedCell?.columnIndex ?? 0) + 1}.`,
                }
              : {
                  tone: "success",
                  text: `Pantalla creada y asignada al videowall existente en fila ${(selectedCell?.rowIndex ?? 0) + 1}, columna ${(selectedCell?.columnIndex ?? 0) + 1}.`,
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
        }
      } else if (isMultiOutputMode(mode)) {
        nextNotice = {
          tone: "success",
          text: `${getDeviceModeLabel(mode)} preparado con ${effectiveOutputCount} salidas y canvas ${canvasWidth}x${canvasHeight}.`,
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
              Crea una pantalla single o multi-output, o agrega un nodo a un videowall, sin salir del dashboard.
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
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
            {[
              {
                id: "single" as const,
                title: "Single",
                description: "Una pantalla por canal.",
                icon: Monitor,
              },
              {
                id: "mirror" as const,
                title: "Mirror",
                description: "Varias salidas repiten el mismo contenido.",
                icon: Columns2,
              },
              {
                id: "extended" as const,
                title: "Extended",
                description: "Canvas panorámico con slices automáticos.",
                icon: Columns2,
              },
              {
                id: "independent" as const,
                title: "Independent",
                description: "Preparado para campañas distintas por salida.",
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
                    if (option.id !== "videowall") {
                      const nextOutputCount = getOutputCount(option.id, screenCount);
                      const nextCanvas = getSuggestedCanvas(option.id, resolutionWidth, resolutionHeight, nextOutputCount);
                      setScreenCount(nextOutputCount);
                      setCanvasWidth(nextCanvas.width);
                      setCanvasHeight(nextCanvas.height);
                      setIsCanvasCustom(false);
                    }
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

            {isMultiOutputMode(mode) ? (
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Salidas</label>
                <input
                  type="number"
                  min={2}
                  value={effectiveOutputCount}
                  onChange={(event) => {
                    const nextCount = Math.max(2, normalizePositiveInt(Number(event.target.value), effectiveOutputCount));
                    setScreenCount(nextCount);
                    if (!isCanvasCustom) {
                      const nextCanvas = getSuggestedCanvas(mode, resolutionWidth, resolutionHeight, nextCount);
                      setCanvasWidth(nextCanvas.width);
                      setCanvasHeight(nextCanvas.height);
                    }
                  }}
                />
              </div>
            ) : null}

            {mode !== "videowall" ? (
              <>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">
                    {isMultiOutputMode(mode) ? "Resolución por salida" : "Resolución ancho"}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={resolutionWidth}
                    name="resolution_width"
                    onChange={(event) => setResolutionWidth(normalizePositiveInt(Number(event.target.value), resolutionWidth))}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">
                    {isMultiOutputMode(mode) ? "Resolución alto por salida" : "Resolución alto"}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={resolutionHeight}
                    name="resolution_height"
                    onChange={(event) => setResolutionHeight(normalizePositiveInt(Number(event.target.value), resolutionHeight))}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Canvas ancho</label>
                  <input
                    type="number"
                    min={1}
                    disabled={mode === "single"}
                    value={mode === "single" ? resolutionWidth : canvasWidth}
                    onChange={(event) => {
                      setCanvasWidth(normalizePositiveInt(Number(event.target.value), canvasWidth));
                      setIsCanvasCustom(true);
                    }}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Canvas alto</label>
                  <input
                    type="number"
                    min={1}
                    disabled={mode === "single"}
                    value={mode === "single" ? resolutionHeight : canvasHeight}
                    onChange={(event) => {
                      setCanvasHeight(normalizePositiveInt(Number(event.target.value), canvasHeight));
                      setIsCanvasCustom(true);
                    }}
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

            {mode === "single" ? <ContentRotationField value={rotation} onChange={setRotation} /> : null}

            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Hardware opcional</label>
              <input
                value={hardwareIdentifier}
                onChange={(event) => setHardwareIdentifier(event.target.value)}
                placeholder="Mini PC lobby"
              />
            </div>
          </div>

          {mode !== "videowall" ? (
            <DeviceOutputsPreview
              model={{
                deviceMode: mode,
                outputCount: effectiveOutputCount,
                outputWidth: resolutionWidth,
                outputHeight: resolutionHeight,
                canvasWidth: mode === "single" ? resolutionWidth : canvasWidth,
                canvasHeight: mode === "single" ? resolutionHeight : canvasHeight,
              }}
            />
          ) : null}

          {mode === "videowall" ? (
            <section className="rounded-[28px] border border-slate-200 bg-slate-50 p-5">
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)]">
                <div className="rounded-[24px] border border-slate-200 bg-white p-4">
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre del grupo</label>
                  <input
                    value={videowallDraft.name}
                    onChange={(event) => setVideowallDraft((current) => ({ ...current, name: event.target.value }))}
                    placeholder="Videowall lobby"
                  />
                </div>
                <div className="rounded-[24px] border border-slate-200 bg-white p-4">
                  <p className="mb-3 text-sm font-semibold text-slate-700">Tipo de videowall</p>
                  <div className="grid gap-3 md:grid-cols-2">
                    {[
                      {
                        id: "multi-node" as VideowallRenderMode,
                        title: "Distribuido por varios Players",
                        description: "Un canal por monitor, con crop individual y sincronización por nodo.",
                      },
                      {
                        id: "hardware-single-input" as VideowallRenderMode,
                        title: "Cascada HDMI / una sola entrada",
                        description: "Una sola PC y una sola salida HDMI para todo el muro lógico.",
                      },
                    ].map((option) => {
                      const active = videowallDraft.render_mode === option.id;

                      return (
                        <button
                          key={option.id}
                          className={[
                            "rounded-[22px] border px-4 py-3 text-left transition",
                            active ? "border-cyan-300 bg-cyan-50" : "border-slate-200 bg-white hover:border-slate-300",
                          ].join(" ")}
                          type="button"
                          onClick={() => {
                            setVideowallDraft((current) => ({
                              ...current,
                              render_mode: option.id,
                            }));
                            if (option.id === "hardware-single-input") {
                              setVideowallFlow("create");
                            }
                          }}
                        >
                          <p className="font-semibold text-ink">{option.title}</p>
                          <p className="mt-1 text-sm text-slate-600">{option.description}</p>
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>

              {!videowallDerived.isHardwareSingleInput ? (
                <>
              <div className="mt-5 flex flex-wrap gap-3">
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
                    {compatibleExistingVideowalls.length === 0 ? <option value="">No hay videowalls multi-nodo disponibles</option> : null}
                    {compatibleExistingVideowalls.map((item) => (
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
                      Fila {(videowallDerived.selectedCell?.rowIndex ?? 0) + 1} · Columna {(videowallDerived.selectedCell?.columnIndex ?? 0) + 1}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">Posición {videowallDerived.selectedPosition}</p>
                  </div>
                  <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Recorte del nodo</p>
                    <p className="mt-2 text-sm text-slate-700">
                      x:{videowallDerived.selectedCell?.x ?? 0} · y:{videowallDerived.selectedCell?.y ?? 0} · {videowallDerived.selectedCell?.width ?? 0}x
                      {videowallDerived.selectedCell?.height ?? 0}
                    </p>
                  </div>
                </div>
              </div>
                </>
              ) : (
                <div className="mt-5 grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                  <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3 xl:col-span-2">
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
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho total del muro</label>
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
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Alto total del muro</label>
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
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Salida HDMI ancho</label>
                      <input
                        type="number"
                        min={1}
                        value={videowallDraft.output_width}
                        onChange={(event) =>
                          setVideowallDraft((current) => ({
                            ...current,
                            output_width: normalizePositiveInt(Number(event.target.value), current.output_width),
                          }))
                        }
                      />
                    </div>
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Salida HDMI alto</label>
                      <input
                        type="number"
                        min={1}
                        value={videowallDraft.output_height}
                        onChange={(event) =>
                          setVideowallDraft((current) => ({
                            ...current,
                            output_height: normalizePositiveInt(Number(event.target.value), current.output_height),
                          }))
                        }
                      />
                    </div>
                  </div>
                  <div className="grid gap-3">
                    <div className="rounded-[20px] border border-cyan-200 bg-cyan-50 px-4 py-4">
                      <p className="text-xs uppercase tracking-[0.2em] text-cyan-700">Canal principal</p>
                      <p className="mt-2 text-base font-semibold text-ink">{name || "Pantalla en creación"}</p>
                      <p className="mt-2 text-sm text-slate-700">
                        Esta pantalla será el canal principal del muro HDMI. No se crearán nodos ni crops individuales.
                      </p>
                    </div>
                    <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                      <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Tipo</p>
                      <p className="mt-2 font-semibold text-ink">Cascada HDMI / una sola entrada</p>
                    </div>
                    <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                      <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Muro lógico</p>
                      <p className="mt-2 font-semibold text-ink">
                        {videowallDerived.totalWidth}x{videowallDerived.totalHeight}
                      </p>
                    </div>
                    <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                      <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Salida HDMI</p>
                      <p className="mt-2 font-semibold text-ink">
                        {videowallDerived.outputWidth}x{videowallDerived.outputHeight}
                      </p>
                    </div>
                    <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
                      <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Distribución</p>
                      <p className="mt-2 font-semibold text-ink">
                        {videowallDerived.columns}x{videowallDerived.rows}
                      </p>
                    </div>
                  </div>
                </div>
              )}
            </section>
          ) : null}

          {mode !== "videowall" ? (
            <LedOutputMappingPanel
              key={branch.id}
              value={outputMapping}
              fallbackWidth={resolutionWidth}
              fallbackHeight={resolutionHeight}
              onChange={setOutputMapping}
              onValidationChange={setOutputMappingError}
              disabled={isSubmitting}
            />
          ) : null}

          <div className="flex flex-wrap gap-3">
            <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" disabled={isSubmitting || (mode !== "videowall" && !!outputMappingError)} type="submit">
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
