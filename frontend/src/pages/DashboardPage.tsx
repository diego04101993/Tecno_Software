import { useEffect, useState } from "react";

import { SectionCard } from "../components/SectionCard";
import { StatCard } from "../components/StatCard";
import { StatusBadge } from "../components/StatusBadge";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatChannelHeartbeatState, formatChannelMode } from "../lib/labels";
import { canAccessGlobalClients } from "../lib/rbac";
import type { Branch, Campaign, Channel, Client, ContentItem } from "../types/domain";

function getPresenceStatus(channel: Channel): Channel["status"] {
  return channel.is_online ? "online" : "offline";
}

export function DashboardPage() {
  const { token, user } = useAuth();
  const [clients, setClients] = useState<Client[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [contents, setContents] = useState<ContentItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      return;
    }

    Promise.all([
      apiRequest<Client[]>("/clients", { token }),
      apiRequest<Branch[]>("/branches", { token }),
      apiRequest<Channel[]>("/channels", { token }),
      apiRequest<Campaign[]>("/campaigns", { token }),
      apiRequest<ContentItem[]>("/contents", { token }),
    ])
      .then(([clientsResponse, branchesResponse, channelsResponse, campaignsResponse, contentsResponse]) => {
        setClients(clientsResponse);
        setBranches(branchesResponse);
        setChannels(channelsResponse);
        setCampaigns(campaignsResponse);
        setContents(contentsResponse);
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el dashboard");
      });
  }, [token]);

  const onlineChannels = channels.filter((channel) => channel.is_online);
  const clientName = canAccessGlobalClients(user?.role) ? "Operación multicliente" : clients[0]?.name ?? "Tu cuenta";

  return (
    <div className="space-y-6">
      <section className="grid gap-4 lg:grid-cols-4">
        <StatCard label="Clientes" value={String(clients.length)} hint={clientName} tone="teal" />
        <StatCard label="Sucursales" value={String(branches.length)} hint="Organización por ubicación" />
        <StatCard label="Canales" value={String(channels.length)} hint={`${onlineChannels.length} conectados`} tone="orange" />
        <StatCard label="Campañas" value={String(campaigns.length)} hint={`${contents.length} contenidos activos`} />
      </section>

      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <div className="grid gap-6 xl:grid-cols-[1.2fr_0.8fr]">
        <SectionCard title="Estado de canales" subtitle="Último playback, modo operativo y estado real según heartbeat reciente.">
          <div className="grid gap-4 md:grid-cols-2">
            {channels.map((channel) => {
              const heartbeat = formatChannelHeartbeatState(channel);

              return (
                <article key={channel.id} className="rounded-[26px] border border-slate-200 bg-white p-5">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-semibold text-ink">{channel.name}</p>
                      <p className="mt-1 text-sm text-slate-500">
                        {channel.resolution_width}x{channel.resolution_height} · {formatChannelMode(channel.mode)}
                      </p>
                    </div>
                    <StatusBadge status={getPresenceStatus(channel)} />
                  </div>
                  <p className="mt-4 text-sm text-slate-700">
                    Reproducción actual: {channel.current_playback ?? "Esperando heartbeat"}
                  </p>
                  <p className="mt-2 text-xs uppercase tracking-wide text-slate-400">
                    Última comunicación: {heartbeat.label}
                  </p>
                </article>
              );
            })}
          </div>
        </SectionCard>

        <SectionCard title="Prioridades del CTO" subtitle="Puntos clave que ya quedan preparados en esta base para crecer sin rehacer arquitectura.">
          <div className="space-y-4">
            {[
              "Aislamiento multi-tenant por cliente con JWT y scope por entidad.",
              "Canales preparados para modo normal, expandido y videowall con heartbeat base.",
              "Timeline por campañas y horarios con layouts reutilizables.",
              "Modelo de kiosko touch con hotspots, attract mode y navegación futura.",
            ].map((item) => (
              <div key={item} className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                {item}
              </div>
            ))}
          </div>
        </SectionCard>
      </div>
    </div>
  );
}
