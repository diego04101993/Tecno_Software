export function SimpleVideowallMatrix({
  columns,
  rows,
  occupiedPositions = [],
  selectedPosition,
  onSelectPosition,
  showLabels = false,
}: {
  columns: number;
  rows: number;
  occupiedPositions?: number[];
  selectedPosition?: number;
  onSelectPosition?: (position: number) => void;
  showLabels?: boolean;
}) {
  const safeColumns = Math.max(1, columns);
  const safeRows = Math.max(1, rows);
  const total = safeColumns * safeRows;
  const occupiedSet = new Set(occupiedPositions);

  return (
    <div
      className="grid gap-2"
      style={{
        gridTemplateColumns: `repeat(${safeColumns}, minmax(0, 1fr))`,
      }}
    >
      {Array.from({ length: total }, (_, index) => {
        const position = index + 1;
        const rowIndex = Math.floor(index / safeColumns);
        const columnIndex = index % safeColumns;
        const isOccupied = occupiedSet.has(position);
        const isSelected = selectedPosition === position;
        const className = [
          "aspect-square rounded-[12px] border transition flex items-center justify-center text-[11px] font-semibold",
          isSelected
            ? "border-cyan-500 bg-cyan-100 text-cyan-900 shadow-[0_0_0_1px_rgba(34,211,238,0.32)]"
            : isOccupied
              ? "border-emerald-300 bg-emerald-100 text-emerald-800"
              : "border-slate-200 bg-slate-100 text-slate-500 hover:border-cyan-300 hover:bg-white",
          onSelectPosition ? "cursor-pointer" : "cursor-default",
        ].join(" ");
        const label = showLabels ? position : null;

        if (!onSelectPosition) {
          return (
            <div key={position} aria-hidden className={className} title={`Fila ${rowIndex + 1}, columna ${columnIndex + 1}`}>
              {label}
            </div>
          );
        }

        return (
          <button
            key={position}
            aria-label={`Seleccionar fila ${rowIndex + 1}, columna ${columnIndex + 1}`}
            className={className}
            title={`Fila ${rowIndex + 1}, columna ${columnIndex + 1}`}
            type="button"
            onClick={() => onSelectPosition(position)}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
