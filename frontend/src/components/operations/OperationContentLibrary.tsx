import { FileCode2, FileText, FolderInput, Globe, ImageIcon, Plus, Search, Trash2, Video } from "lucide-react";

import { API_ORIGIN } from "../../lib/api";
import { formatContentType } from "../../lib/labels";
import type { ContentItem, ContentType } from "../../types/domain";

function resolveAssetPath(path: string | null) {
  if (!path) {
    return null;
  }
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  return `${API_ORIGIN}${path}`;
}

function getContentIcon(type: ContentItem["type"]) {
  switch (type) {
    case "video":
      return Video;
    case "url":
      return Globe;
    case "html":
      return FileCode2;
    case "text":
      return FileText;
    default:
      return ImageIcon;
  }
}

const libraryFilters: Array<{ value: ContentType | "all"; label: string }> = [
  { value: "all", label: "Todo" },
  { value: "image", label: "Imagen" },
  { value: "video", label: "Video" },
  { value: "url", label: "URL" },
  { value: "html", label: "HTML" },
  { value: "text", label: "Texto" },
];

type OperationContentLibraryProps = {
  contents: ContentItem[];
  folderNameById: Record<string, string>;
  selectedFolderLabel: string;
  searchTerm: string;
  selectedFilter: ContentType | "all";
  onSearchTermChange: (value: string) => void;
  onFilterChange: (value: ContentType | "all") => void;
  onMoveContent: (content: ContentItem) => void;
  onAddToPlaylist: (contentId: string) => void;
  onDeleteContent: (content: ContentItem) => void;
  addingContentId: string | null;
  canEditPlaylist: boolean;
  canMoveContent: boolean;
  canDeleteContent: boolean;
  selectedCampaignName: string | null;
};

export function OperationContentLibrary({
  contents,
  folderNameById,
  selectedFolderLabel,
  searchTerm,
  selectedFilter,
  onSearchTermChange,
  onFilterChange,
  onMoveContent,
  onAddToPlaylist,
  onDeleteContent,
  addingContentId,
  canEditPlaylist,
  canMoveContent,
  canDeleteContent,
  selectedCampaignName,
}: OperationContentLibraryProps) {
  return (
    <div className="flex min-h-0 min-w-0 flex-col overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 pb-3">
        <div className="min-w-0">
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Archivos</p>
          <p className="mt-1 text-sm text-slate-600">Lista compacta estilo explorador para agregar piezas al timeline actual.</p>
          <p className="mt-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">Carpeta actual: {selectedFolderLabel}</p>
        </div>
        <span className="rounded-full bg-slate-100 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
          {contents.length} archivo(s)
        </span>
      </div>

      <div className="mt-3 flex flex-col gap-3">
        <div className="relative">
          <Search className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            className="pl-11"
            value={searchTerm}
            onChange={(event) => onSearchTermChange(event.target.value)}
            placeholder="Buscar por nombre, archivo, URL o texto"
          />
        </div>

        <div className="flex flex-wrap gap-2">
          {libraryFilters.map((filter) => (
            <button
              key={filter.value}
              className={[
                "rounded-full px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] transition",
                filter.value === selectedFilter
                  ? "bg-slate-900 text-white"
                  : "border border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:text-ink",
              ].join(" ")}
              type="button"
              onClick={() => onFilterChange(filter.value)}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>

      {!canEditPlaylist ? (
        <div className="mt-3 rounded-[16px] border border-slate-200 bg-slate-50 px-3 py-3 text-sm text-slate-600">
          Tu rol puede consultar la biblioteca, pero no modificar la secuencia de campanas.
        </div>
      ) : null}

      <div className="mt-3 min-h-0 flex-1 overflow-hidden rounded-[20px] border border-slate-200 bg-white">
        <div className="hidden grid-cols-[44px_minmax(0,1fr)_72px_64px_160px] items-center gap-2.5 border-b border-slate-200 bg-slate-50 px-3 py-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500 xl:grid">
          <span>Prev.</span>
          <span>Archivo</span>
          <span>Tipo</span>
          <span>Durac.</span>
          <span className="text-right">Acciones</span>
        </div>

        <div className="min-h-0 h-full overflow-y-auto">
          {contents.length > 0 ? (
            <div className="divide-y divide-slate-200">
              {contents.map((content) => {
                const assetPath = resolveAssetPath(content.file_path ?? content.source_url);
                const Icon = getContentIcon(content.type);
                const isAddingThisContent = addingContentId === content.id;
                const addDisabled = !canEditPlaylist || !selectedCampaignName || addingContentId !== null;

                return (
                  <article
                    key={content.id}
                    className="grid min-w-0 grid-cols-[44px_minmax(0,1fr)] gap-2.5 px-3 py-2.5 transition hover:bg-slate-50 xl:grid-cols-[44px_minmax(0,1fr)_72px_64px_160px]"
                  >
                    <div className="overflow-hidden rounded-xl border border-slate-200 bg-slate-100">
                      {content.type === "image" && assetPath ? (
                        <img alt={content.name} className="h-[44px] w-[44px] object-cover" src={assetPath} />
                      ) : content.type === "video" && assetPath ? (
                        <video className="h-[44px] w-[44px] object-cover" muted preload="metadata" src={assetPath} />
                      ) : (
                        <div className="grid h-[44px] w-[44px] place-items-center bg-gradient-to-br from-accentSoft to-emberSoft text-slate-700">
                          <Icon className="h-4 w-4" />
                        </div>
                      )}
                    </div>

                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <p className="truncate text-sm font-semibold text-ink" title={content.name}>
                          {content.name}
                        </p>
                        <span className="rounded-full bg-slate-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500 xl:hidden">
                          {formatContentType(content.type)}
                        </span>
                      </div>
                      <p className="mt-1 truncate text-xs text-slate-500">
                        {content.folder_id ? folderNameById[content.folder_id] ?? "Carpeta" : "Sin carpeta"} -{" "}
                        {content.text_content ?? content.source_url ?? content.file_path ?? "Listo para timeline"}
                      </p>
                    </div>

                    <div className="hidden min-w-0 items-center xl:flex">
                      <span className="truncate rounded-full bg-slate-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">
                        {formatContentType(content.type)}
                      </span>
                    </div>

                    <div className="hidden items-center text-xs font-medium text-slate-600 xl:flex">{content.duration_seconds}s</div>

                    <div className="col-span-2 flex flex-wrap items-center justify-between gap-2 xl:col-span-1 xl:min-w-0 xl:flex-nowrap xl:justify-end">
                      <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.14em] text-slate-500 xl:hidden">
                        <span>{content.duration_seconds}s</span>
                      </div>

                      <div className="flex min-w-0 flex-wrap items-center gap-1.5 xl:flex-nowrap">
                        {canMoveContent ? (
                          <button
                            className="rounded-full border border-slate-200 p-2 text-slate-500 transition hover:border-slate-300 hover:text-ink"
                            title="Mover a carpeta"
                            type="button"
                            onClick={() => onMoveContent(content)}
                          >
                            <FolderInput className="h-3.5 w-3.5" />
                          </button>
                        ) : null}
                        {canDeleteContent ? (
                          <button
                            className="rounded-full border border-slate-200 p-2 text-slate-500 transition hover:border-rose-300 hover:text-rose-600"
                            title="Eliminar contenido"
                            type="button"
                            onClick={() => onDeleteContent(content)}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        ) : null}
                        <button
                          className="inline-flex min-w-[92px] items-center justify-center gap-1 rounded-[12px] bg-ink px-2.5 py-2 text-[11px] font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
                          disabled={addDisabled}
                          title={selectedCampaignName ? `Agregar a ${selectedCampaignName}` : "Selecciona una campana para agregar contenido"}
                          type="button"
                          onClick={() => onAddToPlaylist(content.id)}
                        >
                          <Plus className="h-3.5 w-3.5" />
                          {isAddingThisContent ? "Agregando..." : "Agregar"}
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="grid h-full min-h-[220px] place-items-center rounded-[18px] bg-slate-50 px-5 text-center text-sm text-slate-600">
              No hay contenidos visibles para este filtro. Ajusta la busqueda o sube un archivo nuevo.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
