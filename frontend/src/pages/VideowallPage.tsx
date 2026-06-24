import { FormEvent, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { SectionCard } from "../components/SectionCard";
import { VideowallMatrix } from "../components/VideowallMatrix";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { canAccessGlobalClients, canWriteClientScope } from "../lib/rbac";
import type { Channel, Client, Videowall, VideowallNode } from "../types/domain";

export function VideowallPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const [clients, setClients] = useState<Client[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [videowalls, setVideowalls] = useState<Videowall[]>([]);
  const [nodes, setNodes] = useState<VideowallNode[]>([]);
  const [selectedVideowallId, setSelectedVideowallId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wallForm, setWallForm] = useState({
    client_id: clientId ?? user?.client_id ?? "",
    name: "",
    columns: 2,
    rows: 2,
    total_width: 3840,
    total_height: 2160,
    start_tolerance_ms: 250,
    sync_mode: "play_at_timestamp",
  });
  const [nodeForm, setNodeForm] = useState({
    channel_id: "",
    position_index: 1,
    row_index: 0,
    column_index: 0,
    x: 0,
    y: 0,
    width: 1920,
    height: 1080,
  });

  const canManageVideowalls = canWriteClientScope(user?.role);

  async function loadData(nextSelectedId?: string | null) {
    if (!token) {
      return;
    }

    const scopedClientId = clientId ?? wallForm.client_id ?? user?.client_id ?? "";
    const channelPath = scopedClientId ? `/channels?client_id=${scopedClientId}` : "/channels";
    const videowallPath = scopedClientId ? `/videowalls?client_id=${scopedClientId}` : "/videowalls";

    try {
      const [clientsResponse, channelsResponse, videowallsResponse] = await Promise.all([
        apiRequest<Client[]>("/clients", { token }),
        apiRequest<Channel[]>(channelPath, { token }),
        apiRequest<Videowall[]>(videowallPath, { token }),
      ]);
      setClients(clientsResponse);
      setChannels(channelsResponse);
      setVideowalls(videowallsResponse);

      const selected = nextSelectedId ?? selectedVideowallId ?? videowallsResponse[0]?.id ?? null;
      setSelectedVideowallId(selected);

      if (selected) {
        const preview = await apiRequest<{ nodes: VideowallNode[] }>(`/videowalls/${selected}/preview`, { token });
        setNodes(preview.nodes);
      } else {
        setNodes([]);
      }
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el videowall");
    }
  }

  useEffect(() => {
    loadData();
  }, [clientId, token]);

  useEffect(() => {
    if (!selectedVideowallId || !token) {
      return;
    }
    apiRequest<{ nodes: VideowallNode[] }>(`/videowalls/${selectedVideowallId}/preview`, { token })
      .then((response) => setNodes(response.nodes))
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el preview");
      });
  }, [selectedVideowallId, token]);

  useEffect(() => {
    if (clientId || user?.client_id) {
      setWallForm((current) => ({ ...current, client_id: clientId ?? user?.client_id ?? current.client_id }));
    }
  }, [clientId, user?.client_id]);

  async function createVideowall(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !canManageVideowalls) {
      return;
    }

    try {
      const created = await apiRequest<Videowall>("/videowalls", {
        method: "POST",
        token,
        body: { ...wallForm, client_id: wallForm.client_id || user?.client_id },
      });
      setWallForm((current) => ({ ...current, name: "" }));
      loadData(created.id);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear el videowall");
    }
  }

  async function saveNode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !selectedVideowallId || !canManageVideowalls) {
      return;
    }

    try {
      await apiRequest<VideowallNode>(`/videowalls/${selectedVideowallId}/nodes`, {
        method: "POST",
        token,
        body: nodeForm,
      });
      loadData(selectedVideowallId);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo guardar el nodo");
    }
  }

  const selectedVideowall = videowalls.find((videowall) => videowall.id === selectedVideowallId) ?? null;
  const scopedChannels = canAccessGlobalClients(user?.role)
    ? channels.filter((channel) => !wallForm.client_id || channel.client_id === wallForm.client_id)
    : channels;

  return (
    <div className="space-y-6">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <div className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
        <SectionCard title="Videowalls" subtitle="La configuración permanece simple, pero ya vive dentro del cliente correcto.">
          <div className="space-y-4">
            {videowalls.map((videowall) => (
              <button
                type="button"
                key={videowall.id}
                onClick={() => setSelectedVideowallId(videowall.id)}
                className={`w-full rounded-[28px] border p-5 text-left transition ${
                  selectedVideowallId === videowall.id
                    ? "border-accent bg-accentSoft/70"
                    : "border-slate-200 bg-white hover:border-accent/30"
                }`}
              >
                <p className="font-display text-2xl text-ink">{videowall.name}</p>
                <p className="mt-2 text-sm text-slate-600">
                  {videowall.columns}x{videowall.rows} · {videowall.total_width}x{videowall.total_height}px
                </p>
              </button>
            ))}
          </div>
        </SectionCard>

        <SectionCard title="Crear videowall" subtitle="La asignación queda contextual al cliente aunque la funcionalidad avanzada siga intacta.">
          {!canManageVideowalls ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-5 text-sm text-slate-600">
              Este rol solo puede consultar configuraciones de videowall.
            </div>
          ) : (
            <form className="grid gap-4 md:grid-cols-2" onSubmit={createVideowall}>
              {canAccessGlobalClients(user?.role) && !clientId ? (
                <div className="md:col-span-2">
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Cliente</label>
                  <select value={wallForm.client_id} onChange={(event) => setWallForm({ ...wallForm, client_id: event.target.value })} required>
                    <option value="">Selecciona un cliente</option>
                    {clients.map((client) => (
                      <option key={client.id} value={client.id}>
                        {client.name}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}
              <div className="md:col-span-2">
                <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
                <input value={wallForm.name} onChange={(event) => setWallForm({ ...wallForm, name: event.target.value })} required />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Columnas</label>
                <input type="number" value={wallForm.columns} onChange={(event) => setWallForm({ ...wallForm, columns: Number(event.target.value) })} />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Filas</label>
                <input type="number" value={wallForm.rows} onChange={(event) => setWallForm({ ...wallForm, rows: Number(event.target.value) })} />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Resolución total de ancho</label>
                <input
                  type="number"
                  value={wallForm.total_width}
                  onChange={(event) => setWallForm({ ...wallForm, total_width: Number(event.target.value) })}
                />
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Resolución total de alto</label>
                <input
                  type="number"
                  value={wallForm.total_height}
                  onChange={(event) => setWallForm({ ...wallForm, total_height: Number(event.target.value) })}
                />
              </div>
              <div className="md:col-span-2">
                <label className="mb-2 block text-sm font-semibold text-slate-700">Tolerancia ms</label>
                <input
                  type="number"
                  value={wallForm.start_tolerance_ms}
                  onChange={(event) => setWallForm({ ...wallForm, start_tolerance_ms: Number(event.target.value) })}
                />
              </div>
              <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white md:col-span-2" type="submit">
                Guardar videowall
              </button>
            </form>
          )}
        </SectionCard>
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.1fr_0.9fr]">
        <VideowallMatrix videowall={selectedVideowall} nodes={nodes} channels={channels} />

        <SectionCard title="Asignar nodo" subtitle="La asignación sigue igual, pero ya filtrada al cliente activo.">
          {!canManageVideowalls ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-5 text-sm text-slate-600">
              Este rol puede revisar la matriz, pero no reasignar nodos.
            </div>
          ) : (
            <form className="space-y-4" onSubmit={saveNode}>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Canal</label>
                <select value={nodeForm.channel_id} onChange={(event) => setNodeForm({ ...nodeForm, channel_id: event.target.value })} required>
                  <option value="">Selecciona un canal</option>
                  {scopedChannels.map((channel) => (
                    <option key={channel.id} value={channel.id}>
                      {channel.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid gap-4 md:grid-cols-3">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Posición</label>
                  <input
                    type="number"
                    value={nodeForm.position_index}
                    onChange={(event) => setNodeForm({ ...nodeForm, position_index: Number(event.target.value) })}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Fila</label>
                  <input type="number" value={nodeForm.row_index} onChange={(event) => setNodeForm({ ...nodeForm, row_index: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Columna</label>
                  <input
                    type="number"
                    value={nodeForm.column_index}
                    onChange={(event) => setNodeForm({ ...nodeForm, column_index: Number(event.target.value) })}
                  />
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">X</label>
                  <input type="number" value={nodeForm.x} onChange={(event) => setNodeForm({ ...nodeForm, x: Number(event.target.value) })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Y</label>
                  <input type="number" value={nodeForm.y} onChange={(event) => setNodeForm({ ...nodeForm, y: Number(event.target.value) })} />
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Ancho</label>
                  <input
                    type="number"
                    value={nodeForm.width}
                    onChange={(event) => setNodeForm({ ...nodeForm, width: Number(event.target.value) })}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Alto</label>
                  <input
                    type="number"
                    value={nodeForm.height}
                    onChange={(event) => setNodeForm({ ...nodeForm, height: Number(event.target.value) })}
                  />
                </div>
              </div>
              <button className="w-full rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" type="submit" disabled={!selectedVideowallId}>
                Guardar nodo
              </button>
            </form>
          )}
        </SectionCard>
      </div>
    </div>
  );
}
