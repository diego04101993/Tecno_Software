import { Clapperboard } from "lucide-react";

import type { PreviewSequenceEntry } from "../../lib/preview";
import type { Campaign } from "../../types/domain";
import { OperationTimelineTrack } from "./OperationTimelineTrack";

type OperationSequenceTimelineProps = {
  campaign: Campaign | null;
  entries: PreviewSequenceEntry[];
  selectedItemId: string | null;
  activeItemId: string | null;
  playbackMode: "sequential" | "random";
  currentLoopTimeSeconds: number;
  canEditPlaylist: boolean;
  onSelectItem: (sequenceItemId: string) => void;
  onSelectAndSeekItem: (sequenceItemId: string, startSeconds: number) => void;
  onMoveItem: (sequenceItemId: string, direction: "left" | "right") => void;
  onDuplicateItem: (sequenceItemId: string) => void;
  onRemoveItem: (entry: PreviewSequenceEntry) => void;
  onUpdateDuration: (sequenceItemId: string, durationSeconds: number) => void;
  onSeek: (seconds: number) => void;
  onRequestAddContent: () => void;
};

export function OperationSequenceTimeline({
  campaign,
  entries,
  selectedItemId,
  activeItemId,
  playbackMode,
  currentLoopTimeSeconds,
  canEditPlaylist,
  onSelectItem,
  onSelectAndSeekItem,
  onMoveItem,
  onDuplicateItem,
  onRemoveItem,
  onUpdateDuration,
  onSeek,
  onRequestAddContent,
}: OperationSequenceTimelineProps) {
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden px-3 py-3">
      <div className="min-h-0 flex-1 overflow-hidden">
        {campaign ? (
          <OperationTimelineTrack
            activeItemId={activeItemId}
            canEdit={canEditPlaylist}
            currentLoopTimeSeconds={currentLoopTimeSeconds}
            entries={entries}
            onDuplicateItem={onDuplicateItem}
            onMoveItem={onMoveItem}
            onRemoveItem={onRemoveItem}
            onRequestAddContent={onRequestAddContent}
            onSeek={onSeek}
            onSelectAndSeekItem={onSelectAndSeekItem}
            onSelectItem={onSelectItem}
            onUpdateDuration={onUpdateDuration}
            playbackMode={playbackMode}
            selectedItemId={selectedItemId}
            totalDurationSeconds={Math.max(1, entries.reduce((sum, entry) => sum + entry.duration_seconds, 0))}
          />
        ) : (
          <div className="grid h-full min-h-[220px] place-items-center rounded-[18px] border border-dashed border-slate-300 bg-slate-50 px-5 text-center text-sm text-slate-600">
            <div>
              <span className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-white text-accent shadow-sm">
                <Clapperboard className="h-5 w-5" />
              </span>
              <p className="mt-4 font-semibold text-ink">Campaign Studio listo</p>
              <p className="mt-2 max-w-xl">Selecciona una campaña en el panel izquierdo y agrega contenido desde la biblioteca derecha para construir la timeline profesional.</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
