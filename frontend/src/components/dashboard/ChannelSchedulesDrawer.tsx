import { CalendarClock, PencilLine, Power, Save, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Branch, Campaign, Channel, ScheduleItem } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";

const WEEKDAY_OPTIONS = [
  { value: 1, label: "Lun" },
  { value: 2, label: "Mar" },
  { value: 3, label: "Mie" },
  { value: 4, label: "Jue" },
  { value: 5, label: "Vie" },
  { value: 6, label: "Sab" },
  { value: 7, label: "Dom" },
];

const ALL_WEEKDAY_VALUES = WEEKDAY_OPTIONS.map((day) => day.value);

type ScheduleFormState = {
  campaignId: string;
  title: string;
  startsOn: string;
  endsOn: string;
  startTime: string;
  endTime: string;
  weekdays: number[];
  priority: number;
  isActive: boolean;
};

function chipClassName(active = false) {
  return [
    "rounded-full border px-3 py-2 text-xs font-semibold uppercase tracking-[0.14em] transition",
    active
      ? "border-slate-900 bg-slate-900 text-white"
      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-ink",
  ].join(" ");
}

function toggleWeekday(current: number[], day: number) {
  const next = new Set(current);
  if (next.has(day)) {
    next.delete(day);
  } else {
    next.add(day);
  }
  return Array.from(next).sort((left, right) => left - right);
}

function deriveRecurrence({
  startsOn,
  endsOn,
  weekdays,
}: {
  startsOn: string;
  endsOn: string;
  weekdays: number[];
}) {
  if (weekdays.length === ALL_WEEKDAY_VALUES.length) {
    return "daily";
  }
  if (weekdays.length > 0) {
    return "weekly";
  }
  if (startsOn && endsOn && startsOn === endsOn) {
    return "once";
  }
  return "daily";
}

function buildFormState(schedule: ScheduleItem): ScheduleFormState {
  return {
    campaignId: schedule.campaign_id,
    title: schedule.title,
    startsOn: schedule.starts_on ?? "",
    endsOn: schedule.ends_on ?? "",
    startTime: schedule.start_time ? schedule.start_time.slice(0, 5) : "",
    endTime: schedule.end_time ? schedule.end_time.slice(0, 5) : "",
    weekdays: [...schedule.days_of_week].sort((left, right) => left - right),
    priority: schedule.priority,
    isActive: schedule.is_active,
  };
}

function formatWeekdays(days: number[]) {
  if (days.length === 0 || days.length === ALL_WEEKDAY_VALUES.length) {
    return "Todos los dias";
  }
  return WEEKDAY_OPTIONS.filter((day) => days.includes(day.value))
    .map((day) => day.label)
    .join(", ");
}

function formatDateRange(schedule: ScheduleItem) {
  if (!schedule.starts_on && !schedule.ends_on) {
    return "Sin limite de fechas";
  }
  if (schedule.starts_on && schedule.ends_on) {
    return `${schedule.starts_on} -> ${schedule.ends_on}`;
  }
  return `${schedule.starts_on ?? "Sin inicio"} -> ${schedule.ends_on ?? "Sin fin"}`;
}

function formatTimeRange(schedule: ScheduleItem) {
  if (!schedule.start_time && !schedule.end_time) {
    return "Todo el dia";
  }
  return `${schedule.start_time?.slice(0, 5) ?? "00:00"} -> ${schedule.end_time?.slice(0, 5) ?? "23:59"}`;
}

function getZonedNow(timezone: string) {
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone || "America/Mexico_City",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });

  const parts = formatter.formatToParts(new Date());
  const partMap = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  const weekdayMap: Record<string, number> = {
    Mon: 1,
    Tue: 2,
    Wed: 3,
    Thu: 4,
    Fri: 5,
    Sat: 6,
    Sun: 7,
  };

  return {
    isoDate: `${partMap.year}-${partMap.month}-${partMap.day}`,
    weekday: weekdayMap[partMap.weekday] ?? 1,
    minutes: Number(partMap.hour ?? "0") * 60 + Number(partMap.minute ?? "0"),
  };
}

function minutesFromTime(raw: string | null) {
  if (!raw) {
    return 0;
  }

  const [hours, minutes] = raw.split(":").map((value) => Number(value));
  return hours * 60 + minutes;
}

function getScheduleState(schedule: ScheduleItem) {
  if (!schedule.is_active) {
    return { label: "Inactiva", tone: "slate" } as const;
  }

  const zonedNow = getZonedNow(schedule.timezone || "America/Mexico_City");
  if (schedule.starts_on && zonedNow.isoDate < schedule.starts_on) {
    return { label: "Futura", tone: "amber" } as const;
  }
  if (schedule.ends_on && zonedNow.isoDate > schedule.ends_on) {
    return { label: "Finalizada", tone: "rose" } as const;
  }
  if (schedule.days_of_week.length > 0 && !schedule.days_of_week.includes(zonedNow.weekday)) {
    return { label: "Programada", tone: "amber" } as const;
  }

  const startMinutes = schedule.start_time ? minutesFromTime(schedule.start_time) : 0;
  const endMinutes = schedule.end_time ? minutesFromTime(schedule.end_time) : 24 * 60;
  if (zonedNow.minutes < startMinutes) {
    return { label: "Futura", tone: "amber" } as const;
  }
  if (zonedNow.minutes > endMinutes) {
    return { label: "Fuera de horario", tone: "slate" } as const;
  }

  return { label: "Activa", tone: "emerald" } as const;
}

function statusClassName(tone: "emerald" | "amber" | "rose" | "slate") {
  switch (tone) {
    case "emerald":
      return "bg-emerald-50 text-emerald-700";
    case "amber":
      return "bg-amber-50 text-amber-800";
    case "rose":
      return "bg-rose-50 text-rose-700";
    default:
      return "bg-slate-100 text-slate-700";
  }
}

function sortSchedules(schedules: ScheduleItem[]) {
  return schedules.slice().sort((left, right) => {
    const rank = (schedule: ScheduleItem) => {
      const stateLabel = getScheduleState(schedule).label;
      if (stateLabel === "Activa") {
        return 0;
      }
      if (stateLabel === "Futura" || stateLabel === "Programada") {
        return 1;
      }
      if (stateLabel === "Fuera de horario") {
        return 2;
      }
      if (stateLabel === "Inactiva") {
        return 3;
      }
      return 4;
    };

    const rankDifference = rank(left) - rank(right);
    if (rankDifference !== 0) {
      return rankDifference;
    }

    const leftDate = `${left.starts_on ?? "9999-12-31"} ${left.start_time ?? "23:59"}`;
    const rightDate = `${right.starts_on ?? "9999-12-31"} ${right.start_time ?? "23:59"}`;
    return leftDate.localeCompare(rightDate) || right.priority - left.priority;
  });
}

export function ChannelSchedulesDrawer({
  open,
  token,
  branch,
  channel,
  schedules,
  campaigns,
  onClose,
  onSaved,
}: {
  open: boolean;
  token: string | null;
  branch: Branch | null;
  channel: Channel | null;
  schedules: ScheduleItem[];
  campaigns: Campaign[];
  onClose: () => void;
  onSaved: (message: string) => Promise<void> | void;
}) {
  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null);
  const [formState, setFormState] = useState<ScheduleFormState | null>(null);
  const [busyAction, setBusyAction] = useState<"save" | "toggle" | "delete" | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const campaignsById = useMemo(() => new Map(campaigns.map((campaign) => [campaign.id, campaign])), [campaigns]);
  const orderedSchedules = useMemo(() => sortSchedules(schedules), [schedules]);
  const editingSchedule = useMemo(
    () => orderedSchedules.find((schedule) => schedule.id === editingScheduleId) ?? null,
    [editingScheduleId, orderedSchedules],
  );
  const allDaysSelected = Boolean(formState && formState.weekdays.length === ALL_WEEKDAY_VALUES.length);

  useEffect(() => {
    if (!open) {
      setEditingScheduleId(null);
      setFormState(null);
      setBusyAction(null);
      setFeedback(null);
      setError(null);
      return;
    }

    if (editingScheduleId && editingSchedule) {
      return;
    }

    setEditingScheduleId(null);
    setFormState(null);
  }, [editingSchedule, editingScheduleId, open]);

  async function handleSaveSchedule() {
    if (!token || !editingSchedule || !formState) {
      return;
    }

    setBusyAction("save");
    setFeedback(null);
    setError(null);

    try {
      await apiRequest<ScheduleItem>(`/schedules/${editingSchedule.id}`, {
        method: "PATCH",
        token,
        body: {
          campaign_id: formState.campaignId,
          title: formState.title.trim(),
          recurrence: deriveRecurrence({
            startsOn: formState.startsOn,
            endsOn: formState.endsOn,
            weekdays: formState.weekdays,
          }),
          days_of_week: formState.weekdays,
          starts_on: formState.startsOn || null,
          ends_on: formState.endsOn || null,
          start_time: formState.startTime || null,
          end_time: formState.endTime || null,
          is_active: formState.isActive,
          priority: Math.max(1, formState.priority || 1),
          timezone: editingSchedule.timezone,
        },
      });
      setFeedback("Programacion actualizada correctamente.");
      await onSaved("Programacion actualizada correctamente.");
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, "No se pudo actualizar la programacion."));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleToggleSchedule(schedule: ScheduleItem) {
    if (!token) {
      return;
    }

    setBusyAction("toggle");
    setFeedback(null);
    setError(null);

    try {
      await apiRequest<ScheduleItem>(`/schedules/${schedule.id}`, {
        method: "PATCH",
        token,
        body: {
          is_active: !schedule.is_active,
        },
      });
      const nextMessage = schedule.is_active ? "Programacion desactivada correctamente." : "Programacion reactivada correctamente.";
      setFeedback(nextMessage);
      await onSaved(nextMessage);
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, "No se pudo cambiar el estado de la programacion."));
    } finally {
      setBusyAction(null);
    }
  }

  async function handleDeleteSchedule(schedule: ScheduleItem) {
    if (!token) {
      return;
    }

    const confirmed = window.confirm(`¿Eliminar la programacion "${schedule.title}"?`);
    if (!confirmed) {
      return;
    }

    setBusyAction("delete");
    setFeedback(null);
    setError(null);

    try {
      await apiRequest(`/schedules/${schedule.id}`, {
        method: "DELETE",
        token,
      });
      if (editingScheduleId === schedule.id) {
        setEditingScheduleId(null);
        setFormState(null);
      }
      setFeedback("Programacion eliminada correctamente.");
      await onSaved("Programacion eliminada correctamente.");
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, "No se pudo eliminar la programacion."));
    } finally {
      setBusyAction(null);
    }
  }

  if (!open || !branch || !channel) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-950/45 backdrop-blur-sm">
      <button aria-label="Cerrar" className="flex-1 cursor-default" type="button" onClick={onClose} />

      <aside className="flex h-screen w-[min(95vw,1120px)] max-w-none flex-col overflow-x-hidden overflow-y-auto border-l border-white/10 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-6 py-5">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.24em] text-accent">Programaciones</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{channel.name}</h3>
            <p className="mt-2 text-sm text-slate-600">
              {branch.name} · {channel.channel_code} · {channel.mode} · {channel.resolution_width}x{channel.resolution_height}
            </p>
          </div>

          <button
            aria-label="Cerrar drawer"
            className="rounded-2xl border border-slate-200 p-3 text-slate-500 transition hover:border-slate-300 hover:text-ink"
            type="button"
            onClick={onClose}
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="grid gap-5 px-6 py-5 xl:grid-cols-[minmax(0,1.25fr)_380px]">
          <div className="space-y-4">
            {feedback ? <div className="rounded-[20px] bg-emerald-50 px-4 py-3 text-sm text-emerald-700">{feedback}</div> : null}
            {error ? <div className="rounded-[20px] bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

            <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
              <div className="flex items-start gap-3">
                <span className="rounded-2xl bg-white p-3 text-accent shadow-sm">
                  <CalendarClock className="h-5 w-5" />
                </span>
                <div>
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Resumen</p>
                  <p className="mt-1 text-sm text-slate-600">
                    Esta vista mezcla reglas especificas de la pantalla y reglas de sucursal que tambien afectan a este canal.
                  </p>
                </div>
              </div>
            </div>

            {orderedSchedules.length === 0 ? (
              <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
                Esta pantalla no tiene programaciones activas o futuras. La publicacion inmediata sigue funcionando sin reglas horarias.
              </div>
            ) : (
              orderedSchedules.map((schedule) => {
                const status = getScheduleState(schedule);
                const campaignName = campaignsById.get(schedule.campaign_id)?.name ?? "Campana desconocida";

                return (
                  <article key={schedule.id} className="rounded-[24px] border border-slate-200 bg-white px-4 py-4 shadow-sm">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.14em] ${statusClassName(status.tone)}`}>
                            {status.label}
                          </span>
                          <span className="rounded-full bg-slate-100 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-600">
                            Prioridad {schedule.priority}
                          </span>
                        </div>
                        <p className="mt-3 text-lg font-semibold text-ink">{schedule.title}</p>
                        <p className="mt-1 text-sm text-slate-600">{campaignName}</p>
                        <p className="mt-1 text-xs text-slate-500">
                          {schedule.channel_id ? "Afecta solo a esta pantalla" : "Regla de sucursal que tambien aplica a esta pantalla"}
                        </p>
                      </div>

                      <div className="flex flex-wrap gap-2">
                        <button
                          className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 transition hover:border-cyan-300 hover:text-ink"
                          type="button"
                          onClick={() => {
                            setEditingScheduleId(schedule.id);
                            setFormState(buildFormState(schedule));
                            setFeedback(null);
                            setError(null);
                          }}
                        >
                          <PencilLine className="h-3.5 w-3.5" />
                          Editar
                        </button>
                        <button
                          className="inline-flex items-center gap-2 rounded-full border border-amber-200 px-3 py-2 text-xs font-semibold text-amber-800 transition hover:border-amber-300 hover:bg-amber-50"
                          type="button"
                          onClick={() => void handleToggleSchedule(schedule)}
                        >
                          <Power className="h-3.5 w-3.5" />
                          {schedule.is_active ? "Desactivar" : "Reactivar"}
                        </button>
                        <button
                          className="inline-flex items-center gap-2 rounded-full border border-rose-200 px-3 py-2 text-xs font-semibold text-rose-700 transition hover:border-rose-300 hover:bg-rose-50"
                          type="button"
                          onClick={() => void handleDeleteSchedule(schedule)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          Eliminar
                        </button>
                      </div>
                    </div>

                    <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                      <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
                        <p className="text-[11px] uppercase tracking-[0.2em] text-slate-400">Pantalla / sucursal</p>
                        <p className="mt-2 text-sm font-semibold text-ink">{channel.name}</p>
                        <p className="mt-1 text-xs text-slate-500">{branch.name}</p>
                      </div>
                      <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
                        <p className="text-[11px] uppercase tracking-[0.2em] text-slate-400">Fechas</p>
                        <p className="mt-2 text-sm font-semibold text-ink">{formatDateRange(schedule)}</p>
                      </div>
                      <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
                        <p className="text-[11px] uppercase tracking-[0.2em] text-slate-400">Horario</p>
                        <p className="mt-2 text-sm font-semibold text-ink">{formatTimeRange(schedule)}</p>
                      </div>
                      <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
                        <p className="text-[11px] uppercase tracking-[0.2em] text-slate-400">Dias</p>
                        <p className="mt-2 text-sm font-semibold text-ink">{formatWeekdays(schedule.days_of_week)}</p>
                      </div>
                    </div>
                  </article>
                );
              })
            )}
          </div>

          <div className="space-y-4">
            <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
              <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Edicion</p>
              {editingSchedule && formState ? (
                <div className="mt-4 space-y-4">
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Campana</label>
                    <select
                      value={formState.campaignId}
                      onChange={(event) => setFormState((current) => (current ? { ...current, campaignId: event.target.value } : current))}
                    >
                      {campaigns.map((campaign) => (
                        <option key={campaign.id} value={campaign.id}>
                          {campaign.name}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre de programacion</label>
                    <input
                      value={formState.title}
                      onChange={(event) => setFormState((current) => (current ? { ...current, title: event.target.value } : current))}
                    />
                  </div>

                  <div className="grid gap-4 md:grid-cols-2">
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Fecha inicio</label>
                      <input
                        type="date"
                        value={formState.startsOn}
                        onChange={(event) => setFormState((current) => (current ? { ...current, startsOn: event.target.value } : current))}
                      />
                    </div>
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Fecha fin</label>
                      <input
                        type="date"
                        value={formState.endsOn}
                        onChange={(event) => setFormState((current) => (current ? { ...current, endsOn: event.target.value } : current))}
                      />
                    </div>
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Hora inicio</label>
                      <input
                        type="time"
                        value={formState.startTime}
                        onChange={(event) => setFormState((current) => (current ? { ...current, startTime: event.target.value } : current))}
                      />
                    </div>
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Hora fin</label>
                      <input
                        type="time"
                        value={formState.endTime}
                        onChange={(event) => setFormState((current) => (current ? { ...current, endTime: event.target.value } : current))}
                      />
                    </div>
                    <div>
                      <label className="mb-2 block text-sm font-semibold text-slate-700">Prioridad</label>
                      <input
                        type="number"
                        min={1}
                        value={formState.priority}
                        onChange={(event) =>
                          setFormState((current) =>
                            current ? { ...current, priority: Math.max(1, Number(event.target.value) || 1) } : current,
                          )
                        }
                      />
                    </div>
                    <label className="flex items-center gap-3 rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-3">
                      <input
                        checked={formState.isActive}
                        className="h-4 w-4 rounded border-slate-300 text-slate-900 focus:ring-slate-400"
                        type="checkbox"
                        onChange={(event) => setFormState((current) => (current ? { ...current, isActive: event.target.checked } : current))}
                      />
                      <span className="text-sm font-semibold text-ink">Regla activa</span>
                    </label>
                  </div>

                  <div>
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-sm font-semibold text-slate-700">Dias de la semana</p>
                      <button
                        className={chipClassName(allDaysSelected)}
                        type="button"
                        onClick={() =>
                          setFormState((current) =>
                            current
                              ? {
                                  ...current,
                                  weekdays: allDaysSelected ? [] : [...ALL_WEEKDAY_VALUES],
                                }
                              : current,
                          )
                        }
                      >
                        Todos los dias
                      </button>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {WEEKDAY_OPTIONS.map((day) => (
                        <button
                          key={day.value}
                          className={chipClassName(formState.weekdays.includes(day.value))}
                          type="button"
                          onClick={() =>
                            setFormState((current) =>
                              current
                                ? {
                                    ...current,
                                    weekdays: toggleWeekday(current.weekdays, day.value),
                                  }
                                : current,
                            )
                          }
                        >
                          {day.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="flex flex-wrap gap-3">
                    <button
                      className="inline-flex items-center gap-2 rounded-[18px] bg-ink px-5 py-3 font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
                      disabled={!formState.title.trim() || !formState.campaignId || busyAction !== null}
                      type="button"
                      onClick={() => void handleSaveSchedule()}
                    >
                      <Save className="h-4 w-4" />
                      {busyAction === "save" ? "Guardando..." : "Guardar cambios"}
                    </button>
                    <button
                      className="rounded-[18px] border border-slate-200 px-5 py-3 font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink"
                      type="button"
                      onClick={() => {
                        setEditingScheduleId(null);
                        setFormState(null);
                        setFeedback(null);
                        setError(null);
                      }}
                    >
                      Cancelar
                    </button>
                  </div>
                </div>
              ) : (
                <div className="mt-4 rounded-[20px] border border-dashed border-slate-300 bg-slate-50 px-4 py-4 text-sm text-slate-600">
                  Selecciona una programacion de la lista para editarla, desactivarla o eliminarla.
                </div>
              )}
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
}
