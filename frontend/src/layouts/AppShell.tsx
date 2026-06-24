import { LayoutGrid, Library, MonitorPlay, PanelsTopLeft, Presentation, Route, TvMinimalPlay, UserCircle2 } from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../lib/auth";


const navigation = [
  { to: "/app/dashboard", label: "Resumen", icon: LayoutGrid },
  { to: "/app/clients", label: "Clientes", icon: UserCircle2 },
  { to: "/app/branches", label: "Sucursales", icon: Route },
  { to: "/app/channels", label: "Canales", icon: MonitorPlay },
  { to: "/app/campaigns", label: "Campañas", icon: Presentation },
  { to: "/app/contents", label: "Contenido", icon: Library },
  { to: "/app/videowalls", label: "Videowall", icon: PanelsTopLeft },
  { to: "/app/kiosk", label: "Kiosko", icon: TvMinimalPlay },
];


export function AppShell() {
  const { user, logout } = useAuth();

  return (
    <div className="min-h-screen bg-mist">
      <div className="pointer-events-none fixed inset-0 bg-[radial-gradient(circle_at_top_left,_rgba(15,118,110,0.18),_transparent_28%),radial-gradient(circle_at_bottom_right,_rgba(249,115,22,0.16),_transparent_24%),linear-gradient(180deg,_rgba(255,255,255,0)_0%,_rgba(255,255,255,0.68)_100%)]" />
      <div className="relative mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <header className="mb-8 rounded-[36px] border border-white/70 bg-white/70 p-6 shadow-card backdrop-blur">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.34em] text-accent">Tecno Control Cloud</p>
              <h1 className="mt-3 font-display text-4xl text-ink">Señalización digital, kiosko y videowall en una sola consola.</h1>
              <p className="mt-3 max-w-3xl text-sm text-slate-600">
                Opera clientes, sucursales, campañas, layouts, sincronización y experiencias touch con una UI pensada para vender y escalar.
              </p>
            </div>
            <div className="flex items-center gap-3 rounded-[28px] border border-slate-200 bg-card px-4 py-3">
              <div>
                <p className="text-sm font-semibold text-ink">{user?.full_name}</p>
                <p className="text-xs uppercase tracking-wide text-slate-500">{user?.role}</p>
              </div>
              <button
                type="button"
                onClick={logout}
                className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-ink hover:text-ink"
              >
                Cerrar sesión
              </button>
            </div>
          </div>
          <nav className="mt-6 grid gap-3 md:grid-cols-4 xl:grid-cols-8">
            {navigation.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `group rounded-[24px] border px-4 py-4 transition ${
                      isActive
                        ? "border-accent bg-accent text-white shadow-lg shadow-accent/20"
                        : "border-slate-200 bg-white text-slate-700 hover:border-accent/40 hover:text-ink"
                    }`
                  }
                >
                  <Icon className="mb-3 h-5 w-5" />
                  <div className="text-sm font-semibold">{item.label}</div>
                </NavLink>
              );
            })}
          </nav>
        </header>
        <main>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

