import { X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import { formatClientStatus } from "../../lib/labels";
import { ClientUsersPanel } from "./ClientUsersPanel";
import type { Client } from "../../types/domain";

type DrawerTab = "general" | "users" | "status";

type EditClientDrawerProps = {
  open: boolean;
  client: Client | null;
  token: string | null;
  canManageUsers: boolean;
  canDelete: boolean;
  canUpdateClient: boolean;
  initialTab?: DrawerTab;
  onClose: () => void;
  onClientUpdated: (client: Client) => void;
  onRequestDelete: (client: Client) => void;
};

type GeneralFormState = {
  name: string;
  slug: string;
  brand_name: string;
  contact_email: string;
};

function buildForm(client: Client | null): GeneralFormState {
  return {
    name: client?.name ?? "",
    slug: client?.slug ?? "",
    brand_name: client?.brand_name ?? "",
    contact_email: client?.contact_email ?? "",
  };
}

export function EditClientDrawer({
  open,
  client,
  token,
  canManageUsers,
  canDelete,
  canUpdateClient,
  initialTab = "general",
  onClose,
  onClientUpdated,
  onRequestDelete,
}: EditClientDrawerProps) {
  const [activeTab, setActiveTab] = useState<DrawerTab>(initialTab);
  const [generalForm, setGeneralForm] = useState<GeneralFormState>(() => buildForm(client));
  const [savingGeneral, setSavingGeneral] = useState(false);
  const [savingStatus, setSavingStatus] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    setActiveTab(initialTab);
  }, [initialTab, open]);

  useEffect(() => {
    setGeneralForm(buildForm(client));
    setError(null);
    setNotice(null);
  }, [client]);

  const tabOptions = useMemo(() => {
    const tabs: Array<{ value: DrawerTab; label: string }> = [
      { value: "general", label: "Informacion general" },
      { value: "status", label: "Estado" },
    ];
    if (canManageUsers) {
      tabs.splice(1, 0, { value: "users", label: "Usuarios del cliente" });
    }
    return tabs;
  }, [canManageUsers]);

  if (!open || !client) {
    return null;
  }

  const clientId = client.id;
  const clientStatus = client.status;

  async function handleSaveGeneral() {
    if (!token || !canUpdateClient) {
      return;
    }
    setSavingGeneral(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await apiRequest<Client>(`/clients/${clientId}`, {
        method: "PATCH",
        token,
        body: {
          name: generalForm.name,
          slug: generalForm.slug,
          brand_name: generalForm.brand_name || null,
          contact_email: generalForm.contact_email || null,
        },
      });
      onClientUpdated(updated);
      setNotice("Cliente actualizado");
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo actualizar el cliente");
    } finally {
      setSavingGeneral(false);
    }
  }

  async function handleToggleStatus() {
    if (!token || !canUpdateClient) {
      return;
    }
    setSavingStatus(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await apiRequest<Client>(`/clients/${clientId}/status`, {
        method: "PATCH",
        token,
        body: {
          status: clientStatus === "active" ? "suspended" : "active",
        },
      });
      onClientUpdated(updated);
      setNotice(updated.status === "suspended" ? "Cliente suspendido" : "Cliente reactivado");
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo actualizar el estado del cliente");
    } finally {
      setSavingStatus(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[90] bg-slate-950/40 backdrop-blur-sm">
      <div className="absolute inset-y-0 right-0 flex w-full max-w-5xl flex-col border-l border-slate-300 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-300 px-6 py-5">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.28em] text-accent">Gestion SaaS</p>
            <h3 className="mt-2 truncate font-display text-3xl text-ink">{client.name}</h3>
            <p className="mt-2 text-sm text-slate-600">
              Ajusta datos generales, usuarios del cliente y estado de la cuenta sin salir de la cartera global.
            </p>
          </div>
          <button
            className="rounded-full border border-slate-300 p-2 text-slate-600 transition hover:border-slate-400 hover:text-ink"
            type="button"
            onClick={onClose}
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex flex-wrap gap-2 border-b border-slate-300 px-6 py-4">
          {tabOptions.map((tab) => (
            <button
              key={tab.value}
              className={`rounded-full border px-4 py-2 text-sm font-semibold transition ${
                activeTab === tab.value
                  ? "border-ink bg-ink text-white"
                  : "border-slate-300 bg-white text-slate-700 hover:border-slate-400 hover:text-ink"
              }`}
              type="button"
              onClick={() => setActiveTab(tab.value)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-6">
          {error ? <div className="mb-4 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
          {notice ? <div className="mb-4 rounded-2xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{notice}</div> : null}

          {activeTab === "general" ? (
            <div className="grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
              <section className="rounded-[24px] border border-slate-300 bg-white p-5">
                <h4 className="text-base font-semibold text-ink">Informacion general</h4>
                <div className="mt-5 grid gap-4">
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre comercial</label>
                    <input value={generalForm.name} onChange={(event) => setGeneralForm({ ...generalForm, name: event.target.value })} />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Slug</label>
                    <input value={generalForm.slug} onChange={(event) => setGeneralForm({ ...generalForm, slug: event.target.value })} />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Marca</label>
                    <input
                      value={generalForm.brand_name}
                      onChange={(event) => setGeneralForm({ ...generalForm, brand_name: event.target.value })}
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Correo de contacto</label>
                    <input
                      type="email"
                      value={generalForm.contact_email}
                      onChange={(event) => setGeneralForm({ ...generalForm, contact_email: event.target.value })}
                    />
                  </div>
                  <div className="pt-2">
                    <button
                      className="rounded-full bg-ink px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
                      disabled={!canUpdateClient || savingGeneral}
                      type="button"
                      onClick={() => void handleSaveGeneral()}
                    >
                      {savingGeneral ? "Guardando..." : "Guardar informacion"}
                    </button>
                  </div>
                </div>
              </section>

              <section className="rounded-[24px] border border-slate-300 bg-slate-50/80 p-5">
                <h4 className="text-base font-semibold text-ink">Resumen rapido</h4>
                <div className="mt-5 grid gap-3">
                  <div className="rounded-2xl border border-slate-300 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-wide text-slate-500">Estado</p>
                    <p className="mt-2 font-semibold text-ink">{formatClientStatus(client.status)}</p>
                  </div>
                  <div className="rounded-2xl border border-slate-300 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-wide text-slate-500">Slug</p>
                    <p className="mt-2 font-semibold text-ink">{client.slug}</p>
                  </div>
                  <div className="rounded-2xl border border-slate-300 bg-white px-4 py-3">
                    <p className="text-xs uppercase tracking-wide text-slate-500">Contacto</p>
                    <p className="mt-2 font-semibold text-ink">{client.contact_email || "Sin correo definido"}</p>
                  </div>
                </div>
              </section>
            </div>
          ) : null}

          {activeTab === "users" && canManageUsers ? <ClientUsersPanel clientId={client.id} token={token ?? ""} clientStatus={client.status} /> : null}

          {activeTab === "status" ? (
            <div className="grid gap-5 xl:grid-cols-[0.9fr_1.1fr]">
              <section className="rounded-[24px] border border-slate-300 bg-white p-5">
                <h4 className="text-base font-semibold text-ink">Estado del cliente</h4>
                <div className="mt-5 rounded-[24px] border border-slate-300 bg-slate-50 p-5">
                  <p className="text-xs uppercase tracking-wide text-slate-500">Estado actual</p>
                  <p className="mt-3 text-2xl font-semibold text-ink">{formatClientStatus(client.status)}</p>
                  <p className="mt-3 text-sm leading-6 text-slate-600">
                    Si el cliente queda suspendido, sus usuarios ya no podran iniciar sesion ni refrescar /auth/me hasta que se reactive.
                  </p>
                  <div className="mt-5">
                    <button
                      className={`rounded-full px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60 ${
                        client.status === "active" ? "bg-amber-500 hover:bg-amber-600" : "bg-emerald-600 hover:bg-emerald-700"
                      }`}
                      disabled={!canUpdateClient || savingStatus}
                      type="button"
                      onClick={() => void handleToggleStatus()}
                    >
                      {savingStatus ? "Actualizando..." : client.status === "active" ? "Suspender cliente" : "Reactivar cliente"}
                    </button>
                  </div>
                </div>
              </section>

              <section className="rounded-[24px] border border-rose-200 bg-rose-50/70 p-5">
                <h4 className="text-base font-semibold text-ink">Zona destructiva</h4>
                <p className="mt-3 text-sm leading-6 text-slate-700">
                  El borrado total elimina usuarios, sucursales, pantallas, campañas, contenidos, carpetas, videowalls y media del cliente.
                </p>
                <div className="mt-5">
                  <button
                    className="rounded-full border border-rose-300 bg-white px-4 py-2.5 text-sm font-semibold text-rose-600 disabled:opacity-60"
                    disabled={!canDelete}
                    type="button"
                    onClick={() => onRequestDelete(client)}
                  >
                    Eliminar cliente completo
                  </button>
                </div>
                {!canDelete ? (
                  <p className="mt-3 text-xs uppercase tracking-wide text-slate-500">Solo super_admin puede borrar clientes</p>
                ) : null}
              </section>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
