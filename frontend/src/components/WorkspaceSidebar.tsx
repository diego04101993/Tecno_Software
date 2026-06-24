import type { LucideIcon } from "lucide-react";
import { NavLink } from "react-router-dom";

export type WorkspaceNavItem = {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
};

export function WorkspaceSidebar({
  items,
  title,
}: {
  items: WorkspaceNavItem[];
  title: string;
}) {
  return (
    <aside className="self-start rounded-[32px] border border-white/70 bg-card/90 p-5 shadow-card backdrop-blur lg:sticky lg:top-5">
      <p className="text-xs uppercase tracking-[0.28em] text-slate-400">{title}</p>
      <nav className="mt-5 space-y-3">
        {items.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              end={item.end}
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-[22px] border px-4 py-4 text-sm font-semibold transition ${
                  isActive
                    ? "border-accent bg-accent text-white shadow-lg shadow-accent/20"
                    : "border-slate-200 bg-white text-slate-700 hover:border-accent/40 hover:text-ink"
                }`
              }
            >
              <Icon className="h-5 w-5" />
              <span>{item.label}</span>
            </NavLink>
          );
        })}
      </nav>
    </aside>
  );
}
