import { ChevronRight, MonitorPlay, Store, Trash2 } from "lucide-react";
import { Link } from "react-router-dom";

import { StatusBadge } from "../StatusBadge";
import { buildBranchChannelsPath } from "../../lib/workspace";
import type { Branch, Channel } from "../../types/domain";

type OperationBranchTreeProps = {
  clientId: string;
  branches: Branch[];
  channels: Channel[];
  selectedBranchId: string | null;
  selectedChannelId: string | null;
  canManage: boolean;
  onSelectBranch: (branchId: string) => void;
  onSelectChannel: (channelId: string) => void;
  onDeleteBranch: (branch: Branch) => void;
  onDeleteChannel: (channel: Channel) => void;
};

export function OperationBranchTree({
  clientId,
  branches,
  channels,
  selectedBranchId,
  selectedChannelId,
  canManage,
  onSelectBranch,
  onSelectChannel,
  onDeleteBranch,
  onDeleteChannel,
}: OperationBranchTreeProps) {
  return (
    <div className="flex min-h-0 flex-col">
      <div className="mb-3 flex items-center justify-between gap-3 border-b border-slate-200 pb-3">
        <div>
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Navegación operativa</p>
          <p className="mt-1 text-sm text-slate-600">Sucursales, canales y acceso directo al destino de publicación.</p>
        </div>
        <span className="rounded-full bg-slate-100 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
          {branches.length} sucursales
        </span>
      </div>

      <div className="min-h-0 space-y-2 overflow-y-auto pr-1">
        {branches.map((branch) => {
          const branchChannels = channels.filter((channel) => channel.branch_id === branch.id);
          const isSelectedBranch = branch.id === selectedBranchId;

          return (
            <section key={branch.id} className="rounded-[18px] border border-slate-200 bg-slate-50">
              <div
                className={[
                  "flex items-center gap-3 rounded-[18px] px-3 py-3 transition",
                  isSelectedBranch ? "bg-slate-900 text-white" : "hover:bg-white",
                ].join(" ")}
              >
                <button className="flex min-w-0 flex-1 items-center gap-3 text-left" type="button" onClick={() => onSelectBranch(branch.id)}>
                  <span className={["rounded-xl p-2", isSelectedBranch ? "bg-white/10 text-white" : "bg-white text-accent"].join(" ")}>
                    <Store className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold" title={branch.name}>
                      {branch.name}
                    </p>
                    <p className={["mt-0.5 text-xs", isSelectedBranch ? "text-white/70" : "text-slate-500"].join(" ")}>
                      {branch.code} · {branchChannels.length} canal(es)
                    </p>
                  </div>
                  <ChevronRight className={["h-4 w-4 shrink-0", isSelectedBranch ? "text-white/70" : "text-slate-400"].join(" ")} />
                </button>

                {canManage ? (
                  <button
                    className={[
                      "rounded-full border p-2 transition",
                      isSelectedBranch
                        ? "border-white/15 text-white/80 hover:border-white/30 hover:text-white"
                        : "border-slate-200 bg-white text-slate-500 hover:border-rose-300 hover:text-rose-600",
                    ].join(" ")}
                    title="Eliminar sucursal"
                    type="button"
                    onClick={() => onDeleteBranch(branch)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                ) : null}
              </div>

              {isSelectedBranch ? (
                <div className="space-y-2 border-t border-slate-200 bg-white/80 p-2">
                  {branchChannels.length > 0 ? (
                    branchChannels.map((channel) => (
                      <div
                        key={channel.id}
                        className={[
                          "flex items-start gap-3 rounded-[14px] border px-3 py-3 transition",
                          channel.id === selectedChannelId
                            ? "border-cyan-300 bg-cyan-50"
                            : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50",
                        ].join(" ")}
                      >
                        <button className="flex min-w-0 flex-1 items-start gap-3 text-left" type="button" onClick={() => onSelectChannel(channel.id)}>
                          <span className="rounded-xl bg-slate-100 p-2 text-slate-600">
                            <MonitorPlay className="h-4 w-4" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm font-semibold text-ink" title={channel.name}>
                              {channel.name}
                            </p>
                            <p className="mt-0.5 text-xs text-slate-500">
                              {channel.mode} · {channel.resolution_width}x{channel.resolution_height}
                            </p>
                          </div>
                        </button>

                        <div className="shrink-0 space-y-2 text-right">
                          <StatusBadge status={channel.status} />
                          <div className="flex items-center justify-end gap-2">
                            <Link
                              className="inline-block text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500 hover:text-ink"
                              to={buildBranchChannelsPath(clientId, branch.id)}
                            >
                              Abrir
                            </Link>
                            {canManage ? (
                              <button
                                className="rounded-full border border-slate-200 p-1.5 text-slate-500 transition hover:border-rose-300 hover:text-rose-600"
                                title="Eliminar canal"
                                type="button"
                                onClick={() => onDeleteChannel(channel)}
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </button>
                            ) : null}
                          </div>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="rounded-[14px] border border-dashed border-slate-300 bg-slate-50 px-3 py-4 text-sm text-slate-500">
                      Esta sucursal todavía no tiene canales visibles.
                    </div>
                  )}
                </div>
              ) : null}
            </section>
          );
        })}
      </div>
    </div>
  );
}
