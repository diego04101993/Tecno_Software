import type { Campaign, ScheduleItem } from "../types/domain";

const dayLabels = [
  { value: 1, label: "Lun" },
  { value: 2, label: "Mar" },
  { value: 3, label: "Mie" },
  { value: 4, label: "Jue" },
  { value: 5, label: "Vie" },
  { value: 6, label: "Sab" },
  { value: 7, label: "Dom" },
];

function minutesFromTime(raw: string | null) {
  if (!raw) {
    return 0;
  }

  const [hours, minutes] = raw.split(":").map((value) => Number(value));
  return hours * 60 + minutes;
}

function scheduleContainsNow(schedule: ScheduleItem) {
  const now = new Date();
  const currentDay = now.getDay() === 0 ? 7 : now.getDay();
  if (schedule.days_of_week.length > 0 && !schedule.days_of_week.includes(currentDay)) {
    return false;
  }

  const currentMinutes = now.getHours() * 60 + now.getMinutes();
  const startMinutes = schedule.start_time ? minutesFromTime(schedule.start_time) : 0;
  const endMinutes = schedule.end_time ? minutesFromTime(schedule.end_time) : 24 * 60;
  return currentMinutes >= startMinutes && currentMinutes <= endMinutes;
}

export function TimelineBoard({
  schedules,
  campaigns,
  selectedCampaignId,
}: {
  schedules: ScheduleItem[];
  campaigns: Campaign[];
  selectedCampaignId: string | null;
}) {
  const filtered = selectedCampaignId ? schedules.filter((item) => item.campaign_id === selectedCampaignId) : schedules;
  const now = new Date();
  const nowLeft = `${((now.getHours() * 60 + now.getMinutes()) / (24 * 60)) * 100}%`;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-[88px_repeat(12,minmax(0,1fr))] gap-2 text-xs uppercase tracking-[0.2em] text-slate-400">
        <span />
        {Array.from({ length: 12 }, (_, index) => (
          <span key={index}>{`${index * 2}:00`}</span>
        ))}
      </div>
      {dayLabels.map((day) => (
        <div key={day.value} className="grid grid-cols-[88px_1fr] gap-4">
          <div className="rounded-2xl bg-slate-100 px-4 py-3 text-sm font-semibold text-slate-700">{day.label}</div>
          <div className="relative h-28 overflow-hidden rounded-3xl border border-slate-200 bg-white">
            <div className="absolute inset-0 grid grid-cols-12">
              {Array.from({ length: 12 }, (_, index) => (
                <div key={index} className="border-r border-dashed border-slate-200 last:border-r-0" />
              ))}
            </div>
            <div className="absolute bottom-0 top-0 w-px bg-cyan-400/80" style={{ left: nowLeft }} />
            {filtered
              .filter((item) => item.days_of_week.includes(day.value) || item.days_of_week.length === 0)
              .map((item) => {
                const start = minutesFromTime(item.start_time);
                const end = Math.max(minutesFromTime(item.end_time), start + 60);
                const left = `${(start / (24 * 60)) * 100}%`;
                const width = `${((end - start) / (24 * 60)) * 100}%`;
                const campaignName = campaigns.find((campaign) => campaign.id === item.campaign_id)?.name ?? "Campaña";
                const activeNow = scheduleContainsNow(item);

                return (
                  <div
                    key={item.id}
                    className={[
                      "absolute top-4 flex h-16 min-w-[180px] items-center rounded-2xl border px-4 shadow-sm",
                      activeNow
                        ? "border-cyan-300 bg-cyan-100/90"
                        : "border-accent/10 bg-accentSoft/70",
                    ].join(" ")}
                    style={{ left, width }}
                  >
                    <div>
                      <p className="font-semibold text-ink">{item.title}</p>
                      <p className="text-xs text-slate-600">
                        {campaignName} · {item.start_time ?? "--"} a {item.end_time ?? "--"}
                      </p>
                    </div>
                  </div>
                );
              })}
          </div>
        </div>
      ))}
    </div>
  );
}
