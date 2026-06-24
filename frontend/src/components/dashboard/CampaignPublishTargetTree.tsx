import { useEffect, useRef } from "react";

import type { Channel, ChannelMode } from "../../types/domain";

export type PublishableChannelTarget = {
  branchId: string;
  branchName: string;
  branchCode: string;
  branchTimezone: string;
  channelId: string;
  channelName: string;
  channelCode: string;
  mode: ChannelMode;
  status: Channel["status"];
  hasCampaign: boolean;
  currentCampaignId: string | null;
  currentCampaignLabel: string;
  slotIndex: number;
  visualLabel: string;
};

export type PublishTargetBranchGroup = {
  branchId: string;
  branchName: string;
  branchCode: string;
  branchTimezone: string;
  targets: PublishableChannelTarget[];
};

function SelectionCheckbox({
  checked,
  indeterminate = false,
  disabled = false,
  onChange,
}: {
  checked: boolean;
  indeterminate?: boolean;
  disabled?: boolean;
  onChange: () => void;
}) {
  const ref = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (ref.current) {
      ref.current.indeterminate = indeterminate;
    }
  }, [indeterminate]);

  return (
    <input
      ref={ref}
      checked={checked}
      className="h-4 w-4 rounded border-slate-300 text-slate-900 focus:ring-slate-400"
      disabled={disabled}
      type="checkbox"
      onChange={onChange}
    />
  );
}

function statusStyles(status: Channel["status"]) {
  if (status === "online") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  }
  if (status === "offline") {
    return "border-rose-200 bg-rose-50 text-rose-700";
  }
  return "border-amber-200 bg-amber-50 text-amber-800";
}

export function CampaignPublishTargetTree({
  branchGroups,
  selectedChannelIds,
  onToggleBranch,
  onToggleChannel,
}: {
  branchGroups: PublishTargetBranchGroup[];
  selectedChannelIds: Set<string>;
  onToggleBranch: (branchId: string) => void;
  onToggleChannel: (channelId: string) => void;
}) {
  if (branchGroups.length === 0) {
    return (
      <div className="grid min-h-[260px] place-items-center rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-6 text-center text-sm text-slate-600">
        No hay pantallas visibles para este alcance o para los filtros activos.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {branchGroups.map((branchGroup) => {
        const branchSelectedCount = branchGroup.targets.filter((target) => selectedChannelIds.has(target.channelId)).length;
        const allSelected = branchGroup.targets.length > 0 && branchSelectedCount === branchGroup.targets.length;
        const partiallySelected = branchSelectedCount > 0 && branchSelectedCount < branchGroup.targets.length;
        const onlineCount = branchGroup.targets.filter((target) => target.status === "online").length;

        return (
          <article key={branchGroup.branchId} className="rounded-[24px] border border-slate-200 bg-white">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
              <div className="flex min-w-0 items-center gap-3">
                <SelectionCheckbox checked={allSelected} indeterminate={partiallySelected} onChange={() => onToggleBranch(branchGroup.branchId)} />
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold text-ink" title={branchGroup.branchName}>
                    {branchGroup.branchName}
                  </p>
                  <p className="mt-1 text-xs text-slate-500">
                    {branchGroup.branchCode} · {branchGroup.targets.length} pantalla(s) visible(s)
                  </p>
                </div>
              </div>

              <div className="flex flex-wrap gap-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">{onlineCount} online</span>
                <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">{branchSelectedCount} seleccionadas</span>
              </div>
            </div>

            <div className="divide-y divide-slate-100">
              {branchGroup.targets.map((target) => (
                <label
                  key={target.channelId}
                  className="grid cursor-pointer grid-cols-[auto_minmax(0,1fr)] gap-3 px-4 py-3 transition hover:bg-slate-50"
                >
                  <div className="pt-1">
                    <SelectionCheckbox checked={selectedChannelIds.has(target.channelId)} onChange={() => onToggleChannel(target.channelId)} />
                  </div>

                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="text-sm font-semibold text-ink">{target.visualLabel}</p>
                      <span className={`rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] ${statusStyles(target.status)}`}>
                        {target.status}
                      </span>
                      <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                        {target.mode}
                      </span>
                    </div>

                    <p className="mt-1 text-sm text-slate-700" title={target.channelName}>
                      {target.channelName}
                    </p>
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-500">
                      <span>{target.channelCode}</span>
                      <span>{target.hasCampaign ? target.currentCampaignLabel : "Sin campaña"}</span>
                    </div>
                  </div>
                </label>
              ))}
            </div>
          </article>
        );
      })}
    </div>
  );
}
