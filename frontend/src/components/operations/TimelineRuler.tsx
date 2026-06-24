type TimelineRulerProps = {
  totalDurationSeconds: number;
  timelineWidthPx: number;
  resolvePixelForSeconds: (seconds: number) => number;
  resolveSecondsForPixel: (pixels: number) => number;
  onSeek: (seconds: number) => void;
};

export function TimelineRuler({
  totalDurationSeconds,
  timelineWidthPx,
  resolvePixelForSeconds,
  resolveSecondsForPixel,
  onSeek,
}: TimelineRulerProps) {
  const roundedDuration = Math.max(1, Math.ceil(totalDurationSeconds));
  const majorStep = roundedDuration <= 30 ? 5 : roundedDuration <= 90 ? 10 : 15;
  const marks = Array.from({ length: Math.floor(roundedDuration / majorStep) + 1 }, (_, index) => index * majorStep);
  if (marks[marks.length - 1] !== roundedDuration) {
    marks.push(roundedDuration);
  }

  return (
    <div
      className="relative h-11 cursor-pointer border-b border-white/10 bg-slate-950/95"
      style={{ width: `${Math.max(timelineWidthPx, 320)}px` }}
      onClick={(event) => {
        const bounds = event.currentTarget.getBoundingClientRect();
        const pixelOffset = Math.max(0, Math.min(bounds.width, event.clientX - bounds.left));
        onSeek(resolveSecondsForPixel(pixelOffset));
      }}
    >
      {marks.map((second) => (
        <div key={second} className="absolute inset-y-0" style={{ left: `${resolvePixelForSeconds(second)}px` }}>
          <div className="h-3 w-px bg-white/35" />
          <div className="mt-1.5 -translate-x-1/2 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-300">{second}s</div>
        </div>
      ))}
    </div>
  );
}
