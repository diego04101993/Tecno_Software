import type { DeviceMode } from "../../types/domain";

export interface DeviceOutputsPreviewModel {
  deviceMode: DeviceMode;
  outputCount: number;
  outputWidth: number;
  outputHeight: number;
  canvasWidth: number;
  canvasHeight: number;
}

interface PreviewOutput {
  index: number;
  x: number;
  y: number;
  width: number;
  height: number;
  sourceX: number;
  sourceY: number;
  sourceWidth: number;
  sourceHeight: number;
}

function distribute(total: number, count: number) {
  const safeTotal = Math.max(1, total);
  const safeCount = Math.max(1, count);
  const base = Math.floor(safeTotal / safeCount);
  const remainder = safeTotal - base * safeCount;
  return Array.from({ length: safeCount }, (_, index) => base + (index < remainder ? 1 : 0));
}

export function getSuggestedCanvas(deviceMode: DeviceMode, outputWidth: number, outputHeight: number, outputCount: number) {
  const safeWidth = Math.max(1, outputWidth);
  const safeHeight = Math.max(1, outputHeight);
  const safeCount = Math.max(1, outputCount);

  if (deviceMode === "extended") {
    return {
      width: safeWidth * safeCount,
      height: safeHeight,
    };
  }

  return {
    width: safeWidth,
    height: safeHeight,
  };
}

function buildPreviewOutputs(model: DeviceOutputsPreviewModel): PreviewOutput[] {
  const outputCount = Math.max(1, model.outputCount);
  const outputWidth = Math.max(1, model.outputWidth);
  const outputHeight = Math.max(1, model.outputHeight);
  const canvasWidth = Math.max(1, model.canvasWidth);
  const canvasHeight = Math.max(1, model.canvasHeight);

  if (model.deviceMode === "extended") {
    const sliceWidths = distribute(canvasWidth, outputCount);
    let sourceCursor = 0;
    return sliceWidths.map((sourceWidth, index) => {
      const output = {
        index,
        x: outputWidth * index,
        y: 0,
        width: outputWidth,
        height: outputHeight,
        sourceX: sourceCursor,
        sourceY: 0,
        sourceWidth,
        sourceHeight: canvasHeight,
      };
      sourceCursor += sourceWidth;
      return output;
    });
  }

  return Array.from({ length: outputCount }, (_, index) => ({
    index,
    x: outputWidth * index,
    y: 0,
    width: outputWidth,
    height: outputHeight,
    sourceX: 0,
    sourceY: 0,
    sourceWidth: model.deviceMode === "mirror" ? canvasWidth : outputWidth,
    sourceHeight: model.deviceMode === "mirror" ? canvasHeight : outputHeight,
  }));
}

function getModeLabel(deviceMode: DeviceMode) {
  switch (deviceMode) {
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

function getModeDescription(deviceMode: DeviceMode) {
  switch (deviceMode) {
    case "mirror":
      return "Todas las salidas repiten el mismo contenido.";
    case "extended":
      return "El canvas total se reparte en slices horizontales.";
    case "independent":
      return "Preparado para campañas independientes por salida.";
    default:
      return "Una sola salida física controlada por el canal raíz.";
  }
}

export function DeviceOutputsPreview({ model }: { model: DeviceOutputsPreviewModel }) {
  const outputs = buildPreviewOutputs(model);
  const desktopWidth = Math.max(model.outputWidth * Math.max(1, model.outputCount), model.outputWidth);
  const desktopHeight = Math.max(model.outputHeight, model.canvasHeight);
  const scale = Math.min(1, 320 / desktopWidth, 180 / desktopHeight);

  return (
    <div className="rounded-[24px] border border-slate-200 bg-slate-50/90 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Preview simple</p>
          <p className="mt-2 text-sm font-semibold text-ink">{getModeLabel(model.deviceMode)}</p>
          <p className="mt-1 text-sm text-slate-600">{getModeDescription(model.deviceMode)}</p>
        </div>
        <div className="flex flex-wrap gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-600">
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1.5">{Math.max(1, model.outputCount)} salida(s)</span>
          <span className="rounded-full border border-slate-200 bg-white px-3 py-1.5">
            Canvas {model.canvasWidth}x{model.canvasHeight}
          </span>
        </div>
      </div>

      <div className="mt-4 rounded-[22px] border border-slate-300 bg-slate-900 p-4 text-white">
        <div className="flex items-center justify-between gap-3 text-[11px] uppercase tracking-[0.18em] text-slate-300">
          <span>Desktop físico</span>
          <span>
            {desktopWidth}x{desktopHeight}
          </span>
        </div>
        <div className="mt-4 overflow-hidden rounded-[20px] border border-slate-700 bg-slate-950/70 p-4">
          <div
            className="relative rounded-[18px] border border-cyan-400/60 bg-cyan-950/40"
            style={{
              width: `${desktopWidth * scale}px`,
              height: `${desktopHeight * scale}px`,
              minHeight: "72px",
            }}
          >
            {outputs.map((output) => (
              <div
                key={`${model.deviceMode}-${output.index}`}
                className="absolute overflow-hidden rounded-[14px] border border-white/30 bg-white/10 p-2"
                style={{
                  left: `${output.x * scale}px`,
                  top: `${output.y * scale}px`,
                  width: `${Math.max(30, output.width * scale)}px`,
                  height: `${Math.max(30, output.height * scale)}px`,
                }}
              >
                <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-white/90">Salida {output.index + 1}</p>
                <p className="mt-1 text-[10px] text-slate-200">
                  {output.width}x{output.height}
                </p>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {outputs.map((output) => (
          <div key={`summary-${output.index}`} className="rounded-[18px] border border-slate-200 bg-white px-4 py-3">
            <p className="text-[11px] uppercase tracking-[0.18em] text-slate-400">Salida {output.index + 1}</p>
            <p className="mt-2 text-sm font-semibold text-ink">
              x:{output.x} · y:{output.y}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              {output.width}x{output.height}
            </p>
            <p className="mt-2 text-xs text-slate-500">
              source {output.sourceX},{output.sourceY} · {output.sourceWidth}x{output.sourceHeight}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
