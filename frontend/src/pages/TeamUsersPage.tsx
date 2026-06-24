import { useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";

import { SectionCard } from "../components/SectionCard";
import { TeamUserFormDialog } from "../components/team/TeamUserFormDialog";
import { DeleteConfirmDialog } from "../components/operations/DeleteConfirmDialog";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatUserRole, formatUserStatus } from "../lib/labels";
import { canManageInternalTeam } from "../lib/rbac";
import type { SessionUser, UserRole, UserStatus } from "../types/domain";

type FormDialogState =
  | { mode: "create"; user: null }
  | { mode: "edit"; user: SessionUser }
  | null;

export function TeamUsersPage() {
  const { token, user } = useAuth();
  const [teamUsers, setTeamUsers] = useState<SessionUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formDialogState, setFormDialogState] = useState<FormDialogState>(null);
  const [deleteTarget, setDeleteTarget] = useState<SessionUser | null>(null);

  async function loadTeamUsers() {
    if (!token) {
      return;
    }
    setLoading(true);
    try {
      const response = await apiRequest<SessionUser[]>("/team/users", { token });
      setTeamUsers(response);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el equipo interno");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadTeamUsers();
  }, [token]);

  const orderedUsers = useMemo(
    () =>
      [...teamUsers].sort((left, right) => {
        if (left.role === "super_admin" && right.role !== "super_admin") {
          return -1;
        }
        if (left.role !== "super_admin" && right.role === "super_admin") {
          return 1;
        }
        return left.full_name.localeCompare(right.full_name, "es");
      }),
    [teamUsers],
  );

  if (!canManageInternalTeam(user?.role)) {
    return <Navigate to="/app/clients" replace />;
  }

  async function handleSubmitForm(payload: {
    full_name: string;
    email: string;
    password: string;
    role: UserRole;
    status: UserStatus;
  }) {
    if (!token || !formDialogState) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      if (formDialogState.mode === "create") {
        await apiRequest<SessionUser>("/team/users", {
          method: "POST",
          token,
          body: payload,
        });
      } else {
        await apiRequest<SessionUser>(`/team/users/${formDialogState.user.id}`, {
          method: "PATCH",
          token,
          body: {
            full_name: payload.full_name,
            email: payload.email,
            password: payload.password || null,
            role: payload.role,
          },
        });
        await apiRequest<SessionUser>(`/team/users/${formDialogState.user.id}/status`, {
          method: "PATCH",
          token,
          body: { status: payload.status },
        });
      }

      setFormDialogState(null);
      await loadTeamUsers();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo guardar el usuario interno");
    } finally {
      setSaving(false);
    }
  }

  async function handleToggleStatus(target: SessionUser) {
    if (!token) {
      return;
    }
    setBusyUserId(target.id);
    setError(null);
    try {
      await apiRequest<SessionUser>(`/team/users/${target.id}/status`, {
        method: "PATCH",
        token,
        body: {
          status: target.status === "active" ? "suspended" : "active",
        },
      });
      await loadTeamUsers();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo actualizar el estado");
    } finally {
      setBusyUserId(null);
    }
  }

  async function handleDeleteUser() {
    if (!token || !deleteTarget) {
      return;
    }
    setBusyUserId(deleteTarget.id);
    setError(null);
    try {
      await apiRequest<void>(`/team/users/${deleteTarget.id}`, {
        method: "DELETE",
        token,
      });
      setDeleteTarget(null);
      await loadTeamUsers();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo eliminar el usuario interno");
    } finally {
      setBusyUserId(null);
    }
  }

  return (
    <>
      <SectionCard
        title="Equipo interno"
        subtitle="Gestiona los accesos del equipo Tecno Control con proteccion para el ultimo super_admin."
        action={
          <button
            className="rounded-full bg-ink px-4 py-2.5 text-sm font-semibold text-white"
            type="button"
            onClick={() => setFormDialogState({ mode: "create", user: null })}
          >
            Crear usuario interno
          </button>
        }
      >
        {error ? <div className="mb-4 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
        {loading ? <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-600">Cargando equipo...</div> : null}
        {!loading ? (
          <div className="grid gap-4 xl:grid-cols-2">
            {orderedUsers.map((teamUser) => {
              const isBusy = busyUserId === teamUser.id;
              const statusTone =
                teamUser.status === "active"
                  ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                  : "border-amber-200 bg-amber-50 text-amber-800";

              return (
                <article key={teamUser.id} className="rounded-[26px] border border-slate-300 bg-white p-5">
                  <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-semibold text-ink">{teamUser.full_name}</p>
                        <span className="rounded-full border border-slate-300 bg-slate-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-slate-600">
                          {formatUserRole(teamUser.role)}
                        </span>
                        <span className={`rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-wide ${statusTone}`}>
                          {formatUserStatus(teamUser.status)}
                        </span>
                      </div>
                      <p className="mt-2 text-sm text-slate-600">{teamUser.email}</p>
                      <p className="mt-2 text-xs uppercase tracking-wide text-slate-500">
                        Ultimo acceso: {teamUser.last_login_at ? new Date(teamUser.last_login_at).toLocaleString("es-MX") : "Sin registro"}
                      </p>
                    </div>

                    <div className="flex flex-wrap gap-2">
                      <button
                        className="rounded-full border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700"
                        type="button"
                        onClick={() => setFormDialogState({ mode: "edit", user: teamUser })}
                      >
                        Editar
                      </button>
                      <button
                        className="rounded-full border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700"
                        disabled={isBusy}
                        type="button"
                        onClick={() => void handleToggleStatus(teamUser)}
                      >
                        {isBusy ? "Guardando..." : teamUser.status === "active" ? "Suspender" : "Reactivar"}
                      </button>
                      <button
                        className="rounded-full border border-rose-300 px-3 py-2 text-xs font-semibold text-rose-600"
                        type="button"
                        onClick={() => setDeleteTarget(teamUser)}
                      >
                        Eliminar
                      </button>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        ) : null}
      </SectionCard>

      <TeamUserFormDialog
        open={Boolean(formDialogState)}
        mode={formDialogState?.mode ?? "create"}
        initialUser={formDialogState?.mode === "edit" ? formDialogState.user : null}
        errorMessage={error}
        isSaving={saving}
        onCancel={() => setFormDialogState(null)}
        onSubmit={(payload) => {
          void handleSubmitForm(payload);
        }}
      />

      <DeleteConfirmDialog
        open={Boolean(deleteTarget)}
        title={deleteTarget ? `Eliminar usuario ${deleteTarget.full_name}` : "Eliminar usuario"}
        description="Esta accion quitara el acceso interno seleccionado. El sistema bloqueara el ultimo super_admin."
        confirmLabel="Eliminar usuario"
        errorMessage={error}
        isDeleting={Boolean(deleteTarget && busyUserId === deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => {
          void handleDeleteUser();
        }}
      />
    </>
  );
}
