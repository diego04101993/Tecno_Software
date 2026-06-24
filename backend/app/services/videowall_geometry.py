from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideowallCellGeometry:
    position_index: int
    row_index: int
    column_index: int
    x: int
    y: int
    width: int
    height: int


def build_axis_segments(count: int, total_size: int) -> list[int]:
    safe_count = max(1, int(count))
    safe_total_size = max(1, int(total_size))
    base_size = safe_total_size // safe_count
    remainder = safe_total_size - (base_size * safe_count)
    return [base_size + (remainder if index == safe_count - 1 else 0) for index in range(safe_count)]


def position_from_row_col(columns: int, row_index: int, column_index: int) -> int:
    safe_columns = max(1, int(columns))
    return (int(row_index) * safe_columns) + int(column_index) + 1


def row_col_from_position(columns: int, position_index: int) -> tuple[int, int]:
    safe_columns = max(1, int(columns))
    safe_position = max(1, int(position_index))
    zero_based = safe_position - 1
    return zero_based // safe_columns, zero_based % safe_columns


def compute_videowall_cell_geometry(
    *,
    columns: int,
    rows: int,
    total_width: int,
    total_height: int,
    position_index: int,
) -> VideowallCellGeometry:
    safe_columns = max(1, int(columns))
    safe_rows = max(1, int(rows))
    max_position = safe_columns * safe_rows
    safe_position = min(max(1, int(position_index)), max_position)
    row_index, column_index = row_col_from_position(safe_columns, safe_position)
    widths = build_axis_segments(safe_columns, total_width)
    heights = build_axis_segments(safe_rows, total_height)

    return VideowallCellGeometry(
        position_index=safe_position,
        row_index=row_index,
        column_index=column_index,
        x=sum(widths[:column_index]),
        y=sum(heights[:row_index]),
        width=widths[column_index],
        height=heights[row_index],
    )


def compute_videowall_cell_by_row_col(
    *,
    columns: int,
    rows: int,
    total_width: int,
    total_height: int,
    row_index: int,
    column_index: int,
) -> VideowallCellGeometry:
    return compute_videowall_cell_geometry(
        columns=columns,
        rows=rows,
        total_width=total_width,
        total_height=total_height,
        position_index=position_from_row_col(columns, row_index, column_index),
    )
