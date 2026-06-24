import { Database, FileSpreadsheet, Link2 } from "lucide-react";

import { SectionCard } from "../SectionCard";
import type { DataSourceType } from "../../types/domain";

const CONNECTORS: Array<{
  type: DataSourceType;
  title: string;
  description: string;
  detail: string;
  available: boolean;
  icon: typeof FileSpreadsheet;
}> = [
  {
    type: "file_upload",
    title: "Archivo Excel o CSV",
    description: "Carga manual versionada para tarifarios, horarios, menús y cualquier tabla del cliente.",
    detail: "Disponible ahora",
    available: true,
    icon: FileSpreadsheet,
  },
  {
    type: "google_sheets",
    title: "Google Sheets",
    description: "Preparado para conectar hojas compartidas y refrescar datasets sin descargar archivos.",
    detail: "Proximamente",
    available: false,
    icon: Link2,
  },
  {
    type: "api",
    title: "API externa",
    description: "Pensado para fuentes en tiempo real como salidas, tarifas o sistemas operativos del cliente.",
    detail: "Proximamente",
    available: false,
    icon: Database,
  },
];

export function DataSourceConnectorCards({
  selectedType,
  onSelect,
}: {
  selectedType: DataSourceType;
  onSelect: (type: DataSourceType) => void;
}) {
  return (
    <SectionCard
      title="Conectores de datos"
      subtitle="Define el origen del dataset. En esta fase se habilita carga por archivo con el mismo modelo listo para Google Sheets y APIs."
    >
      <div className="grid gap-4 lg:grid-cols-3">
        {CONNECTORS.map((connector) => {
          const Icon = connector.icon;
          const active = connector.type === selectedType;
          return (
            <button
              key={connector.type}
              type="button"
              disabled={!connector.available}
              onClick={() => connector.available && onSelect(connector.type)}
              className={[
                "rounded-[26px] border p-5 text-left transition",
                connector.available
                  ? active
                    ? "border-cyan-300 bg-cyan-50 shadow-[0_0_0_1px_rgba(6,182,212,0.2)]"
                    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"
                  : "cursor-not-allowed border-slate-200 bg-slate-50 opacity-70",
              ].join(" ")}
            >
              <div className="flex items-start gap-3">
                <span
                  className={[
                    "rounded-2xl p-3",
                    active && connector.available ? "bg-cyan-500 text-white" : "bg-slate-900 text-white",
                  ].join(" ")}
                >
                  <Icon className="h-5 w-5" />
                </span>
                <div className="min-w-0">
                  <p className="font-semibold text-ink">{connector.title}</p>
                  <p className="mt-2 text-sm text-slate-600">{connector.description}</p>
                  <p className="mt-4 text-xs uppercase tracking-[0.24em] text-slate-500">{connector.detail}</p>
                </div>
              </div>
            </button>
          );
        })}
      </div>
    </SectionCard>
  );
}
