import { FormEvent, useEffect, useMemo, useState } from "react";

import { DeleteConfirmDialog } from "../operations/DeleteConfirmDialog";
import { apiRequest } from "../../lib/api";
import { formatUserRole, formatUserStatus } from "../../lib/labels";
import type { SessionUser, UserRole, UserStatus } from "../../types/domain";

type ClientUsersPanelProps = {
  clientId: string;
  token: string;
  clientStatus?: string;
};

type UserFormState = {
  full_name: string;
  email: string;
  password: string;
  role: UserRole;
  status: UserStatus;
};

const defaultForm: UserFormState = {
  full_name: "",
  email: "",
  password: "ChangeMe123!",
  role: "client_admin",
  status: "active",
};

const roleOptions: Array<{ value: UserRole; label: string }> = [
  { value: "client_admin", label: "Administrador de cliente" },
  { value: "client_operator", label: "Operador de cliente" },
  { value: "branch_manager", label: "Gerente de sucursal (legacy)" },
  { value: "operator", label: "Operador (legacy)" },
];

export function ClientUsersPanel({ clientId, token, clientStatus = "active" }: ClientUsersPanelProps) {
  const [users, setUsers] = useState<SessionUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<UserFormState>(defaultForm);
  const [editingUserId, setEditingUserId] = useState<string | null>(null);
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<SessionUser | null>(null);

  const submitLabel = editingUserId ? "Guardar cambios" : "Crear usuario";
  const statusTone =
    clientStatus === "suspended"
      ? "border-amber-200 bg-amber-50 text-amber-900"
      : "border-emerald-200 bg-emerald-50 text-emerald-800";

  async function loadUsers() {
    setLoading(true);
    try {
      const response = await apiRequest<SessionUser[]>(`/clients/${clientId}/users`, { token });
      setUsers(response);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar los usuarios del cliente");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadUsers();
  }, [clientId, token]);

  const orderedUsers = useMemo(
    () =>
      [...users].sort((left, right) => {
        if (left.role === "client_admin" && right.role !== "client_admin") {
          return -1;
        }
        if (left.role !== "client_admin" && right.role === "client_admin") {
          return 1;
        }
        return left.full_name.localeCompare(right.full_name, "es");
      }),
    [users],
  );

  function resetForm() {
    setForm(defaultForm);
    setEditingUserId(null);
  }

  function startEditingUser(user: SessionUser) {
    setEditingUserId(user.id);
    setForm({
      full_name: user.full_name,
      email: user.email,
      password: "",
      role: user.role,
      status: user.status,
    });
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);

    try {
      if (editingUserId) {
        await apiRequest<SessionUser>(`/clients/${clientId}/users/${editingUserId}`, {
          method: "PATCH",
          token,
          body: {
            full_name: form.full_name,
            email: form.email,
            password: form.password || null,
            role: form.role,
          },
        });
        await apiRequest<SessionUser>(`/clients/${clientId}/users/${editingUserId}/status`, {
          method: "PATCH",
          token,
          body: { status: form.status },
        });
      } else {
        await apiRequest<SessionUser>(`/clients/${clientId}/users`, {
          method: "POST",
          token,
          body: {
            full_name: form.full_name,
            email: form.email,
            password: form.password,
            role: form.role,
            status: form.status,
          },
        });
      }

      resetForm();
      await loadUsers();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo guardar el usuario");
    } finally {
      setSaving(false);
    }
  }

  async function handleToggleStatus(user: SessionUser) {
    setBusyUserId(user.id);
    setError(null);
    try {
      await apiRequest<SessionUser>(`/clients/${clientId}/users/${user.id}/status`, {
        method: "PATCH",
        token,
        body: {
          status: user.status === "active" ? "suspended" : "active",
        },
      });
      await loadUsers();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo actualizar el estado del usuario");
    } finally {
      setBusyUserId(null);
    }
  }

  async function handleDeleteUser() {
    if (!deleteTarget) {
      return;
    }
    setBusyUserId(deleteTarget.id);
    setError(null);
    try {
      await apiRequest<void>(`/clients/${clientId}/users/${deleteTarget.id}`, {
        method: "DELETE",
        token,
      });
      if (editingUserId === deleteTarget.id) {
        resetForm();
      }
      setDeleteTarget(null);
      await loadUsers();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo eliminar el usuario");
    } finally {
      setBusyUserId(null);
    }
  }

  return (
    <div className="grid gap-5 xl:grid-cols-[0.95fr_1.05fr]">
      <section className="rounded-[24px] border border-slate-300 bg-slate-50/80 p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h4 className="text-base font-semibold text-ink">{editingUserId ? "Editar usuario" : "Nuevo usuario del cliente"}</h4>
            <p className="mt-1 text-sm text-slate-600">Crea accesos para este cliente sin salir del panel.</p>
          </div>
          <span className={`rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide ${statusTone}`}>
            Cliente {formatUserStatus(clientStatus)}
          </span>
        </div>

        <form className="mt-5 grid gap-4" onSubmit={handleSubmit}>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
            <input value={form.full_name} onChange={(event) => setForm({ ...form, full_name: event.target.value })} required />
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Email</label>
            <input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} required />
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">{editingUserId ? "Nueva clave (opcional)" : "Contrasena temporal"}</label>
              <input
                value={form.password}
                onChange={(event) => setForm({ ...form, password: event.target.value })}
                required={!editingUserId}
              />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Rol</label>
              <select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value as UserRole })}>
                {roleOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Estado</label>
            <select value={form.status} onChange={(event) => setForm({ ...form, status: event.target.value as UserStatus })}>
              <option value="active">Activo</option>
              <option value="suspended">Suspendido</option>
            </select>
          </div>

          {error ? <div className="rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

          <div className="flex flex-wrap gap-3">
            <button className="rounded-full bg-ink px-4 py-2.5 text-sm font-semibold text-white" disabled={saving} type="submit">
              {saving ? "Guardando..." : submitLabel}
            </button>
            {editingUserId ? (
              <button
                className="rounded-full border border-slate-300 px-4 py-2.5 text-sm font-semibold text-slate-700"
                type="button"
                onClick={resetForm}
              >
                Cancelar edicion
              </button>
            ) : null}
          </div>
        </form>
      </section>

      <section className="rounded-[24px] border border-slate-300 bg-white p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h4 className="text-base font-semibold text-ink">Usuarios del cliente</h4>
            <p className="mt-1 text-sm text-slate-600">{orderedUsers.length} acceso(s) con alcance en este workspace.</p>
          </div>
        </div>

        <div className="mt-4 space-y-3">
          {loading ? <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-600">Cargando usuarios...</div> : null}
          {!loading && orderedUsers.length === 0 ? (
            <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-600">Todavia no hay usuarios registrados para este cliente.</div>
          ) : null}
          {!loading
            ? orderedUsers.map((user) => {
                const isBusy = busyUserId === user.id;
                const badgeTone =
                  user.status === "active"
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                    : "border-amber-200 bg-amber-50 text-amber-800";

                return (
                  <article key={user.id} className="rounded-[22px] border border-slate-300 bg-slate-50/60 p-4">
                    <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="font-semibold text-ink">{user.full_name}</p>
                          <span className="rounded-full border border-slate-300 bg-white px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-slate-600">
                            {formatUserRole(user.role)}
                          </span>
                          <span className={`rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-wide ${badgeTone}`}>
                            {formatUserStatus(user.status)}
                          </span>
                        </div>
                        <p className="mt-2 text-sm text-slate-600">{user.email}</p>
                        <p className="mt-2 text-xs uppercase tracking-wide text-slate-500">
                          Ultimo acceso: {user.last_login_at ? new Date(user.last_login_at).toLocaleString("es-MX") : "Sin registro"}
                        </p>
                      </div>

                      <div className="flex flex-wrap gap-2">
                        <button
                          className="rounded-full border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700"
                          type="button"
                          onClick={() => startEditingUser(user)}
                        >
                          Editar
                        </button>
                        <button
                          className="rounded-full border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700"
                          disabled={isBusy}
                          type="button"
                          onClick={() => void handleToggleStatus(user)}
                        >
                          {isBusy ? "Guardando..." : user.status === "active" ? "Suspender" : "Reactivar"}
                        </button>
                        <button
                          className="rounded-full border border-rose-300 px-3 py-2 text-xs font-semibold text-rose-600"
                          type="button"
                          onClick={() => setDeleteTarget(user)}
                        >
                          Eliminar
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })
            : null}
        </div>
      </section>

      <DeleteConfirmDialog
        open={Boolean(deleteTarget)}
        title={deleteTarget ? `Eliminar usuario ${deleteTarget.full_name}` : "Eliminar usuario"}
        description="Esta accion borrara el acceso seleccionado del cliente."
        confirmLabel="Eliminar usuario"
        errorMessage={error}
        isDeleting={Boolean(deleteTarget && busyUserId === deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => {
          void handleDeleteUser();
        }}
      />
    </div>
  );
}
