import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { ContentFolderDialog } from "../../components/operations/ContentFolderDialog";
import { DeleteConfirmDialog } from "../../components/operations/DeleteConfirmDialog";
import { MoveContentFolderDialog } from "../../components/operations/MoveContentFolderDialog";
import { OperationCampaignSidebar } from "../../components/operations/OperationCampaignSidebar";
import { OperationItemInspector } from "../../components/operations/OperationItemInspector";
import { OperationLibraryPanel } from "../../components/operations/OperationLibraryPanel";
import { OperationPreviewSidebar } from "../../components/operations/OperationPreviewSidebar";
import { OperationSequenceTimeline } from "../../components/operations/OperationSequenceTimeline";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { decorateResolvedSequenceEntries, enrichSequenceEntries, getSequenceTotalDuration, type PreviewSequenceEntry } from "../../lib/preview";
import { canWriteClientScope } from "../../lib/rbac";
import type {
  Campaign,
  CampaignSequenceItem,
  CampaignSequencePreviewPayload,
  ContentFolder,
  ContentFolderDeleteImpact,
  ContentItem,
  ContentType,
  Layout,
} from "../../types/domain";

const paneClassName = "min-w-0 overflow-hidden rounded-[24px] border border-slate-200 bg-white/95 shadow-sm backdrop-blur";
const allowedStudioTypes = new Set<ContentType>(["image", "video", "url", "html", "text"]);

type DeleteTarget =
  | { kind: "content"; entity: ContentItem }
  | { kind: "folder"; entity: ContentFolder }
  | { kind: "sequence_item"; entity: PreviewSequenceEntry }
  | { kind: "campaign"; entity: Campaign }
  | null;

type FolderScope = "all" | "uncategorized" | string;

type FolderDialogState =
  | { mode: "create_root" }
  | { mode: "create_child"; folder: ContentFolder }
  | { mode: "rename"; folder: ContentFolder }
  | null;

type OperationNotice =
  | {
      tone: "success" | "info";
      message: string;
    }
  | null;

function resolveApiErrorMessage(error: unknown, fallback: string) {
  if (!(error instanceof Error)) {
    return fallback;
  }

  if (error.message === "Failed to fetch") {
    return "No se pudo conectar con el backend. Revisa que el servidor siga en línea.";
  }

  try {
    const parsed = JSON.parse(error.message) as { detail?: string };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return parsed.detail;
    }
  } catch {
    // noop
  }

  if (/^Request failed with status \d+$/.test(error.message)) {
    return fallback;
  }

  return error.message || fallback;
}

function buildPlaybackOrder(length: number, mode: "sequential" | "random") {
  const indexes = Array.from({ length }, (_, index) => index);
  if (mode === "sequential" || length <= 1) {
    return indexes;
  }

  const shuffled = indexes.slice();
  for (let index = shuffled.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [shuffled[index], shuffled[swapIndex]] = [shuffled[swapIndex], shuffled[index]];
  }
  return shuffled;
}

export function ClientOperationPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const [contents, setContents] = useState<ContentItem[]>([]);
  const [folders, setFolders] = useState<ContentFolder[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [sequenceItems, setSequenceItems] = useState<CampaignSequenceItem[]>([]);
  const [sequencePreview, setSequencePreview] = useState<CampaignSequencePreviewPayload | null>(null);
  const [selectedCampaignId, setSelectedCampaignId] = useState("");
  const [selectedSequenceItemId, setSelectedSequenceItemId] = useState<string | null>(null);
  const [selectedFolderScope, setSelectedFolderScope] = useState<FolderScope>("all");
  const [contentSearch, setContentSearch] = useState("");
  const [contentTypeFilter, setContentTypeFilter] = useState<ContentType | "all">("all");
  const [playbackMode, setPlaybackMode] = useState<"sequential" | "random">("sequential");
  const [showCampaignComposer, setShowCampaignComposer] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<OperationNotice>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget>(null);
  const [folderDialogState, setFolderDialogState] = useState<FolderDialogState>(null);
  const [moveTargetContent, setMoveTargetContent] = useState<ContentItem | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isSavingFolder, setIsSavingFolder] = useState(false);
  const [isMovingContent, setIsMovingContent] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [addingContentIds, setAddingContentIds] = useState<string[]>([]);
  const [folderDeleteImpact, setFolderDeleteImpact] = useState<ContentFolderDeleteImpact | null>(null);
  const [folderDeleteImpactLoading, setFolderDeleteImpactLoading] = useState(false);
  const selectedCampaignIdRef = useRef("");
  const sequenceRequestIdRef = useRef(0);
  const previewRequestIdRef = useRef(0);
  const addSequenceQueueRef = useRef<Promise<void>>(Promise.resolve());

  const canEditOperation = canWriteClientScope(user?.role);

  async function loadContentsCatalog() {
    if (!token || !clientId) {
      return [];
    }

    const response = await apiRequest<ContentItem[]>(`/contents?client_id=${clientId}`, { token });
    setContents(response);
    return response;
  }

  async function loadFolders() {
    if (!token || !clientId) {
      return [];
    }

    const response = await apiRequest<ContentFolder[]>(`/content-folders?client_id=${clientId}`, { token });
    setFolders(response);
    return response;
  }

  async function loadCampaignCatalog() {
    if (!token || !clientId) {
      return [];
    }

    const response = await apiRequest<Campaign[]>(`/campaigns?client_id=${clientId}`, { token });
    setCampaigns(response);
    setSelectedCampaignId((current) => {
      if (current && response.some((campaign) => campaign.id === current)) {
        return current;
      }
      return response[0]?.id ?? "";
    });
    return response;
  }

  async function loadOperationData() {
    if (!token || !clientId) {
      return;
    }

    try {
      const [contentsResponse, foldersResponse, campaignsResponse, layoutsResponse] = await Promise.all([
        apiRequest<ContentItem[]>(`/contents?client_id=${clientId}`, { token }),
        apiRequest<ContentFolder[]>(`/content-folders?client_id=${clientId}`, { token }),
        apiRequest<Campaign[]>(`/campaigns?client_id=${clientId}`, { token }),
        apiRequest<Layout[]>(`/layouts?client_id=${clientId}`, { token }),
      ]);

      setContents(contentsResponse);
      setFolders(foldersResponse);
      setCampaigns(campaignsResponse);
      setLayouts(layoutsResponse);
      setSelectedCampaignId((current) => {
        if (current && campaignsResponse.some((campaign) => campaign.id === current)) {
          return current;
        }
        return campaignsResponse[0]?.id ?? "";
      });
      setError(null);
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo cargar Campaign Studio"));
    }
  }

  async function loadSequence(campaignId: string) {
    if (!token || !campaignId) {
      setSequenceItems([]);
      return [];
    }

    const requestId = ++sequenceRequestIdRef.current;
    try {
      const response = await apiRequest<CampaignSequenceItem[]>(`/campaigns/${campaignId}/sequence`, { token });
      if (requestId !== sequenceRequestIdRef.current) {
        return response;
      }
      setSequenceItems(response);
      setError(null);
      return response;
    } catch (nextError) {
      if (requestId !== sequenceRequestIdRef.current) {
        return [];
      }
      setSequenceItems([]);
      setError(resolveApiErrorMessage(nextError, "No se pudo cargar la timeline de la campaña"));
      return [];
    }
  }

  async function loadSequencePreview(campaignId: string) {
    if (!token || !campaignId) {
      setSequencePreview(null);
      return null;
    }

    const requestId = ++previewRequestIdRef.current;
    setPreviewLoading(true);
    try {
      const response = await apiRequest<CampaignSequencePreviewPayload>(`/campaigns/${campaignId}/sequence-preview`, { token });
      if (requestId !== previewRequestIdRef.current) {
        return response;
      }
      setSequencePreview(response);
      setError(null);
      return response;
    } catch (nextError) {
      if (requestId !== previewRequestIdRef.current) {
        return null;
      }
      setSequencePreview(null);
      setError(resolveApiErrorMessage(nextError, "No se pudo cargar el preview de runtime de la campaña"));
      return null;
    } finally {
      if (requestId === previewRequestIdRef.current) {
        setPreviewLoading(false);
      }
    }
  }

  useEffect(() => {
    void loadOperationData();
  }, [clientId, token]);

  useEffect(() => {
    if (selectedCampaignId) {
      void loadSequence(selectedCampaignId);
      void loadSequencePreview(selectedCampaignId);
      return;
    }
    setSequenceItems([]);
    setSequencePreview(null);
  }, [selectedCampaignId, token]);

  useEffect(() => {
    if (selectedSequenceItemId && sequenceItems.some((item) => item.id === selectedSequenceItemId)) {
      return;
    }
    setSelectedSequenceItemId(sequenceItems[0]?.id ?? null);
  }, [sequenceItems, selectedSequenceItemId]);

  useEffect(() => {
    selectedCampaignIdRef.current = selectedCampaignId;
  }, [selectedCampaignId]);

  useEffect(() => {
    if (selectedFolderScope === "all" || selectedFolderScope === "uncategorized") {
      return;
    }
    if (!folders.some((folder) => folder.id === selectedFolderScope)) {
      setSelectedFolderScope("all");
    }
  }, [folders, selectedFolderScope]);

  useEffect(() => {
    if (!token || deleteTarget?.kind !== "folder") {
      setFolderDeleteImpact(null);
      setFolderDeleteImpactLoading(false);
      return;
    }

    let cancelled = false;
    setFolderDeleteImpactLoading(true);
    void apiRequest<ContentFolderDeleteImpact>(`/content-folders/${deleteTarget.entity.id}/delete-impact`, { token })
      .then((response) => {
        if (!cancelled) {
          setFolderDeleteImpact(response);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setFolderDeleteImpact(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setFolderDeleteImpactLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [deleteTarget, token]);

  const selectedCampaign = useMemo(
    () => campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? null,
    [campaigns, selectedCampaignId],
  );

  useEffect(() => {
    setPlaybackMode(selectedCampaign?.playback_mode ?? "sequential");
  }, [selectedCampaign?.id, selectedCampaign?.playback_mode]);

  const libraryContents = useMemo(() => contents.filter((content) => allowedStudioTypes.has(content.type)), [contents]);
  const folderNameById = useMemo(
    () =>
      folders.reduce<Record<string, string>>((accumulator, folder) => {
        accumulator[folder.id] = folder.name;
        return accumulator;
      }, {}),
    [folders],
  );
  const selectedFolder = useMemo(
    () => (selectedFolderScope === "all" || selectedFolderScope === "uncategorized" ? null : folders.find((folder) => folder.id === selectedFolderScope) ?? null),
    [folders, selectedFolderScope],
  );
  const selectedFolderLabel = useMemo(() => {
    if (selectedFolderScope === "all") {
      return "Todos";
    }
    if (selectedFolderScope === "uncategorized") {
      return "Sin carpeta";
    }
    return selectedFolder?.name ?? "Carpeta";
  }, [selectedFolder, selectedFolderScope]);

  const folderCounts = useMemo(() => {
    const directCounts = folders.reduce<Record<string, number>>((accumulator, folder) => {
      accumulator[folder.id] = 0;
      return accumulator;
    }, {});
    for (const content of libraryContents) {
      if (content.folder_id) {
        directCounts[content.folder_id] = (directCounts[content.folder_id] ?? 0) + 1;
      }
    }

    const childrenByParent = folders.reduce<Record<string, ContentFolder[]>>((accumulator, folder) => {
      const key = folder.parent_id ?? "root";
      accumulator[key] = [...(accumulator[key] ?? []), folder];
      return accumulator;
    }, {});

    const recursiveCounts: Record<string, number> = {};
    const countFolder = (folderId: string): number => {
      if (recursiveCounts[folderId] !== undefined) {
        return recursiveCounts[folderId];
      }
      const children = childrenByParent[folderId] ?? [];
      const total = (directCounts[folderId] ?? 0) + children.reduce((sum: number, child) => sum + countFolder(child.id), 0);
      recursiveCounts[folderId] = total;
      return total;
    };

    for (const folder of folders) {
      countFolder(folder.id);
    }

    return recursiveCounts;
  }, [folders, libraryContents]);

  const folderDescendantsById = useMemo(() => {
    const childrenByParent = folders.reduce<Record<string, string[]>>((accumulator, folder) => {
      const key = folder.parent_id ?? "root";
      accumulator[key] = [...(accumulator[key] ?? []), folder.id];
      return accumulator;
    }, {});

    const descendants: Record<string, string[]> = {};
    const collect = (folderId: string): string[] => {
      if (descendants[folderId]) {
        return descendants[folderId];
      }
      const childIds = childrenByParent[folderId] ?? [];
      const nested = childIds.flatMap((childId) => collect(childId));
      descendants[folderId] = [folderId, ...nested];
      return descendants[folderId];
    };

    for (const folder of folders) {
      collect(folder.id);
    }

    return descendants;
  }, [folders]);

  const filteredContents = useMemo(() => {
    const normalizedSearch = contentSearch.trim().toLowerCase();
    const selectedFolderIds =
      selectedFolderScope !== "all" && selectedFolderScope !== "uncategorized"
        ? new Set(folderDescendantsById[selectedFolderScope] ?? [selectedFolderScope])
        : null;

    return libraryContents.filter((content) => {
      if (selectedFolderScope === "uncategorized" && content.folder_id) {
        return false;
      }
      if (
        selectedFolderScope !== "all" &&
        selectedFolderScope !== "uncategorized" &&
        (!content.folder_id || !selectedFolderIds?.has(content.folder_id))
      ) {
        return false;
      }

      const matchesType = contentTypeFilter === "all" || content.type === contentTypeFilter;
      if (!matchesType) {
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
  }, [contentSearch, contentTypeFilter, folderDescendantsById, libraryContents, selectedFolderScope]);

  const sequenceEntries = useMemo(() => enrichSequenceEntries(sequenceItems, contents, layouts), [contents, layouts, sequenceItems]);
  const runtimePreviewEntries = useMemo(
    () => (sequencePreview ? decorateResolvedSequenceEntries(sequencePreview.resolved_items) : sequenceEntries),
    [sequenceEntries, sequencePreview],
  );
  const editableTimelineEntries = useMemo(
    () => runtimePreviewEntries.filter((entry) => entry.is_enabled),
    [runtimePreviewEntries],
  );
  const totalSequenceDuration = useMemo(
    () => sequencePreview?.duration_total ?? getSequenceTotalDuration(editableTimelineEntries),
    [editableTimelineEntries, sequencePreview?.duration_total],
  );
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackOrder, setPlaybackOrder] = useState<number[]>([]);
  const [playbackCursor, setPlaybackCursor] = useState(0);
  const [elapsedMs, setElapsedMs] = useState(0);
  const playbackSignature = useMemo(
    () =>
      `${sequencePreview?.runtime_signature ?? "local"}:${editableTimelineEntries
        .map((entry) => `${entry.id}:${entry.sort_order}:${entry.duration_seconds}:${entry.is_enabled}`)
        .join("|")}`,
    [editableTimelineEntries, sequencePreview?.runtime_signature],
  );

  useEffect(() => {
    setPlaybackOrder(buildPlaybackOrder(editableTimelineEntries.length, playbackMode));
    setPlaybackCursor(0);
    setElapsedMs(0);
    setSelectedSequenceItemId(editableTimelineEntries[0]?.id ?? null);
    setIsPlaying(false);
  }, [editableTimelineEntries.length, playbackMode, playbackSignature, selectedCampaignId]);

  useEffect(() => {
    if (!isPlaying || editableTimelineEntries.length === 0) {
      return;
    }

    const timer = window.setInterval(() => {
      setElapsedMs((current) => {
        const activeIndex = playbackOrder[playbackCursor] ?? 0;
        const activeEntry = editableTimelineEntries[activeIndex] ?? null;
        const itemDurationMs = Math.max(1000, (activeEntry?.duration_seconds ?? 1) * 1000);
        const nextElapsed = current + 200;

        if (nextElapsed < itemDurationMs) {
          return nextElapsed;
        }

        setPlaybackCursor((cursor) => {
          const nextCursor = cursor + 1;
          if (nextCursor < playbackOrder.length) {
            const nextEntry = editableTimelineEntries[playbackOrder[nextCursor] ?? 0];
            if (nextEntry) {
              setSelectedSequenceItemId(nextEntry.id);
            }
            return nextCursor;
          }
          const nextOrder = buildPlaybackOrder(editableTimelineEntries.length, playbackMode);
          setPlaybackOrder(nextOrder);
          const wrappedEntry = editableTimelineEntries[nextOrder[0] ?? 0];
          if (wrappedEntry) {
            setSelectedSequenceItemId(wrappedEntry.id);
          }
          return 0;
        });
        return 0;
      });
    }, 200);

    return () => window.clearInterval(timer);
  }, [editableTimelineEntries, isPlaying, playbackMode, playbackCursor, playbackOrder]);

  const playbackEntries = useMemo(
    () => playbackOrder.map((index) => editableTimelineEntries[index]).filter(Boolean),
    [editableTimelineEntries, playbackOrder],
  );
  const timelineDisplayEntries = useMemo(
    () => (playbackMode === "random" && playbackEntries.length > 0 ? playbackEntries : editableTimelineEntries),
    [editableTimelineEntries, playbackEntries, playbackMode],
  );
  const currentPlaybackIndex = playbackOrder[playbackCursor] ?? 0;
  const currentEntry = editableTimelineEntries[currentPlaybackIndex] ?? null;
  const nextPlaybackIndex = playbackOrder.length > 0 ? playbackOrder[(playbackCursor + 1) % playbackOrder.length] ?? 0 : 0;
  const nextEntry = editableTimelineEntries[nextPlaybackIndex] ?? null;
  const currentItemDurationSeconds = Math.max(1, currentEntry?.duration_seconds ?? 1);
  const currentLoopTimeSeconds = useMemo(() => {
    if (playbackEntries.length === 0) {
      return 0;
    }

    const elapsedSeconds = elapsedMs / 1000;
    const completedSeconds = playbackEntries.slice(0, playbackCursor).reduce((sum, entry) => sum + entry.duration_seconds, 0);
    return Math.max(0, Math.min(totalSequenceDuration, completedSeconds + elapsedSeconds));
  }, [elapsedMs, playbackCursor, playbackEntries, totalSequenceDuration]);
  const progressPercent = currentItemDurationSeconds > 0 ? (elapsedMs / (currentItemDurationSeconds * 1000)) * 100 : 0;
  const selectedEntry = useMemo(
    () => timelineDisplayEntries.find((entry) => entry.id === selectedSequenceItemId) ?? timelineDisplayEntries[0] ?? null,
    [selectedSequenceItemId, timelineDisplayEntries],
  );

  function pausePreviewPlayback() {
    setIsPlaying(false);
  }

  function handleSelectAndSeekItem(sequenceItemId: string, startSeconds: number) {
    pausePreviewPlayback();
    setSelectedSequenceItemId(sequenceItemId);

    const entryIndex = playbackEntries.findIndex((entry) => entry.id === sequenceItemId);
    if (entryIndex >= 0) {
      setPlaybackCursor(entryIndex);
      setElapsedMs(0);
      return;
    }

    if (totalSequenceDuration <= 0) {
      return;
    }

    const targetSeconds = Math.max(0, Math.min(totalSequenceDuration, startSeconds));
    let accumulated = 0;
    for (let index = 0; index < playbackEntries.length; index += 1) {
      const entry = playbackEntries[index];
      const nextAccumulated = accumulated + entry.duration_seconds;
      if (targetSeconds < nextAccumulated || index === playbackEntries.length - 1) {
        setPlaybackCursor(index);
        setElapsedMs(Math.max(0, targetSeconds - accumulated) * 1000);
        return;
      }
      accumulated = nextAccumulated;
    }
  }

  function handleSeekTimeline(seconds: number) {
    pausePreviewPlayback();
    const targetSeconds = Math.max(0, Math.min(totalSequenceDuration, seconds));
    if (playbackEntries.length === 0) {
      setElapsedMs(0);
      return;
    }

    let accumulated = 0;
    for (let index = 0; index < playbackEntries.length; index += 1) {
      const entry = playbackEntries[index];
      const nextAccumulated = accumulated + entry.duration_seconds;
      if (targetSeconds < nextAccumulated || index === playbackEntries.length - 1) {
        setPlaybackCursor(index);
        setElapsedMs(Math.max(0, targetSeconds - accumulated) * 1000);
        setSelectedSequenceItemId(entry.id);
        return;
      }
      accumulated = nextAccumulated;
    }
  }

  function handleNextPlayback() {
    if (playbackEntries.length === 0) {
      return;
    }

    setPlaybackCursor((cursor) => {
      const nextCursor = cursor + 1;
      if (nextCursor < playbackOrder.length) {
        const nextEntry = playbackEntries[nextCursor];
        if (nextEntry) {
          setSelectedSequenceItemId(nextEntry.id);
        }
        return nextCursor;
      }

      const nextOrder = buildPlaybackOrder(editableTimelineEntries.length, playbackMode);
      setPlaybackOrder(nextOrder);
      const wrappedEntry = editableTimelineEntries[nextOrder[0] ?? 0];
      if (wrappedEntry) {
        setSelectedSequenceItemId(wrappedEntry.id);
      }
      return 0;
    });
    setElapsedMs(0);
  }

  function handleRequestAddContent() {
      setNotice({
        tone: "info",
        message: "Selecciona un contenido en la biblioteca derecha y usa Agregar para insertarlo al final de la timeline.",
      });
  }

  async function handleQuickUpload(payload: { file: File; name: string; durationSeconds: number }) {
    if (!token || !clientId) {
      return;
    }

    const formData = new FormData();
    formData.append("file", payload.file);
    formData.append("name", payload.name);
    formData.append("client_id", clientId);
    formData.append("duration_seconds", String(payload.durationSeconds));
    if (selectedFolderScope !== "all" && selectedFolderScope !== "uncategorized") {
      formData.append("folder_id", selectedFolderScope);
    }

    try {
      await apiRequest<ContentItem>("/contents/upload", {
        method: "POST",
        token,
        formData,
      });
      await loadContentsCatalog();
      setError(null);
      setNotice({
        tone: "success",
        message:
          selectedFolderScope !== "all" && selectedFolderScope !== "uncategorized"
            ? `Contenido subido correctamente a ${selectedFolderLabel}.`
            : "Contenido subido correctamente a la biblioteca del studio.",
      });
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo subir el contenido"));
      throw nextError;
    }
  }

  async function handleCreateUrlContent(payload: { name: string; url: string; durationSeconds: number | null }) {
    if (!token || !clientId || !selectedCampaignId) {
      return;
    }

    const campaignId = selectedCampaignId;
    const trimmedName = payload.name.trim();
    const trimmedUrl = payload.url.trim();
    if (!trimmedName) {
      setError("Escribe un nombre para la URL.");
      return;
    }
    if (!trimmedUrl) {
      setError("Pega una URL válida para continuar.");
      return;
    }
    if (!/^[a-z]+:\/\//i.test(trimmedUrl)) {
      setError("La URL debe incluir protocolo, por ejemplo https://, http://, rtmp:// o rtsp://.");
      return;
    }

    try {
      const created = await apiRequest<ContentItem>("/contents", {
        method: "POST",
        token,
        body: {
          client_id: clientId,
          folder_id: selectedFolderScope !== "all" && selectedFolderScope !== "uncategorized" ? selectedFolderScope : null,
          name: trimmedName,
          type: "url",
          source_url: trimmedUrl,
          ...(payload.durationSeconds ? { duration_seconds: Math.max(1, Math.round(payload.durationSeconds)) } : {}),
        },
      });

      setContents((current) => [created, ...current]);
      setError(null);
      await handleAddToSequence(created.id, created, campaignId);
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo guardar la URL en la biblioteca"));
      throw nextError;
    }
  }

  async function handleCreateCampaign(name: string) {
    if (!token || !clientId) {
      return;
    }

    try {
      const created = await apiRequest<Campaign>("/campaigns", {
        method: "POST",
        token,
        body: {
          client_id: clientId,
          layout_id: null,
          name,
          description: null,
          default_duration_seconds: 15,
          is_active: true,
          loop_enabled: true,
          playback_mode: "sequential",
        },
      });

      setCampaigns((current) => [created, ...current]);
      setSelectedCampaignId(created.id);
      setShowCampaignComposer(false);
      setNotice({
        tone: "success",
        message: `Campaña "${created.name}" creada. Ya puedes construir su timeline V3.`,
      });
      setError(null);
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo crear la campaña"));
      throw nextError;
    }
  }

  async function handleChangePlaybackMode(mode: "sequential" | "random") {
    if (!selectedCampaignId || !token || !canEditOperation) {
      setPlaybackMode(mode);
      return;
    }

    pausePreviewPlayback();
    const previousMode = playbackMode;
    setPlaybackMode(mode);
    try {
      const updated = await apiRequest<Campaign>(`/campaigns/${selectedCampaignId}/playback-mode`, {
        method: "PATCH",
        token,
        body: { playback_mode: mode },
      });
      setCampaigns((current) => current.map((campaign) => (campaign.id === updated.id ? updated : campaign)));
      await loadSequencePreview(selectedCampaignId);
      setNotice({
        tone: "success",
        message: `Modo de reproducción actualizado a ${mode === "random" ? "aleatorio" : "secuencia"}.`,
      });
      setError(null);
    } catch (nextError) {
      setPlaybackMode(previousMode);
      setError(resolveApiErrorMessage(nextError, "No se pudo actualizar el modo de reproducción"));
    }
  }

  async function refreshSequenceState(campaignId: string) {
    if (!campaignId) {
      return;
    }
    await Promise.all([loadSequence(campaignId), loadSequencePreview(campaignId)]);
  }

  async function handleAddToSequence(contentId: string, contentOverride?: ContentItem | null, campaignIdOverride?: string) {
    const campaignId = campaignIdOverride ?? selectedCampaignId;
    if (!token || !campaignId) {
      return;
    }

    pausePreviewPlayback();
    setAddingContentIds((current) => (current.includes(contentId) ? current : [...current, contentId]));
    const content = contentOverride ?? contents.find((item) => item.id === contentId) ?? null;
    const requestTask = async () => {
      try {
        const created = await apiRequest<CampaignSequenceItem>(`/campaigns/${campaignId}/sequence`, {
          method: "POST",
          token,
          body: {
            item_type: "content",
            content_id: contentId,
            sort_order: 999999,
            duration_seconds: content?.duration_seconds ?? selectedCampaign?.default_duration_seconds ?? 15,
            options_json: {
              zone_key: "main",
            },
            is_enabled: true,
          },
        });

        if (selectedCampaignIdRef.current === campaignId) {
          setSequenceItems((current) =>
            [...current, created].sort((left, right) => left.sort_order - right.sort_order || left.created_at.localeCompare(right.created_at)),
          );
          setSelectedSequenceItemId(created.id);
        }
        setNotice({
          tone: "success",
          message: `${content?.name ?? "Contenido"} agregado a la timeline V3.`,
        });
        setError(null);
      } catch (nextError) {
        setError(resolveApiErrorMessage(nextError, "No se pudo agregar el contenido a la secuencia"));
      } finally {
        if (selectedCampaignIdRef.current === campaignId) {
          await refreshSequenceState(campaignId);
        }
        setAddingContentIds((current) => current.filter((itemId) => itemId !== contentId));
      }
    };

    addSequenceQueueRef.current = addSequenceQueueRef.current.then(requestTask, requestTask);
    await addSequenceQueueRef.current;
  }

  async function handleMoveSequenceItem(sequenceItemId: string, direction: "left" | "right") {
    if (!token || !selectedCampaignId) {
      return;
    }

    pausePreviewPlayback();
    const orderedIds = timelineDisplayEntries.map((entry) => entry.id);
    const currentIndex = orderedIds.indexOf(sequenceItemId);
    const targetIndex = direction === "left" ? currentIndex - 1 : currentIndex + 1;
    if (currentIndex < 0 || targetIndex < 0 || targetIndex >= orderedIds.length) {
      return;
    }

    [orderedIds[currentIndex], orderedIds[targetIndex]] = [orderedIds[targetIndex], orderedIds[currentIndex]];

    try {
      await apiRequest<CampaignSequenceItem[]>(`/campaigns/${selectedCampaignId}/sequence/reorder`, {
        method: "POST",
        token,
        body: { ordered_item_ids: orderedIds },
      });
      await Promise.all([loadSequence(selectedCampaignId), loadSequencePreview(selectedCampaignId)]);
      setError(null);
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo reordenar la timeline"));
    }
  }

  async function handleDuplicateSequenceItem(sequenceItemId: string) {
    if (!token || !selectedCampaignId) {
      return;
    }

    pausePreviewPlayback();
    try {
      const duplicated = await apiRequest<CampaignSequenceItem>(`/campaigns/${selectedCampaignId}/sequence/${sequenceItemId}/duplicate`, {
        method: "POST",
        token,
      });
      await Promise.all([loadSequence(selectedCampaignId), loadSequencePreview(selectedCampaignId)]);
      setSelectedSequenceItemId(duplicated.id);
      setError(null);
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo duplicar el bloque"));
    }
  }

  async function handleUpdateSequenceDuration(sequenceItemId: string, durationSeconds: number) {
    if (!token || !selectedCampaignId || !Number.isFinite(durationSeconds)) {
      return;
    }

    pausePreviewPlayback();
    const sanitizedDuration = Math.max(1, Math.round(durationSeconds));
    const targetEntry = timelineDisplayEntries.find((entry) => entry.id === sequenceItemId);
    if (!targetEntry || targetEntry.duration_seconds === sanitizedDuration) {
      return;
    }

    try {
      await apiRequest<CampaignSequenceItem>(`/campaigns/${selectedCampaignId}/sequence/${sequenceItemId}`, {
        method: "PATCH",
        token,
        body: { duration_seconds: sanitizedDuration },
      });
      await Promise.all([loadSequence(selectedCampaignId), loadSequencePreview(selectedCampaignId)]);
      setError(null);
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo actualizar la duración del bloque"));
    }
  }

  async function handleConfirmDelete() {
    if (!token || !deleteTarget) {
      return;
    }

    const target = deleteTarget;
    setIsDeleting(true);
    try {
      let successMessage = "Elemento eliminado.";

      if (target.kind === "content") {
        await apiRequest(`/contents/${target.entity.id}`, {
          method: "DELETE",
          token,
        });
        await loadContentsCatalog();
        if (selectedCampaignId) {
          await refreshSequenceState(selectedCampaignId);
        }
        successMessage = `Contenido "${target.entity.name}" eliminado.`;
      }

      if (target.kind === "folder") {
        await apiRequest(`/content-folders/${target.entity.id}?cascade=true`, {
          method: "DELETE",
          token,
        });
        await Promise.all([loadFolders(), loadContentsCatalog()]);
        if (selectedCampaignId) {
          await refreshSequenceState(selectedCampaignId);
        }
        const descendantFolderSet = new Set(folderDescendantsById[target.entity.id] ?? [target.entity.id]);
        if (selectedFolderScope !== "all" && selectedFolderScope !== "uncategorized" && descendantFolderSet.has(selectedFolderScope)) {
          setSelectedFolderScope("all");
        }
        successMessage = `Carpeta "${target.entity.name}" y su contenido eliminados.`;
      }

      if (target.kind === "sequence_item" && selectedCampaignId) {
        pausePreviewPlayback();
        await apiRequest(`/campaigns/${selectedCampaignId}/sequence/${target.entity.id}`, {
          method: "DELETE",
          token,
        });
        await Promise.all([loadSequence(selectedCampaignId), loadSequencePreview(selectedCampaignId)]);
        successMessage = `Clip "${target.entity.itemLabel}" eliminado del loop.`;
      }

      if (target.kind === "campaign") {
        await apiRequest(`/campaigns/${target.entity.id}`, {
          method: "DELETE",
          token,
        });
        await loadOperationData();
        successMessage = `Campaña "${target.entity.name}" eliminada.`;
      }

      setDeleteTarget(null);
      setFolderDeleteImpact(null);
      setError(null);
      setNotice({ tone: "success", message: successMessage });
    } catch (nextError) {
      const fallbackMessage =
        target.kind === "campaign"
          ? "No se pudo eliminar la campaña. Revisa si tiene asignaciones, secuencias o dependencias activas."
          : target.kind === "folder"
            ? "No se pudo eliminar la carpeta completa. Revisa si alguno de sus archivos tiene dependencias activas."
            : "No se pudo completar la eliminación";
      setError(resolveApiErrorMessage(nextError, fallbackMessage));
    } finally {
      setIsDeleting(false);
    }
  }

  async function handleSubmitFolderDialog(name: string) {
    if (!token || !clientId || !folderDialogState) {
      return;
    }

    setIsSavingFolder(true);
    try {
      if (folderDialogState.mode === "rename") {
        const updated = await apiRequest<ContentFolder>(`/content-folders/${folderDialogState.folder.id}`, {
          method: "PATCH",
          token,
          body: { name },
        });
        await loadFolders();
        setSelectedFolderScope(updated.id);
        setNotice({ tone: "success", message: `Carpeta "${updated.name}" renombrada.` });
      } else {
        const created = await apiRequest<ContentFolder>("/content-folders", {
          method: "POST",
          token,
          body: {
            client_id: clientId,
            parent_id: folderDialogState.mode === "create_child" ? folderDialogState.folder.id : null,
            name,
          },
        });
        await loadFolders();
        setSelectedFolderScope(created.id);
        setNotice({ tone: "success", message: `Carpeta "${created.name}" creada.` });
      }
      setFolderDialogState(null);
      setError(null);
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo guardar la carpeta"));
    } finally {
      setIsSavingFolder(false);
    }
  }

  async function handleMoveContentToFolder(folderId: string | null) {
    if (!token || !moveTargetContent) {
      return;
    }

    setIsMovingContent(true);
    try {
      await apiRequest<ContentItem>(`/contents/${moveTargetContent.id}/folder`, {
        method: "PATCH",
        token,
        body: { folder_id: folderId },
      });
      await loadContentsCatalog();
      setMoveTargetContent(null);
      setError(null);
      setNotice({
        tone: "success",
        message: folderId ? `Contenido movido a ${folderNameById[folderId] ?? "la carpeta seleccionada"}.` : "Contenido movido a la raíz de la biblioteca.",
      });
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo mover el contenido"));
    } finally {
      setIsMovingContent(false);
    }
  }

  async function handleMoveContentByIdToFolder(contentId: string, folderId: string | null) {
    if (!token) {
      return;
    }

    try {
      await apiRequest<ContentItem>(`/contents/${contentId}/folder`, {
        method: "PATCH",
        token,
        body: { folder_id: folderId },
      });
      await loadContentsCatalog();
      setError(null);
      setNotice({
        tone: "success",
        message: folderId ? `Contenido movido a ${folderNameById[folderId] ?? "la carpeta seleccionada"}.` : "Contenido movido a la raíz de la biblioteca.",
      });
    } catch (nextError) {
      setError(resolveApiErrorMessage(nextError, "No se pudo mover el contenido"));
    }
  }

  const deleteDialogCopy = useMemo(() => {
    if (!deleteTarget) {
      return { title: "", description: "", confirmLabel: "Eliminar" };
    }

    switch (deleteTarget.kind) {
      case "content":
        return {
          title: `Eliminar contenido "${deleteTarget.entity.name}"`,
          description: "Se eliminará de la biblioteca del studio junto con sus referencias en timeline, playlists y runtime donde aplique.",
          confirmLabel: "Eliminar contenido",
        };
      case "folder":
        return {
          title: `Eliminar carpeta "${deleteTarget.entity.name}"`,
          description: folderDeleteImpactLoading
            ? "Analizando subcarpetas, contenidos y referencias publicadas antes de eliminar."
            : folderDeleteImpact
              ? folderDeleteImpact.has_published_content
                ? `Se eliminarán ${folderDeleteImpact.folder_count} carpeta(s) y ${folderDeleteImpact.content_count} contenido(s). Advertencia: hay ${folderDeleteImpact.published_campaign_count} campaña(s) publicada(s) afectadas${folderDeleteImpact.published_campaign_names.length > 0 ? `: ${folderDeleteImpact.published_campaign_names.join(", ")}` : ""}.`
                : `Se eliminarán ${folderDeleteImpact.folder_count} carpeta(s) y ${folderDeleteImpact.content_count} contenido(s), junto con sus archivos físicos y referencias en timeline.`
              : "Se eliminará la carpeta junto con sus subcarpetas, contenidos, archivos físicos y referencias en timeline.",
          confirmLabel: "Eliminar carpeta y contenidos",
        };
      case "sequence_item":
        return {
          title: `Eliminar bloque "${deleteTarget.entity.itemLabel}"`,
          description: "El bloque saldrá de la timeline actual y el orden restante se recalculará automáticamente.",
          confirmLabel: "Eliminar clip",
        };
      case "campaign":
        return {
          title: `Eliminar campaña "${deleteTarget.entity.name}"`,
          description: "Se eliminarán la campaña, su timeline V3, playlist legacy, asignaciones y programación relacionada. La biblioteca de contenidos no se borrará.",
          confirmLabel: "Eliminar campaña completa",
        };
    }
  }, [deleteTarget, folderDeleteImpact, folderDeleteImpactLoading]);

  return (
    <>
      <ContentFolderDialog
        open={Boolean(folderDialogState)}
        mode={folderDialogState?.mode ?? "create_root"}
        parentLabel={folderDialogState?.mode === "create_child" ? folderDialogState.folder.name : null}
        initialName={folderDialogState?.mode === "rename" ? folderDialogState.folder.name : ""}
        isSubmitting={isSavingFolder}
        onClose={() => {
          if (!isSavingFolder) {
            setFolderDialogState(null);
          }
        }}
        onSubmit={(name) => handleSubmitFolderDialog(name)}
      />

      <MoveContentFolderDialog
        open={Boolean(moveTargetContent)}
        content={moveTargetContent}
        folders={folders}
        isSubmitting={isMovingContent}
        onClose={() => {
          if (!isMovingContent) {
            setMoveTargetContent(null);
          }
        }}
        onSubmit={(folderId) => handleMoveContentToFolder(folderId)}
      />

      <DeleteConfirmDialog
        open={Boolean(deleteTarget)}
        title={deleteDialogCopy.title}
        description={deleteDialogCopy.description}
        confirmLabel={deleteDialogCopy.confirmLabel}
        isDeleting={isDeleting}
        confirmDisabled={deleteTarget?.kind === "folder" && folderDeleteImpactLoading}
        onCancel={() => {
          if (!isDeleting) {
            setDeleteTarget(null);
            setFolderDeleteImpact(null);
          }
        }}
        onConfirm={() => {
          void handleConfirmDelete();
        }}
      />

      <div className="flex min-h-[calc(100vh-190px)] min-w-0 flex-col gap-4 overflow-x-hidden">
        {error ? <div className="rounded-[18px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}
        {notice ? (
          <div
            className={
              notice.tone === "success"
                ? "rounded-[18px] bg-emerald-50 px-5 py-4 text-sm text-emerald-700"
                : "rounded-[18px] bg-amber-50 px-5 py-4 text-sm text-amber-800"
            }
          >
            {notice.message}
          </div>
        ) : null}

        <div className="grid min-h-0 min-w-0 flex-1 gap-4 xl:grid-cols-[360px_minmax(0,1fr)_420px]">
          <aside className="min-h-0 min-w-0">
            <section className={`${paneClassName} h-full min-h-0 min-w-0`}>
              <OperationCampaignSidebar
                campaigns={campaigns}
                selectedCampaignId={selectedCampaignId}
                canEdit={canEditOperation}
                composerOpen={showCampaignComposer}
                onComposerOpenChange={setShowCampaignComposer}
                onSelectCampaign={setSelectedCampaignId}
                onCreateCampaign={handleCreateCampaign}
                onDeleteCampaign={(campaign) => setDeleteTarget({ kind: "campaign", entity: campaign })}
              />
            </section>
          </aside>

          <section className="grid min-h-0 min-w-0 gap-4 xl:grid-rows-[minmax(0,1fr)_380px_auto]">
            <section className={`${paneClassName} min-h-0 min-w-0`}>
              <OperationPreviewSidebar
                campaign={selectedCampaign}
                previewPayload={sequencePreview}
                entries={playbackEntries}
                currentEntry={currentEntry}
                nextEntry={nextEntry}
                currentIndex={playbackEntries.length === 0 ? 0 : playbackCursor}
                currentLoopTimeSeconds={currentLoopTimeSeconds}
                currentItemDurationSeconds={currentItemDurationSeconds}
                totalDurationSeconds={totalSequenceDuration}
                progressPercent={progressPercent}
                isPlaying={isPlaying}
                isLoading={previewLoading}
                onTogglePlay={() => setIsPlaying((current) => !current)}
                onNext={handleNextPlayback}
              />
            </section>

            <section className={`${paneClassName} min-h-0 min-w-0`}>
              <OperationSequenceTimeline
                campaign={selectedCampaign}
                entries={timelineDisplayEntries}
                selectedItemId={selectedEntry?.id ?? null}
                activeItemId={currentEntry?.id ?? null}
                playbackMode={playbackMode}
                currentLoopTimeSeconds={currentLoopTimeSeconds}
                canEditPlaylist={canEditOperation}
                onSelectItem={setSelectedSequenceItemId}
                onSelectAndSeekItem={handleSelectAndSeekItem}
                onMoveItem={handleMoveSequenceItem}
                onDuplicateItem={handleDuplicateSequenceItem}
                onRemoveItem={(entry) => setDeleteTarget({ kind: "sequence_item", entity: entry })}
                onUpdateDuration={handleUpdateSequenceDuration}
                onSeek={handleSeekTimeline}
                onRequestAddContent={handleRequestAddContent}
              />
            </section>

            <section className={`${paneClassName} min-h-0 min-w-0`}>
              <OperationItemInspector
                campaign={selectedCampaign}
                selectedEntry={selectedEntry}
                playbackMode={playbackMode}
                totalItems={timelineDisplayEntries.length}
                totalDurationSeconds={totalSequenceDuration}
                canEdit={canEditOperation}
                onChangePlaybackMode={(mode) => {
                  void handleChangePlaybackMode(mode);
                }}
                onUpdateDuration={handleUpdateSequenceDuration}
                onRemoveSelectedItem={(entry) => setDeleteTarget({ kind: "sequence_item", entity: entry })}
              />
            </section>
          </section>

          <aside className="min-h-0 min-w-0">
            <section className={`${paneClassName} h-full min-h-0 min-w-0`}>
              <OperationLibraryPanel
                contents={filteredContents}
                allContents={libraryContents}
                folders={folders}
                folderCounts={folderCounts}
                selectedFolderScope={selectedFolderScope}
                selectedFolderLabel={selectedFolderLabel}
                folderNameById={folderNameById}
                searchTerm={contentSearch}
                selectedFilter={contentTypeFilter}
                onSearchTermChange={setContentSearch}
                onFilterChange={setContentTypeFilter}
                onSelectFolderScope={setSelectedFolderScope}
                onCreateRootFolder={() => setFolderDialogState({ mode: "create_root" })}
                onCreateChildFolder={(folder) => setFolderDialogState({ mode: "create_child", folder })}
                onRenameFolder={(folder) => setFolderDialogState({ mode: "rename", folder })}
                onDeleteFolder={(folder) => setDeleteTarget({ kind: "folder", entity: folder })}
                onMoveContent={setMoveTargetContent}
                onMoveContentToFolder={handleMoveContentByIdToFolder}
                onAddToPlaylist={handleAddToSequence}
                onDeleteContent={(content) => setDeleteTarget({ kind: "content", entity: content })}
                onUpload={handleQuickUpload}
                onCreateUrlContent={handleCreateUrlContent}
                addingContentIds={addingContentIds}
                canEdit={canEditOperation}
                selectedCampaignName={selectedCampaign?.name ?? null}
              />
            </section>
          </aside>
        </div>
      </div>
    </>
  );
}

