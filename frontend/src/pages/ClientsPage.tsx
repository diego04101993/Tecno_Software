import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { SectionCard } from "../components/SectionCard";
import { DeleteConfirmDialog } from "../components/operations/DeleteConfirmDialog";
import { DeleteClientConfirmDialog } from "../components/clients/DeleteClientConfirmDialog";
import { EditClientDrawer } from "../components/clients/EditClientDrawer";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatClientStatus } from "../lib/labels";
import { canManageClientDirectory, canManageClientUsers, canManageInternalTeam, isSuperAdmin } from "../lib/rbac";
import { buildClientOverviewPath } from "../lib/workspace";
import type { Branch, Channel, Client, SessionUser } from "../types/domain";

type ClientFormState = {
  name: string;
  slug: string;
  contact_email: string;
  brand_name: string;
  self_managed: boolean;
  admin_full_name: string;
  admin_email: string;
  temporary_password: string;
};

type ProvisioningNotice = {
  tone: "success" | "warning";
  title: string;
  description: string;
  clientName: string;
  workspacePath: string;
  adminName?: string;
  adminEmail?: string;
  temporaryPassword?: string;
};

const initialForm: ClientFormState = {
  name: "",
  slug: "",
  contact_email: "",
  brand_name: "",
  self_managed: true,
  admin_full_name: "",
  admin_email: "",
  temporary_password: "ChangeMe123!",
};

export function ClientsPage() {
  const { token, user } = useAuth();
  const [clients, setClients] = useState<Client[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<ProvisioningNotice | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [statusUpdating, setStatusUpdating] = useState(false);
  const [deletingClient, setDeletingClient] = useState(false);
  const [form, setForm] = useState<ClientFormState>(initialForm);
  const [editingClient, setEditingClient] = useState<Client | null>(null);
  const [drawerTab, setDrawerTab] = useState<"general" | "users" | "status">("general");
  const [statusTarget, setStatusTarget] = useState<Client | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Client | null>(null);

  const canManageDirectory = canManageClientDirectory(user?.role);
  const canDeleteClients = isSuperAdmin(user?.role);
  const canOpenTeam = canManageInternalTeam(user?.role);

  const clientCards = useMemo(
    () =>
      clients.map((client) => {
        const clientBranches = branches.filter((branch) => branch.client_id === client.id);
        const clientChannels = channels.filter((channel) => channel.client_id === client.id);
        const onlineChannels = clientChannels.filter((channel) => channel.is_online).length;

        return {
          client,
          branchCount: clientBranches.length,
          channelCount: clientChannels.length,
          onlineChannels,
        };
      }),
    [branches, channels, clients],
  );

  async function loadData() {
    if (!token) {
      return;
    }

    try {
      const [clientsResponse, branchesResponse, channelsResponse] = await Promise.all([
        apiRequest<Client[]>("/clients", { token }),
        apiRequest<Branch[]>("/branches", { token }),
        apiRequest<Channel[]>("/channels", { token }),
      ]);
      setClients(clientsResponse);
      setBranches(branchesResponse);
      setChannels(channelsResponse);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar los clientes");
    }
  }

  useEffect(() => {
    void loadData();
  }, [token]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !canManageDirectory) {
      return;
    }

    setSubmitting(true);
    setError(null);
    setNotice(null);

    try {
      const createdClient = await apiRequest<Client>("/clients", {
        method: "POST",
        token,
        body: {
          name: form.name,
          slug: form.slug,
          contact_email: form.contact_email || null,
          brand_name: form.brand_name || null,
        },
      });

      const workspacePath = buildClientOverviewPath(createdClient.id);

      if (!form.self_managed) {
        setNotice({
          tone: "success",
          title: "Cliente creado",
          description: "La cuenta SaaS quedo registrada y lista para configurar usuarios mas adelante.",
          clientName: createdClient.name,
          workspacePath,
        });
      } else {
        try {
          await apiRequest<SessionUser>(`/clients/${createdClient.id}/users`, {
            method: "POST",
            token,
            body: {
              full_name: form.admin_full_name,
              email: form.admin_email,
              password: form.temporary_password,
              role: "client_admin",
              status: "active",
            },
          });

          setNotice({
            tone: "success",
            title: "Cliente y acceso inicial listos",
            description: "Se creo el cliente y su administrador principal quedo listo para iniciar sesion.",
            clientName: createdClient.name,
            workspacePath,
            adminName: form.admin_full_name,
            adminEmail: form.admin_email,
            temporaryPassword: form.temporary_password,
          });
        } catch (userError) {
          setNotice({
            tone: "warning",
            title: "Cliente creado; acceso administrador pendiente",
            description:
              userError instanceof Error
                ? userError.message
                : "El cliente se creo, pero no se pudo provisionar el usuario administrador.",
            clientName: createdClient.name,
            workspacePath,
            adminName: form.admin_full_name,
            adminEmail: form.admin_email,
            temporaryPassword: form.temporary_password,
          });
        }
      }

      setForm(initialForm);
      await loadData();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear el cliente");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleToggleClientStatus() {
    if (!token || !statusTarget) {
      return;
    }
    setStatusUpdating(true);
    setError(null);

    try {
      const updated = await apiRequest<Client>(`/clients/${statusTarget.id}/status`, {
        method: "PATCH",
        token,
        body: {
          status: statusTarget.status === "active" ? "suspended" : "active",
        },
      });
      setClients((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setEditingClient((current) => (current?.id === updated.id ? updated : current));
      setStatusTarget(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo actualizar el estado del cliente");
    } finally {
      setStatusUpdating(false);
    }
  }

  async function handleDeleteClient(confirmText: string) {
    if (!token || !deleteTarget) {
      return;
    }
    setDeletingClient(true);
    setError(null);

    try {
      await apiRequest<void>(`/clients/${deleteTarget.id}`, {
        method: "DELETE",
        token,
        body: { confirm_text: confirmText },
      });
      setClients((current) => current.filter((item) => item.id !== deleteTarget.id));
      setEditingClient((current) => (current?.id === deleteTarget.id ? null : current));
      setDeleteTarget(null);
      await loadData();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo eliminar el cliente");
    } finally {
      setDeletingClient(false);
    }
  }

  function openClientDrawer(client: Client, tab: "general" | "users" | "status" = "general") {
    setEditingClient(client);
    setDrawerTab(tab);
  }

  return (
    <>
      <div className="grid gap-5 2xl:grid-cols-[1.12fr_0.88fr]">
        <SectionCard
          title="Cartera de clientes"
          subtitle="Controla cuentas SaaS, estados, accesos y entrada directa al workspace de cada cliente."
          action={
            canOpenTeam ? (
              <Link className="text-sm font-semibold text-ink underline decoration-accent/40 underline-offset-4" to="/app/team/users">
                Ir a equipo interno
              </Link>
            ) : null
          }
        >
          {error ? <div className="mb-4 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
          <div className="grid gap-4 xl:grid-cols-2">
            {clientCards.map(({ client, branchCount, channelCount, onlineChannels }) => {
              const statusTone =
                client.status === "active"
                  ? "bg-accentSoft text-accent"
                  : "bg-amber-100 text-amber-900";

              return (
                <article key={client.id} className="rounded-[28px] border border-slate-300 bg-white p-5 shadow-sm">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <p className="truncate font-display text-2xl text-ink" title={client.name}>
                        {client.name}
                      </p>
                      <p className="mt-2 truncate text-sm text-slate-500" title={client.slug}>
                        {client.slug}
                      </p>
                    </div>
                    <span className={`rounded-full px-4 py-2 text-xs font-semibold uppercase tracking-wide ${statusTone}`}>
                      {formatClientStatus(client.status)}
                    </span>
                  </div>

                  <div className="mt-5 grid gap-3 md:grid-cols-3">
                    <div className="rounded-2xl border border-slate-300 bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      <p className="text-xs uppercase tracking-wide text-slate-500">Sucursales</p>
                      <p className="mt-2 font-semibold text-ink">{branchCount}</p>
                    </div>
                    <div className="rounded-2xl border border-slate-300 bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      <p className="text-xs uppercase tracking-wide text-slate-500">Pantallas</p>
                      <p className="mt-2 font-semibold text-ink">
                        {channelCount} totales · {onlineChannels} online
                      </p>
                    </div>
                    <div className="rounded-2xl border border-slate-300 bg-slate-50 px-4 py-3 text-sm text-slate-700">
                      <p className="text-xs uppercase tracking-wide text-slate-500">Contacto</p>
                      <p className="mt-2 truncate font-semibold text-ink" title={client.contact_email ?? "Sin correo definido"}>
                        {client.contact_email ?? "Sin correo definido"}
                      </p>
                    </div>
                  </div>

                  <div className="mt-5 flex flex-wrap gap-2">
                    <Link
                      className="inline-flex rounded-full border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
                      to={buildClientOverviewPath(client.id)}
                    >
                      Abrir workspace
                    </Link>
                    {canManageDirectory ? (
                      <>
                        <button
                          className="rounded-full border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:text-ink"
                          type="button"
                          onClick={() => openClientDrawer(client, "general")}
                        >
                          Editar
                        </button>
                        <button
                          className="rounded-full border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:text-ink"
                          type="button"
                          onClick={() => setStatusTarget(client)}
                        >
                          {client.status === "active" ? "Suspender" : "Reactivar"}
                        </button>
                      </>
                    ) : null}
                    {canDeleteClients ? (
                      <button
                        className="rounded-full border border-rose-300 px-4 py-2 text-sm font-semibold text-rose-600 transition hover:bg-rose-50"
                        type="button"
                        onClick={() => setDeleteTarget(client)}
                      >
                        Eliminar
                      </button>
                    ) : null}
                  </div>
                </article>
              );
            })}
          </div>
        </SectionCard>

        <SectionCard
          title="Alta de cliente"
          subtitle="Provisiona una cuenta SaaS y, si hace falta, deja listo el primer client_admin desde el dia uno."
          action={canManageDirectory ? <span className="text-xs uppercase tracking-[0.2em] text-accent">Flujo SaaS</span> : null}
        >
          {notice ? (
            <div
              className={`mb-4 rounded-[24px] border px-4 py-4 text-sm ${
                notice.tone === "success"
                  ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                  : "border-amber-200 bg-amber-50 text-amber-900"
              }`}
            >
              <p className="font-semibold">{notice.title}</p>
              <p className="mt-2">{notice.description}</p>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <div className="rounded-2xl bg-white/90 px-4 py-3">
                  <p className="text-xs uppercase tracking-wide text-slate-500">Cliente</p>
                  <p className="mt-2 font-semibold text-ink">{notice.clientName}</p>
                </div>
                <div className="rounded-2xl bg-white/90 px-4 py-3">
                  <p className="text-xs uppercase tracking-wide text-slate-500">Ruta</p>
                  <p className="mt-2 font-semibold text-ink">/login</p>
                </div>
                {notice.adminEmail ? (
                  <div className="rounded-2xl bg-white/90 px-4 py-3">
                    <p className="text-xs uppercase tracking-wide text-slate-500">Correo administrador</p>
                    <p className="mt-2 font-semibold text-ink">{notice.adminEmail}</p>
                  </div>
                ) : null}
                {notice.temporaryPassword ? (
                  <div className="rounded-2xl bg-white/90 px-4 py-3">
                    <p className="text-xs uppercase tracking-wide text-slate-500">Clave temporal</p>
                    <p className="mt-2 font-semibold text-ink">{notice.temporaryPassword}</p>
                  </div>
                ) : null}
              </div>
              <div className="mt-4">
                <Link className="inline-flex text-sm font-semibold text-ink underline decoration-accent/40 underline-offset-4" to={notice.workspacePath}>
                  Abrir workspace del cliente
                </Link>
              </div>
            </div>
          ) : null}

          {!canManageDirectory ? (
            <div className="rounded-[24px] border border-slate-300 bg-slate-50 p-5 text-sm text-slate-600">
              Tu rol puede operar clientes existentes, pero no crear ni editar la cartera comercial.
            </div>
          ) : (
            <form className="grid gap-4 xl:grid-cols-2" onSubmit={handleSubmit}>
              <div className="md:col-span-2">
                <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre comercial</label>
                <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Slug</label>
                <input value={form.slug} onChange={(event) => setForm({ ...form, slug: event.target.value })} required />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Marca</label>
                <input value={form.brand_name} onChange={(event) => setForm({ ...form, brand_name: event.target.value })} />
              </div>
              <div className="md:col-span-2">
                <label className="mb-2 block text-sm font-semibold text-slate-700">Correo de contacto</label>
                <input type="email" value={form.contact_email} onChange={(event) => setForm({ ...form, contact_email: event.target.value })} />
              </div>

              <label className="md:col-span-2 flex items-start gap-3 rounded-[24px] border border-slate-300 bg-slate-50 px-4 py-4">
                <input
                  className="mt-1 h-5 w-5"
                  type="checkbox"
                  checked={form.self_managed}
                  onChange={(event) => setForm({ ...form, self_managed: event.target.checked })}
                />
                <span>
                  <span className="block text-sm font-semibold text-slate-800">Crear tambien el acceso principal del cliente</span>
                  <span className="mt-1 block text-sm text-slate-600">
                    Provisiona un client_admin con clave temporal para que entre directo a su workspace.
                  </span>
                </span>
              </label>

              {form.self_managed ? (
                <>
                  <div className="md:col-span-2">
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre del administrador</label>
                    <input
                      value={form.admin_full_name}
                      onChange={(event) => setForm({ ...form, admin_full_name: event.target.value })}
                      required={form.self_managed}
                    />
                  </div>
                  <div className="md:col-span-2">
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Correo del administrador</label>
                    <input
                      type="email"
                      value={form.admin_email}
                      onChange={(event) => setForm({ ...form, admin_email: event.target.value })}
                      required={form.self_managed}
                    />
                  </div>
                  <div className="md:col-span-2">
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Contrasena temporal</label>
                    <input
                      value={form.temporary_password}
                      onChange={(event) => setForm({ ...form, temporary_password: event.target.value })}
                      required={form.self_managed}
                    />
                  </div>
                </>
              ) : null}

              <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white md:col-span-2" type="submit" disabled={submitting}>
                {submitting ? "Guardando..." : form.self_managed ? "Crear cliente y acceso inicial" : "Crear cliente"}
              </button>
            </form>
          )}
        </SectionCard>
      </div>

      <EditClientDrawer
        open={Boolean(editingClient)}
        client={editingClient}
        token={token}
        canManageUsers={canManageClientUsers(user?.role)}
        canDelete={canDeleteClients}
        canUpdateClient={canManageDirectory}
        initialTab={drawerTab}
        onClose={() => setEditingClient(null)}
        onClientUpdated={(updated) => {
          setClients((current) => current.map((item) => (item.id === updated.id ? updated : item)));
          setEditingClient(updated);
        }}
        onRequestDelete={(client) => {
          setDeleteTarget(client);
        }}
      />

      <DeleteConfirmDialog
        open={Boolean(statusTarget)}
        title={statusTarget ? `${statusTarget.status === "active" ? "Suspender" : "Reactivar"} cliente ${statusTarget.name}` : "Cambiar estado"}
        description={
          statusTarget?.status === "active"
            ? "Los usuarios del cliente dejaran de iniciar sesion y /auth/me quedara bloqueado hasta reactivar la cuenta."
            : "La cuenta y sus usuarios volveran a poder iniciar sesion con normalidad."
        }
        confirmLabel={statusTarget?.status === "active" ? "Suspender cliente" : "Reactivar cliente"}
        errorMessage={error}
        isDeleting={statusUpdating}
        busyLabel="Actualizando..."
        onCancel={() => setStatusTarget(null)}
        onConfirm={() => {
          void handleToggleClientStatus();
        }}
      />

      <DeleteClientConfirmDialog
        open={Boolean(deleteTarget)}
        clientName={deleteTarget?.name ?? "Cliente"}
        errorMessage={error}
        isDeleting={deletingClient}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={(confirmText) => {
          void handleDeleteClient(confirmText);
        }}
      />
    </>
  );
}
