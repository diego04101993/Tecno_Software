export type CampaignPublishStatusFilter = "all" | "online" | "offline";
export type CampaignPublishCampaignFilter = "all" | "with" | "without";

type CampaignPublishQuickActionsProps = {
  visibleTargetCount: number;
  selectedCount: number;
  slotIndexes: number[];
  selectedSlotIndexes: number[];
  statusFilter: CampaignPublishStatusFilter;
  campaignFilter: CampaignPublishCampaignFilter;
  onSelectAllVisible: () => void;
  onSelectOnlineVisible: () => void;
  onSelectWithoutCampaignVisible: () => void;
  onClearSelection: () => void;
  onToggleSlotSelection: (slotIndex: number) => void;
  onSetStatusFilter: (filter: CampaignPublishStatusFilter) => void;
  onSetCampaignFilter: (filter: CampaignPublishCampaignFilter) => void;
};

function chipClassName(active = false) {
  return [
    "rounded-full border px-3 py-2 text-xs font-semibold uppercase tracking-[0.14em] transition",
    active
      ? "border-slate-900 bg-slate-900 text-white"
      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-ink",
  ].join(" ");
}

function filterChipClassName(active = false) {
  return [
    "rounded-full border px-3 py-2 text-xs font-semibold transition",
    active
      ? "border-cyan-300 bg-cyan-50 text-cyan-900"
      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-ink",
  ].join(" ");
}

export function CampaignPublishQuickActions({
  visibleTargetCount,
  selectedCount,
  slotIndexes,
  selectedSlotIndexes,
  statusFilter,
  campaignFilter,
  onSelectAllVisible,
  onSelectOnlineVisible,
  onSelectWithoutCampaignVisible,
  onClearSelection,
  onToggleSlotSelection,
  onSetStatusFilter,
  onSetCampaignFilter,
}: CampaignPublishQuickActionsProps) {
  return (
    <div className="space-y-4 rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Seleccion rapida</p>
          <p className="mt-1 text-sm text-slate-600">
            {visibleTargetCount} pantalla(s) visible(s) y {selectedCount} seleccionada(s).
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <button className={chipClassName()} type="button" onClick={onSelectAllVisible}>
          Todas
        </button>
        <button className={chipClassName()} type="button" onClick={onSelectOnlineVisible}>
          Online
        </button>
        <button className={chipClassName()} type="button" onClick={onSelectWithoutCampaignVisible}>
          Sin campaña
        </button>
        <button className={chipClassName()} type="button" onClick={onClearSelection}>
          Limpiar
        </button>
      </div>

      <div className="space-y-2 border-t border-slate-200 pt-4">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Posiciones de pantalla</p>
          <p className="mt-1 text-sm text-slate-600">Selecciona una o varias posiciones para marcarlas en todas las sucursales que las tengan.</p>
        </div>

        <div className="flex flex-wrap gap-2">
          {slotIndexes.length > 0 ? (
            slotIndexes.map((slotIndex) => (
              <button
                key={slotIndex}
                className={chipClassName(selectedSlotIndexes.includes(slotIndex))}
                type="button"
                onClick={() => onToggleSlotSelection(slotIndex)}
              >
                P{slotIndex}
              </button>
            ))
          ) : (
            <span className="rounded-full border border-dashed border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-400">
              Sin posiciones
            </span>
          )}
        </div>
      </div>

      <div className="space-y-2 border-t border-slate-200 pt-4">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Filtros visuales</p>
          <p className="mt-1 text-sm text-slate-600">Solo afectan lo visible en el arbol. No cambian la seleccion por si solos.</p>
        </div>

        <div className="flex flex-wrap gap-2">
          <button className={filterChipClassName(statusFilter === "all")} type="button" onClick={() => onSetStatusFilter("all")}>
            Todas
          </button>
          <button className={filterChipClassName(statusFilter === "online")} type="button" onClick={() => onSetStatusFilter("online")}>
            Mostrar solo online
          </button>
          <button className={filterChipClassName(statusFilter === "offline")} type="button" onClick={() => onSetStatusFilter("offline")}>
            Mostrar solo offline
          </button>
        </div>

        <div className="flex flex-wrap gap-2">
          <button className={filterChipClassName(campaignFilter === "all")} type="button" onClick={() => onSetCampaignFilter("all")}>
            Todas
          </button>
          <button className={filterChipClassName(campaignFilter === "without")} type="button" onClick={() => onSetCampaignFilter("without")}>
            Mostrar solo sin campaña
          </button>
          <button className={filterChipClassName(campaignFilter === "with")} type="button" onClick={() => onSetCampaignFilter("with")}>
            Mostrar solo con campaña
          </button>
        </div>
      </div>
    </div>
  );
}
