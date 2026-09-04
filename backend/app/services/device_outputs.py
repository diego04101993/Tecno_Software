from __future__ import annotations

from app.models.entities import Channel, ChannelMode, DeviceMode, DeviceOutput, DeviceOutputOrientation, Orientation


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


def normalize_device_mode(raw_mode: DeviceMode | str | None, *, channel_mode: ChannelMode) -> DeviceMode:
    if channel_mode != ChannelMode.EXPANDED:
        return DeviceMode.SINGLE
    if isinstance(raw_mode, DeviceMode):
        return raw_mode
    if isinstance(raw_mode, str):
        candidate = raw_mode.strip().lower()
        for mode in DeviceMode:
            if mode.value == candidate:
                return mode
    return DeviceMode.EXTENDED


def normalize_screen_count(
    raw_count: int | None,
    *,
    channel_mode: ChannelMode,
    device_mode: DeviceMode,
) -> int:
    base_count = _positive_int(raw_count, 1)
    if channel_mode != ChannelMode.EXPANDED or device_mode == DeviceMode.SINGLE:
        return 1
    return max(2, base_count)


def normalize_canvas_dimensions(
    *,
    device_mode: DeviceMode,
    resolution_width: int,
    resolution_height: int,
    screen_count: int,
    canvas_width: int | None,
    canvas_height: int | None,
) -> tuple[int, int]:
    safe_resolution_width = _positive_int(resolution_width, 1920)
    safe_resolution_height = _positive_int(resolution_height, 1080)
    safe_screen_count = max(1, screen_count)

    if device_mode == DeviceMode.EXTENDED:
        default_width = safe_resolution_width * safe_screen_count
        default_height = safe_resolution_height
    else:
        default_width = safe_resolution_width
        default_height = safe_resolution_height

    if device_mode == DeviceMode.SINGLE:
        return safe_resolution_width, safe_resolution_height

    return (
        _positive_int(canvas_width, default_width),
        _positive_int(canvas_height, default_height),
    )


def build_legacy_expanded_outputs(device_mode: DeviceMode, screen_count: int) -> list[dict[str, object]]:
    if device_mode == DeviceMode.SINGLE or screen_count <= 1:
        return []

    role_by_mode = {
        DeviceMode.MIRROR: "mirror",
        DeviceMode.EXTENDED: "extended",
        DeviceMode.INDEPENDENT: "independent",
    }
    role = role_by_mode.get(device_mode, "extended")

    return [
        {
            "output_index": index + 1,
            "label": f"Salida {index + 1}",
            "role": "primary" if index == 0 else role,
            "device_mode": device_mode.value,
        }
        for index in range(screen_count)
    ]


def normalize_channel_device_fields(
    channel: Channel,
    *,
    requested_device_mode: DeviceMode | str | None = None,
    requested_screen_count: int | None = None,
    requested_canvas_width: int | None = None,
    requested_canvas_height: int | None = None,
) -> None:
    channel.device_mode = normalize_device_mode(requested_device_mode or channel.device_mode, channel_mode=channel.mode)
    channel.screen_count = normalize_screen_count(
        requested_screen_count if requested_screen_count is not None else channel.screen_count,
        channel_mode=channel.mode,
        device_mode=channel.device_mode,
    )
    channel.canvas_width, channel.canvas_height = normalize_canvas_dimensions(
        device_mode=channel.device_mode,
        resolution_width=channel.resolution_width,
        resolution_height=channel.resolution_height,
        screen_count=channel.screen_count,
        canvas_width=requested_canvas_width if requested_canvas_width is not None else channel.canvas_width,
        canvas_height=requested_canvas_height if requested_canvas_height is not None else channel.canvas_height,
    )
    channel.expanded_outputs = build_legacy_expanded_outputs(channel.device_mode, channel.screen_count)


def _orientation_for_output(channel_orientation: Orientation) -> DeviceOutputOrientation:
    if channel_orientation == Orientation.VERTICAL:
        return DeviceOutputOrientation.PORTRAIT
    return DeviceOutputOrientation.LANDSCAPE


def _distribute(total: int, count: int) -> list[int]:
    safe_total = max(1, total)
    safe_count = max(1, count)
    base = safe_total // safe_count
    remainder = safe_total - base * safe_count
    return [base + (1 if index < remainder else 0) for index in range(safe_count)]


def build_device_output_definitions(channel: Channel) -> list[dict[str, object]]:
    safe_width = _positive_int(channel.resolution_width, 1920)
    safe_height = _positive_int(channel.resolution_height, 1080)
    safe_canvas_width = _positive_int(channel.canvas_width, safe_width)
    safe_canvas_height = _positive_int(channel.canvas_height, safe_height)
    screen_count = normalize_screen_count(channel.screen_count, channel_mode=channel.mode, device_mode=channel.device_mode)
    orientation = _orientation_for_output(channel.orientation)

    if channel.device_mode == DeviceMode.EXTENDED and screen_count > 1:
        slice_widths = _distribute(safe_canvas_width, screen_count)
        definitions: list[dict[str, object]] = []
        source_cursor = 0
        geometry_cursor = 0
        for index, slice_width in enumerate(slice_widths):
            geometry_width = safe_width
            geometry_height = safe_height
            definitions.append(
                {
                    "name": f"Salida {index + 1}",
                    "display_order": index,
                    "display_uid": None,
                    "channel_id": channel.id,
                    "x": geometry_cursor,
                    "y": 0,
                    "width": geometry_width,
                    "height": geometry_height,
                    "source_x": source_cursor,
                    "source_y": 0,
                    "source_width": slice_width,
                    "source_height": safe_canvas_height,
                    "rotation": 0,
                    "orientation": orientation,
                    "sync_group": "main",
                    "enabled": True,
                }
            )
            source_cursor += slice_width
            geometry_cursor += geometry_width
        return definitions

    definitions = []
    for index in range(screen_count):
        definitions.append(
            {
                "name": f"Salida {index + 1}",
                "display_order": index,
                "display_uid": None,
                "channel_id": None if channel.device_mode == DeviceMode.INDEPENDENT else channel.id,
                "x": safe_width * index,
                "y": 0,
                "width": safe_width,
                "height": safe_height,
                "source_x": 0,
                "source_y": 0,
                "source_width": safe_canvas_width if channel.device_mode == DeviceMode.MIRROR else safe_width,
                "source_height": safe_canvas_height if channel.device_mode == DeviceMode.MIRROR else safe_height,
                "rotation": 0,
                "orientation": orientation,
                "sync_group": "main" if channel.device_mode != DeviceMode.INDEPENDENT else None,
                "enabled": True,
            }
        )
    return definitions


def sync_channel_device_outputs(channel: Channel) -> None:
    definitions = build_device_output_definitions(channel)
    existing_by_order = {output.display_order: output for output in list(channel.device_outputs or [])}
    next_outputs: list[DeviceOutput] = []

    for definition in definitions:
        display_order = int(definition["display_order"])
        output = existing_by_order.pop(display_order, None)
        if output is None:
            output = DeviceOutput(root_channel_id=channel.id)
        for field, value in definition.items():
            if field == "display_uid" and value is None and output.display_uid:
                continue
            if (
                field == "channel_id"
                and channel.device_mode == DeviceMode.INDEPENDENT
                and output.channel_id
                and output.channel_id != channel.id
            ):
                continue
            setattr(output, field, value)
        next_outputs.append(output)

    channel.device_outputs = next_outputs


def serialize_runtime_device(channel: Channel) -> dict[str, object]:
    expected_outputs = len([output for output in channel.device_outputs if output.enabled]) or channel.screen_count or 1
    return {
        "mode": channel.device_mode.value,
        "canvas_width": channel.canvas_width,
        "canvas_height": channel.canvas_height,
        "expected_outputs": expected_outputs,
        "root_channel_id": channel.id,
    }


def serialize_runtime_output(output: DeviceOutput) -> dict[str, object]:
    return {
        "output_id": output.id,
        "name": output.name,
        "display_uid": output.display_uid,
        "display_order": output.display_order,
        "channel_id": output.channel_id,
        "geometry": {
            "x": output.x,
            "y": output.y,
            "width": output.width,
            "height": output.height,
        },
        "source": {
            "x": output.source_x,
            "y": output.source_y,
            "width": output.source_width,
            "height": output.source_height,
        },
        "x": output.x,
        "y": output.y,
        "width": output.width,
        "height": output.height,
        "source_x": output.source_x,
        "source_y": output.source_y,
        "source_width": output.source_width,
        "source_height": output.source_height,
        "rotation": output.rotation,
        "orientation": output.orientation.value,
        "sync_group": output.sync_group,
        "enabled": output.enabled,
    }
