import type { ReactNode } from "react";
import { FolderPen, FolderPlus, FolderTree, Trash2 } from "lucide-react";

import type { ContentFolder } from "../../types/domain";

export type OperationFolderScope = "all" | "uncategorized" | string;

type OperationFolderTreeProps = {
  folders: ContentFolder[];
  selectedScope: OperationFolderScope;
  totalCount: number;
  uncategorizedCount: number;
  countsByFolderId: Record<string, number>;
  canEdit: boolean;
  onSelectScope: (scope: OperationFolderScope) => void;
  onCreateRootFolder: () => void;
  onCreateChildFolder: (folder: ContentFolder) => void;
  onRenameFolder: (folder: ContentFolder) => void;
  onDeleteFolder: (folder: ContentFolder) => void;
};

function sortFolders(folders: ContentFolder[]) {
  return [...folders].sort((left, right) => {
    if (left.sort_order !== right.sort_order) {
      return left.sort_order - right.sort_order;
    }
    return left.name.localeCompare(right.name, "es");
  });
}

export function OperationFolderTree({
  folders,
  selectedScope,
  totalCount,
  uncategorizedCount,
  countsByFolderId,
  canEdit,
  onSelectScope,
  onCreateRootFolder,
  onCreateChildFolder,
  onRenameFolder,
  onDeleteFolder,
}: OperationFolderTreeProps) {
  const orderedFolders = sortFolders(folders);

  function renderFolderLevel(parentId: string | null, depth = 0): ReactNode {
    return orderedFolders
      .filter((folder) => folder.parent_id === parentId)
      .map((folder) => {
        const count = countsByFolderId[folder.id] ?? 0;
        const selected = selectedScope === folder.id;

        return (
          <div key={folder.id} className="space-y-1" style={{ paddingLeft: `${depth * 14}px` }}>
            <div
              className={[
                "group flex min-w-0 items-center gap-2 rounded-[14px] px-2 py-2 transition",
                selected ? "bg-accentSoft/80 text-ink" : "hover:bg-slate-100/90",
              ].join(" ")}
            >
              <button className="flex min-w-0 flex-1 items-center gap-2 text-left" type="button" onClick={() => onSelectScope(folder.id)}>
                <span
                  className={[
                    "grid h-8 w-8 shrink-0 place-items-center rounded-xl border text-slate-600",
                    selected ? "border-accent/20 bg-white" : "border-slate-200 bg-white",
                  ].join(" ")}
                >
                  <FolderTree className="h-3.5 w-3.5" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-ink">{folder.name}</span>
                  <span className="mt-0.5 block text-[10px] uppercase tracking-[0.16em] text-slate-500">{count} archivo(s)</span>
                </span>
              </button>

              {canEdit ? (
                <div className="flex items-center gap-1 opacity-70 transition group-hover:opacity-100">
                  <button
                    className="rounded-full border border-slate-200 bg-white p-1.5 text-slate-500 transition hover:border-slate-300 hover:text-ink"
                    title="Crear subcarpeta"
                    type="button"
                    onClick={() => onCreateChildFolder(folder)}
                  >
                    <FolderPlus className="h-3.5 w-3.5" />
                  </button>
                  <button
                    className="rounded-full border border-slate-200 bg-white p-1.5 text-slate-500 transition hover:border-slate-300 hover:text-ink"
                    title="Renombrar carpeta"
                    type="button"
                    onClick={() => onRenameFolder(folder)}
                  >
                    <FolderPen className="h-3.5 w-3.5" />
                  </button>
                  <button
                    className="rounded-full border border-slate-200 bg-white p-1.5 text-slate-500 transition hover:border-rose-300 hover:text-rose-600"
                    title="Eliminar carpeta"
                    type="button"
                    onClick={() => onDeleteFolder(folder)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ) : null}
            </div>

            {renderFolderLevel(folder.id, depth + 1)}
          </div>
        );
      });
  }

  return (
    <div className="grid h-full min-h-0 min-w-0 grid-rows-[auto_minmax(0,1fr)_auto] gap-2 overflow-hidden rounded-[18px] border border-slate-200 bg-slate-50/80 px-3 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Carpetas</p>
          <p className="mt-1 text-sm text-slate-600">Explorador compacto para organizar la biblioteca del studio.</p>
        </div>
        <span className="rounded-full bg-slate-100 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
          {folders.length} carpeta(s)
        </span>
      </div>

      <div className="min-h-0 overflow-y-auto rounded-[18px] border border-slate-200 bg-slate-50/80 p-2 pr-1">
        <div className="space-y-1">
          <button
            className={[
              "flex w-full items-center justify-between rounded-[14px] px-3 py-2 text-left transition",
              selectedScope === "all" ? "bg-accentSoft/80" : "hover:bg-white",
            ].join(" ")}
            type="button"
            onClick={() => onSelectScope("all")}
          >
            <span className="text-sm font-medium text-ink">Todos</span>
            <span className="text-[10px] uppercase tracking-[0.16em] text-slate-500">{totalCount}</span>
          </button>

          <button
            className={[
              "flex w-full items-center justify-between rounded-[14px] px-3 py-2 text-left transition",
              selectedScope === "uncategorized" ? "bg-accentSoft/80" : "hover:bg-white",
            ].join(" ")}
            type="button"
            onClick={() => onSelectScope("uncategorized")}
          >
            <span className="text-sm font-medium text-ink">Sin carpeta</span>
            <span className="text-[10px] uppercase tracking-[0.16em] text-slate-500">{uncategorizedCount}</span>
          </button>
        </div>

        <div className="mt-2 space-y-1">{renderFolderLevel(null)}</div>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-600 transition hover:border-slate-300 hover:bg-white hover:text-ink disabled:cursor-not-allowed disabled:opacity-60"
          disabled={!canEdit}
          type="button"
          onClick={onCreateRootFolder}
        >
          <FolderPlus className="h-3.5 w-3.5" />
          Crear carpeta
        </button>
      </div>
    </div>
  );
}
