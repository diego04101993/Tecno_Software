import { Database, LayoutGrid, Settings2, TvMinimalPlay, UserCircle2, Volume2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Navigate, Outlet, useLocation, useParams } from "react-router-dom";

import { WorkspaceShellFrame } from "../components/WorkspaceShellFrame";
import { apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { canManageClientUsers } from "../lib/rbac";
import {
  buildClientAudioPath,
  buildClientDataSourcesPath,
  buildClientKioskPath,
  buildClientOperationsPath,
  buildClientOverviewPath,
  buildClientUsersPath,
  canAccessClientWorkspace,
  getDefaultAppPath,
} from "../lib/workspace";
import type { Client } from "../types/domain";

export function ClientShell() {
  const { token, user, logout } = useAuth();
  const { clientId } = useParams();
  const location = useLocation();
  const [client, setClient] = useState<Client | null>(null);
  const [error, setError] = useState<string | null>(null);
  const isOperationsRoute = location.pathname.endsWith("/operations");

  useEffect(() => {
    if (!token || !clientId) {
      return;
    }

    apiRequest<Client>(`/clients/${clientId}`, { token })
      .then((response) => {
        setClient(response);
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el cliente");
      });
  }, [clientId, token]);

  const navigation = useMemo(() => {
    const items = [
      { to: buildClientOverviewPath(clientId ?? ""), label: "Dashboard", icon: LayoutGrid, end: true },
      { to: buildClientOperationsPath(clientId ?? ""), label: "Operacion", icon: Settings2 },
      { to: buildClientKioskPath(clientId ?? ""), label: "Kiosko", icon: TvMinimalPlay },
      { to: buildClientDataSourcesPath(clientId ?? ""), label: "Data Sources", icon: Database },
      { to: buildClientAudioPath(clientId ?? ""), label: "Audio", icon: Volume2 },
    ];

    if (canManageClientUsers(user?.role)) {
      items.push({ to: buildClientUsersPath(clientId ?? ""), label: "Usuarios", icon: UserCircle2 });
    }

    return items;
  }, [clientId, user?.role]);

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  if (!clientId || !canAccessClientWorkspace(user, clientId)) {
    return <Navigate to={getDefaultAppPath(user)} replace />;
  }

  return (
    <WorkspaceShellFrame
      eyebrow="Workspace Cliente"
      title={client?.name ?? "Cliente"}
      description={
        error
          ? "No se pudo cargar el contexto del cliente."
          : "Opera el cliente con dashboard, operacion, kiosko, data sources, audio y usuarios segun tu rol."
      }
      breadcrumbs={[
        { label: "Clientes", to: "/app/clients" },
        { label: client?.name ?? "Cliente" },
      ]}
      navigation={navigation}
      userName={user.full_name}
      userRole={user.role}
      onLogout={logout}
      layoutMode={isOperationsRoute ? "editor" : "default"}
    >
      <Outlet />
    </WorkspaceShellFrame>
  );
}
