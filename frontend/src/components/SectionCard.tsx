import type { ReactNode } from "react";


type SectionCardProps = {
  title: string;
  subtitle: string;
  action?: ReactNode;
  children: ReactNode;
};


export function SectionCard({ title, subtitle, action, children }: SectionCardProps) {
  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="flex flex-col gap-3 border-b border-slate-200 pb-5 md:flex-row md:items-center md:justify-between">
        <div>
          <h2 className="font-display text-2xl text-ink">{title}</h2>
          <p className="mt-1 text-sm text-slate-600">{subtitle}</p>
        </div>
        {action}
      </div>
      <div className="mt-6">{children}</div>
    </section>
  );
}

