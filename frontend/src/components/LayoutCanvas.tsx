import { useEffect, useMemo, useRef, useState } from "react";

import { applySnapping, clampWithinCanvas, computeGuides, sortByLayer, type SelectionTarget } from "../lib/layoutEditor";
import { formatLayoutWidgetType } from "../lib/labels";
import type { LayoutEditorState, LayoutRegion, LayoutWidget } from "../types/domain";

type DragState = {
  kind: "widget" | "region";
  id: string;
  mode: "move" | "resize";
  startMouseX: number;
  startMouseY: number;
  startX: number;
  startY: number;
  startWidth: number;
  startHeight: number;
};

type LayoutCanvasProps = {
  state: LayoutEditorState;
  selectedTarget: SelectionTarget;
  canManage: boolean;
  onSelectTarget: (target: SelectionTarget) => void;
  onWidgetPatch: (widgetId: string, patch: Partial<LayoutWidget>) => void;
  onRegionPatch: (regionId: string, patch: Partial<LayoutRegion>) => void;
};

export function LayoutCanvas({ state, selectedTarget, canManage, onSelectTarget, onWidgetPatch, onRegionPatch }: LayoutCanvasProps) {
  const [dragState, setDragState] = useState<DragState | null>(null);
  const [guides, setGuides] = useState<{ vertical: number[]; horizontal: number[] }>({ vertical: [], horizontal: [] });
  const surfaceRef = useRef<HTMLDivElement | null>(null);

  const scale = useMemo(() => {
    const maxStageWidth = 920;
    const maxStageHeight = 620;
    return Math.min(maxStageWidth / state.canvas.width, maxStageHeight / state.canvas.height, 1);
  }, [state.canvas.height, state.canvas.width]);

  useEffect(() => {
    if (!dragState) {
      return;
    }
    const currentDrag = dragState;

    function handleMove(event: MouseEvent) {
      const deltaX = Math.round((event.clientX - currentDrag.startMouseX) / scale);
      const deltaY = Math.round((event.clientY - currentDrag.startMouseY) / scale);
      const snapThreshold = state.guides.snap_threshold;
      const nextGuides = computeGuides(state, currentDrag.id, currentDrag.kind);

      if (currentDrag.kind === "widget") {
        if (currentDrag.mode === "move") {
          const snappedX = applySnapping(currentDrag.startX + deltaX, nextGuides.vertical, snapThreshold);
          const snappedY = applySnapping(currentDrag.startY + deltaY, nextGuides.horizontal, snapThreshold);
          const bounds = clampWithinCanvas(state, {
            x: snappedX.value,
            y: snappedY.value,
            width: currentDrag.startWidth,
            height: currentDrag.startHeight,
          });
          onWidgetPatch(currentDrag.id, { x: bounds.x, y: bounds.y });
          setGuides({
            vertical: snappedX.active !== null ? [snappedX.active] : [],
            horizontal: snappedY.active !== null ? [snappedY.active] : [],
          });
        } else {
          const snappedWidth = applySnapping(currentDrag.startX + currentDrag.startWidth + deltaX, nextGuides.vertical, snapThreshold);
          const snappedHeight = applySnapping(currentDrag.startY + currentDrag.startHeight + deltaY, nextGuides.horizontal, snapThreshold);
          const bounds = clampWithinCanvas(state, {
            x: currentDrag.startX,
            y: currentDrag.startY,
            width: snappedWidth.value - currentDrag.startX,
            height: snappedHeight.value - currentDrag.startY,
          });
          onWidgetPatch(currentDrag.id, { width: bounds.width, height: bounds.height });
          setGuides({
            vertical: snappedWidth.active !== null ? [snappedWidth.active] : [],
            horizontal: snappedHeight.active !== null ? [snappedHeight.active] : [],
          });
        }
      } else {
        if (currentDrag.mode === "move") {
          const snappedX = applySnapping(currentDrag.startX + deltaX, nextGuides.vertical, snapThreshold);
          const snappedY = applySnapping(currentDrag.startY + deltaY, nextGuides.horizontal, snapThreshold);
          const bounds = clampWithinCanvas(state, {
            x: snappedX.value,
            y: snappedY.value,
            width: currentDrag.startWidth,
            height: currentDrag.startHeight,
          });
          onRegionPatch(currentDrag.id, { x: bounds.x, y: bounds.y });
          setGuides({
            vertical: snappedX.active !== null ? [snappedX.active] : [],
            horizontal: snappedY.active !== null ? [snappedY.active] : [],
          });
        } else {
          const snappedWidth = applySnapping(currentDrag.startX + currentDrag.startWidth + deltaX, nextGuides.vertical, snapThreshold);
          const snappedHeight = applySnapping(currentDrag.startY + currentDrag.startHeight + deltaY, nextGuides.horizontal, snapThreshold);
          const bounds = clampWithinCanvas(state, {
            x: currentDrag.startX,
            y: currentDrag.startY,
            width: snappedWidth.value - currentDrag.startX,
            height: snappedHeight.value - currentDrag.startY,
          });
          onRegionPatch(currentDrag.id, { width: bounds.width, height: bounds.height });
          setGuides({
            vertical: snappedWidth.active !== null ? [snappedWidth.active] : [],
            horizontal: snappedHeight.active !== null ? [snappedHeight.active] : [],
          });
        }
      }
    }

    function handleUp() {
      setDragState(null);
      setGuides({ vertical: [], horizontal: [] });
    }

    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
  }, [dragState, onRegionPatch, onWidgetPatch, scale, state]);

  const sortedWidgets = sortByLayer(state.widgets);

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="border-b border-slate-200 pb-5">
        <h2 className="font-display text-2xl text-ink">Layout Canvas</h2>
        <p className="mt-1 text-sm text-slate-600">Canvas visual tipo Canva con guías, snapping básico y soporte para múltiples regiones, capas y resoluciones personalizadas.</p>
      </div>

      <div className="mt-6 overflow-auto rounded-[28px] border border-slate-200 bg-slate-100 p-4">
        <div
          ref={surfaceRef}
          className="relative mx-auto origin-top-left overflow-hidden rounded-[24px] border border-slate-200 shadow-2xl"
          style={{
            width: `${state.canvas.width * scale}px`,
            height: `${state.canvas.height * scale}px`,
            backgroundColor: state.canvas.background_color,
          }}
          onClick={() => onSelectTarget({ kind: "canvas" })}
        >
          {state.guides.show_grid
            ? Array.from({ length: Math.floor(state.canvas.width / 120) }).map((_, index) => (
                <div
                  key={`grid-v-${index}`}
                  className="pointer-events-none absolute top-0 h-full border-l border-white/10"
                  style={{ left: `${index * 120 * scale}px` }}
                />
              ))
            : null}
          {state.guides.show_grid
            ? Array.from({ length: Math.floor(state.canvas.height / 120) }).map((_, index) => (
                <div
                  key={`grid-h-${index}`}
                  className="pointer-events-none absolute left-0 w-full border-t border-white/10"
                  style={{ top: `${index * 120 * scale}px` }}
                />
              ))
            : null}

          {guides.vertical.map((guide) => (
            <div key={`v-${guide}`} className="pointer-events-none absolute top-0 h-full w-px bg-accent" style={{ left: `${guide * scale}px` }} />
          ))}
          {guides.horizontal.map((guide) => (
            <div key={`h-${guide}`} className="pointer-events-none absolute left-0 w-full border-t border-accent" style={{ top: `${guide * scale}px` }} />
          ))}

          {state.regions.map((region) => {
            const isSelected = selectedTarget.kind === "region" && selectedTarget.id === region.id;
            return (
              <div
                key={region.id}
                className={[
                  "absolute overflow-hidden rounded-[18px] border border-dashed transition",
                  isSelected ? "border-accent bg-accentSoft/25" : "border-white/45 bg-white/5",
                ].join(" ")}
                style={{
                  left: `${region.x * scale}px`,
                  top: `${region.y * scale}px`,
                  width: `${region.width * scale}px`,
                  height: `${region.height * scale}px`,
                }}
                onClick={(event) => {
                  event.stopPropagation();
                  onSelectTarget({ kind: "region", id: region.id });
                }}
                onMouseDown={(event) => {
                  if (!canManage || region.locked) {
                    return;
                  }
                  event.stopPropagation();
                  setDragState({
                    kind: "region",
                    id: region.id,
                    mode: "move",
                    startMouseX: event.clientX,
                    startMouseY: event.clientY,
                    startX: region.x,
                    startY: region.y,
                    startWidth: region.width,
                    startHeight: region.height,
                  });
                }}
              >
                <div className="rounded-br-2xl bg-slate-950/75 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-white">
                  {region.label}
                </div>
                {isSelected && canManage ? (
                  <div
                    className="absolute bottom-1 right-1 h-4 w-4 cursor-se-resize rounded-full bg-accent"
                    onMouseDown={(event) => {
                      event.stopPropagation();
                      setDragState({
                        kind: "region",
                        id: region.id,
                        mode: "resize",
                        startMouseX: event.clientX,
                        startMouseY: event.clientY,
                        startX: region.x,
                        startY: region.y,
                        startWidth: region.width,
                        startHeight: region.height,
                      });
                    }}
                  />
                ) : null}
              </div>
            );
          })}

          {sortedWidgets.map((widget) => {
            const isSelected = selectedTarget.kind === "widget" && selectedTarget.id === widget.id;
            return (
              <div
                key={widget.id}
                className={[
                  "absolute overflow-hidden rounded-[18px] border shadow-lg transition",
                  isSelected ? "border-accent ring-2 ring-accent/40" : "border-white/35",
                ].join(" ")}
                style={{
                  left: `${widget.x * scale}px`,
                  top: `${widget.y * scale}px`,
                  width: `${widget.width * scale}px`,
                  height: `${widget.height * scale}px`,
                  opacity: widget.opacity,
                  transform: `rotate(${widget.rotation}deg)`,
                  zIndex: widget.z_index,
                  background: widget.widget_type === "overlay_png" ? "transparent" : "rgba(255,255,255,0.94)",
                }}
                onClick={(event) => {
                  event.stopPropagation();
                  onSelectTarget({ kind: "widget", id: widget.id });
                }}
                onMouseDown={(event) => {
                  if (!canManage || widget.locked) {
                    return;
                  }
                  event.stopPropagation();
                  setDragState({
                    kind: "widget",
                    id: widget.id,
                    mode: "move",
                    startMouseX: event.clientX,
                    startMouseY: event.clientY,
                    startX: widget.x,
                    startY: widget.y,
                    startWidth: widget.width,
                    startHeight: widget.height,
                  });
                }}
              >
                <div className="flex h-full flex-col">
                  <div className="border-b border-slate-200/70 bg-white/90 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">
                    {formatLayoutWidgetType(widget.widget_type)}
                  </div>
                  <div className="flex h-full items-center justify-center px-4 py-4 text-center text-sm text-slate-600">
                    {widget.widget_type === "dataset_table"
                      ? "Tabla dinámica conectable"
                      : widget.widget_type === "overlay_png"
                        ? "Overlay PNG / marco / logo"
                        : widget.name}
                  </div>
                </div>
                {isSelected && canManage ? (
                  <div
                    className="absolute bottom-1 right-1 h-4 w-4 cursor-se-resize rounded-full bg-accent"
                    onMouseDown={(event) => {
                      event.stopPropagation();
                      setDragState({
                        kind: "widget",
                        id: widget.id,
                        mode: "resize",
                        startMouseX: event.clientX,
                        startMouseY: event.clientY,
                        startX: widget.x,
                        startY: widget.y,
                        startWidth: widget.width,
                        startHeight: widget.height,
                      });
                    }}
                  />
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
