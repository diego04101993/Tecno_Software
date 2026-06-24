import { LayoutGrid, Monitor, MonitorPlay, PanelsTopLeft, Presentation, Volume2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Navigate, Outlet, useParams } from "react-router-dom";

import { WorkspaceShellFrame } from "../components/WorkspaceShellFrame";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  buildBranchAudioPath,
  buildBranchChannelsPath,
  buildBranchLayoutsPath,
  buildBranchOverviewPath,
  buildBranchPreviewPath,
  buildBranchTimelinePath,
  buildClientOverviewPath,
  canAccessBranchWorkspace,
  getDefaultAppPath,
} from "../lib/workspace";
import type { Branch, Channel, Client } from "../types/domain";

export function BranchShell() {
  const { token, user, logout } = useAuth();
  const { clientId, branchId, channelId } = useParams();
  const [client, setClient] = useState<Client | null>(null);
  const [branch, setBranch] = useState<Branch | null>(null);
  const [channel, setChannel] = useState<Channel | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !clientId || !branchId) {
      return;
    }

    Promise.all([
      apiRequest<Client>(`/clients/${clientId}`, { token }),
      apiRequest<Branch[]>(`/branches?client_id=${clientId}`, { token }),
      channelId ? apiRequest<Channel[]>(`/channels?branch_id=${branchId}`, { token }) : Promise.resolve([] as Channel[]),
    ])
      .then(([clientResponse, branchesResponse, channelsResponse]) => {
        setClient(clientResponse);
        setBranch(branchesResponse.find((item) => item.id === branchId) ?? null);
        setChannel(channelsResponse.find((item) => item.id === channelId) ?? null);
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar la sucursal");
      });
  }, [branchId, channelId, clientId, token]);

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  if (!clientId || !branchId || !canAccessBranchWorkspace(user, clientId, branchId)) {
    return <Navigate to={getDefaultAppPath(user)} replace />;
  }

  const title = channel?.name ?? branch?.name ?? "Sucursal";
  const description = error
    ? "No se pudo cargar el contexto de la sucursal."
    : channel
      ? "Ficha operativa del canal con playback, heartbeat, campañas visibles, preview visual y soporte para audio."
      : "Centro de control de sucursal con monitoreo operativo, timeline, audio, preview visual y acceso directo a cada canal.";

  const breadcrumbs: Array<{ label: string; to?: string }> = [
    { label: "Clientes", to: "/app/clients" },
    { label: client?.name ?? "Cliente", to: buildClientOverviewPath(clientId) },
    { label: branch?.name ?? "Sucursal", to: buildBranchOverviewPath(clientId, branchId) },
  ];

  if (channel) {
    breadcrumbs.push({ label: channel.name });
  }

  return (
    <WorkspaceShellFrame
      eyebrow="Workspace Sucursal"
      title={title}
      description={description}
      breadcrumbs={breadcrumbs}
      navigation={[
        { to: buildBranchOverviewPath(clientId, branchId), label: "Dashboard", icon: LayoutGrid, end: true },
        { to: buildBranchChannelsPath(clientId, branchId), label: "Canales", icon: MonitorPlay },
        { to: buildBranchAudioPath(clientId, branchId), label: "Audio", icon: Volume2 },
        { to: buildBranchPreviewPath(clientId, branchId), label: "Preview", icon: Monitor },
        { to: buildBranchTimelinePath(clientId, branchId), label: "Timeline", icon: Presentation },
        { to: buildBranchLayoutsPath(clientId, branchId), label: "Layouts", icon: PanelsTopLeft },
      ]}
      userName={user.full_name}
      userRole={user.role}
      onLogout={logout}
    >
      <Outlet />
    </WorkspaceShellFrame>
  );
}
