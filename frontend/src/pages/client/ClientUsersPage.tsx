import { useEffect, useState } from "react";
import { Navigate, useParams } from "react-router-dom";

import { SectionCard } from "../../components/SectionCard";
import { ClientUsersPanel } from "../../components/clients/ClientUsersPanel";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { canManageClientUsers } from "../../lib/rbac";
import { getDefaultAppPath } from "../../lib/workspace";
import type { Client } from "../../types/domain";

export function ClientUsersPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const [client, setClient] = useState<Client | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  if (!clientId || !token) {
    return null;
  }

  if (!canManageClientUsers(user?.role)) {
    return <Navigate to={getDefaultAppPath(user)} replace />;
  }

  return (
    <SectionCard
      title="Usuarios del cliente"
      subtitle="Administra accesos del workspace actual con control de estado, roles y credenciales temporales."
    >
      {error ? <div className="mb-4 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
      <ClientUsersPanel clientId={clientId} token={token} clientStatus={client?.status ?? "active"} />
    </SectionCard>
  );
}
