import { PanelTopOpen, Plane, ReceiptText, Ticket } from "lucide-react";

import { formatLayoutBindingPreset } from "../lib/labels";
import { layoutBindingPresets } from "../lib/layoutBindings";
import type { LayoutBindingPreset } from "../types/domain";

const presetIcons = {
  autobuses: ReceiptText,
  aeropuerto: Plane,
  menu: PanelTopOpen,
  turnero: Ticket,
} satisfies Record<LayoutBindingPreset, typeof ReceiptText>;

export function TemplatePresetPicker({
  selectedPreset,
  onSelect,
  disabled = false,
}: {
  selectedPreset: LayoutBindingPreset;
  onSelect: (preset: LayoutBindingPreset) => void;
  disabled?: boolean;
}) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {layoutBindingPresets.map((preset) => {
        const Icon = presetIcons[preset.key];
        const isActive = preset.key === selectedPreset;
        return (
          <button
            key={preset.key}
            type="button"
            disabled={disabled}
            onClick={() => onSelect(preset.key)}
            className={[
              "rounded-[24px] border px-4 py-4 text-left transition",
              disabled ? "cursor-not-allowed opacity-60" : "hover:border-accent/40 hover:bg-accentSoft/30",
              isActive ? "border-accent bg-accentSoft/50 shadow-card" : "border-slate-200 bg-white",
            ].join(" ")}
          >
            <div className="flex items-start gap-3">
              <span className={["rounded-2xl p-3", isActive ? "bg-white text-accent" : "bg-slate-100 text-slate-500"].join(" ")}>
                <Icon className="h-5 w-5" />
              </span>
              <div className="min-w-0">
                <p className="font-semibold text-ink">{formatLayoutBindingPreset(preset.key)}</p>
                <p className="mt-1 text-sm text-slate-600">{preset.description}</p>
                <p className="mt-3 text-xs uppercase tracking-[0.22em] text-slate-400">
                  {preset.fields.filter((field) => field.required).length} campos obligatorios
                </p>
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
