import type {
  ChannelOutputMapping,
  OutputMappingMode,
  OutputMappingProfile,
  OutputMappingSlice,
  OutputMappingSliceDirection,
} from "../../types/domain";

export type ChannelOutputMappingDraft = ChannelOutputMapping;

const PROFILE_OPTIONS: Array<{
  id: OutputMappingProfile;
  label: string;
  description: string;
}> = [
  {
    id: "normal",
    label: "Pantalla normal",
    description: "Usa la salida HDMI tal como la detecta Windows. Ideal para monitores y TVs tradicionales.",
  },
  {
    id: "led_wide",
    label: "LED panoramico / franjas",
    description: "Empaca un canvas LED raro dentro del HDMI usando varias franjas calibrables.",
  },
  {
    id: "custom",
    label: "Configuracion avanzada",
    description: "Usa un bloque libre para mover, recortar y escalar el contenido manualmente.",
  },
];

const DIRECTION_OPTIONS: Array<{
  id: OutputMappingSliceDirection;
  label: string;
  description: string;
}> = [
  {
    id: "horizontal_stack",
    label: "Canvas horizontal -> arriba / abajo",
    description: "Parte el canvas por ancho y coloca las franjas una debajo de otra dentro del HDMI.",
  },
  {
    id: "vertical_stack",
    label: "Canvas vertical -> izquierda / derecha",
    description: "Parte el canvas por alto y coloca las franjas lado a lado dentro del HDMI.",
  },
];

const SLICE_STYLES = [
  {
    border: "border-cyan-300/90",
    background: "bg-cyan-400/18",
    label: "bg-cyan-400/15 text-cyan-100 border-cyan-300/40",
    badge: "border-cyan-200 bg-cyan-50 text-cyan-800",
  },
  {
    border: "border-emerald-300/90",
    background: "bg-emerald-400/18",
    label: "bg-emerald-400/15 text-emerald-100 border-emerald-300/40",
    badge: "border-emerald-200 bg-emerald-50 text-emerald-800",
  },
  {
    border: "border-violet-300/90",
    background: "bg-violet-400/18",
    label: "bg-violet-400/15 text-violet-100 border-violet-300/40",
    badge: "border-violet-200 bg-violet-50 text-violet-800",
  },
  {
    border: "border-amber-300/90",
    background: "bg-amber-400/18",
    label: "bg-amber-400/15 text-amber-100 border-amber-300/40",
    badge: "border-amber-200 bg-amber-50 text-amber-800",
  },
];

function positiveInt(value: unknown, fallback: number) {
  if (value === null || value === undefined || value === "") {
    return Math.max(1, Math.round(fallback));
  }

  const candidate = Number(value);
  if (!Number.isFinite(candidate) || candidate <= 0) {
    return Math.max(1, Math.round(fallback));
  }

  return Math.max(1, Math.round(candidate));
}

function nonNegativeInt(value: unknown, fallback: number) {
  if (value === null || value === undefined || value === "") {
    return Math.max(0, Math.round(fallback));
  }

  const candidate = Number(value);
  if (!Number.isFinite(candidate)) {
    return Math.max(0, Math.round(fallback));
  }

  return Math.max(0, Math.round(candidate));
}

function signedInt(value: unknown, fallback: number) {
  if (value === null || value === undefined || value === "") {
    return Math.round(fallback);
  }

  const candidate = Number(value);
  if (!Number.isFinite(candidate)) {
    return Math.round(fallback);
  }

  return Math.round(candidate);
}

function positiveNumber(value: unknown, fallback: number) {
  if (value === null || value === undefined || value === "") {
    return Number(fallback.toFixed(6));
  }

  const candidate = Number(value);
  if (!Number.isFinite(candidate) || candidate <= 0) {
    return Number(fallback.toFixed(6));
  }

  return Number(candidate.toFixed(6));
}

function roundScale(value: number) {
  return Number(value.toFixed(6));
}

function normalizeMode(rawMode: unknown, profile: OutputMappingProfile): OutputMappingMode {
  if (rawMode === "normal" || rawMode === "sliced" || rawMode === "custom") {
    return rawMode;
  }

  if (rawMode === "split_horizontal") {
    return "sliced";
  }

  if (rawMode === "contain" || rawMode === "cover") {
    return profile === "custom" ? "custom" : "normal";
  }

  if (profile === "led_wide") {
    return "sliced";
  }

  if (profile === "custom") {
    return "custom";
  }

  return "normal";
}

function normalizeDirection(rawDirection: unknown, fallback: OutputMappingSliceDirection): OutputMappingSliceDirection {
  return rawDirection === "horizontal_stack" || rawDirection === "vertical_stack" ? rawDirection : fallback;
}

function distribute(total: number, count: number) {
  const safeTotal = Math.max(1, total);
  const safeCount = Math.max(1, count);
  const base = Math.floor(safeTotal / safeCount);
  const remainder = safeTotal - base * safeCount;

  return Array.from({ length: safeCount }, (_, index) => base + (index < remainder ? 1 : 0));
}

function fitBox(width: number, height: number, maxWidth: number, maxHeight: number) {
  const safeWidth = Math.max(1, width);
  const safeHeight = Math.max(1, height);
  const scale = Math.min(maxWidth / safeWidth, maxHeight / safeHeight);

  return {
    width: Math.max(80, Math.round(safeWidth * scale)),
    height: Math.max(80, Math.round(safeHeight * scale)),
  };
}

function getAspectPreviewLabel(width: number, height: number) {
  const ratio = width / Math.max(1, height);

  if (ratio >= 2.1) {
    return {
      label: "Ultra wide",
      description: "Lienzo muy panoramico para LED horizontal o banners digitales extendidos.",
    };
  }

  if (ratio <= 0.8) {
    return {
      label: "Vertical",
      description: "Formato tipo totem o carteleria vertical con prioridad en altura.",
    };
  }

  if (ratio >= 0.9 && ratio <= 1.15) {
    return {
      label: "Cuadrado",
      description: "Relacion casi 1:1 para pantallas cuadradas o composiciones compactas.",
    };
  }

  return {
    label: "16:9",
    description: "Formato horizontal tradicional para displays, TVs y senalizacion estandar.",
  };
}

function getSuggestedSliceDirection(
  outputWidth: number,
  outputHeight: number,
  canvasWidth: number,
  canvasHeight: number,
): OutputMappingSliceDirection {
  if (canvasWidth > outputWidth && canvasHeight <= outputHeight) {
    return "horizontal_stack";
  }

  if (canvasHeight > outputHeight && canvasWidth <= outputWidth) {
    return "vertical_stack";
  }

  return canvasWidth >= canvasHeight ? "horizontal_stack" : "vertical_stack";
}

function getSuggestedSliceCount(
  outputWidth: number,
  outputHeight: number,
  canvasWidth: number,
  canvasHeight: number,
  direction: OutputMappingSliceDirection,
) {
  if (direction === "horizontal_stack") {
    return Math.max(1, Math.ceil(canvasWidth / Math.max(1, outputWidth)));
  }

  return Math.max(1, Math.ceil(canvasHeight / Math.max(1, outputHeight)));
}

function buildAutoSlices(draft: {
  output_width: number;
  output_height: number;
  source_canvas_width: number;
  source_canvas_height: number;
  slice_count: number;
  slice_direction: OutputMappingSliceDirection;
}) {
  const outputWidth = Math.max(1, draft.output_width);
  const outputHeight = Math.max(1, draft.output_height);
  const canvasWidth = Math.max(1, draft.source_canvas_width);
  const canvasHeight = Math.max(1, draft.source_canvas_height);
  const sliceCount = Math.max(1, draft.slice_count);

  if (draft.slice_direction === "horizontal_stack") {
    const sourceWidths = distribute(canvasWidth, sliceCount);
    let sourceCursor = 0;
    let outputCursor = 0;

    return sourceWidths.map((sourceWidth, index) => {
      const scale = Math.min(1, outputWidth / Math.max(1, sourceWidth));
      const outputSliceWidth = Math.max(1, Math.round(sourceWidth * scale));
      const outputSliceHeight = Math.max(1, Math.round(canvasHeight * scale));
      const slice: OutputMappingSlice = {
        slice_index: index + 1,
        source_x: sourceCursor,
        source_y: 0,
        source_width: sourceWidth,
        source_height: canvasHeight,
        output_x: 0,
        output_y: outputCursor,
        output_width: outputSliceWidth,
        output_height: outputSliceHeight,
        scale_x: roundScale(outputSliceWidth / Math.max(1, sourceWidth)),
        scale_y: roundScale(outputSliceHeight / Math.max(1, canvasHeight)),
      };
      sourceCursor += sourceWidth;
      outputCursor += outputSliceHeight;
      return slice;
    });
  }

  const sourceHeights = distribute(canvasHeight, sliceCount);
  let sourceCursor = 0;
  let outputCursor = 0;

  return sourceHeights.map((sourceHeight, index) => {
    const scale = Math.min(1, outputHeight / Math.max(1, sourceHeight));
    const outputSliceWidth = Math.max(1, Math.round(canvasWidth * scale));
    const outputSliceHeight = Math.max(1, Math.round(sourceHeight * scale));
    const slice: OutputMappingSlice = {
      slice_index: index + 1,
      source_x: 0,
      source_y: sourceCursor,
      source_width: canvasWidth,
      source_height: sourceHeight,
      output_x: outputCursor,
      output_y: 0,
      output_width: outputSliceWidth,
      output_height: outputSliceHeight,
      scale_x: roundScale(outputSliceWidth / Math.max(1, canvasWidth)),
      scale_y: roundScale(outputSliceHeight / Math.max(1, sourceHeight)),
    };
    sourceCursor += sourceHeight;
    outputCursor += outputSliceWidth;
    return slice;
  });
}

function buildCustomDefaultSlice(outputWidth: number, outputHeight: number, canvasWidth: number, canvasHeight: number): OutputMappingSlice {
  const scale = Math.min(outputWidth / Math.max(1, canvasWidth), outputHeight / Math.max(1, canvasHeight));
  const scaledWidth = Math.max(1, Math.round(canvasWidth * scale));
  const scaledHeight = Math.max(1, Math.round(canvasHeight * scale));

  return {
    slice_index: 1,
    source_x: 0,
    source_y: 0,
    source_width: canvasWidth,
    source_height: canvasHeight,
    output_x: Math.round((outputWidth - scaledWidth) / 2),
    output_y: Math.round((outputHeight - scaledHeight) / 2),
    output_width: scaledWidth,
    output_height: scaledHeight,
    scale_x: roundScale(scaledWidth / Math.max(1, canvasWidth)),
    scale_y: roundScale(scaledHeight / Math.max(1, canvasHeight)),
  };
}

function normalizeSlice(rawSlice: Partial<OutputMappingSlice> | null | undefined, fallback: OutputMappingSlice): OutputMappingSlice {
  return {
    slice_index: positiveInt(rawSlice?.slice_index ?? fallback.slice_index, fallback.slice_index),
    source_x: nonNegativeInt(rawSlice?.source_x, fallback.source_x),
    source_y: nonNegativeInt(rawSlice?.source_y, fallback.source_y),
    source_width: positiveInt(rawSlice?.source_width, fallback.source_width),
    source_height: positiveInt(rawSlice?.source_height, fallback.source_height),
    output_x: signedInt(rawSlice?.output_x, fallback.output_x),
    output_y: signedInt(rawSlice?.output_y, fallback.output_y),
    output_width: positiveInt(rawSlice?.output_width, fallback.output_width),
    output_height: positiveInt(rawSlice?.output_height, fallback.output_height),
    scale_x: positiveNumber(rawSlice?.scale_x, fallback.scale_x),
    scale_y: positiveNumber(rawSlice?.scale_y, fallback.scale_y),
  };
}

function summarizeSlices(slices: OutputMappingSlice[], fallbackWidth: number, fallbackHeight: number) {
  if (!slices.length) {
    return {
      viewport_x: 0,
      viewport_y: 0,
      viewport_width: fallbackWidth,
      viewport_height: fallbackHeight,
      scale_x: 1,
      scale_y: 1,
    };
  }

  const minX = Math.min(...slices.map((slice) => slice.output_x));
  const minY = Math.min(...slices.map((slice) => slice.output_y));
  const maxX = Math.max(...slices.map((slice) => slice.output_x + slice.output_width));
  const maxY = Math.max(...slices.map((slice) => slice.output_y + slice.output_height));
  const firstSlice = slices[0];

  return {
    viewport_x: minX,
    viewport_y: minY,
    viewport_width: Math.max(1, maxX - minX),
    viewport_height: Math.max(1, maxY - minY),
    scale_x: firstSlice?.scale_x ?? 1,
    scale_y: firstSlice?.scale_y ?? 1,
  };
}

function normalizeDraft(draft: Partial<ChannelOutputMappingDraft>, fallbackWidth: number, fallbackHeight: number): ChannelOutputMappingDraft {
  const outputWidth = positiveInt(draft.output_width ?? fallbackWidth, fallbackWidth);
  const outputHeight = positiveInt(draft.output_height ?? fallbackHeight, fallbackHeight);
  const physicalWidth = positiveInt(draft.physical_width ?? outputWidth, outputWidth);
  const physicalHeight = positiveInt(draft.physical_height ?? outputHeight, outputHeight);
  const canvasWidth = positiveInt(draft.source_canvas_width ?? draft.canvas_width ?? physicalWidth, physicalWidth);
  const canvasHeight = positiveInt(draft.source_canvas_height ?? draft.canvas_height ?? physicalHeight, physicalHeight);

  let profile = (draft.profile ?? (draft.enabled ? "custom" : "normal")) as OutputMappingProfile;
  let mode = normalizeMode(draft.mapping_mode ?? draft.mode, profile);

  if (mode === "normal") {
    profile = "normal";
  } else if (mode === "sliced") {
    profile = "led_wide";
  } else {
    profile = "custom";
  }

  const suggestedDirection = getSuggestedSliceDirection(outputWidth, outputHeight, canvasWidth, canvasHeight);
  const sliceDirection = normalizeDirection(draft.slice_direction, suggestedDirection);
  const suggestedSliceCount = getSuggestedSliceCount(outputWidth, outputHeight, canvasWidth, canvasHeight, sliceDirection);
  const sliceCount = mode === "sliced" ? positiveInt(draft.slice_count ?? suggestedSliceCount, suggestedSliceCount) : 1;
  const rawSlices = Array.isArray(draft.slices) ? draft.slices : [];

  if (mode === "normal") {
    return {
      enabled: false,
      profile,
      mode,
      mapping_mode: mode,
      slice_count: 1,
      slice_direction: suggestedDirection,
      output_width: outputWidth,
      output_height: outputHeight,
      physical_width: physicalWidth,
      physical_height: physicalHeight,
      source_canvas_width: outputWidth,
      source_canvas_height: outputHeight,
      canvas_width: outputWidth,
      canvas_height: outputHeight,
      slices: [],
      viewport_x: 0,
      viewport_y: 0,
      viewport_width: outputWidth,
      viewport_height: outputHeight,
      scale_x: 1,
      scale_y: 1,
    };
  }

  if (mode === "sliced") {
    const autoSlices = buildAutoSlices({
      output_width: outputWidth,
      output_height: outputHeight,
      source_canvas_width: canvasWidth,
      source_canvas_height: canvasHeight,
      slice_count: sliceCount,
      slice_direction: sliceDirection,
    });
    const slices = autoSlices.map((fallbackSlice, index) => normalizeSlice(rawSlices[index], fallbackSlice));
    const summary = summarizeSlices(slices, outputWidth, outputHeight);

    return {
      enabled: true,
      profile,
      mode,
      mapping_mode: mode,
      slice_count: sliceCount,
      slice_direction: sliceDirection,
      output_width: outputWidth,
      output_height: outputHeight,
      physical_width: physicalWidth,
      physical_height: physicalHeight,
      source_canvas_width: canvasWidth,
      source_canvas_height: canvasHeight,
      canvas_width: canvasWidth,
      canvas_height: canvasHeight,
      slices,
      viewport_x: summary.viewport_x,
      viewport_y: summary.viewport_y,
      viewport_width: summary.viewport_width,
      viewport_height: summary.viewport_height,
      scale_x: summary.scale_x,
      scale_y: summary.scale_y,
    };
  }

  const fallbackSlice = buildCustomDefaultSlice(outputWidth, outputHeight, canvasWidth, canvasHeight);
  const rawPrimarySlice = rawSlices[0] ?? {
    slice_index: 1,
    source_x: 0,
    source_y: 0,
    source_width: canvasWidth,
    source_height: canvasHeight,
    output_x: draft.viewport_x,
    output_y: draft.viewport_y,
    output_width: draft.viewport_width,
    output_height: draft.viewport_height,
    scale_x: draft.scale_x,
    scale_y: draft.scale_y,
  };
  const primarySlice = normalizeSlice(rawPrimarySlice, fallbackSlice);

  return {
    enabled: true,
    profile,
    mode,
    mapping_mode: mode,
    slice_count: 1,
    slice_direction: suggestedDirection,
    output_width: outputWidth,
    output_height: outputHeight,
    physical_width: physicalWidth,
    physical_height: physicalHeight,
    source_canvas_width: canvasWidth,
    source_canvas_height: canvasHeight,
    canvas_width: canvasWidth,
    canvas_height: canvasHeight,
    slices: [primarySlice],
    viewport_x: primarySlice.output_x,
    viewport_y: primarySlice.output_y,
    viewport_width: primarySlice.output_width,
    viewport_height: primarySlice.output_height,
    scale_x: primarySlice.scale_x,
    scale_y: primarySlice.scale_y,
  };
}

export function buildOutputMappingDraft(
  mapping: Partial<ChannelOutputMapping> | null | undefined,
  fallbackWidth: number,
  fallbackHeight: number,
): ChannelOutputMappingDraft {
  return normalizeDraft(mapping ?? {}, fallbackWidth, fallbackHeight);
}

function profileCardClassName(active: boolean) {
  return [
    "rounded-[20px] border px-4 py-4 text-left transition",
    active
      ? "border-slate-900 bg-slate-900 text-white shadow-sm"
      : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:text-ink",
  ].join(" ");
}

function directionCardClassName(active: boolean) {
  return [
    "rounded-[18px] border px-4 py-4 text-left transition",
    active
      ? "border-cyan-300 bg-cyan-50 text-cyan-950 shadow-sm"
      : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:text-ink",
  ].join(" ");
}

function getProfileLabel(profile: OutputMappingProfile) {
  return PROFILE_OPTIONS.find((option) => option.id === profile)?.label ?? "Pantalla normal";
}

function getOverflowMessage(value: ChannelOutputMappingDraft) {
  if (value.mapping_mode !== "sliced" || !value.slices.length) {
    return null;
  }

  const maxRight = Math.max(...value.slices.map((slice) => slice.output_x + slice.output_width));
  const maxBottom = Math.max(...value.slices.map((slice) => slice.output_y + slice.output_height));

  if (value.slice_direction === "horizontal_stack" && maxBottom > value.output_height) {
    return "Las franjas no caben completas en la salida HDMI. Reduce altura, aumenta escala o usa configuracion avanzada.";
  }

  if (value.slice_direction === "vertical_stack" && maxRight > value.output_width) {
    return "Las franjas no caben completas en la salida HDMI. Reduce ancho, aumenta escala o usa configuracion avanzada.";
  }

  return null;
}

export function LedOutputMappingPanel({
  value,
  fallbackWidth,
  fallbackHeight,
  disabled = false,
  onChange,
}: {
  value: ChannelOutputMappingDraft;
  fallbackWidth: number;
  fallbackHeight: number;
  disabled?: boolean;
  onChange: (nextValue: ChannelOutputMappingDraft) => void;
}) {
  const aspectPreview = getAspectPreviewLabel(value.source_canvas_width, value.source_canvas_height);
  const previewFrame = fitBox(value.output_width, value.output_height, 620, 320);
  const previewCanvas = fitBox(value.source_canvas_width, value.source_canvas_height, 180, 72);
  const overflowMessage = getOverflowMessage(value);
  const primarySlice = value.slices[0] ?? null;

  function commit(nextDraft: Partial<ChannelOutputMappingDraft>) {
    onChange(normalizeDraft({ ...value, ...nextDraft }, fallbackWidth, fallbackHeight));
  }

  function commitSlicedWithAutoLayout(nextDraft: Partial<ChannelOutputMappingDraft>) {
    const normalized = normalizeDraft(
      {
        ...value,
        ...nextDraft,
        enabled: true,
        profile: "led_wide",
        mode: "sliced",
        mapping_mode: "sliced",
      },
      fallbackWidth,
      fallbackHeight,
    );

    const rebuilt = normalizeDraft(
      {
        ...normalized,
        slices: buildAutoSlices(normalized),
      },
      fallbackWidth,
      fallbackHeight,
    );

    onChange(rebuilt);
  }

  function commitCustomWithDefault(nextDraft: Partial<ChannelOutputMappingDraft>) {
    const normalized = normalizeDraft(
      {
        ...value,
        ...nextDraft,
        enabled: true,
        profile: "custom",
        mode: "custom",
        mapping_mode: "custom",
      },
      fallbackWidth,
      fallbackHeight,
    );
    onChange(normalized);
  }

  function applyProfile(profile: OutputMappingProfile) {
    if (profile === "normal") {
      onChange(
        normalizeDraft(
          {
            ...value,
            enabled: false,
            profile: "normal",
            mode: "normal",
            mapping_mode: "normal",
            output_width: fallbackWidth,
            output_height: fallbackHeight,
            source_canvas_width: fallbackWidth,
            source_canvas_height: fallbackHeight,
            physical_width: fallbackWidth,
            physical_height: fallbackHeight,
            slice_count: 1,
            slices: [],
          },
          fallbackWidth,
          fallbackHeight,
        ),
      );
      return;
    }

    if (profile === "led_wide") {
      commitSlicedWithAutoLayout({
        output_width: value.output_width || fallbackWidth,
        output_height: value.output_height || fallbackHeight,
        source_canvas_width: value.source_canvas_width || value.physical_width || fallbackWidth,
        source_canvas_height: value.source_canvas_height || value.physical_height || fallbackHeight,
      });
      return;
    }

    commitCustomWithDefault({
      output_width: value.output_width || fallbackWidth,
      output_height: value.output_height || fallbackHeight,
      source_canvas_width: value.source_canvas_width || value.physical_width || fallbackWidth,
      source_canvas_height: value.source_canvas_height || value.physical_height || fallbackHeight,
    });
  }

  function updateSlice(sliceIndex: number, partial: Partial<OutputMappingSlice>) {
    const currentSlice = value.slices[sliceIndex];
    if (!currentSlice) {
      return;
    }

    const nextSlices = value.slices.map((slice, index) => (index === sliceIndex ? { ...slice, ...partial } : slice));
    onChange(normalizeDraft({ ...value, slices: nextSlices }, fallbackWidth, fallbackHeight));
  }

  function resetSlice(sliceIndex: number) {
    if (value.mapping_mode === "sliced") {
      const defaults = buildAutoSlices(value);
      const fallbackSlice = defaults[sliceIndex];
      if (!fallbackSlice) {
        return;
      }
      updateSlice(sliceIndex, fallbackSlice);
      return;
    }

    const fallbackSlice = buildCustomDefaultSlice(
      value.output_width,
      value.output_height,
      value.source_canvas_width,
      value.source_canvas_height,
    );
    updateSlice(sliceIndex, fallbackSlice);
  }

  function centerSlice(sliceIndex: number) {
    const slice = value.slices[sliceIndex];
    if (!slice) {
      return;
    }

    updateSlice(sliceIndex, {
      output_x: Math.round((value.output_width - slice.output_width) / 2),
      output_y: Math.round((value.output_height - slice.output_height) / 2),
    });
  }

  function nudgeSlice(sliceIndex: number, deltaX: number, deltaY: number) {
    const slice = value.slices[sliceIndex];
    if (!slice) {
      return;
    }

    updateSlice(sliceIndex, {
      output_x: slice.output_x + deltaX,
      output_y: slice.output_y + deltaY,
    });
  }

  function renderSliceCard(slice: OutputMappingSlice, index: number) {
    const style = SLICE_STYLES[index % SLICE_STYLES.length];

    return (
      <div key={`slice-card-${slice.slice_index}`} className="rounded-[20px] border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Franja {slice.slice_index}</p>
            <p className="mt-1 text-sm text-slate-600">Ajusta origen, posicion HDMI y escala de esta franja.</p>
          </div>
          <span className={["rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]", style.badge].join(" ")}>
            Slice {slice.slice_index}
          </span>
        </div>

        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <div className="rounded-[18px] border border-slate-200 bg-slate-50 p-4">
            <p className="text-xs uppercase tracking-[0.16em] text-slate-500">Origen del contenido</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-sm font-semibold text-slate-700">Source X</label>
                <input
                  disabled={disabled}
                  type="number"
                  value={slice.source_x}
                  onChange={(event) => updateSlice(index, { source_x: nonNegativeInt(event.target.value, slice.source_x) })}
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-semibold text-slate-700">Source Y</label>
                <input
                  disabled={disabled}
                  type="number"
                  value={slice.source_y}
                  onChange={(event) => updateSlice(index, { source_y: nonNegativeInt(event.target.value, slice.source_y) })}
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-semibold text-slate-700">Source ancho</label>
                <input
                  disabled={disabled}
                  type="number"
                  min={1}
                  value={slice.source_width}
                  onChange={(event) => updateSlice(index, { source_width: positiveInt(event.target.value, slice.source_width) })}
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-semibold text-slate-700">Source alto</label>
                <input
                  disabled={disabled}
                  type="number"
                  min={1}
                  value={slice.source_height}
                  onChange={(event) => updateSlice(index, { source_height: positiveInt(event.target.value, slice.source_height) })}
                />
              </div>
            </div>
          </div>

          <div className="rounded-[18px] border border-slate-200 bg-slate-50 p-4">
            <p className="text-xs uppercase tracking-[0.16em] text-slate-500">Posicion en HDMI</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-sm font-semibold text-slate-700">Output X</label>
                <input
                  disabled={disabled}
                  type="number"
                  value={slice.output_x}
                  onChange={(event) => updateSlice(index, { output_x: signedInt(event.target.value, slice.output_x) })}
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-semibold text-slate-700">Output Y</label>
                <input
                  disabled={disabled}
                  type="number"
                  value={slice.output_y}
                  onChange={(event) => updateSlice(index, { output_y: signedInt(event.target.value, slice.output_y) })}
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-semibold text-slate-700">Output ancho</label>
                <input
                  disabled={disabled}
                  type="number"
                  min={1}
                  value={slice.output_width}
                  onChange={(event) => updateSlice(index, { output_width: positiveInt(event.target.value, slice.output_width) })}
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-semibold text-slate-700">Output alto</label>
                <input
                  disabled={disabled}
                  type="number"
                  min={1}
                  value={slice.output_height}
                  onChange={(event) => updateSlice(index, { output_height: positiveInt(event.target.value, slice.output_height) })}
                />
              </div>
            </div>
          </div>
        </div>

        <div className="mt-4 rounded-[18px] border border-slate-200 bg-slate-50 p-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <div>
              <label className="mb-1 block text-sm font-semibold text-slate-700">Scale X</label>
              <input
                disabled={disabled}
                type="number"
                step="0.01"
                min={0.01}
                value={slice.scale_x}
                onChange={(event) => updateSlice(index, { scale_x: positiveNumber(event.target.value, slice.scale_x) })}
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-semibold text-slate-700">Scale Y</label>
              <input
                disabled={disabled}
                type="number"
                step="0.01"
                min={0.01}
                value={slice.scale_y}
                onChange={(event) => updateSlice(index, { scale_y: positiveNumber(event.target.value, slice.scale_y) })}
              />
            </div>
            <div className="rounded-[16px] border border-slate-200 bg-white px-3 py-3">
              <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Origen</p>
              <p className="mt-2 text-sm font-semibold text-ink">
                {slice.source_width}x{slice.source_height}
              </p>
            </div>
            <div className="rounded-[16px] border border-slate-200 bg-white px-3 py-3">
              <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Salida</p>
              <p className="mt-2 text-sm font-semibold text-ink">
                {slice.output_width}x{slice.output_height}
              </p>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            <button disabled={disabled} type="button" className="rounded-full border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink" onClick={() => centerSlice(index)}>
              Centrar
            </button>
            <button disabled={disabled} type="button" className="rounded-full border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink" onClick={() => nudgeSlice(index, 0, -1)}>
              Subir 1px
            </button>
            <button disabled={disabled} type="button" className="rounded-full border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink" onClick={() => nudgeSlice(index, 0, 1)}>
              Bajar 1px
            </button>
            <button disabled={disabled} type="button" className="rounded-full border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink" onClick={() => nudgeSlice(index, -1, 0)}>
              Izquierda 1px
            </button>
            <button disabled={disabled} type="button" className="rounded-full border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink" onClick={() => nudgeSlice(index, 1, 0)}>
              Derecha 1px
            </button>
            <button disabled={disabled} type="button" className="rounded-full border border-rose-200 bg-rose-50 px-3 py-2 text-sm font-semibold text-rose-700 transition hover:border-rose-300 hover:text-rose-800" onClick={() => resetSlice(index)}>
              Reset
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-[24px] border border-slate-200 bg-slate-50 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Resolucion real / LED mapping</p>
          <p className="mt-1 text-sm text-slate-600">
            Configura como empacar un canvas LED raro dentro del HDMI sin cambiar la reproduccion actual.
          </p>
        </div>
        <span className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
          {getProfileLabel(value.profile)}
        </span>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-3">
        {PROFILE_OPTIONS.map((option) => {
          const active = value.profile === option.id;

          return (
            <button
              key={option.id}
              className={profileCardClassName(active)}
              disabled={disabled}
              type="button"
              onClick={() => applyProfile(option.id)}
            >
              <p className={["text-sm font-semibold", active ? "text-white" : "text-ink"].join(" ")}>{option.label}</p>
              <p className={["mt-2 text-sm leading-5", active ? "text-slate-200" : "text-slate-600"].join(" ")}>
                {option.description}
              </p>
            </button>
          );
        })}
      </div>

      <div className="mt-4 rounded-[20px] border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        Esta configuracion aun no modifica la reproduccion. Solo guarda metadata para el Player.
      </div>

      <div className="mt-4 rounded-[20px] border border-cyan-200 bg-cyan-50 px-4 py-3 text-sm text-cyan-950">
        <p className="font-semibold">
          Este modo no cambia la resolucion de Windows. Empaca el contenido real dentro de la salida HDMI para que el procesador LED tome la zona correcta.
        </p>
        <p className="mt-1">Los cambios de X/Y sirven para calibrar lo que realmente esta viendo el procesador LED.</p>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Salida HDMI ancho</label>
          <input
            disabled={disabled}
            type="number"
            min={1}
            value={value.output_width}
            onChange={(event) =>
              value.mapping_mode === "sliced"
                ? commitSlicedWithAutoLayout({ output_width: positiveInt(event.target.value, value.output_width) })
                : commit({ output_width: positiveInt(event.target.value, value.output_width) })
            }
          />
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Salida HDMI alto</label>
          <input
            disabled={disabled}
            type="number"
            min={1}
            value={value.output_height}
            onChange={(event) =>
              value.mapping_mode === "sliced"
                ? commitSlicedWithAutoLayout({ output_height: positiveInt(event.target.value, value.output_height) })
                : commit({ output_height: positiveInt(event.target.value, value.output_height) })
            }
          />
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">LED real ancho</label>
          <input
            disabled={disabled}
            type="number"
            min={1}
            value={value.physical_width}
            onChange={(event) => commit({ physical_width: positiveInt(event.target.value, value.physical_width) })}
          />
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">LED real alto</label>
          <input
            disabled={disabled}
            type="number"
            min={1}
            value={value.physical_height}
            onChange={(event) => commit({ physical_height: positiveInt(event.target.value, value.physical_height) })}
          />
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Canvas fuente ancho</label>
          <input
            disabled={disabled}
            type="number"
            min={1}
            value={value.source_canvas_width}
            onChange={(event) =>
              value.mapping_mode === "sliced"
                ? commitSlicedWithAutoLayout({ source_canvas_width: positiveInt(event.target.value, value.source_canvas_width) })
                : commit({ source_canvas_width: positiveInt(event.target.value, value.source_canvas_width) })
            }
          />
        </div>
        <div>
          <label className="mb-2 block text-sm font-semibold text-slate-700">Canvas fuente alto</label>
          <input
            disabled={disabled}
            type="number"
            min={1}
            value={value.source_canvas_height}
            onChange={(event) =>
              value.mapping_mode === "sliced"
                ? commitSlicedWithAutoLayout({ source_canvas_height: positiveInt(event.target.value, value.source_canvas_height) })
                : commit({ source_canvas_height: positiveInt(event.target.value, value.source_canvas_height) })
            }
          />
        </div>
      </div>

      {value.mapping_mode === "sliced" ? (
        <div className="mt-4 rounded-[20px] border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">LED panoramico / franjas</p>
              <p className="mt-1 text-sm text-slate-600">
                Usa varias franjas para meter un canvas ancho o raro dentro de un HDMI fijo.
              </p>
            </div>
            <button
              disabled={disabled}
              type="button"
              className="rounded-full border border-slate-200 bg-slate-50 px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink"
              onClick={() => commitSlicedWithAutoLayout({})}
            >
              Recalcular franjas
            </button>
          </div>

          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Cantidad de franjas</label>
              <input
                disabled={disabled}
                type="number"
                min={1}
                value={value.slice_count}
                onChange={(event) => commitSlicedWithAutoLayout({ slice_count: positiveInt(event.target.value, value.slice_count) })}
              />
              <p className="mt-1 text-xs text-slate-500">
                Sugerencia automatica: {getSuggestedSliceCount(value.output_width, value.output_height, value.source_canvas_width, value.source_canvas_height, value.slice_direction)}
              </p>
            </div>
            <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-3">
              <p className="text-sm font-semibold text-ink">Ejemplo real</p>
              <p className="mt-1 text-sm text-slate-600">
                3840x540 sobre 1920x1080 normalmente genera 2 franjas de 1920x540 apiladas dentro del HDMI.
              </p>
            </div>
          </div>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            {DIRECTION_OPTIONS.map((option) => {
              const active = value.slice_direction === option.id;
              return (
                <button
                  key={option.id}
                  disabled={disabled}
                  type="button"
                  className={directionCardClassName(active)}
                  onClick={() => commitSlicedWithAutoLayout({ slice_direction: option.id })}
                >
                  <p className={["text-sm font-semibold", active ? "text-cyan-950" : "text-ink"].join(" ")}>{option.label}</p>
                  <p className="mt-2 text-sm leading-5 text-slate-600">{option.description}</p>
                </button>
              );
            })}
          </div>

          {overflowMessage ? (
            <div className="mt-4 rounded-[18px] border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">{overflowMessage}</div>
          ) : null}
        </div>
      ) : null}

      {value.mapping_mode === "custom" && primarySlice ? (
        <div className="mt-4 rounded-[20px] border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Configuracion avanzada</p>
              <p className="mt-1 text-sm text-slate-600">
                Usa un bloque libre para decidir que parte del canvas se manda al HDMI y donde se acomoda.
              </p>
            </div>
            <button
              disabled={disabled}
              type="button"
              className="rounded-full border border-rose-200 bg-rose-50 px-3 py-2 text-sm font-semibold text-rose-700 transition hover:border-rose-300 hover:text-rose-800"
              onClick={() => resetSlice(0)}
            >
              Reset bloque
            </button>
          </div>

          <div className="mt-4 rounded-[18px] border border-slate-200 bg-slate-50 p-4">
            <p className="text-sm font-semibold text-ink">Bloque unico</p>
            <p className="mt-1 text-sm text-slate-600">
              Mueve este bloque por X/Y incluso si queda parcialmente fuera del HDMI para calibrar lo que captura el procesador LED.
            </p>
            <p className="mt-2 text-xs text-slate-500">
              En este modo, Output X/Y y Output ancho/alto funcionan como viewport_x, viewport_y, viewport_width y viewport_height.
            </p>
          </div>

          {renderSliceCard(primarySlice, 0)}
        </div>
      ) : null}

      {value.mapping_mode === "sliced" ? (
        <div className="mt-4 space-y-4">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Calibracion de franjas</p>
            <p className="mt-1 text-sm text-slate-600">
              Ajusta cada franja por separado para que el procesador LED vea exactamente la zona correcta del HDMI.
            </p>
          </div>
          {value.slices.map((slice, index) => renderSliceCard(slice, index))}
        </div>
      ) : null}

      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-[18px] border border-slate-200 bg-white px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Salida HDMI</p>
          <p className="mt-2 text-sm font-semibold text-ink">
            {value.output_width}x{value.output_height}
          </p>
        </div>
        <div className="rounded-[18px] border border-slate-200 bg-white px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Canvas real</p>
          <p className="mt-2 text-sm font-semibold text-ink">
            {value.source_canvas_width}x{value.source_canvas_height}
          </p>
        </div>
        <div className="rounded-[18px] border border-slate-200 bg-white px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Slices</p>
          <p className="mt-2 text-sm font-semibold text-ink">{value.mapping_mode === "sliced" ? value.slice_count : value.slices.length || 1}</p>
        </div>
        <div className="rounded-[18px] border border-slate-200 bg-white px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Modo runtime</p>
          <p className="mt-2 text-sm font-semibold text-ink">{value.mapping_mode}</p>
        </div>
      </div>

      <div className="mt-5 rounded-[20px] border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Preview simple</p>
            <p className="mt-1 text-sm text-slate-600">
              Marco HDMI grande con las franjas o el bloque libre exactamente donde quedarian.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-600">
              {aspectPreview.label}
            </span>
            <span className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
              {value.source_canvas_width}x{value.source_canvas_height}
            </span>
          </div>
        </div>

        <div className="mt-4 rounded-[18px] border border-slate-200 bg-slate-950/95 p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-cyan-100">{aspectPreview.label}</p>
              <p className="mt-1 text-sm text-slate-300">{aspectPreview.description}</p>
            </div>
            <div className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-200">
              HDMI {value.output_width}x{value.output_height}
            </div>
          </div>

          <div className="mt-4 rounded-[16px] border border-white/10 bg-white/5 p-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-[11px] uppercase tracking-[0.14em] text-slate-400">Canvas real</p>
                <p className="mt-1 text-sm font-semibold text-white">
                  {value.source_canvas_width}x{value.source_canvas_height}
                </p>
              </div>
              <div
                className="rounded-[14px] border border-cyan-300/40 bg-cyan-400/10"
                style={{ width: `${previewCanvas.width}px`, height: `${previewCanvas.height}px` }}
              />
            </div>
          </div>

          <div className="mt-6 grid min-h-[340px] place-items-center">
            <div
              className="relative overflow-hidden rounded-[16px] border border-slate-500 bg-slate-900 shadow-inner"
              style={{ width: `${previewFrame.width}px`, height: `${previewFrame.height}px` }}
            >
              <div className="absolute left-3 top-3 rounded-full border border-white/10 bg-slate-950/70 px-2 py-1 text-[9px] font-semibold uppercase tracking-[0.14em] text-slate-200">
                Salida HDMI
              </div>

              {value.mapping_mode === "normal" ? (
                <div className="absolute inset-[8%] rounded-[12px] border border-dashed border-cyan-300/70 bg-cyan-400/10" />
              ) : (
                value.slices.map((slice, index) => {
                  const style = SLICE_STYLES[index % SLICE_STYLES.length];
                  const left = (slice.output_x / Math.max(1, value.output_width)) * 100;
                  const top = (slice.output_y / Math.max(1, value.output_height)) * 100;
                  const width = (slice.output_width / Math.max(1, value.output_width)) * 100;
                  const height = (slice.output_height / Math.max(1, value.output_height)) * 100;

                  return (
                    <div
                      key={`preview-slice-${slice.slice_index}`}
                      className={["absolute rounded-[12px] border-2 shadow-[0_12px_30px_rgba(15,23,42,0.18)]", style.border, style.background].join(" ")}
                      style={{
                        left: `${left}%`,
                        top: `${top}%`,
                        width: `${width}%`,
                        height: `${height}%`,
                      }}
                    >
                      <div className={["absolute left-2 top-2 rounded-full border px-2 py-1 text-[9px] font-semibold uppercase tracking-[0.14em]", style.label].join(" ")}>
                        Franja {slice.slice_index}
                      </div>
                    </div>
                  );
                })
              )}

              <div className="absolute bottom-3 left-3 rounded-full border border-white/10 bg-slate-950/70 px-2 py-1 text-[9px] font-semibold uppercase tracking-[0.14em] text-slate-200">
                Preview HDMI
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
