import type { LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";

type WorkspaceQuickLink = {
  label: string;
  caption: string;
  to: string;
  icon: LucideIcon;
};

export function WorkspaceQuickLinks({
  title,
  subtitle,
  links,
}: {
  title: string;
  subtitle: string;
  links: WorkspaceQuickLink[];
}) {
  return (
    <div className="rounded-[28px] border border-slate-200 bg-white p-5">
      <div className="border-b border-slate-200 pb-4">
        <h3 className="font-display text-2xl text-ink">{title}</h3>
        <p className="mt-1 text-sm text-slate-600">{subtitle}</p>
      </div>
      <div className="mt-4 grid gap-3">
        {links.map((link) => {
          const Icon = link.icon;
          return (
            <Link
              key={link.to}
              to={link.to}
              className="group flex items-start gap-4 rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 transition hover:border-accent/40 hover:bg-accentSoft/30"
            >
              <span className="rounded-2xl bg-white p-3 text-accent shadow-sm">
                <Icon className="h-5 w-5" />
              </span>
              <span className="min-w-0">
                <span className="block font-semibold text-ink group-hover:text-slate-900">{link.label}</span>
                <span className="mt-1 block text-sm text-slate-600">{link.caption}</span>
              </span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
