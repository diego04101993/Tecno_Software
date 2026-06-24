import {
  AlertTriangle,
  Columns2,
  Info,
  Library,
  Monitor,
  PanelsTopLeft,
  Presentation,
  Route,
  Volume2,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { ChannelOperationalCard } from "../components/ChannelOperationalCard";
import { ExpandedOutputsPreview } from "../components/ExpandedOutputsPreview";
import { SectionCard } from "../components/SectionCard";
import { StatCard } from "../components/StatCard";
import { StatusBadge } from "../components/StatusBadge";
import { VideowallGridPreview, getVideowallCellMetrics } from "../components/VideowallGridPreview";
import { WorkspaceQuickLinks } from "../components/WorkspaceQuickLinks";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatChannelHeartbeatState, formatChannelMode, formatOrientation } from "../lib/labels";
import { canAccessGlobalClients, canWriteBranchScope, isClientAdminLike } from "../lib/rbac";
import {
  buildBranchLayoutsPath,
  buildBranchTimelinePath,
  buildChannelDetailPath,
  buildClientCampaignsPath,
  buildClientContentsPath,
} from "../lib/workspace";
import type {
  Branch,
  Campaign,
  Channel,
  ChannelMode,
  Client,
  ScheduleItem,
  Videowall,
  VideowallNode,
} from "../types/domain";

type ResolutionBasis = "monitor" | "total";
type VideowallFlow = "create" | "existing";

type SubmitFeedback =
  | {
      tone: "success" | "warning";
      text: string;
    }
  | null;

const defaultVideowallDraft = {
  name: "",
  columns: 2,
  rows: 2,
  resolution_basis: "monitor" as ResolutionBasis,
  monitor_width: 1920,
  monitor_height: 1080,
  total_width: 3840,
  total_height: 2160,
  position_index: 1,
};

function minutesFromTime(raw: string | null) {
  if (!raw) {
    return 0;
  }

  const [hours, minutes] = raw.split(":").map((value) => Number(value));
  return hours * 60 + minutes;
}

function isScheduleActiveNow(schedule: ScheduleItem) {
  const now = new Date();
  const day = now.getDay() === 0 ? 7 : now.getDay();
  if (schedule.days_of_week.length > 0 && !schedule.days_of_week.includes(day)) {
    return false;
  }

  const currentMinutes = now.getHours() * 60 + now.getMinutes();
  const startMinutes = schedule.start_time ? minutesFromTime(schedule.start_time) : 0;
  const endMinutes = schedule.end_time ? minutesFromTime(schedule.end_time) : 24 * 60;

  return currentMinutes >= startMinutes && currentMinutes <= endMinutes;
}

function getChannelSchedules(channelId: string, branchId: string, schedules: ScheduleItem[]) {
  return schedules.filter((item) => item.channel_id === channelId || (!item.channel_id && item.branch_id === branchId));
}

function buildExpandedOutputs(screenCount: number) {
  return Array.from({ length: screenCount }, (_, index) => ({
    output_index: index + 1,
    label: `HDMI ${index + 1}`,
    role: index === 0 ? "primary" : "extended",
  }));
}

function normalizePositiveInt(value: number, fallback: number) {
  if (!Number.isFinite(value) || value <= 0) {
    return fallback;
  }

  return Math.round(value);
}

function getFirstFreePosition(totalMonitors: number, occupiedPositions: number[]) {
  const occupiedSet = new Set(occupiedPositions);

  for (let position = 1; position <= totalMonitors; position += 1) {
    if (!occupiedSet.has(position)) {
      return position;
    }
  }

  return 1;
}

function ModeSelectorCard({
  mode,
  active,
  title,
  description,
  detail,
  icon: Icon,
  onSelect,
}: {
  mode: ChannelMode;
  active: boolean;
  title: string;
  description: string;
  detail: string;
  icon: typeof Monitor;
  onSelect: (mode: ChannelMode) => void;
}) {
  return (
    <button
      className={[
        "h-full w-full min-h-[196px] min-w-[220px] overflow-hidden rounded-[28px] border p-6 text-left transition",
        active
          ? "border-cyan-300 bg-cyan-50 shadow-[0_0_0_1px_rgba(6,182,212,0.25)]"
          : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50",
      ].join(" ")}
      type="button"
      onClick={() => onSelect(mode)}
    >
      <div className="flex h-full items-start gap-4">
        <span
          className={[
            "mt-0.5 shrink-0 rounded-2xl p-3",
            active ? "bg-cyan-500 text-white" : "bg-slate-900 text-white",
          ].join(" ")}
        >
          <Icon className="h-5 w-5" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col">
          <p className="text-lg font-semibold leading-6 text-ink">{title}</p>
          <p className="mt-2 min-h-[2.75rem] overflow-hidden text-sm leading-5 text-slate-600 [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2]">
            {description}
          </p>
          <p className="mt-auto pt-5 text-xs uppercase tracking-[0.2em] text-slate-500">{detail}</p>
        </div>
      </div>
    </button>
  );
}

export function ChannelsPage() {
  const { token, user } = useAuth();
  const { clientId, branchId } = useParams();
  const modeSelectorRef = useRef<HTMLDivElement | null>(null);
  const [clients, setClients] = useState<Client[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [videowalls, setVideowalls] = useState<Videowall[]>([]);
  const [existingVideowallNodes, setExistingVideowallNodes] = useState<VideowallNode[]>([]);
  const [videowallFlow, setVideowallFlow] = useState<VideowallFlow>("create");
  const [selectedVideowallId, setSelectedVideowallId] = useState("");
  const [videowallNodesLoading, setVideowallNodesLoading] = useState(false);
  const [videowallLookupError, setVideowallLookupError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitFeedback, setSubmitFeedback] = useState<SubmitFeedback>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [form, setForm] = useState({
    client_id: clientId ?? user?.client_id ?? "",
    branch_id: branchId ?? "",
    name: "",
    resolution_width: 1920,
    resolution_height: 1080,
    orientation: "horizontal",
    mode: "normal" as ChannelMode,
    screen_count: 1,
    hardware_identifier: "",
    notes: "",
  });
  const [videowallDraft, setVideowallDraft] = useState(defaultVideowallDraft);
  const [modeSelectorColumns, setModeSelectorColumns] = useState(2);

  useEffect(() => {
    const node = modeSelectorRef.current;
    if (!node || typeof ResizeObserver === "undefined") {
      return;
    }

    const updateColumns = (width: number) => {
      setModeSelectorColumns(width >= 540 ? 2 : 1);
    };

    updateColumns(node.getBoundingClientRect().width);

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) {
        return;
      }
      updateColumns(entry.contentRect.width);
    });

    observer.observe(node);

    return () => observer.disconnect();
  }, []);

  function loadData() {
    if (!token) {
      return;
    }

    const branchPath = clientId ? `/branches?client_id=${clientId}` : "/branches";
    const channelPath = branchId ? `/channels?branch_id=${branchId}` : clientId ? `/channels?client_id=${clientId}` : "/channels";

    Promise.all([
      apiRequest<Client[]>("/clients", { token }),
      apiRequest<Branch[]>(branchPath, { token }),
      apiRequest<Channel[]>(channelPath, { token }),
      clientId && branchId ? apiRequest<Campaign[]>(`/campaigns?client_id=${clientId}`, { token }) : Promise.resolve([] as Campaign[]),
      clientId && branchId ? apiRequest<ScheduleItem[]>(`/schedules?client_id=${clientId}`, { token }) : Promise.resolve([] as ScheduleItem[]),
    ])
      .then(([clientsResponse, branchesResponse, channelsResponse, campaignsResponse, schedulesResponse]) => {
        const channelIds = new Set(channelsResponse.map((item) => item.id));
        setClients(clientsResponse);
        setBranches(branchesResponse);
        setChannels(channelsResponse);
        setCampaigns(campaignsResponse);
        setSchedules(
          branchId
            ? schedulesResponse.filter(
                (item) => item.branch_id === branchId || (item.channel_id ? channelIds.has(item.channel_id) : false),
              )
            : [],
        );
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar los canales");
      });
  }

  async function loadVideowallCatalog(targetClientId: string) {
    if (!token || !targetClientId) {
      setVideowalls([]);
      setSelectedVideowallId("");
      return;
    }

    try {
      const response = await apiRequest<Videowall[]>(`/videowalls?client_id=${targetClientId}`, { token });
      setVideowalls(response);
      setSelectedVideowallId((current) => {
        if (current && response.some((item) => item.id === current)) {
          return current;
        }

        return response[0]?.id ?? "";
      });
      setVideowallLookupError(null);
    } catch (nextError) {
      setVideowalls([]);
      setSelectedVideowallId("");
      setVideowallLookupError(
        nextError instanceof Error ? nextError.message : "No se pudo cargar el catálogo de videowalls del cliente.",
      );
    }
  }

  useEffect(() => {
    loadData();
  }, [branchId, clientId, token]);

  useEffect(() => {
    if (clientId || user?.client_id) {
      setForm((current) => ({
        ...current,
        client_id: clientId ?? user?.client_id ?? current.client_id,
        branch_id: branchId ?? user?.branch_id ?? current.branch_id,
      }));
    }
  }, [branchId, clientId, user?.branch_id, user?.client_id]);

  useEffect(() => {
    if (!form.client_id) {
      setVideowalls([]);
      setSelectedVideowallId("");
      setExistingVideowallNodes([]);
      return;
    }

    loadVideowallCatalog(form.client_id);
  }, [form.client_id, token]);

  useEffect(() => {
    if (!token || form.mode !== "videowall" || videowallFlow !== "existing" || !selectedVideowallId) {
      setExistingVideowallNodes([]);
      return;
    }

    setVideowallNodesLoading(true);
    apiRequest<VideowallNode[]>(`/videowalls/${selectedVideowallId}/nodes`, { token })
      .then((response) => {
        setExistingVideowallNodes(response);
        setVideowallLookupError(null);
      })
      .catch((nextError) => {
        setExistingVideowallNodes([]);
        setVideowallLookupError(
          nextError instanceof Error ? nextError.message : "No se pudieron cargar las posiciones del videowall seleccionado.",
        );
      })
      .finally(() => {
        setVideowallNodesLoading(false);
      });
  }, [form.mode, selectedVideowallId, token, videowallFlow]);

  const filteredBranches = canAccessGlobalClients(user?.role)
    ? branches.filter((branch) => !form.client_id || branch.client_id === form.client_id)
    : branches;

  const currentBranch = useMemo(() => branches.find((item) => item.id === branchId) ?? null, [branchId, branches]);
  const branchContext = Boolean(clientId && branchId);
  const canOpenClientRoutes = canAccessGlobalClients(user?.role) || isClientAdminLike(user?.role);
  const onlineChannels = useMemo(() => channels.filter((channel) => channel.is_online), [channels]);
  const offlineChannels = useMemo(() => channels.filter((channel) => !channel.is_online), [channels]);
  const selectedExistingVideowall = useMemo(
    () => videowalls.find((item) => item.id === selectedVideowallId) ?? null,
    [selectedVideowallId, videowalls],
  );
  const occupiedPositions = useMemo(
    () => existingVideowallNodes.map((node) => node.position_index).sort((left, right) => left - right),
    [existingVideowallNodes],
  );
  const occupiedPositionLabels = useMemo(() => {
    const labels: Record<number, string> = {};

    existingVideowallNodes.forEach((node) => {
      const channel = channels.find((item) => item.id === node.channel_id);
      labels[node.position_index] = channel?.name ?? "Canal asignado";
    });

    return labels;
  }, [channels, existingVideowallNodes]);

  const videowallDerived = useMemo(() => {
    const usingExistingVideowall = form.mode === "videowall" && videowallFlow === "existing" && selectedExistingVideowall;
    const columns = usingExistingVideowall
      ? selectedExistingVideowall.columns
      : normalizePositiveInt(videowallDraft.columns, 2);
    const rows = usingExistingVideowall ? selectedExistingVideowall.rows : normalizePositiveInt(videowallDraft.rows, 2);
    const totalMonitors = columns * rows;
    const totalWidth = usingExistingVideowall
      ? selectedExistingVideowall.total_width
      : videowallDraft.resolution_basis === "monitor"
        ? normalizePositiveInt(videowallDraft.monitor_width, 1920) * columns
        : Math.max(columns, normalizePositiveInt(videowallDraft.total_width, 3840));
    const totalHeight = usingExistingVideowall
      ? selectedExistingVideowall.total_height
      : videowallDraft.resolution_basis === "monitor"
        ? normalizePositiveInt(videowallDraft.monitor_height, 1080) * rows
        : Math.max(rows, normalizePositiveInt(videowallDraft.total_height, 2160));
    const takenPositions = usingExistingVideowall ? occupiedPositions : [];
    const availablePositions = Array.from({ length: totalMonitors }, (_, index) => index + 1).filter(
      (position) => !takenPositions.includes(position),
    );
    const safePosition = availablePositions.includes(videowallDraft.position_index)
      ? videowallDraft.position_index
      : availablePositions[0] ?? 1;
    const selectedCell = getVideowallCellMetrics(columns, rows, totalWidth, totalHeight, safePosition);

    return {
      columns,
      rows,
      totalMonitors,
      totalWidth,
      totalHeight,
      selectedPosition: safePosition,
      selectedCell,
      widthRemainder: totalWidth % columns,
      heightRemainder: totalHeight % rows,
      nominalMonitorWidth:
        usingExistingVideowall || videowallDraft.resolution_basis === "total"
          ? Math.max(1, Math.floor(totalWidth / columns))
          : normalizePositiveInt(videowallDraft.monitor_width, 1920),
      nominalMonitorHeight:
        usingExistingVideowall || videowallDraft.resolution_basis === "total"
          ? Math.max(1, Math.floor(totalHeight / rows))
          : normalizePositiveInt(videowallDraft.monitor_height, 1080),
      occupiedPositions: takenPositions,
      availablePositions,
    };
  }, [form.mode, occupiedPositions, selectedExistingVideowall, videowallDraft, videowallFlow]);

  useEffect(() => {
    if (form.mode !== "videowall") {
      return;
    }

    if (videowallDraft.position_index !== videowallDerived.selectedPosition) {
      setVideowallDraft((current) => ({
        ...current,
        position_index: videowallDerived.selectedPosition,
      }));
    }
  }, [form.mode, videowallDerived.selectedPosition, videowallDraft.position_index]);

  const channelSnapshots = useMemo(
    () =>
      channels.map((channel) => {
        const branchSchedules = branchId ? getChannelSchedules(channel.id, branchId, schedules) : [];
        const activeSchedules = branchSchedules.filter((item) => isScheduleActiveNow(item));
        const selectedSchedule = [...activeSchedules, ...branchSchedules].sort((left, right) => right.priority - left.priority)[0] ?? null;
        const assignedCampaign = campaigns.find((item) => item.id === selectedSchedule?.campaign_id) ?? null;
        const heartbeat = formatChannelHeartbeatState(channel);

        return {
          channel,
          activeTimelineCount: activeSchedules.length,
          assignedCampaignLabel: assignedCampaign?.name ?? "Sin campaña visible",
          heartbeat,
        };
      }),
    [branchId, campaigns, channels, schedules],
  );

  const alerts = [
    offlineChannels.length > 0 ? `${offlineChannels.length} canal(es) sin conexión` : null,
    channelSnapshots.filter((item) => !item.channel.current_playback).length > 0
      ? `${channelSnapshots.filter((item) => !item.channel.current_playback).length} canal(es) sin playback`
      : null,
    channelSnapshots.filter((item) => item.activeTimelineCount === 0).length > 0
      ? `${channelSnapshots.filter((item) => item.activeTimelineCount === 0).length} canal(es) sin timeline activo`
      : null,
  ].filter(Boolean) as string[];

  function resetVideowallForm() {
    setVideowallFlow("create");
    setSelectedVideowallId("");
    setExistingVideowallNodes([]);
    setVideowallDraft(defaultVideowallDraft);
  }

  function updateMode(nextMode: ChannelMode) {
    setSubmitFeedback(null);
    setError(null);
    setForm((current) => ({
      ...current,
      mode: nextMode,
      screen_count: nextMode === "expanded" ? Math.min(Math.max(current.screen_count, 2), 3) : 1,
    }));

    if (nextMode !== "videowall") {
      resetVideowallForm();
    } else {
      setVideowallDraft((current) => ({
        ...current,
        position_index: 1,
      }));
    }
  }

  function updateVideowallColumns(nextColumns: number) {
    const columns = normalizePositiveInt(nextColumns, 2);
    setVideowallDraft((current) => ({
      ...current,
      columns,
      total_width: current.resolution_basis === "monitor" ? current.monitor_width * columns : current.total_width,
      position_index: Math.min(current.position_index, columns * current.rows),
    }));
  }

  function updateVideowallRows(nextRows: number) {
    const rows = normalizePositiveInt(nextRows, 2);
    setVideowallDraft((current) => ({
      ...current,
      rows,
      total_height: current.resolution_basis === "monitor" ? current.monitor_height * rows : current.total_height,
      position_index: Math.min(current.position_index, current.columns * rows),
    }));
  }

  function updateVideowallFlow(nextFlow: VideowallFlow) {
    setVideowallFlow(nextFlow);
    setSubmitFeedback(null);
    setError(null);
    setVideowallLookupError(null);

    if (nextFlow === "existing") {
      setSelectedVideowallId((current) => current || videowalls[0]?.id || "");
      setVideowallDraft((current) => ({
        ...current,
        position_index: getFirstFreePosition(videowallDerived.totalMonitors, occupiedPositions),
      }));
      return;
    }

    setExistingVideowallNodes([]);
    setSelectedVideowallId("");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !canWriteBranchScope(user?.role)) {
      return;
    }

    if (form.mode === "videowall" && videowallFlow === "existing") {
      if (!selectedExistingVideowall) {
        setError("Selecciona un videowall existente antes de guardar el canal.");
        return;
      }

      if (videowallDerived.availablePositions.length === 0) {
        setError("El videowall seleccionado ya no tiene posiciones libres.");
        return;
      }
    }

    setIsSubmitting(true);
    setError(null);
    setSubmitFeedback(null);

    try {
      const channelPayload = {
        ...form,
        resolution_width: form.mode === "videowall" ? videowallDerived.selectedCell.width : form.resolution_width,
        resolution_height: form.mode === "videowall" ? videowallDerived.selectedCell.height : form.resolution_height,
        screen_count: form.mode === "expanded" ? form.screen_count : 1,
        hardware_identifier: form.hardware_identifier || null,
        notes: form.notes || null,
        expanded_outputs: form.mode === "expanded" ? buildExpandedOutputs(form.screen_count) : [],
      };

      const channel = await apiRequest<Channel>("/channels", {
        method: "POST",
        token,
        body: channelPayload,
      });

      let feedbackTone: SubmitFeedback = { tone: "success", text: "Canal creado correctamente." };

      if (form.mode === "videowall") {
        let targetVideowall = selectedExistingVideowall;

        if (videowallFlow === "create") {
          targetVideowall = await apiRequest<Videowall>("/videowalls", {
            method: "POST",
            token,
            body: {
              client_id: form.client_id,
              name: videowallDraft.name.trim() || `${form.name} Videowall`,
              columns: videowallDerived.columns,
              rows: videowallDerived.rows,
              total_width: videowallDerived.totalWidth,
              total_height: videowallDerived.totalHeight,
              sync_mode: "play_at_timestamp",
            },
          });
        }

        try {
          await apiRequest(`/videowalls/${targetVideowall?.id}/nodes`, {
            method: "POST",
            token,
            body: {
              channel_id: channel.id,
              position_index: videowallDerived.selectedPosition,
              row_index: videowallDerived.selectedCell.rowIndex,
              column_index: videowallDerived.selectedCell.columnIndex,
              x: videowallDerived.selectedCell.x,
              y: videowallDerived.selectedCell.y,
              width: videowallDerived.selectedCell.width,
              height: videowallDerived.selectedCell.height,
            },
          });

          feedbackTone =
            videowallFlow === "create"
              ? {
                  tone: "success",
                  text: `Canal y videowall creados. Este canal quedó asignado al monitor ${videowallDerived.selectedPosition} de ${videowallDerived.totalMonitors}.`,
                }
              : {
                  tone: "success",
                  text: `Canal creado y asignado al videowall existente en el monitor ${videowallDerived.selectedPosition}.`,
                };
        } catch (nodeError) {
          feedbackTone = {
            tone: "warning",
            text:
              nodeError instanceof Error
                ? `El canal se creó, pero la posición del videowall quedó pendiente: ${nodeError.message}`
                : "El canal se creó, pero la posición del videowall quedó pendiente.",
          };
        }
      } else if (form.mode === "expanded") {
        feedbackTone = {
          tone: "success",
          text: `Canal expandido creado con ${form.screen_count} salida(s) HDMI listas para escritorio extendido.`,
        };
      } else if (form.mode === "audio") {
        feedbackTone = {
          tone: "success",
          text: "Canal de audio creado correctamente y listo para recibir playlists ambientales.",
        };
      }

      setForm((current) => ({
        ...current,
        branch_id: branchId ?? user?.branch_id ?? "",
        name: "",
        resolution_width: 1920,
        resolution_height: 1080,
        orientation: "horizontal",
        mode: "normal",
        screen_count: 1,
        hardware_identifier: "",
        notes: "",
      }));
      resetVideowallForm();
      setSubmitFeedback(feedbackTone);
      loadData();
      if (form.client_id) {
        loadVideowallCatalog(form.client_id);
      }
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear el canal");
    } finally {
      setIsSubmitting(false);
    }
  }

  const createForm = (
    <SectionCard
      title="Nuevo canal"
      subtitle="Configura el canal según su operación real: pantalla única, escritorio extendido o nodo dentro de un videowall."
    >
      {error ? <div className="mb-4 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
      {submitFeedback ? (
        <div
          className={[
            "mb-4 rounded-2xl px-4 py-3 text-sm",
            submitFeedback.tone === "success" ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-900",
          ].join(" ")}
        >
          {submitFeedback.text}
        </div>
      ) : null}
      <form className="space-y-5" onSubmit={handleSubmit}>
        <div className="grid gap-4 xl:grid-cols-2">
          {canAccessGlobalClients(user?.role) && !clientId ? (
            <div className="xl:col-span-2">
              <label className="mb-2 block text-sm font-semibold text-slate-700">Cliente</label>
              <select
                value={form.client_id}
                onChange={(event) => {
                  setSubmitFeedback(null);
                  setForm({ ...form, client_id: event.target.value, branch_id: "" });
                  resetVideowallForm();
                }}
                required
              >
                <option value="">Selecciona un cliente</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          {branchId ? (
            <div className="xl:col-span-2">
              <label className="mb-2 block text-sm font-semibold text-slate-700">Sucursal</label>
              <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                {filteredBranches.find((branch) => branch.id === branchId)?.name ?? "Sucursal actual"}
              </div>
            </div>
          ) : (
            <div className="xl:col-span-2">
              <label className="mb-2 block text-sm font-semibold text-slate-700">Sucursal</label>
              <select
                value={form.branch_id}
                onChange={(event) => {
                  setSubmitFeedback(null);
                  setForm({ ...form, branch_id: event.target.value });
                }}
                required
              >
                <option value="">Selecciona una sucursal</option>
                {filteredBranches.map((branch) => (
                  <option key={branch.id} value={branch.id}>
                    {branch.name}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="xl:col-span-2">
            <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre del canal</label>
            <input
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder="Ej. Pantalla caja 1"
              required
            />
          </div>
        </div>

        <section className="min-w-0 overflow-hidden rounded-[28px] border border-slate-200 bg-slate-50/80 p-5">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-sm font-semibold text-slate-700">Modo de operación</p>
              <p className="mt-1 text-sm text-slate-500">Elige cómo trabaja este canal según su instalación real.</p>
            </div>
            <span className="w-fit rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">
              Selección principal
            </span>
          </div>

          <div
            ref={modeSelectorRef}
            className="mt-4 grid auto-rows-fr gap-4"
            style={{ gridTemplateColumns: `repeat(${modeSelectorColumns}, minmax(0, 1fr))` }}
          >
            <div className="min-w-0">
              <ModeSelectorCard
                mode="normal"
                active={form.mode === "normal"}
                title="Normal"
                description="Una sola pantalla con el contenido completo y resolución estándar."
                detail="1 pantalla"
                icon={Monitor}
                onSelect={updateMode}
              />
            </div>
            <div className="min-w-0">
              <ModeSelectorCard
                mode="expanded"
                active={form.mode === "expanded"}
                title="Expandido"
                description="Una misma PC reparte el escritorio extendido en 2 o 3 salidas HDMI."
                detail="2 o 3 salidas"
                icon={Columns2}
                onSelect={updateMode}
              />
            </div>
            <div className="min-w-0">
              <ModeSelectorCard
                mode="videowall"
                active={form.mode === "videowall"}
                title="Videowall"
                description="Cada player ocupa una posición exacta dentro de un lienzo total compartido."
                detail="Matriz sincronizable"
                icon={PanelsTopLeft}
                onSelect={updateMode}
              />
            </div>
            <div className="min-w-0">
              <ModeSelectorCard
                mode="audio"
                active={form.mode === "audio"}
                title="Audio"
                description="Canal pensado para música ambiental y spots sin depender del timeline visual."
                detail="Playlist de audio"
                icon={Volume2}
                onSelect={updateMode}
              />
            </div>
          </div>
        </section>

        <div className="grid gap-4 xl:grid-cols-2">
          {form.mode !== "videowall" && form.mode !== "audio" ? (
            <>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho</label>
                <input
                  type="number"
                  min={1}
                  value={form.resolution_width}
                  onChange={(event) =>
                    setForm({ ...form, resolution_width: normalizePositiveInt(Number(event.target.value), form.resolution_width) })
                  }
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Alto</label>
                <input
                  type="number"
                  min={1}
                  value={form.resolution_height}
                  onChange={(event) =>
                    setForm({ ...form, resolution_height: normalizePositiveInt(Number(event.target.value), form.resolution_height) })
                  }
                />
              </div>
            </>
          ) : null}

          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Orientación</label>
            <select value={form.orientation} onChange={(event) => setForm({ ...form, orientation: event.target.value })}>
              <option value="horizontal">Horizontal</option>
              <option value="vertical">Vertical</option>
            </select>
          </div>

          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Hardware ID</label>
            <input
              value={form.hardware_identifier}
              onChange={(event) => setForm({ ...form, hardware_identifier: event.target.value })}
              placeholder="Opcional"
            />
          </div>
        </div>

        {form.mode === "normal" ? (
          <div className="md:col-span-2 rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
            <p className="font-semibold text-ink">Modo normal</p>
            <p className="mt-1">
              Este canal usará una sola pantalla con el contenido completo. La resolución seleccionada se aplicará
              directamente al display.
            </p>
          </div>
        ) : null}

        {form.mode === "expanded" ? (
          <div className="space-y-4 md:col-span-2">
            <div className="rounded-[28px] border border-slate-200 bg-slate-50/90 p-4">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <p className="text-sm font-semibold text-ink">Salidas HDMI</p>
                  <p className="mt-1 text-sm text-slate-600">
                    Usa este modo solo cuando una misma PC extiende el escritorio a 2 o 3 monitores.
                  </p>
                </div>
                <div className="grid w-full max-w-[280px] grid-cols-2 rounded-full border border-slate-200 bg-white p-1">
                  {[2, 3].map((count) => (
                    <button
                      key={count}
                      className={[
                        "min-w-0 rounded-full px-3 py-2 text-center text-sm font-semibold leading-tight transition",
                        form.screen_count === count ? "bg-ink text-white" : "text-slate-600 hover:bg-slate-100",
                      ].join(" ")}
                      type="button"
                      onClick={() => setForm({ ...form, screen_count: count })}
                    >
                      {count} salidas
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <ExpandedOutputsPreview screenCount={form.screen_count} />
          </div>
        ) : null}

        {form.mode === "audio" ? (
          <div className="md:col-span-2 rounded-[24px] border border-cyan-200 bg-cyan-50 px-4 py-4 text-sm text-cyan-900">
            <p className="font-semibold text-ink">Modo audio</p>
            <p className="mt-1">
              Este canal se reserva para música ambiental y spots. La asignación de playlists se administra desde el
              modulo Audio del cliente o de la sucursal.
            </p>
          </div>
        ) : null}

        {form.mode === "videowall" ? (
          <div className="space-y-4 md:col-span-2">
            <div className="rounded-[28px] border border-cyan-200 bg-cyan-50/70 p-4">
              <div className="flex gap-3">
                <Info className="mt-0.5 h-4 w-4 shrink-0 text-cyan-700" />
                <div className="text-sm text-cyan-900">
                  <p className="font-semibold">Constructor de videowall</p>
                  <p className="mt-1">
                    Cada pantalla mostrará su sección exacta del contenido completo sin deformar la imagen.
                  </p>
                </div>
              </div>
            </div>

            <div className="rounded-[28px] border border-slate-200 bg-white p-4">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <p className="text-sm font-semibold text-ink">Origen del videowall</p>
                  <p className="mt-1 text-sm text-slate-600">
                    Puedes crear un videowall nuevo o asignar este canal a uno existente del mismo cliente.
                  </p>
                </div>
                <div className="grid w-full max-w-[340px] grid-cols-2 rounded-full border border-slate-200 bg-slate-50 p-1">
                  {[
                    { key: "create" as VideowallFlow, label: "Crear nuevo" },
                    { key: "existing" as VideowallFlow, label: "Asignar existente" },
                  ].map((option) => (
                    <button
                      key={option.key}
                      className={[
                        "min-w-0 rounded-full px-3 py-2 text-center text-sm font-semibold leading-tight transition",
                        videowallFlow === option.key ? "bg-ink text-white" : "text-slate-600 hover:bg-white",
                      ].join(" ")}
                      type="button"
                      onClick={() => updateVideowallFlow(option.key)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="grid gap-4 xl:grid-cols-[minmax(0,1.08fr)_minmax(320px,0.92fr)]">
              <div className="space-y-4">
                {videowallFlow === "create" ? (
                  <>
                    <div className="rounded-[28px] border border-slate-200 bg-slate-50/90 p-4">
                      <div className="grid gap-4 min-[720px]:grid-cols-2">
                        <div className="md:col-span-2">
                          <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre del videowall</label>
                          <input
                            value={videowallDraft.name}
                            onChange={(event) => setVideowallDraft({ ...videowallDraft, name: event.target.value })}
                            placeholder="Ej. Lobby principal 2x2"
                          />
                        </div>
                        <div>
                          <label className="mb-2 block text-sm font-semibold text-slate-700">Columnas</label>
                          <input
                            type="number"
                            min={1}
                            value={videowallDraft.columns}
                            onChange={(event) => updateVideowallColumns(Number(event.target.value))}
                          />
                        </div>
                        <div>
                          <label className="mb-2 block text-sm font-semibold text-slate-700">Filas</label>
                          <input
                            type="number"
                            min={1}
                            value={videowallDraft.rows}
                            onChange={(event) => updateVideowallRows(Number(event.target.value))}
                          />
                        </div>
                      </div>
                    </div>

                    <div className="rounded-[28px] border border-slate-200 bg-white p-4">
                      <div className="flex flex-wrap items-center justify-between gap-4">
                        <div>
                          <p className="text-sm font-semibold text-ink">Definición de resolución</p>
                          <p className="mt-1 text-sm text-slate-600">
                            Puedes definir el videowall por monitor o por resolución total del lienzo.
                          </p>
                        </div>
                        <div className="grid w-full max-w-[360px] grid-cols-2 rounded-full border border-slate-200 bg-slate-50 p-1">
                          {[
                            { key: "monitor" as ResolutionBasis, label: "Por monitor" },
                            { key: "total" as ResolutionBasis, label: "Por lienzo total" },
                          ].map((option) => (
                            <button
                              key={option.key}
                              className={[
                                "min-w-0 rounded-full px-3 py-2 text-center text-sm font-semibold leading-tight transition",
                                videowallDraft.resolution_basis === option.key
                                  ? "bg-ink text-white"
                                  : "text-slate-600 hover:bg-white",
                              ].join(" ")}
                              type="button"
                              onClick={() => setVideowallDraft({ ...videowallDraft, resolution_basis: option.key })}
                            >
                              {option.label}
                            </button>
                          ))}
                        </div>
                      </div>

                      {videowallDraft.resolution_basis === "monitor" ? (
                        <div className="mt-4 grid gap-4 min-[720px]:grid-cols-2">
                          <div>
                            <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho por monitor</label>
                            <input
                              type="number"
                              min={1}
                              value={videowallDraft.monitor_width}
                              onChange={(event) =>
                                setVideowallDraft({
                                  ...videowallDraft,
                                  monitor_width: normalizePositiveInt(Number(event.target.value), videowallDraft.monitor_width),
                                })
                              }
                            />
                          </div>
                          <div>
                            <label className="mb-2 block text-sm font-semibold text-slate-700">Alto por monitor</label>
                            <input
                              type="number"
                              min={1}
                              value={videowallDraft.monitor_height}
                              onChange={(event) =>
                                setVideowallDraft({
                                  ...videowallDraft,
                                  monitor_height: normalizePositiveInt(Number(event.target.value), videowallDraft.monitor_height),
                                })
                              }
                            />
                          </div>
                        </div>
                      ) : (
                        <div className="mt-4 grid gap-4 min-[720px]:grid-cols-2">
                          <div>
                            <label className="mb-2 block text-sm font-semibold text-slate-700">Resolución total ancho</label>
                            <input
                              type="number"
                              min={videowallDerived.columns}
                              value={videowallDraft.total_width}
                              onChange={(event) =>
                                setVideowallDraft({
                                  ...videowallDraft,
                                  total_width: normalizePositiveInt(Number(event.target.value), videowallDraft.total_width),
                                })
                              }
                            />
                          </div>
                          <div>
                            <label className="mb-2 block text-sm font-semibold text-slate-700">Resolución total alto</label>
                            <input
                              type="number"
                              min={videowallDerived.rows}
                              value={videowallDraft.total_height}
                              onChange={(event) =>
                                setVideowallDraft({
                                  ...videowallDraft,
                                  total_height: normalizePositiveInt(Number(event.target.value), videowallDraft.total_height),
                                })
                              }
                            />
                          </div>
                        </div>
                      )}

                      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                        <div className="min-w-0 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3">
                          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Monitores totales</p>
                          <p className="mt-2 font-display text-2xl text-ink">{videowallDerived.totalMonitors}</p>
                        </div>
                        <div className="min-w-0 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3">
                          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Resolución total</p>
                          <p className="mt-2 break-words font-display text-2xl text-ink">
                            {videowallDerived.totalWidth}x{videowallDerived.totalHeight}
                          </p>
                        </div>
                        <div className="min-w-0 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3">
                          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Monitor seleccionado</p>
                          <p className="mt-2 break-words font-display text-2xl text-ink">
                            {videowallDerived.selectedCell.width}x{videowallDerived.selectedCell.height}
                          </p>
                        </div>
                      </div>

                      {videowallDraft.resolution_basis === "total" &&
                      (videowallDerived.widthRemainder > 0 || videowallDerived.heightRemainder > 0) ? (
                        <div className="mt-4 rounded-[22px] border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                          La resolución total no se divide de forma exacta entre todas las columnas o filas. La última
                          columna o fila absorberá los píxeles sobrantes para conservar el lienzo completo.
                        </div>
                      ) : null}
                    </div>
                  </>
                ) : (
                  <>
                    <div className="rounded-[28px] border border-slate-200 bg-white p-4">
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Videowall del cliente</label>
                      <select
                        value={selectedVideowallId}
                        onChange={(event) => setSelectedVideowallId(event.target.value)}
                        disabled={videowalls.length === 0}
                      >
                        {videowalls.length === 0 ? <option value="">No hay videowalls disponibles</option> : null}
                        {videowalls.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </select>
                      <p className="mt-2 text-sm text-slate-600">
                        Se usarán su matriz, resolución total y posiciones ya ocupadas.
                      </p>
                    </div>

                    {selectedExistingVideowall ? (
                      <div className="rounded-[28px] border border-slate-200 bg-white p-4">
                        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                          <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3">
                            <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Matriz</p>
                            <p className="mt-2 font-display text-2xl text-ink">
                              {selectedExistingVideowall.columns}x{selectedExistingVideowall.rows}
                            </p>
                          </div>
                          <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3">
                            <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Resolución total</p>
                            <p className="mt-2 font-display text-2xl text-ink">
                              {selectedExistingVideowall.total_width}x{selectedExistingVideowall.total_height}
                            </p>
                          </div>
                          <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3">
                            <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Posiciones ocupadas</p>
                            <p className="mt-2 font-display text-2xl text-ink">{videowallDerived.occupiedPositions.length}</p>
                          </div>
                          <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3">
                            <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Posiciones libres</p>
                            <p className="mt-2 font-display text-2xl text-ink">{videowallDerived.availablePositions.length}</p>
                          </div>
                        </div>

                        {videowallNodesLoading ? (
                          <div className="mt-4 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
                            Cargando posiciones ocupadas del videowall...
                          </div>
                        ) : null}
                        {videowallLookupError ? (
                          <div className="mt-4 rounded-[22px] border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                            {videowallLookupError}
                          </div>
                        ) : null}
                        {videowallDerived.availablePositions.length === 0 && !videowallNodesLoading ? (
                          <div className="mt-4 rounded-[22px] border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                            Este videowall ya no tiene monitores libres para asignar otro canal.
                          </div>
                        ) : null}
                      </div>
                    ) : (
                      <div className="rounded-[28px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
                        Selecciona un videowall existente para revisar su matriz y las posiciones disponibles.
                      </div>
                    )}
                  </>
                )}

                <div className="rounded-[28px] border border-slate-200 bg-white p-4">
                  <div className="grid gap-4 min-[720px]:grid-cols-2">
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Posición de este canal</label>
                      <select
                        value={videowallDerived.selectedPosition}
                        onChange={(event) =>
                          setVideowallDraft({
                            ...videowallDraft,
                            position_index: Number(event.target.value),
                          })
                        }
                        disabled={videowallFlow === "existing" && videowallDerived.availablePositions.length === 0}
                      >
                        {(videowallFlow === "existing"
                          ? videowallDerived.availablePositions
                          : Array.from({ length: videowallDerived.totalMonitors }, (_, index) => index + 1)
                        ).map((position) => (
                          <option key={position} value={position}>
                            Monitor {position} de {videowallDerived.totalMonitors}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="min-w-0 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      <p className="font-semibold text-ink">Coordenadas del monitor</p>
                      <p className="mt-2 break-words">
                        x:{videowallDerived.selectedCell.x} · y:{videowallDerived.selectedCell.y}
                      </p>
                      <p className="mt-1">
                        fila {videowallDerived.selectedCell.rowIndex + 1}, columna {videowallDerived.selectedCell.columnIndex + 1}
                      </p>
                    </div>
                  </div>
                </div>
              </div>

              <VideowallGridPreview
                columns={videowallDerived.columns}
                rows={videowallDerived.rows}
                totalWidth={videowallDerived.totalWidth}
                totalHeight={videowallDerived.totalHeight}
                selectedPosition={videowallDerived.selectedPosition}
                occupiedPositions={videowallFlow === "existing" ? videowallDerived.occupiedPositions : []}
                occupiedLabels={videowallFlow === "existing" ? occupiedPositionLabels : {}}
                onSelectPosition={(position) =>
                  setVideowallDraft((current) => ({
                    ...current,
                    position_index: position,
                  }))
                }
              />
            </div>
          </div>
        ) : null}

        <div className="md:col-span-2">
          <label className="mb-2 block text-sm font-semibold text-slate-700">Notas</label>
          <textarea rows={3} value={form.notes} onChange={(event) => setForm({ ...form, notes: event.target.value })} />
        </div>

        {canWriteBranchScope(user?.role) ? (
          <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white md:col-span-2" disabled={isSubmitting} type="submit">
            {isSubmitting ? "Guardando configuración..." : "Crear canal"}
          </button>
        ) : (
          <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600 md:col-span-2">
            Este rol solo puede monitorear canales dentro de su alcance.
          </div>
        )}
      </form>
    </SectionCard>
  );

  if (!branchContext) {
    return (
      <div className="grid gap-6 xl:grid-cols-[1.05fr_0.95fr]">
        <SectionCard
          title="Inventario de canales"
          subtitle="El canal ya puede configurarse como pantalla única, escritorio extendido o nodo de videowall sin salir del flujo actual."
        >
          <div className="space-y-4">
            {channels.map((channel) => {
              const branch = branches.find((item) => item.id === channel.branch_id);
              return (
                <article key={channel.id} className="rounded-[28px] border border-slate-200 bg-white p-5">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-display text-2xl text-ink">{channel.name}</p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold tracking-[0.18em] text-slate-600">
                          {channel.channel_code}
                        </span>
                        <button
                          className="rounded-full border border-slate-200 px-3 py-1 text-xs font-semibold text-slate-600 transition hover:border-cyan-300 hover:text-ink"
                          type="button"
                          onClick={() => navigator.clipboard.writeText(channel.channel_code).catch(() => undefined)}
                        >
                          Copiar código
                        </button>
                      </div>
                      <p className="mt-2 text-sm text-slate-500">
                        {branch?.name ?? "Sucursal"} · {formatChannelMode(channel.mode)} · {channel.screen_count} salida(s)
                      </p>
                    </div>
                    <StatusBadge status={channel.status} />
                  </div>
                  <div className="mt-5 grid gap-3 md:grid-cols-3">
                    <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      {channel.resolution_width}x{channel.resolution_height}
                    </div>
                    <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">{formatOrientation(channel.orientation)}</div>
                    <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      {channel.current_playback ?? "Esperando contenido"}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        </SectionCard>

        {createForm}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-[1.06fr_0.94fr]">
        <article className="rounded-[32px] border border-white/70 bg-card/90 p-5 shadow-card backdrop-blur">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-accent">Canales de sucursal</p>
              <h2 className="mt-3 font-display text-4xl text-ink">{currentBranch?.name ?? "Sucursal"}</h2>
              <p className="mt-3 text-sm text-slate-600">
                {currentBranch?.code ?? "Sin código"} · {currentBranch?.timezone ?? "Sin zona horaria"}
              </p>
              <p className="mt-2 text-sm text-slate-700">{currentBranch?.address ?? "Dirección pendiente"}</p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-[24px] border border-emerald-200 bg-emerald-50 px-4 py-4">
                <p className="text-xs uppercase tracking-wide text-emerald-700">Canales en línea</p>
                <p className="mt-2 font-display text-3xl text-ink">{onlineChannels.length}</p>
              </div>
              <div className="rounded-[24px] border border-amber-200 bg-amber-50 px-4 py-4">
                <p className="text-xs uppercase tracking-wide text-amber-700">Alertas visibles</p>
                <p className="mt-2 font-display text-3xl text-ink">{alerts.length}</p>
              </div>
            </div>
          </div>
        </article>

        <section className="grid gap-3 sm:grid-cols-2">
          <StatCard label="Canales" value={String(channels.length)} hint="Inventario operativo" tone="teal" />
          <StatCard label="Offline" value={String(offlineChannels.length)} hint="Pendientes de atención" tone="orange" />
          <StatCard
            label="Timeline activo"
            value={String(channelSnapshots.reduce((sum, item) => sum + item.activeTimelineCount, 0))}
            hint="Slots vigentes en los canales"
          />
          <StatCard label="Campañas visibles" value={String(campaigns.length)} hint="Catálogo disponible para esta sucursal" />
        </section>
      </section>

      {createForm}

      <div className="grid gap-5 xl:grid-cols-[1.06fr_0.94fr]">
        <SectionCard title="Consola operativa" subtitle="Estado por canal con playback, heartbeat, campaña visible y lectura rápida del timeline.">
          <div className="space-y-4">
            {channelSnapshots.map((snapshot) => (
              <ChannelOperationalCard
                key={snapshot.channel.id}
                channel={snapshot.channel}
                assignedCampaignLabel={snapshot.assignedCampaignLabel}
                activeTimelineCount={snapshot.activeTimelineCount}
                heartbeatLabel={snapshot.heartbeat.label}
                heartbeatTone={snapshot.heartbeat.tone}
                detailTo={clientId && branchId ? buildChannelDetailPath(clientId, branchId, snapshot.channel.id) : undefined}
              />
            ))}
          </div>
        </SectionCard>

        <div className="space-y-5">
          <SectionCard title="Alertas operativas" subtitle="Prioriza los canales que necesitan seguimiento inmediato.">
            <div className="space-y-3">
              {alerts.length > 0 ? (
                alerts.map((alert) => (
                  <article key={alert} className="rounded-[24px] border border-amber-200 bg-amber-50 px-4 py-4 text-sm text-amber-900">
                    <div className="flex items-start gap-3">
                      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                      <div>
                        <p className="font-semibold">{alert}</p>
                        <p className="mt-1">Revisa el detalle del canal para validar heartbeat, playback, timeline o infraestructura.</p>
                      </div>
                    </div>
                  </article>
                ))
              ) : (
                <article className="rounded-[24px] border border-emerald-200 bg-emerald-50 px-4 py-4 text-sm text-emerald-800">
                  <p className="font-semibold">Sin alertas críticas</p>
                  <p className="mt-1">La sucursal no reporta incidentes visibles desde esta vista.</p>
                </article>
              )}
            </div>
          </SectionCard>

          {clientId && branchId ? (
            <WorkspaceQuickLinks
              title="Accesos rápidos"
              subtitle="Atajos operativos para revisar programación, contenido y layouts sin salir de la sucursal."
              links={[
                ...(canOpenClientRoutes
                  ? [
                      {
                        label: "Campañas",
                        caption: "Abrir campañas visibles del cliente.",
                        to: buildClientCampaignsPath(clientId),
                        icon: Presentation,
                      },
                      {
                        label: "Contenido",
                        caption: "Validar la biblioteca del cliente.",
                        to: buildClientContentsPath(clientId),
                        icon: Library,
                      },
                    ]
                  : []),
                {
                  label: "Timeline",
                  caption: "Revisar la programación de esta sucursal.",
                  to: buildBranchTimelinePath(clientId, branchId),
                  icon: Route,
                },
                {
                  label: "Layouts",
                  caption: "Consultar layouts disponibles para operación.",
                  to: buildBranchLayoutsPath(clientId, branchId),
                  icon: PanelsTopLeft,
                },
              ]}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}


