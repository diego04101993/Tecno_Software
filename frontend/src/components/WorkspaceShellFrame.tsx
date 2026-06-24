import type { ReactNode } from "react";

import { formatUserRole } from "../lib/labels";
import { Breadcrumbs, type BreadcrumbItem } from "./Breadcrumbs";
import { WorkspaceSidebar, type WorkspaceNavItem } from "./WorkspaceSidebar";

type WorkspaceShellFrameProps = {
  eyebrow: string;
  title: string;
  description: string;
  breadcrumbs: BreadcrumbItem[];
  navigation: WorkspaceNavItem[];
  userName: string;
  userRole: string;
  onLogout: () => void;
  children: ReactNode;
  layoutMode?: "default" | "editor";
};

export function WorkspaceShellFrame({
  eyebrow,
  title,
  description,
  breadcrumbs,
  navigation,
  userName,
  userRole,
  onLogout,
  children,
  layoutMode = "default",
}: WorkspaceShellFrameProps) {
  const isEditorMode = layoutMode === "editor";

  return (
    <div className={["min-h-screen bg-mist", isEditorMode ? "overflow-x-hidden" : ""].join(" ")}>
      <div className="pointer-events-none fixed inset-0 bg-[radial-gradient(circle_at_top_left,_rgba(15,118,110,0.18),_transparent_28%),radial-gradient(circle_at_bottom_right,_rgba(249,115,22,0.16),_transparent_24%),linear-gradient(180deg,_rgba(255,255,255,0)_0%,_rgba(255,255,255,0.68)_100%)]" />
      <div
        className={[
          "relative w-full max-w-none py-5",
          isEditorMode ? "px-4 sm:px-5 xl:px-6 2xl:px-8" : "px-4 sm:px-5 xl:px-6 2xl:px-8",
        ].join(" ")}
      >
        <header
          className={[
            "rounded-[36px] border border-white/70 bg-white/70 shadow-card backdrop-blur",
            isEditorMode ? "mb-4 p-4 xl:p-5" : "mb-6 p-6",
          ].join(" ")}
        >
          <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.34em] text-accent">{eyebrow}</p>
              <div className="mt-3">
                <Breadcrumbs items={breadcrumbs} />
              </div>
              <h1 className={["mt-4 font-display text-ink", isEditorMode ? "text-3xl xl:text-[2.2rem]" : "text-4xl"].join(" ")}>
                {title}
              </h1>
              <p className={["mt-3 text-sm text-slate-600", isEditorMode ? "max-w-5xl" : "max-w-3xl"].join(" ")}>{description}</p>
            </div>
            <div className="flex items-center gap-3 rounded-[28px] border border-slate-200 bg-card px-4 py-3">
              <div>
                <p className="text-sm font-semibold text-ink">{userName}</p>
                <p className="text-xs uppercase tracking-wide text-slate-500">{formatUserRole(userRole)}</p>
              </div>
              <button
                type="button"
                onClick={onLogout}
                className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-ink hover:text-ink"
              >
                Cerrar sesión
              </button>
            </div>
          </div>
        </header>
        <div
          className={[
            "grid min-w-0",
            isEditorMode ? "gap-4 xl:grid-cols-[230px_minmax(0,1fr)]" : "gap-5 xl:grid-cols-[240px_minmax(0,1fr)]",
          ].join(" ")}
        >
          <WorkspaceSidebar items={navigation} title="Workspace" />
          <main className={isEditorMode ? "min-w-0 overflow-x-hidden" : "min-w-0"}>{children}</main>
        </div>
      </div>
    </div>
  );
}
