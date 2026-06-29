import { ChangeEvent, FormEvent, useMemo, useRef, useState } from "react";
import {
  FileCode2,
  FileText,
  Folder,
  FolderInput,
  FolderOpen,
  FolderPen,
  FolderPlus,
  Globe,
  ImageIcon,
  Plus,
  Search,
  Trash2,
  UploadCloud,
  Video,
} from "lucide-react";

import { API_ORIGIN } from "../../lib/api";
import { formatContentType } from "../../lib/labels";
import type { ContentFolder, ContentItem, ContentType } from "../../types/domain";
import { type OperationFolderScope } from "./OperationFolderTree";

type OperationLibraryPanelProps = {
  contents: ContentItem[];
  allContents: ContentItem[];
  folders: ContentFolder[];
  folderCounts: Record<string, number>;
  selectedFolderScope: OperationFolderScope;
  selectedFolderLabel: string;
  folderNameById: Record<string, string>;
  searchTerm: string;
  selectedFilter: ContentType | "all";
  onSearchTermChange: (value: string) => void;
  onFilterChange: (value: ContentType | "all") => void;
  onSelectFolderScope: (scope: OperationFolderScope) => void;
  onCreateRootFolder: () => void;
  onCreateChildFolder: (folder: ContentFolder) => void;
  onRenameFolder: (folder: ContentFolder) => void;
  onDeleteFolder: (folder: ContentFolder) => void;
  onMoveContent: (content: ContentItem) => void;
  onMoveContentToFolder: (contentId: string, folderId: string | null) => Promise<void> | void;
  onAddToPlaylist: (contentId: string) => void;
  onDeleteContent: (content: ContentItem) => void;
  onUpload: (payload: { file: File; name: string; durationSeconds: number }) => Promise<void>;
  onCreateUrlContent: (payload: { name: string; url: string; durationSeconds: number | null }) => Promise<void>;
  addingContentIds: string[];
  canEdit: boolean;
  selectedCampaignName: string | null;
};

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

function sortFolders(folders: ContentFolder[]) {
  return [...folders].sort((left, right) => {
    if (left.sort_order !== right.sort_order) {
      return left.sort_order - right.sort_order;
    }
    return left.name.localeCompare(right.name, "es");
  });
}

function resolveLocalVideoDuration(file: File): Promise<number | null> {
  if (!file.type.startsWith("video/")) {
    return Promise.resolve(null);
  }

  return new Promise((resolve) => {
    const video = document.createElement("video");
    const objectUrl = URL.createObjectURL(file);
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      const duration = Number.isFinite(video.duration) && video.duration > 0 ? Math.max(1, Math.round(video.duration)) : null;
      URL.revokeObjectURL(objectUrl);
      resolve(duration);
    };
    video.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(null);
    };
    video.src = objectUrl;
  });
}

export function OperationLibraryPanel({
  allContents,
  folders,
  folderCounts,
  selectedFolderScope,
  folderNameById,
  searchTerm,
  selectedFilter,
  onSearchTermChange,
  onFilterChange,
  onSelectFolderScope,
  onCreateRootFolder,
  onCreateChildFolder,
  onRenameFolder,
  onDeleteFolder,
  onMoveContent,
  onMoveContentToFolder,
  onAddToPlaylist,
  onDeleteContent,
  onUpload,
  onCreateUrlContent,
  addingContentIds,
  canEdit,
  selectedCampaignName,
}: OperationLibraryPanelProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [uploadName, setUploadName] = useState("");
  const [uploadDurationSeconds, setUploadDurationSeconds] = useState(15);
  const [uploading, setUploading] = useState(false);
  const [urlName, setUrlName] = useState("");
  const [urlValue, setUrlValue] = useState("");
  const [urlDurationSeconds, setUrlDurationSeconds] = useState("15");
  const [creatingUrlContent, setCreatingUrlContent] = useState(false);
  const [draggingContentId, setDraggingContentId] = useState<string | null>(null);
  const [dropFolderId, setDropFolderId] = useState<string | null>(null);
  const orderedFolders = useMemo(() => sortFolders(folders), [folders]);

  const currentFolder = useMemo(
    () => (selectedFolderScope === "all" || selectedFolderScope === "uncategorized" ? null : folders.find((folder) => folder.id === selectedFolderScope) ?? null),
    [folders, selectedFolderScope],
  );

  const currentFolderChildren = useMemo(
    () => orderedFolders.filter((folder) => folder.parent_id === (currentFolder?.id ?? null)),
    [currentFolder?.id, orderedFolders],
  );

  const visibleFiles = useMemo(() => {
    const normalizedSearch = searchTerm.trim().toLowerCase();

    return allContents.filter((content) => {
      const isInsideCurrentFolder = currentFolder ? content.folder_id === currentFolder.id : !content.folder_id;
      if (!isInsideCurrentFolder) {
        return false;
      }

      if (selectedFilter !== "all" && content.type !== selectedFilter) {
        return false;
      }

      if (!normalizedSearch) {
        return true;
      }

      const haystack = [content.name, content.type, content.text_content ?? "", content.source_url ?? "", content.file_path ?? ""]
        .join(" ")
        .toLowerCase();
      return haystack.includes(normalizedSearch);
    });
  }, [allContents, currentFolder, searchTerm, selectedFilter]);

  const folderPath = useMemo(() => {
    if (!currentFolder) {
      return [];
    }

    const path: ContentFolder[] = [];
    let cursor: ContentFolder | null = currentFolder;
    while (cursor) {
      path.unshift(cursor);
      cursor = cursor.parent_id ? folders.find((folder) => folder.id === cursor?.parent_id) ?? null : null;
    }
    return path;
  }, [currentFolder, folders]);

  async function handlePickFile(event: ChangeEvent<HTMLInputElement>) {
    const nextFile = event.target.files?.[0] ?? null;
    setPendingFile(nextFile);
    setUploadName(nextFile ? nextFile.name.replace(/\.[^.]+$/, "") : "");
    if (!nextFile) {
      setUploadDurationSeconds(15);
      return;
    }
    const detectedDuration = await resolveLocalVideoDuration(nextFile);
    setUploadDurationSeconds(detectedDuration ?? 15);
  }

  async function handleUploadSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!pendingFile || uploading) {
      return;
    }

    setUploading(true);
    try {
      await onUpload({
        file: pendingFile,
        name: uploadName.trim() || pendingFile.name.replace(/\.[^.]+$/, ""),
        durationSeconds: uploadDurationSeconds,
      });
      setPendingFile(null);
      setUploadName("");
      setUploadDurationSeconds(15);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    } finally {
      setUploading(false);
    }
  }

  async function handleCreateUrlSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creatingUrlContent) {
      return;
    }

    setCreatingUrlContent(true);
    try {
      await onCreateUrlContent({
        name: urlName.trim(),
        url: urlValue.trim(),
        durationSeconds: urlDurationSeconds.trim() ? Number(urlDurationSeconds) || 1 : null,
      });
      setUrlName("");
      setUrlValue("");
      setUrlDurationSeconds("15");
    } finally {
      setCreatingUrlContent(false);
    }
  }

  const explorerItemCount = currentFolderChildren.length + visibleFiles.length;
  const urlSubmitDisabled =
    !canEdit ||
    !selectedCampaignName ||
    !urlName.trim() ||
    !urlValue.trim() ||
    creatingUrlContent;

  return (
    <div id="operation-library-panel" className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden px-4 py-4">
      <div className="border-b border-slate-200 pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Explorador de archivos</p>
            <p className="mt-1 text-sm text-slate-600">Carpetas y archivos juntos, con vista compacta tipo Windows para organizar la biblioteca.</p>
          </div>
          <span className="rounded-full bg-slate-100 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
            {allContents.length} activos
          </span>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            className="inline-flex items-center gap-2 rounded-full bg-ink px-4 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
            disabled={!canEdit}
            type="button"
            onClick={() => fileInputRef.current?.click()}
          >
            <UploadCloud className="h-3.5 w-3.5" />
            Subir archivos
          </button>
          <button
            className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-600 transition hover:border-slate-300 hover:text-ink disabled:cursor-not-allowed disabled:opacity-60"
            disabled={!canEdit}
            type="button"
            onClick={() => (currentFolder ? onCreateChildFolder(currentFolder) : onCreateRootFolder())}
          >
            <FolderPlus className="h-3.5 w-3.5" />
            Crear carpeta
          </button>
          {currentFolder ? (
            <button
              className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-4 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-600 transition hover:border-slate-300 hover:bg-white hover:text-ink"
              type="button"
              onClick={() => onSelectFolderScope(currentFolder.parent_id ?? "all")}
            >
              <FolderOpen className="h-3.5 w-3.5" />
              Volver
            </button>
          ) : null}
        </div>

        <input ref={fileInputRef} className="hidden" type="file" accept="image/*,video/*" onChange={handlePickFile} />

        {pendingFile ? (
          <form className="mt-3 rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3" onSubmit={handleUploadSubmit}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Archivo listo para subir</p>
                <p className="mt-1 truncate text-sm font-semibold text-ink" title={pendingFile.name}>
                  {pendingFile.name}
                </p>
              </div>
              <span className="rounded-full bg-white px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500 shadow-sm">Media</span>
            </div>

            <div className="mt-3 grid gap-2 xl:grid-cols-[minmax(0,1fr)_90px_auto]">
              <input value={uploadName} onChange={(event) => setUploadName(event.target.value)} disabled={uploading} placeholder="Nombre visible" />
              <input
                type="number"
                min={1}
                value={uploadDurationSeconds}
                onChange={(event) => setUploadDurationSeconds(Number(event.target.value) || 1)}
                disabled={uploading}
                placeholder="Seg"
              />
              <button
                className="rounded-[14px] bg-ink px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
                disabled={uploading}
                type="submit"
              >
                {uploading ? "Subiendo..." : "Subir"}
              </button>
            </div>
          </form>
        ) : null}

        <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
          <button
            className={[
              "rounded-full px-3 py-1.5 transition",
              !currentFolder ? "bg-slate-900 text-white" : "border border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-ink",
            ].join(" ")}
            type="button"
            onClick={() => onSelectFolderScope("all")}
          >
            General
          </button>
          {folderPath.map((folder) => (
            <button
              key={folder.id}
              className={[
                "rounded-full px-3 py-1.5 transition",
                currentFolder?.id === folder.id ? "bg-accentSoft text-accent" : "border border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-ink",
              ].join(" ")}
              type="button"
              onClick={() => onSelectFolderScope(folder.id)}
            >
              {folder.name}
            </button>
          ))}
          <span className="rounded-full bg-slate-100 px-3 py-1.5">{explorerItemCount} visible(s)</span>
        </div>
      </div>

      <div className="mt-3 flex flex-col gap-3">
        <div className="relative">
          <Search className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            className="pl-11 text-sm"
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

        {selectedFilter === "url" ? (
          <form className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3" onSubmit={handleCreateUrlSubmit}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Agregar URL / Streaming</p>
                <p className="mt-1 text-sm text-slate-600">Pega una página web, un stream o un enlace embebible para guardarlo y sumarlo a la campaña activa.</p>
              </div>
              <span className="rounded-full bg-white px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500 shadow-sm">URL</span>
            </div>

            <div className="mt-4 space-y-3">
              <label className="block">
                <span className="mb-2 block text-sm font-semibold text-slate-700">Nombre del contenido</span>
                <input
                  value={urlName}
                  onChange={(event) => setUrlName(event.target.value)}
                  disabled={creatingUrlContent}
                  placeholder="Ej. Canal en vivo / YouTube demo"
                />
              </label>

              <label className="block">
                <span className="mb-2 block text-sm font-semibold text-slate-700">URL</span>
                <input
                  value={urlValue}
                  onChange={(event) => setUrlValue(event.target.value)}
                  disabled={creatingUrlContent}
                  placeholder="https://..."
                  type="text"
                />
              </label>

              <label className="block">
                <span className="mb-2 block text-sm font-semibold text-slate-700">Duración en segundos</span>
                <input
                  type="number"
                  min={1}
                  value={urlDurationSeconds}
                  onChange={(event) => setUrlDurationSeconds(event.target.value)}
                  disabled={creatingUrlContent}
                  placeholder="15"
                />
              </label>

              <button
                className="w-full rounded-[16px] bg-ink px-4 py-3 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
                disabled={urlSubmitDisabled}
                type="submit"
              >
                {creatingUrlContent ? "Guardando..." : "Guardar y agregar a campaña"}
              </button>
            </div>

            <div className="mt-4 space-y-2 rounded-[16px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600">
              <p>
                <span className="font-semibold text-slate-700">Carpeta actual:</span> {currentFolder?.name ?? "General"}
              </p>
              <p>
                <span className="font-semibold text-slate-700">Campaña activa:</span> {selectedCampaignName ?? "Selecciona una campaña"}
              </p>
              <p>
                <span className="font-semibold text-slate-700">Duración:</span> {urlDurationSeconds.trim() || "15"}s
              </p>
            </div>

            {!selectedCampaignName ? (
              <p className="mt-3 text-xs text-amber-700">Selecciona una campaña en el panel izquierdo para guardar la URL y agregarla al timeline.</p>
            ) : null}
          </form>
        ) : null}
      </div>

      <div className="mt-3 min-h-0 flex-1 overflow-hidden rounded-[20px] border border-slate-200 bg-white">
        <div className="grid grid-cols-[44px_minmax(0,1fr)_52px_148px] items-center gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-slate-500">
          <span>Item</span>
          <span>Nombre</span>
          <span>Durac.</span>
          <span className="text-right">Acciones</span>
        </div>

        <div className="min-h-0 h-full overflow-y-auto">
          {currentFolderChildren.length > 0 || visibleFiles.length > 0 ? (
            <div className="divide-y divide-slate-200">
              {currentFolderChildren.map((folder) => {
                const isDropTarget = dropFolderId === folder.id && draggingContentId !== null;

                return (
                  <div
                    key={folder.id}
                    className={[
                      "grid min-w-0 grid-cols-[44px_minmax(0,1fr)_52px_148px] items-center gap-2 px-3 py-2.5 transition",
                      isDropTarget ? "bg-cyan-50" : "hover:bg-slate-50",
                    ].join(" ")}
                    onDragOver={(event) => {
                      if (!draggingContentId) {
                        return;
                      }
                      event.preventDefault();
                      setDropFolderId(folder.id);
                    }}
                    onDragLeave={() => {
                      if (dropFolderId === folder.id) {
                        setDropFolderId(null);
                      }
                    }}
                    onDrop={async (event) => {
                      event.preventDefault();
                      if (!draggingContentId) {
                        return;
                      }
                      setDropFolderId(null);
                      setDraggingContentId(null);
                      await onMoveContentToFolder(draggingContentId, folder.id);
                    }}
                  >
                    <button
                      className="grid h-[40px] w-[40px] place-items-center rounded-xl border border-slate-200 bg-slate-50 text-slate-600"
                      type="button"
                      onClick={() => onSelectFolderScope(folder.id)}
                    >
                      <Folder className="h-4 w-4" />
                    </button>

                    <button className="min-w-0 text-left" type="button" onClick={() => onSelectFolderScope(folder.id)}>
                      <p className="truncate text-sm font-semibold text-ink" title={folder.name}>
                        {folder.name}
                      </p>
                      <p className="mt-1 truncate text-xs text-slate-500">Carpeta - {folderCounts[folder.id] ?? 0} archivo(s)</p>
                    </button>

                    <span className="text-xs text-slate-400">-</span>

                    <div className="flex min-w-0 items-center justify-end gap-1">
                      <button
                        className="rounded-full border border-slate-200 bg-white p-2 text-slate-500 transition hover:border-slate-300 hover:text-ink"
                        title="Crear subcarpeta"
                        type="button"
                        onClick={() => onCreateChildFolder(folder)}
                      >
                        <FolderPlus className="h-3.5 w-3.5" />
                      </button>
                      <button
                        className="rounded-full border border-slate-200 bg-white p-2 text-slate-500 transition hover:border-slate-300 hover:text-ink"
                        title="Renombrar carpeta"
                        type="button"
                        onClick={() => onRenameFolder(folder)}
                      >
                        <FolderPen className="h-3.5 w-3.5" />
                      </button>
                      <button
                        className="rounded-full border border-slate-200 bg-white p-2 text-slate-500 transition hover:border-rose-300 hover:text-rose-600"
                        title="Eliminar carpeta"
                        type="button"
                        onClick={() => onDeleteFolder(folder)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                );
              })}

              {visibleFiles.map((content) => {
                const assetPath = resolveAssetPath(content.file_path ?? content.source_url);
                const Icon = getContentIcon(content.type);
                const isAddingThisContent = addingContentIds.includes(content.id);
                const addDisabled = !canEdit || !selectedCampaignName || isAddingThisContent;

                return (
                  <div
                    key={content.id}
                    className="grid min-w-0 grid-cols-[44px_minmax(0,1fr)_52px_148px] items-center gap-2 px-3 py-2.5 transition hover:bg-slate-50"
                    draggable={canEdit}
                    onDragStart={() => setDraggingContentId(content.id)}
                    onDragEnd={() => {
                      setDraggingContentId(null);
                      setDropFolderId(null);
                    }}
                  >
                    <div className="overflow-hidden rounded-xl border border-slate-200 bg-slate-100">
                      {content.type === "image" && assetPath ? (
                        <img alt={content.name} className="h-[40px] w-[40px] object-cover" src={assetPath} />
                      ) : content.type === "video" && assetPath ? (
                        <video className="h-[40px] w-[40px] object-cover" muted preload="metadata" src={assetPath} />
                      ) : (
                        <div className="grid h-[40px] w-[40px] place-items-center bg-gradient-to-br from-accentSoft to-emberSoft text-slate-700">
                          <Icon className="h-4 w-4" />
                        </div>
                      )}
                    </div>

                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-ink" title={content.name}>
                        {content.name}
                      </p>
                      <div className="mt-1 flex min-w-0 items-center gap-2 text-xs text-slate-500">
                        <span className="shrink-0 rounded-full bg-slate-100 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">
                          {formatContentType(content.type)}
                        </span>
                        <span className="truncate">
                          {content.folder_id ? folderNameById[content.folder_id] ?? "Carpeta" : "General"} -{" "}
                          {content.text_content ?? content.source_url ?? content.file_path ?? "Listo para timeline"}
                        </span>
                      </div>
                    </div>

                    <span className="text-xs font-medium text-slate-600">{content.duration_seconds}s</span>

                    <div className="flex min-w-0 items-center justify-end gap-1">
                      <button
                        className="rounded-full border border-slate-200 bg-white p-2 text-slate-500 transition hover:border-slate-300 hover:text-ink"
                        title="Mover a carpeta"
                        type="button"
                        onClick={() => onMoveContent(content)}
                      >
                        <FolderInput className="h-3.5 w-3.5" />
                      </button>
                      <button
                        className="rounded-full border border-slate-200 bg-white p-2 text-slate-500 transition hover:border-rose-300 hover:text-rose-600"
                        title="Eliminar contenido"
                        type="button"
                        onClick={() => onDeleteContent(content)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                      <button
                        className="inline-flex min-w-[78px] items-center justify-center gap-1 rounded-[12px] bg-ink px-2 py-2 text-[11px] font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
                        disabled={addDisabled}
                        title={selectedCampaignName ? `Agregar a ${selectedCampaignName}` : "Selecciona una campaña para agregar contenido"}
                        type="button"
                        onClick={() => onAddToPlaylist(content.id)}
                      >
                        <Plus className="h-3.5 w-3.5" />
                        {isAddingThisContent ? "Agregando..." : "Agregar"}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="grid h-full min-h-[240px] place-items-center px-5 text-center text-sm text-slate-600">
              <div>
                <FolderOpen className="mx-auto h-8 w-8 text-slate-400" />
                <p className="mt-3 font-semibold text-ink">Explorador vacio</p>
                <p className="mt-2">
                  {currentFolder
                    ? `No hay elementos visibles dentro de ${currentFolder.name}.`
                    : "No hay archivos sueltos ni carpetas visibles en el explorador principal."}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

