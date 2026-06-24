import { Activity, LayoutGrid, MonitorPlay, RadioTower, Store, TvMinimalPlay } from "lucide-react";
import type { ReactNode } from "react";

type ExecutiveMetricTone = "neutral" | "emerald" | "rose" | "cyan" | "amber";

type ExecutiveMetric = {
  label: string;
  value: number | string;
  icon: "branches" | "screens" | "online" | "offline" | "videowalls" | "kiosks";
  tone?: ExecutiveMetricTone;
};

const iconMap = {
  branches: LayoutGrid,
  screens: MonitorPlay,
  online: RadioTower,
  offline: Activity,
  videowalls: TvMinimalPlay,
  kiosks: Store,
} as const;

const toneStyles: Record<ExecutiveMetricTone, string> = {
  neutral: "border-slate-200 bg-white text-slate-700",
  emerald: "border-emerald-200 bg-emerald-50 text-emerald-800",
  rose: "border-rose-200 bg-rose-50 text-rose-800",
  cyan: "border-cyan-200 bg-cyan-50 text-cyan-800",
  amber: "border-amber-200 bg-amber-50 text-amber-900",
};

export function DashboardExecutiveBar({
  title,
  subtitle,
  metrics,
  action,
}: {
  title: string;
  subtitle: string;
  metrics: ExecutiveMetric[];
  action?: ReactNode;
}) {
  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-5 shadow-card backdrop-blur">
      <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="text-xs uppercase tracking-[0.28em] text-accent">Dashboard V2</p>
          <h2 className="mt-3 truncate font-display text-4xl text-ink">{title}</h2>
          <p className="mt-3 max-w-3xl text-sm text-slate-600">{subtitle}</p>
        </div>
        {action ? <div className="flex flex-wrap gap-3">{action}</div> : null}
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-3 xl:grid-cols-6">
        {metrics.map((metric) => {
          const Icon = iconMap[metric.icon];
          const tone = metric.tone ?? "neutral";

          return (
            <article key={metric.label} className={`rounded-[24px] border px-4 py-4 ${toneStyles[tone]}`}>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-xs uppercase tracking-[0.24em] opacity-80">{metric.label}</p>
                  <p className="mt-3 font-display text-3xl text-ink">{metric.value}</p>
                </div>
                <span className="rounded-2xl bg-white/70 p-3 shadow-sm">
                  <Icon className="h-5 w-5" />
                </span>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
