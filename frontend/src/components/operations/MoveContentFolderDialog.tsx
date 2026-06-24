import { useEffect, useMemo, useState } from "react";

import type { ContentFolder, ContentItem } from "../../types/domain";

type MoveContentFolderDialogProps = {
  open: boolean;
  content: ContentItem | null;
  folders: ContentFolder[];
  isSubmitting: boolean;
  onClose: () => void;
  onSubmit: (folderId: string | null) => Promise<void> | void;
};

function buildFolderOptions(folders: ContentFolder[], parentId: string | null = null, depth = 0): Array<{ id: string; label: string }> {
  return folders
    .filter((folder) => folder.parent_id === parentId)
    .sort((left, right) => {
      if (left.sort_order !== right.sort_order) {
        return left.sort_order - right.sort_order;
      }
      return left.name.localeCompare(right.name, "es");
    })
    .flatMap((folder) => [
      { id: folder.id, label: `${"  ".repeat(depth)}${folder.name}` },
      ...buildFolderOptions(folders, folder.id, depth + 1),
    ]);
}

export function MoveContentFolderDialog({
  open,
  content,
  folders,
  isSubmitting,
  onClose,
  onSubmit,
}: MoveContentFolderDialogProps) {
  const options = useMemo(() => buildFolderOptions(folders), [folders]);
  const [selectedFolderId, setSelectedFolderId] = useState<string>("root");

  useEffect(() => {
    if (!open || !content) {
      return;
    }
    setSelectedFolderId(content.folder_id ?? "root");
  }, [content, open]);

  if (!open || !content) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/50 px-4">
      <div className="w-full max-w-lg rounded-[24px] border border-slate-200 bg-white p-6 shadow-2xl">
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Mover contenido</p>
          <h3 className="mt-2 text-xl font-semibold text-ink">{content.name}</h3>
          <p className="mt-2 text-sm leading-6 text-slate-600">Selecciona la carpeta destino o devuelve este contenido a la raíz de la biblioteca.</p>
        </div>

        <div className="mt-5 space-y-2">
          <label className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500" htmlFor="move-content-folder">
            Carpeta destino
          </label>
          <select id="move-content-folder" value={selectedFolderId} onChange={(event) => setSelectedFolderId(event.target.value)}>
            <option value="root">Raíz / Sin carpeta</option>
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <button
            className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-600 transition hover:border-slate-300 hover:text-ink"
            disabled={isSubmitting}
            type="button"
            onClick={onClose}
          >
            Cancelar
          </button>
          <button
            className="rounded-full bg-ink px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
            disabled={isSubmitting}
            type="button"
            onClick={() => void onSubmit(selectedFolderId === "root" ? null : selectedFolderId)}
          >
            {isSubmitting ? "Moviendo..." : "Mover contenido"}
          </button>
        </div>
      </div>
    </div>
  );
}
