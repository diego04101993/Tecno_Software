import { Columns2, Monitor, TvMinimalPlay, X } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";

import { apiRequest } from "../../lib/api";
import { formatChannelMode } from "../../lib/labels";
import type { Branch, Channel, Orientation } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";
import { LedOutputMappingPanel, buildOutputMappingDraft, type ChannelOutputMappingDraft } from "./LedOutputMappingPanel";

function buildExpandedOutputs(screenCount: number) {
  return Array.from({ length: screenCount }, (_, index) => ({
    output_index: index + 1,
    label: `HDMI ${index + 1}`,
    role: index === 0 ? "primary" : "extended",
  }));
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
  const [orientation, setOrientation] = useState<Orientation>("horizontal");
  const [resolutionWidth, setResolutionWidth] = useState(1920);
  const [resolutionHeight, setResolutionHeight] = useState(1080);
  const [screenCount, setScreenCount] = useState(2);
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
    setOrientation(channel.orientation);
    setResolutionWidth(channel.resolution_width);
    setResolutionHeight(channel.resolution_height);
    setScreenCount(channel.mode === "expanded" ? Math.max(2, channel.screen_count) : 1);
    setHardwareIdentifier(channel.hardware_identifier ?? "");
    setNotes(channel.notes ?? "");
    setOutputMapping(buildOutputMappingDraft(channel.output_mapping_json, channel.resolution_width, channel.resolution_height));
    setError(null);
  }, [channel, open]);

  if (!open || !channel || !branch) {
    return null;
  }

  const targetChannel = channel;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) {
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      const response = await apiRequest<Channel>(`/channels/${targetChannel.id}`, {
        method: "PATCH",
        token,
        body: {
          name: name.trim(),
          resolution_width: targetChannel.mode === "videowall" ? undefined : Math.max(1, resolutionWidth),
          resolution_height: targetChannel.mode === "videowall" ? undefined : Math.max(1, resolutionHeight),
          orientation: targetChannel.mode === "videowall" ? undefined : orientation,
          screen_count: targetChannel.mode === "expanded" ? screenCount : undefined,
          hardware_identifier: hardwareIdentifier.trim() || null,
          expanded_outputs: targetChannel.mode === "expanded" ? buildExpandedOutputs(screenCount) : undefined,
          output_mapping_json:
            targetChannel.mode === "videowall"
              ? undefined
              : buildOutputMappingDraft(outputMapping, resolutionWidth, resolutionHeight),
          notes: notes.trim() || null,
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
                <p className="font-semibold text-ink">{formatChannelMode(targetChannel.mode)}</p>
                <p className="text-sm text-slate-600">Código player: {targetChannel.channel_code}</p>
                {targetChannel.mode === "videowall" ? (
                  <p className="text-sm text-slate-600">
                    La geometría del videowall se mantiene bloqueada aquí. En esta fase solo se editan datos operativos del canal.
                  </p>
                ) : null}
              </div>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div className="md:col-span-2 xl:col-span-3">
              <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
              <input value={name} onChange={(event) => setName(event.target.value)} required />
            </div>

            {targetChannel.mode === "expanded" ? (
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Salidas</label>
                <select value={screenCount} onChange={(event) => setScreenCount(Number(event.target.value))}>
                  <option value={2}>2 salidas</option>
                  <option value={3}>3 salidas</option>
                </select>
              </div>
            ) : null}

            {targetChannel.mode !== "videowall" ? (
              <>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">
                    {targetChannel.mode === "expanded" ? "Resolución por salida" : "Resolución ancho"}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={resolutionWidth}
                    onChange={(event) => setResolutionWidth(Math.max(1, Number(event.target.value) || 1))}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">
                    {targetChannel.mode === "expanded" ? "Resolución alto por salida" : "Resolución alto"}
                  </label>
                  <input
                    type="number"
                    min={1}
                    value={resolutionHeight}
                    onChange={(event) => setResolutionHeight(Math.max(1, Number(event.target.value) || 1))}
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

            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Hardware opcional</label>
              <input value={hardwareIdentifier} onChange={(event) => setHardwareIdentifier(event.target.value)} placeholder="Mini PC lobby" />
            </div>
          </div>

          {targetChannel.mode !== "videowall" ? (
            <LedOutputMappingPanel
              value={outputMapping}
              fallbackWidth={resolutionWidth}
              fallbackHeight={resolutionHeight}
              onChange={setOutputMapping}
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
