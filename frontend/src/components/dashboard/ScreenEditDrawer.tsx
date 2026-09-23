import { Columns2, Monitor, TvMinimalPlay, X } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";

import { apiRequest } from "../../lib/api";
import { formatChannelMode } from "../../lib/labels";
import type { Branch, Channel, DeviceMode, Orientation } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";
import { DeviceOutputsPreview, getSuggestedCanvas } from "./DeviceOutputsPreview";
import { LedOutputMappingPanel, buildOutputMappingDraft, type ChannelOutputMappingDraft } from "./LedOutputMappingPanel";
import { ContentRotationField } from "../ContentRotationField";

type ScreenMode = DeviceMode | "videowall";

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

function getScreenModeFromChannel(channel: Channel): ScreenMode {
  if (channel.mode === "videowall") {
    return "videowall";
  }
  if (channel.mode === "expanded") {
    return channel.device_mode;
  }
  return "single";
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

function getModeIcon(mode: Channel["mode"]) {
  if (mode === "expanded") {
    return Columns2;
  }
  if (mode === "videowall") {
    return TvMinimalPlay;
  }
  return Monitor;
}

export function ScreenEditDrawer({
  open,
  token,
  branch,
  channel,
  onClose,
  onSaved,
}: {
  open: boolean;
  token: string | null;
  branch: Branch | null;
  channel: Channel | null;
  onClose: () => void;
  onSaved: (channel: Channel) => Promise<void> | void;
}) {
  const [name, setName] = useState("");
  const [mode, setMode] = useState<ScreenMode>("single");
  const [orientation, setOrientation] = useState<Orientation>("horizontal");
  const [rotation, setRotation] = useState<number | null>(null);
  const [rotationChanged, setRotationChanged] = useState(false);
  const [deviceGeometryChanged, setDeviceGeometryChanged] = useState(false);
  const [outputMappingChanged, setOutputMappingChanged] = useState(false);
  const [resolutionWidth, setResolutionWidth] = useState(1920);
  const [resolutionHeight, setResolutionHeight] = useState(1080);
  const [screenCount, setScreenCount] = useState(2);
  const [canvasWidth, setCanvasWidth] = useState(1920);
  const [canvasHeight, setCanvasHeight] = useState(1080);
  const [isCanvasCustom, setIsCanvasCustom] = useState(false);
  const [hardwareIdentifier, setHardwareIdentifier] = useState("");
  const [notes, setNotes] = useState("");
  const [outputMapping, setOutputMapping] = useState<ChannelOutputMappingDraft>(buildOutputMappingDraft(null, 1920, 1080));
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (!open || !channel) {
      return;
    }

    setName(channel.name);
    setMode(getScreenModeFromChannel(channel));
    setOrientation(channel.orientation);
    setRotation(channel.device_outputs.find((output) => output.display_order === 0)?.rotation ?? null);
    setRotationChanged(false);
    setDeviceGeometryChanged(false);
    setOutputMappingChanged(false);
    setResolutionWidth(channel.resolution_width);
    setResolutionHeight(channel.resolution_height);
    setScreenCount(channel.mode === "expanded" ? Math.max(2, channel.screen_count) : 1);
    setCanvasWidth(channel.canvas_width);
    setCanvasHeight(channel.canvas_height);
    setIsCanvasCustom(false);
    setHardwareIdentifier(channel.hardware_identifier ?? "");
    setNotes(channel.notes ?? "");
    setOutputMapping(buildOutputMappingDraft(channel.output_mapping_json, channel.resolution_width, channel.resolution_height));
    setError(null);
  }, [channel, open]);

  const effectiveOutputCount = getOutputCount(mode, screenCount);
  const suggestedCanvas = getSuggestedCanvas(mode === "videowall" ? "single" : mode, resolutionWidth, resolutionHeight, effectiveOutputCount);

  useEffect(() => {
    if (mode === "videowall" || !deviceGeometryChanged) {
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
  }, [deviceGeometryChanged, isCanvasCustom, mode, resolutionHeight, resolutionWidth, suggestedCanvas.height, suggestedCanvas.width]);

  if (!open || !channel || !branch) {
    return null;
  }

  const targetChannel = channel;
  const canEditRotation = targetChannel.mode === "normal" && targetChannel.device_mode === "single" && mode === "single";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) {
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      const legacyMode = getLegacyMode(mode);
      const response = await apiRequest<Channel>(`/channels/${targetChannel.id}`, {
        method: "PATCH",
        token,
        body: {
          name: name.trim(),
          mode: legacyMode === "videowall" || !deviceGeometryChanged ? undefined : legacyMode,
          device_mode: legacyMode === "videowall" || !deviceGeometryChanged ? undefined : mode,
          resolution_width: legacyMode === "videowall" || !deviceGeometryChanged ? undefined : Math.max(1, resolutionWidth),
          resolution_height: legacyMode === "videowall" || !deviceGeometryChanged ? undefined : Math.max(1, resolutionHeight),
          orientation: legacyMode === "videowall" || orientation === targetChannel.orientation ? undefined : orientation,
          rotation: canEditRotation && rotationChanged ? rotation : undefined,
          screen_count: !deviceGeometryChanged ? undefined : (legacyMode === "expanded" ? effectiveOutputCount : 1),
          canvas_width: legacyMode === "videowall" || !deviceGeometryChanged ? undefined : (mode === "single" ? resolutionWidth : canvasWidth),
          canvas_height: legacyMode === "videowall" || !deviceGeometryChanged ? undefined : (mode === "single" ? resolutionHeight : canvasHeight),
          hardware_identifier: hardwareIdentifier.trim() || null,
          output_mapping_json:
            legacyMode === "videowall" || !outputMappingChanged
              ? undefined
              : buildOutputMappingDraft(outputMapping, resolutionWidth, resolutionHeight),
          notes: notes === (targetChannel.notes ?? "") ? undefined : notes.trim() || null,
        },
      });

      await onSaved(response);
      onClose();
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, "No se pudo actualizar la pantalla."));
    } finally {
      setIsSubmitting(false);
    }
  }

  const ModeIcon = getModeIcon(targetChannel.mode);

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-950/45 backdrop-blur-sm">
      <button aria-label="Cerrar" className="flex-1 cursor-default" type="button" onClick={onClose} />
      <aside className="h-screen w-[min(95vw,1100px)] max-w-none overflow-x-hidden overflow-y-auto border-l border-white/10 bg-white px-6 py-6 shadow-2xl lg:px-8">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-accent">Editar pantalla</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{targetChannel.name}</h3>
            <p className="mt-2 text-sm text-slate-600">
              Ajusta la configuración de la pantalla dentro de {branch.name} sin salir del dashboard.
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
          <div className="rounded-[28px] border border-slate-200 bg-slate-50 p-5">
            <div className="flex items-start gap-4">
              <span className="inline-flex rounded-2xl bg-white p-3 text-accent shadow-sm">
                <ModeIcon className="h-5 w-5" />
              </span>
              <div className="space-y-2">
                <p className="font-semibold text-ink">
                  {targetChannel.mode === "videowall" ? formatChannelMode(targetChannel.mode) : getDeviceModeLabel(mode as DeviceMode)}
                </p>
                <p className="text-sm text-slate-600">Código player: {targetChannel.channel_code}</p>
                {targetChannel.mode === "videowall" ? (
                  <p className="text-sm text-slate-600">
                    La geometría del videowall se mantiene bloqueada aquí. En esta fase solo se editan datos operativos del canal.
                  </p>
                ) : null}
              </div>
            </div>
          </div>

          {targetChannel.mode !== "videowall" ? (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
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
                  description: "Canvas panorámico repartido entre salidas.",
                  icon: Columns2,
                },
                {
                  id: "independent" as const,
                  title: "Independent",
                  description: "Preparado para campañas por salida más adelante.",
                  icon: Columns2,
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
                      setDeviceGeometryChanged(true);
                      const nextOutputCount = getOutputCount(option.id, screenCount);
                      const nextCanvas = getSuggestedCanvas(option.id, resolutionWidth, resolutionHeight, nextOutputCount);
                      setMode(option.id);
                      setScreenCount(nextOutputCount);
                      setCanvasWidth(nextCanvas.width);
                      setCanvasHeight(nextCanvas.height);
                      setIsCanvasCustom(false);
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
          ) : null}

          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div className="md:col-span-2 xl:col-span-3">
              <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
              <input value={name} onChange={(event) => setName(event.target.value)} required />
            </div>

            {isMultiOutputMode(mode) ? (
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Salidas</label>
                <input
                  type="number"
                  min={2}
                  value={effectiveOutputCount}
                  onChange={(event) => {
                    setDeviceGeometryChanged(true);
                    const nextCount = Math.max(2, Number(event.target.value) || 2);
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
                    onChange={(event) => { setDeviceGeometryChanged(true); setResolutionWidth(Math.max(1, Number(event.target.value) || 1)); }}
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
                    onChange={(event) => { setDeviceGeometryChanged(true); setResolutionHeight(Math.max(1, Number(event.target.value) || 1)); }}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Canvas ancho</label>
                  <input
                    type="number"
                    min={1}
                    disabled={mode === "single"}
                    value={canvasWidth}
                    onChange={(event) => {
                      setCanvasWidth(Math.max(1, Number(event.target.value) || 1));
                      setDeviceGeometryChanged(true);
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
                    value={canvasHeight}
                    onChange={(event) => {
                      setCanvasHeight(Math.max(1, Number(event.target.value) || 1));
                      setDeviceGeometryChanged(true);
                      setIsCanvasCustom(true);
                    }}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Orientación</label>
                  <select value={orientation} onChange={(event) => setOrientation(event.target.value as Orientation)}>
                    <option value="horizontal">Horizontal</option>
                    <option value="vertical">Vertical</option>
                  </select>
                </div>
              </>
            ) : null}

            {canEditRotation ? (
              <ContentRotationField value={rotation} onChange={(value) => { setRotation(value); setRotationChanged(true); }} />
            ) : null}

            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Hardware opcional</label>
              <input value={hardwareIdentifier} onChange={(event) => setHardwareIdentifier(event.target.value)} placeholder="Mini PC lobby" />
            </div>
            <div className="md:col-span-2 xl:col-span-3">
              <label className="mb-2 block text-sm font-semibold text-slate-700" htmlFor="screen-edit-notes">Notas</label>
              <textarea id="screen-edit-notes" value={notes} onChange={(event) => setNotes(event.target.value)} />
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

          {mode !== "videowall" ? (
            <LedOutputMappingPanel
              value={outputMapping}
              fallbackWidth={resolutionWidth}
              fallbackHeight={resolutionHeight}
              onChange={(value) => { setOutputMapping(value); setOutputMappingChanged(true); }}
            />
          ) : null}

          <div className="flex flex-wrap gap-3">
            <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" disabled={isSubmitting} type="submit">
              {isSubmitting ? "Guardando..." : "Guardar cambios"}
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
