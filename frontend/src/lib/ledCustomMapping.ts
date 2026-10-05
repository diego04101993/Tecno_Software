import type { ChannelOutputMapping } from "../types/domain";

export interface LedCustomDimensions {
  hdmiWidth: number;
  hdmiHeight: number;
  width: number;
  height: number;
  x: number;
  y: number;
}

// A preset is built from dimensions, never by merging a previous advanced mapping.
export function buildLedCustomMapping({ hdmiWidth, hdmiHeight, width, height, x, y }: LedCustomDimensions): ChannelOutputMapping {
  if (![hdmiWidth, hdmiHeight, width, height].every((value) => Number.isSafeInteger(value) && value > 0)) {
    throw new Error("Las dimensiones LED y HDMI deben ser enteros positivos.");
  }
  if (![x, y].every((value) => Number.isSafeInteger(value) && value >= 0)) {
    throw new Error("La posición X/Y debe usar enteros iguales o mayores que cero.");
  }
  if (width > hdmiWidth - x || height > hdmiHeight - y) {
    throw new Error("El canvas LED debe caber completamente dentro de la salida HDMI.");
  }

  return {
    enabled: true,
    profile: "custom",
    mode: "custom",
    mapping_mode: "custom",
    slice_count: 1,
    slice_direction: width >= height ? "horizontal_stack" : "vertical_stack",
    output_width: hdmiWidth,
    output_height: hdmiHeight,
    physical_width: width,
    physical_height: height,
    source_canvas_width: width,
    source_canvas_height: height,
    canvas_width: width,
    canvas_height: height,
    viewport_x: x,
    viewport_y: y,
    viewport_width: width,
    viewport_height: height,
    scale_x: 1,
    scale_y: 1,
    slices: [{
      slice_index: 1,
      source_x: 0,
      source_y: 0,
      source_width: width,
      source_height: height,
      output_x: x,
      output_y: y,
      output_width: width,
      output_height: height,
      scale_x: 1,
      scale_y: 1,
    }],
  };
}
