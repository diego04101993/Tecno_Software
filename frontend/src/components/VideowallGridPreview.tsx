type VideowallCellMetrics = {
  positionIndex: number;
  rowIndex: number;
  columnIndex: number;
  x: number;
  y: number;
  width: number;
  height: number;
};

function buildColumnWidths(columns: number, totalWidth: number) {
  const normalizedColumns = Math.max(1, columns);
  const baseWidth = Math.floor(totalWidth / normalizedColumns);
  const remainder = totalWidth - baseWidth * normalizedColumns;

  return Array.from({ length: normalizedColumns }, (_, index) => baseWidth + (index === normalizedColumns - 1 ? remainder : 0));
}

function buildRowHeights(rows: number, totalHeight: number) {
  const normalizedRows = Math.max(1, rows);
  const baseHeight = Math.floor(totalHeight / normalizedRows);
  const remainder = totalHeight - baseHeight * normalizedRows;

  return Array.from({ length: normalizedRows }, (_, index) => baseHeight + (index === normalizedRows - 1 ? remainder : 0));
}

export function getVideowallCellMetrics(
  columns: number,
  rows: number,
  totalWidth: number,
  totalHeight: number,
  positionIndex: number,
): VideowallCellMetrics {
  const safeColumns = Math.max(1, columns);
  const safeRows = Math.max(1, rows);
  const safePosition = Math.min(Math.max(1, positionIndex), safeColumns * safeRows);
  const rowIndex = Math.floor((safePosition - 1) / safeColumns);
  const columnIndex = (safePosition - 1) % safeColumns;
  const widths = buildColumnWidths(safeColumns, totalWidth);
  const heights = buildRowHeights(safeRows, totalHeight);

  return {
    positionIndex: safePosition,
    rowIndex,
    columnIndex,
    x: widths.slice(0, columnIndex).reduce((sum, width) => sum + width, 0),
    y: heights.slice(0, rowIndex).reduce((sum, height) => sum + height, 0),
    width: widths[columnIndex],
    height: heights[rowIndex],
  };
}

export function VideowallGridPreview({
  columns,
  rows,
  totalWidth,
  totalHeight,
  selectedPosition,
  occupiedPositions = [],
  occupiedLabels = {},
  onSelectPosition,
}: {
  columns: number;
  rows: number;
  totalWidth: number;
  totalHeight: number;
  selectedPosition: number;
  occupiedPositions?: number[];
  occupiedLabels?: Record<number, string>;
  onSelectPosition?: (position: number) => void;
}) {
  const totalMonitors = Math.max(1, columns) * Math.max(1, rows);
  const occupiedSet = new Set(occupiedPositions);
  const previewMinWidth = Math.max(280, Math.max(1, columns) * 148);

  return (
    <div className="min-w-0 overflow-hidden rounded-[28px] border border-slate-200 bg-slate-950 p-4 text-white shadow-card">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-slate-400">Preview de matriz</p>
          <p className="mt-2 text-sm text-slate-300">
            Cada celda representa un monitor dentro del lienzo completo del videowall.
          </p>
        </div>
        <span className="rounded-full border border-white/10 bg-white/10 px-3 py-1 text-xs font-semibold text-slate-200">
          {columns}x{rows} · {totalMonitors} monitor(es)
        </span>
      </div>

      <div className="mt-4 overflow-x-auto">
        <div
          className="grid gap-3 rounded-[22px] border border-white/10 bg-white/5 p-3"
          style={{
            gridTemplateColumns: `repeat(${Math.max(1, columns)}, minmax(0, 1fr))`,
            minWidth: `${previewMinWidth}px`,
          }}
        >
          {Array.from({ length: totalMonitors }, (_, index) => {
            const position = index + 1;
            const metrics = getVideowallCellMetrics(columns, rows, totalWidth, totalHeight, position);
            const isSelected = position === selectedPosition;
            const isOccupied = occupiedSet.has(position);
            const occupiedLabel = occupiedLabels[position];

            return (
              <button
                key={index}
                className={[
                  "min-w-0 rounded-[20px] border p-4 text-left transition",
                  isSelected
                    ? "border-cyan-300 bg-cyan-400/15 shadow-[0_0_0_1px_rgba(103,232,249,0.35)]"
                    : isOccupied
                      ? "border-rose-300/60 bg-rose-400/10"
                      : "border-white/10 bg-white/10 hover:border-cyan-300/50 hover:bg-cyan-400/10",
                  onSelectPosition && !isOccupied ? "cursor-pointer" : "cursor-default",
                ].join(" ")}
                disabled={!onSelectPosition || isOccupied}
                type="button"
                onClick={() => onSelectPosition?.(position)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[11px] uppercase tracking-[0.22em] text-slate-300">Monitor {position}</p>
                    <p className="mt-2 break-words text-base font-semibold text-white sm:text-lg">
                      {metrics.width}x{metrics.height}
                    </p>
                  </div>
                  {isSelected ? (
                    <span className="shrink-0 rounded-full border border-cyan-300/40 bg-cyan-300/15 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.2em] text-cyan-100">
                      Actual
                    </span>
                  ) : isOccupied ? (
                    <span className="shrink-0 rounded-full border border-rose-300/30 bg-rose-300/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.2em] text-rose-100">
                      Ocupado
                    </span>
                  ) : (
                    <span className="shrink-0 rounded-full border border-emerald-300/30 bg-emerald-300/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.2em] text-emerald-100">
                      Libre
                    </span>
                  )}
                </div>
                <p className="mt-3 text-xs leading-5 text-slate-300">
                  Fila {metrics.rowIndex + 1}, columna {metrics.columnIndex + 1}
                </p>
                <p className="mt-1 break-words text-xs leading-5 text-slate-400">
                  x:{metrics.x} · y:{metrics.y}
                </p>
                {isOccupied ? (
                  <p className="mt-2 truncate text-xs text-rose-100" title={occupiedLabel ?? "Posición ocupada"}>
                    {occupiedLabel ?? "Posición ocupada"}
                  </p>
                ) : null}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
