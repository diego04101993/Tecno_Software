import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { SectionCard } from "../components/SectionCard";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatClientStatus } from "../lib/labels";
import { canAccessGlobalClients, canWriteClientScope } from "../lib/rbac";
import { buildBranchOverviewPath } from "../lib/workspace";
import type { Branch, Client } from "../types/domain";

export function BranchesPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const [branches, setBranches] = useState<Branch[]>([]);
  const [clients, setClients] = useState<Client[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    client_id: clientId ?? user?.client_id ?? "",
    name: "",
    code: "",
    address: "",
    timezone: "America/Mexico_City",
  });

  function loadData() {
    if (!token) {
      return;
    }

    const branchPath = clientId ? `/branches?client_id=${clientId}` : "/branches";
    Promise.all([apiRequest<Branch[]>(branchPath, { token }), apiRequest<Client[]>("/clients", { token })])
      .then(([branchesResponse, clientsResponse]) => {
        setBranches(branchesResponse);
        setClients(clientsResponse);
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar las sucursales");
      });
  }

  useEffect(() => {
    loadData();
  }, [clientId, token]);

  useEffect(() => {
    if (clientId || user?.client_id) {
      setForm((current) => ({ ...current, client_id: clientId ?? user?.client_id ?? current.client_id }));
    }
  }, [clientId, user?.client_id]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !canWriteClientScope(user?.role)) {
      return;
    }

    try {
      await apiRequest<Branch>("/branches", { method: "POST", token, body: form });
      setForm((current) => ({ ...current, name: "", code: "", address: "" }));
      loadData();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear la sucursal");
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[1fr_1fr]">
      <SectionCard title="Sucursales" subtitle="El segundo nivel del SaaS ahora vive dentro del workspace del cliente, no como un módulo global mezclado.">
        <div className="space-y-4">
          {branches.map((branch) => {
            const client = clients.find((item) => item.id === branch.client_id);
            return (
              <article key={branch.id} className="rounded-[28px] border border-slate-200 bg-white p-5">
                <p className="font-display text-2xl text-ink">{branch.name}</p>
                <p className="mt-1 text-sm text-slate-500">
                  {branch.code} · {client?.name ?? "Cliente"} · {branch.timezone}
                </p>
                <p className="mt-4 text-sm text-slate-700">{branch.address ?? "Dirección pendiente"}</p>
                <div className="mt-4 flex flex-wrap gap-3 text-xs uppercase tracking-wide text-slate-400">
                  <span>{client ? formatClientStatus(client.status) : "Cliente activo"}</span>
                  <span>Workspace contextual</span>
                </div>
                {clientId ? (
                  <div className="mt-5">
                    <Link
                      className="inline-flex rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
                      to={buildBranchOverviewPath(clientId, branch.id)}
                    >
                      Abrir sucursal
                    </Link>
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      </SectionCard>

      <SectionCard title="Crear sucursal" subtitle="Alta rápida para entrar al flujo cliente → sucursal → canal.">
        {error ? <div className="mb-4 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
        <form className="grid gap-4 md:grid-cols-2" onSubmit={handleSubmit}>
          {canAccessGlobalClients(user?.role) && !clientId ? (
            <div className="md:col-span-2">
              <label className="mb-2 block text-sm font-semibold text-slate-700">Cliente</label>
              <select value={form.client_id} onChange={(event) => setForm({ ...form, client_id: event.target.value })} required>
                <option value="">Selecciona un cliente</option>
                {clients.map((client) => (
                  <option key={client.id} value={client.id}>
                    {client.name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
            <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required />
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Código</label>
            <input value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} required />
          </div>
          <div className="md:col-span-2">
            <label className="mb-2 block text-sm font-semibold text-slate-700">Dirección</label>
            <input value={form.address} onChange={(event) => setForm({ ...form, address: event.target.value })} />
          </div>
          <div className="md:col-span-2">
            <label className="mb-2 block text-sm font-semibold text-slate-700">Zona horaria</label>
            <input value={form.timezone} onChange={(event) => setForm({ ...form, timezone: event.target.value })} />
          </div>
          {canWriteClientScope(user?.role) ? (
            <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white md:col-span-2" type="submit">
              Registrar sucursal
            </button>
          ) : (
            <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600 md:col-span-2">
              Este rol solo puede consultar sucursales dentro de su alcance.
            </div>
          )}
        </form>
      </SectionCard>
    </div>
  );
}
