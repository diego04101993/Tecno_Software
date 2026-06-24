from __future__ import annotations

from collections.abc import Mapping
from math import ceil


ALLOWED_OUTPUT_MAPPING_MODES = {"normal", "sliced", "custom", "contain", "cover", "split_horizontal"}
ALLOWED_OUTPUT_MAPPING_PROFILES = {"normal", "led_wide", "custom"}
ALLOWED_SLICE_DIRECTIONS = {"horizontal_stack", "vertical_stack"}


def _positive_int(value: object, fallback: int) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return max(1, fallback)
    return max(1, candidate)


def _non_negative_int(value: object, fallback: int) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return max(0, fallback)
    return max(0, candidate)


def _number(value: object, fallback: float) -> float:
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if candidate <= 0:
        return float(fallback)
    return float(candidate)


def _signed_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _round_scale(value: float) -> float:
    return round(value, 6)


def _normalize_profile(raw_profile: object, *, enabled: bool) -> str:
    if isinstance(raw_profile, str):
        candidate = raw_profile.strip().lower()
        if candidate in ALLOWED_OUTPUT_MAPPING_PROFILES:
            return candidate
    return "custom" if enabled else "normal"


def _normalize_mode(raw_mode: object, *, profile: str) -> str:
    if isinstance(raw_mode, str):
        candidate = raw_mode.strip().lower()
        if candidate == "split_horizontal":
            return "sliced"
        if candidate in {"normal", "sliced", "custom"}:
            return candidate
        if candidate in {"contain", "cover"}:
            return "custom" if profile == "custom" else "normal"

    if profile == "led_wide":
        return "sliced"
    if profile == "custom":
        return "custom"
    return "normal"


def _normalize_direction(raw_direction: object, fallback: str) -> str:
    if isinstance(raw_direction, str):
        candidate = raw_direction.strip().lower()
        if candidate in ALLOWED_SLICE_DIRECTIONS:
            return candidate
    return fallback


def _get_suggested_slice_direction(*, output_width: int, output_height: int, canvas_width: int, canvas_height: int) -> str:
    if canvas_width > output_width and canvas_height <= output_height:
        return "horizontal_stack"
    if canvas_height > output_height and canvas_width <= output_width:
        return "vertical_stack"
    return "horizontal_stack" if canvas_width >= canvas_height else "vertical_stack"


def _get_suggested_slice_count(
    *,
    output_width: int,
    output_height: int,
    canvas_width: int,
    canvas_height: int,
    direction: str,
) -> int:
    if direction == "horizontal_stack":
        return max(1, ceil(canvas_width / max(1, output_width)))
    return max(1, ceil(canvas_height / max(1, output_height)))


def _distribute(total: int, count: int) -> list[int]:
    safe_total = max(1, total)
    safe_count = max(1, count)
    base = safe_total // safe_count
    remainder = safe_total - base * safe_count
    return [base + (1 if index < remainder else 0) for index in range(safe_count)]


def _build_auto_slices(
    *,
    output_width: int,
    output_height: int,
    canvas_width: int,
    canvas_height: int,
    slice_count: int,
    slice_direction: str,
) -> list[dict[str, object]]:
    safe_count = max(1, slice_count)

    if slice_direction == "horizontal_stack":
        source_widths = _distribute(canvas_width, safe_count)
        source_cursor = 0
        output_cursor = 0
        slices: list[dict[str, object]] = []

        for index, source_width in enumerate(source_widths, start=1):
            scale = min(1.0, output_width / max(1, source_width))
            output_slice_width = max(1, round(source_width * scale))
            output_slice_height = max(1, round(canvas_height * scale))
            slices.append(
                {
                    "slice_index": index,
                    "source_x": source_cursor,
                    "source_y": 0,
                    "source_width": source_width,
                    "source_height": canvas_height,
                    "output_x": 0,
                    "output_y": output_cursor,
                    "output_width": output_slice_width,
                    "output_height": output_slice_height,
                    "scale_x": _round_scale(output_slice_width / max(1, source_width)),
                    "scale_y": _round_scale(output_slice_height / max(1, canvas_height)),
                }
            )
            source_cursor += source_width
            output_cursor += output_slice_height
        return slices

    source_heights = _distribute(canvas_height, safe_count)
    source_cursor = 0
    output_cursor = 0
    slices = []

    for index, source_height in enumerate(source_heights, start=1):
        scale = min(1.0, output_height / max(1, source_height))
        output_slice_width = max(1, round(canvas_width * scale))
        output_slice_height = max(1, round(source_height * scale))
        slices.append(
            {
                "slice_index": index,
                "source_x": 0,
                "source_y": source_cursor,
                "source_width": canvas_width,
                "source_height": source_height,
                "output_x": output_cursor,
                "output_y": 0,
                "output_width": output_slice_width,
                "output_height": output_slice_height,
                "scale_x": _round_scale(output_slice_width / max(1, canvas_width)),
                "scale_y": _round_scale(output_slice_height / max(1, source_height)),
            }
        )
        source_cursor += source_height
        output_cursor += output_slice_width

    return slices


def _build_custom_default_slice(*, output_width: int, output_height: int, canvas_width: int, canvas_height: int) -> dict[str, object]:
    scale = min(output_width / max(1, canvas_width), output_height / max(1, canvas_height))
    scaled_width = max(1, round(canvas_width * scale))
    scaled_height = max(1, round(canvas_height * scale))
    return {
        "slice_index": 1,
        "source_x": 0,
        "source_y": 0,
        "source_width": canvas_width,
        "source_height": canvas_height,
        "output_x": round((output_width - scaled_width) / 2),
        "output_y": round((output_height - scaled_height) / 2),
        "output_width": scaled_width,
        "output_height": scaled_height,
        "scale_x": _round_scale(scaled_width / max(1, canvas_width)),
        "scale_y": _round_scale(scaled_height / max(1, canvas_height)),
    }


def _normalize_slice(raw_slice: Mapping[str, object] | dict[str, object] | None, fallback: Mapping[str, object]) -> dict[str, object]:
    payload = raw_slice if isinstance(raw_slice, Mapping) else {}
    return {
        "slice_index": _positive_int(payload.get("slice_index"), int(fallback["slice_index"])),
        "source_x": _non_negative_int(payload.get("source_x"), int(fallback["source_x"])),
        "source_y": _non_negative_int(payload.get("source_y"), int(fallback["source_y"])),
        "source_width": _positive_int(payload.get("source_width"), int(fallback["source_width"])),
        "source_height": _positive_int(payload.get("source_height"), int(fallback["source_height"])),
        "output_x": _signed_int(payload.get("output_x"), int(fallback["output_x"])),
        "output_y": _signed_int(payload.get("output_y"), int(fallback["output_y"])),
        "output_width": _positive_int(payload.get("output_width"), int(fallback["output_width"])),
        "output_height": _positive_int(payload.get("output_height"), int(fallback["output_height"])),
        "scale_x": _round_scale(_number(payload.get("scale_x"), float(fallback["scale_x"]))),
        "scale_y": _round_scale(_number(payload.get("scale_y"), float(fallback["scale_y"]))),
    }


def _summarize_slices(slices: list[dict[str, object]], *, fallback_width: int, fallback_height: int) -> dict[str, object]:
    if not slices:
        return {
            "viewport_x": 0,
            "viewport_y": 0,
            "viewport_width": fallback_width,
            "viewport_height": fallback_height,
            "scale_x": 1.0,
            "scale_y": 1.0,
        }

    min_x = min(int(slice_item["output_x"]) for slice_item in slices)
    min_y = min(int(slice_item["output_y"]) for slice_item in slices)
    max_x = max(int(slice_item["output_x"]) + int(slice_item["output_width"]) for slice_item in slices)
    max_y = max(int(slice_item["output_y"]) + int(slice_item["output_height"]) for slice_item in slices)
    first_slice = slices[0]

    return {
        "viewport_x": min_x,
        "viewport_y": min_y,
        "viewport_width": max(1, max_x - min_x),
        "viewport_height": max(1, max_y - min_y),
        "scale_x": _round_scale(float(first_slice.get("scale_x", 1.0))),
        "scale_y": _round_scale(float(first_slice.get("scale_y", 1.0))),
    }


def normalize_output_mapping(
    raw_mapping: Mapping[str, object] | dict[str, object] | None,
    *,
    resolution_width: int,
    resolution_height: int,
) -> dict[str, object]:
    payload = raw_mapping if isinstance(raw_mapping, Mapping) else {}

    output_width = _positive_int(payload.get("output_width"), resolution_width)
    output_height = _positive_int(payload.get("output_height"), resolution_height)
    physical_width = _positive_int(payload.get("physical_width"), output_width)
    physical_height = _positive_int(payload.get("physical_height"), output_height)
    canvas_width = _positive_int(payload.get("source_canvas_width") or payload.get("canvas_width"), physical_width)
    canvas_height = _positive_int(payload.get("source_canvas_height") or payload.get("canvas_height"), physical_height)

    profile = _normalize_profile(payload.get("profile"), enabled=bool(payload.get("enabled")))
    mode = _normalize_mode(payload.get("mapping_mode") or payload.get("mode"), profile=profile)

    if mode == "normal":
        profile = "normal"
    elif mode == "sliced":
        profile = "led_wide"
    else:
        profile = "custom"

    suggested_direction = _get_suggested_slice_direction(
        output_width=output_width,
        output_height=output_height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    slice_direction = _normalize_direction(payload.get("slice_direction"), suggested_direction)
    suggested_slice_count = _get_suggested_slice_count(
        output_width=output_width,
        output_height=output_height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        direction=slice_direction,
    )
    slice_count = _positive_int(payload.get("slice_count"), suggested_slice_count) if mode == "sliced" else 1
    raw_slices = payload.get("slices")
    raw_slice_list = list(raw_slices) if isinstance(raw_slices, list) else []

    if mode == "normal":
        return {
            "enabled": False,
            "profile": profile,
            "mode": mode,
            "mapping_mode": mode,
            "slice_count": 1,
            "slice_direction": suggested_direction,
            "output_width": output_width,
            "output_height": output_height,
            "physical_width": output_width,
            "physical_height": output_height,
            "source_canvas_width": output_width,
            "source_canvas_height": output_height,
            "canvas_width": output_width,
            "canvas_height": output_height,
            "slices": [],
            "viewport_x": 0,
            "viewport_y": 0,
            "viewport_width": output_width,
            "viewport_height": output_height,
            "scale_x": 1.0,
            "scale_y": 1.0,
        }

    if mode == "sliced":
        auto_slices = _build_auto_slices(
            output_width=output_width,
            output_height=output_height,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            slice_count=slice_count,
            slice_direction=slice_direction,
        )
        slices = [_normalize_slice(raw_slice_list[index] if index < len(raw_slice_list) else None, fallback) for index, fallback in enumerate(auto_slices)]
        summary = _summarize_slices(slices, fallback_width=output_width, fallback_height=output_height)

        return {
            "enabled": True,
            "profile": profile,
            "mode": mode,
            "mapping_mode": mode,
            "slice_count": slice_count,
            "slice_direction": slice_direction,
            "output_width": output_width,
            "output_height": output_height,
            "physical_width": physical_width,
            "physical_height": physical_height,
            "source_canvas_width": canvas_width,
            "source_canvas_height": canvas_height,
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "slices": slices,
            "viewport_x": summary["viewport_x"],
            "viewport_y": summary["viewport_y"],
            "viewport_width": summary["viewport_width"],
            "viewport_height": summary["viewport_height"],
            "scale_x": summary["scale_x"],
            "scale_y": summary["scale_y"],
        }

    custom_fallback = _build_custom_default_slice(
        output_width=output_width,
        output_height=output_height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    raw_primary_slice = raw_slice_list[0] if raw_slice_list else {
        "slice_index": 1,
        "source_x": 0,
        "source_y": 0,
        "source_width": canvas_width,
        "source_height": canvas_height,
        "output_x": payload.get("viewport_x"),
        "output_y": payload.get("viewport_y"),
        "output_width": payload.get("viewport_width"),
        "output_height": payload.get("viewport_height"),
        "scale_x": payload.get("scale_x"),
        "scale_y": payload.get("scale_y"),
    }
    primary_slice = _normalize_slice(raw_primary_slice, custom_fallback)

    return {
        "enabled": True,
        "profile": profile,
        "mode": mode,
        "mapping_mode": mode,
        "slice_count": 1,
        "slice_direction": suggested_direction,
        "output_width": output_width,
        "output_height": output_height,
        "physical_width": physical_width,
        "physical_height": physical_height,
        "source_canvas_width": canvas_width,
        "source_canvas_height": canvas_height,
        "canvas_width": canvas_width,
        "canvas_height": canvas_height,
        "slices": [primary_slice],
        "viewport_x": primary_slice["output_x"],
        "viewport_y": primary_slice["output_y"],
        "viewport_width": primary_slice["output_width"],
        "viewport_height": primary_slice["output_height"],
        "scale_x": primary_slice["scale_x"],
        "scale_y": primary_slice["scale_y"],
    }
