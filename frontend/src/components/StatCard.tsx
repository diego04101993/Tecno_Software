type StatCardProps = {
  label: string;
  value: string;
  hint: string;
  tone?: "teal" | "orange" | "slate";
};


const toneStyles = {
  teal: "from-accentSoft via-white to-white border-accent/20",
  orange: "from-emberSoft via-white to-white border-ember/20",
  slate: "from-slate-100 via-white to-white border-slate-200",
};


export function StatCard({ label, value, hint, tone = "slate" }: StatCardProps) {
  return (
    <div className={`rounded-[28px] border bg-gradient-to-br p-6 shadow-card ${toneStyles[tone]} animate-floatIn`}>
      <p className="text-xs uppercase tracking-[0.24em] text-slate-500">{label}</p>
      <p className="mt-4 font-display text-4xl text-ink">{value}</p>
      <p className="mt-3 text-sm text-slate-600">{hint}</p>
    </div>
  );
}

