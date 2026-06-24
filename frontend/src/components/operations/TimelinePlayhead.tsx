type TimelinePlayheadProps = {
  leftPx: number;
  heightPx?: number;
};

export function TimelinePlayhead({ leftPx, heightPx = 188 }: TimelinePlayheadProps) {
  return (
    <div className="pointer-events-none absolute top-0 z-20" style={{ left: `${leftPx}px`, height: `${heightPx}px` }}>
      <div className="-translate-x-1/2">
        <div className="mx-auto h-3 w-3 rounded-full border-2 border-white bg-rose-500 shadow-lg" />
        <div className="mx-auto mt-1 w-[2px] rounded-full bg-rose-500 shadow-[0_0_0_1px_rgba(255,255,255,0.2)]" style={{ height: `${Math.max(40, heightPx - 16)}px` }} />
      </div>
    </div>
  );
}
