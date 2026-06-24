import type { LucideIcon } from "lucide-react";
import { ShieldCheck, UserCircle2 } from "lucide-react";
import { Navigate, Outlet, useLocation } from "react-router-dom";

import { WorkspaceShellFrame } from "../components/WorkspaceShellFrame";
import { useAuth } from "../lib/auth";
import { canAccessGlobalClients, canManageInternalTeam } from "../lib/rbac";
import { getDefaultAppPath } from "../lib/workspace";

export function GlobalShell() {
  const { user, logout } = useAuth();
  const location = useLocation();

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  if (!canAccessGlobalClients(user.role)) {
    return <Navigate to={getDefaultAppPath(user)} replace />;
  }

  const isTeamRoute = location.pathname.startsWith("/app/team");
  const navigation: Array<{ to: string; label: string; icon: LucideIcon; end?: boolean }> = [
    {
      to: "/app/clients",
      label: "Clientes",
      icon: UserCircle2,
      end: true,
    },
  ];

  if (canManageInternalTeam(user.role)) {
    navigation.push({
      to: "/app/team/users",
      label: "Equipo interno",
      icon: ShieldCheck,
    });
  }

  return (
    <WorkspaceShellFrame
      eyebrow="Tecno Control Cloud"
      title={isTeamRoute ? "Equipo interno" : "Clientes"}
      description={
        isTeamRoute
          ? "Administra roles globales, suspensiones y la seguridad del ultimo super admin."
          : "Explora tu cartera completa de clientes SaaS y entra a cada operacion con su propio contexto."
      }
      breadcrumbs={isTeamRoute ? [{ label: "Clientes", to: "/app/clients" }, { label: "Equipo interno" }] : [{ label: "Clientes" }]}
      navigation={navigation}
      userName={user.full_name}
      userRole={user.role}
      onLogout={logout}
    >
      <Outlet />
    </WorkspaceShellFrame>
  );
}
