import type { PreviewSequenceEntry } from "../../lib/preview";
import { OperationVideoEditorTimeline } from "./OperationVideoEditorTimeline";

type OperationTimelineTrackProps = {
  entries: PreviewSequenceEntry[];
  selectedItemId: string | null;
  activeItemId: string | null;
  canEdit: boolean;
  totalDurationSeconds: number;
  currentLoopTimeSeconds: number;
  playbackMode: "sequential" | "random";
  onSelectItem: (sequenceItemId: string) => void;
  onSelectAndSeekItem: (sequenceItemId: string, startSeconds: number) => void;
  onMoveItem: (sequenceItemId: string, direction: "left" | "right") => void;
  onDuplicateItem: (sequenceItemId: string) => void;
  onRemoveItem: (entry: PreviewSequenceEntry) => void;
  onUpdateDuration: (sequenceItemId: string, durationSeconds: number) => void;
  onSeek: (seconds: number) => void;
  onRequestAddContent: () => void;
};

export function OperationTimelineTrack(props: OperationTimelineTrackProps) {
  return <OperationVideoEditorTimeline {...props} />;
}
