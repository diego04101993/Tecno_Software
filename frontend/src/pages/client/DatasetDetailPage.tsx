import { Clock3, Database, FileSpreadsheet, History } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { DatasetRowsPreview } from "../../components/data-sources/DatasetRowsPreview";
import { DatasetSchemaTable } from "../../components/data-sources/DatasetSchemaTable";
import { DatasetUploadWizard } from "../../components/data-sources/DatasetUploadWizard";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import {
  formatDataSourceType,
  formatDatasetImportStatus,
  formatDatasetStatus,
} from "../../lib/labels";
import { canWriteClientScope } from "../../lib/rbac";
import type {
  DataSource,
  Dataset,
  DatasetImport,
  DatasetPreview,
} from "../../types/domain";

export function DatasetDetailPage() {
  const { token, user } = useAuth();
  const { clientId, datasetId } = useParams();
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [dataSources, setDataSources] = useState<DataSource[]>([]);
  const [imports, setImports] = useState<DatasetImport[]>([]);
  const [preview, setPreview] = useState<DatasetPreview | null>(null);
  const [selectedImportId, setSelectedImportId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const canManage = canWriteClientScope(user?.role);
  const currentSource = useMemo(
    () => dataSources.find((item) => item.id === dataset?.data_source_id) ?? null,
    [dataSources, dataset?.data_source_id],
  );

  async function loadBaseData() {
    if (!token || !clientId || !datasetId) {
      return;
    }

    try {
      const [datasetResponse, sourcesResponse, importsResponse, previewResponse] = await Promise.all([
        apiRequest<Dataset>(`/datasets/${datasetId}`, { token }),
        apiRequest<DataSource[]>(`/data-sources?client_id=${clientId}`, { token }),
        apiRequest<DatasetImport[]>(`/datasets/${datasetId}/imports`, { token }),
        apiRequest<DatasetPreview>(`/datasets/${datasetId}/preview`, { token }),
      ]);

      setDataset(datasetResponse);
      setDataSources(sourcesResponse);
      setImports(importsResponse);
      setPreview(previewResponse);
      setSelectedImportId((current) => {
        if (current && importsResponse.some((item) => item.id === current)) {
          return current;
        }
        return datasetResponse.current_import_id || importsResponse[0]?.id || "";
      });
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el dataset");
    }
  }

  async function loadPreview(importId: string) {
    if (!token || !datasetId) {
      return;
    }

    try {
      const query = importId ? `?import_id=${importId}` : "";
      const response = await apiRequest<DatasetPreview>(`/datasets/${datasetId}/preview${query}`, { token });
      setPreview(response);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el preview del dataset");
    }
  }

  useEffect(() => {
    void loadBaseData();
  }, [clientId, datasetId, token]);

  useEffect(() => {
    if (!selectedImportId || !datasetId) {
      return;
    }
    void loadPreview(selectedImportId);
  }, [datasetId, selectedImportId, token]);

  async function handleReimport(file: File) {
    if (!token || !datasetId) {
      return;
    }

    try {
      const formData = new FormData();
      formData.append("file", file);
      await apiRequest(`/datasets/${datasetId}/imports/upload`, {
        method: "POST",
        token,
        formData,
      });
      await loadBaseData();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo subir la nueva versión del dataset");
    }
  }

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-[1.06fr_0.94fr]">
        <article className="rounded-[32px] border border-white/70 bg-card/90 p-5 shadow-card backdrop-blur">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-accent">Dataset operativo</p>
              <h2 className="mt-3 font-display text-4xl text-ink">{dataset?.name ?? "Dataset"}</h2>
              <p className="mt-3 text-sm text-slate-500">{dataset?.slug ?? "Sin slug"}</p>
              <p className="mt-2 text-sm text-slate-700">{dataset?.description ?? "Sin descripcion operativa"}</p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                <p className="text-xs uppercase tracking-wide text-slate-400">Estado</p>
                <p className="mt-2 font-semibold text-ink">{formatDatasetStatus(dataset?.status)}</p>
              </div>
              <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                <p className="text-xs uppercase tracking-wide text-slate-400">Origen</p>
                <p className="mt-2 font-semibold text-ink">{formatDataSourceType(currentSource?.source_type)}</p>
              </div>
            </div>
          </div>
        </article>

        <section className="grid gap-3 sm:grid-cols-2">
          <StatCard label="Columnas" value={String(dataset?.column_count ?? 0)} hint="Esquema actual" tone="teal" />
          <StatCard label="Filas" value={String(dataset?.row_count ?? 0)} hint="Snapshot vigente" />
          <StatCard label="Imports" value={String(imports.length)} hint="Historial versionado" tone="orange" />
          <StatCard
            label="Version actual"
            value={preview?.current_import ? "Activa" : "Sin carga"}
            hint={preview?.current_import?.source_filename ?? "Pendiente de importacion"}
          />
        </section>
      </section>

      <div className="grid gap-5 xl:grid-cols-[0.92fr_1.08fr]">
        <DatasetUploadWizard
          mode="reimport"
          sourceType="file_upload"
          disabled={!canManage}
          datasetName={dataset?.name}
          onSubmitReimport={async ({ file }) => handleReimport(file)}
        />

        <SectionCard title="Historial de importaciones" subtitle="Cada archivo genera una nueva versión. Puedes revisar cualquier snapshot sin perder la vigente.">
          <div className="space-y-3">
            {imports.length > 0 ? (
              imports.map((importRecord) => {
                const active = importRecord.id === (selectedImportId || dataset?.current_import_id);
                return (
                  <button
                    key={importRecord.id}
                    type="button"
                    onClick={() => setSelectedImportId(importRecord.id)}
                    className={[
                      "w-full rounded-[24px] border px-4 py-4 text-left transition",
                      active
                        ? "border-cyan-300 bg-cyan-50 shadow-[0_0_0_1px_rgba(6,182,212,0.18)]"
                        : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50",
                    ].join(" ")}
                  >
                    <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                      <div className="min-w-0">
                        <p className="truncate font-semibold text-ink">{importRecord.source_filename}</p>
                        <p className="mt-1 text-sm text-slate-500">
                          {formatDatasetImportStatus(importRecord.import_status)} - {importRecord.column_count} columna(s) - {importRecord.row_count} fila(s)
                        </p>
                      </div>
                      <p className="text-sm text-slate-500">{new Date(importRecord.imported_at).toLocaleString()}</p>
                    </div>
                  </button>
                );
              })
            ) : (
              <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                Este dataset todavia no tiene importaciones registradas.
              </div>
            )}
          </div>
        </SectionCard>
      </div>

      <section className="grid gap-4 xl:grid-cols-3">
        {[
          {
            title: "Archivo vigente",
            detail: preview?.current_import?.source_filename ?? "Sin archivo activo",
            icon: FileSpreadsheet,
          },
          {
            title: "Ultima carga",
            detail: preview?.current_import?.imported_at ? new Date(preview.current_import.imported_at).toLocaleString() : "Sin fecha",
            icon: Clock3,
          },
          {
            title: "Historial listo",
            detail: `${imports.length} versión(es) registradas`,
            icon: History,
          },
        ].map((item) => {
          const Icon = item.icon;
          return (
            <article key={item.title} className="rounded-[28px] border border-white/70 bg-card/90 p-5 shadow-card backdrop-blur">
              <div className="flex items-start gap-4">
                <span className="rounded-2xl bg-accentSoft p-3 text-accent">
                  <Icon className="h-5 w-5" />
                </span>
                <div>
                  <p className="font-semibold text-ink">{item.title}</p>
                  <p className="mt-2 text-sm text-slate-600">{item.detail}</p>
                </div>
              </div>
            </article>
          );
        })}
      </section>

      <DatasetSchemaTable columns={preview?.columns ?? []} />
      <DatasetRowsPreview columns={preview?.columns ?? []} rows={preview?.rows ?? []} />

      <SectionCard title="Arquitectura preparada" subtitle="Este dataset ya queda listo para la siguiente fase de Layout Bindings sin mezclar ingestiones con edicion visual.">
        <div className="grid gap-4 md:grid-cols-3">
          {[
            {
              title: "Data Source",
              description: currentSource?.name ?? "Origen pendiente",
              icon: Database,
            },
            {
              title: "Version activa",
              description: preview?.current_import?.id ?? "Sin import activo",
              icon: FileSpreadsheet,
            },
            {
              title: "Ready para bindings",
              description: "Columnas y filas ya pueden mapearse a widgets dinámicos.",
              icon: History,
            },
          ].map((item) => {
            const Icon = item.icon;
            return (
              <article key={item.title} className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                <div className="flex items-start gap-3">
                  <span className="rounded-2xl bg-slate-100 p-3 text-slate-700">
                    <Icon className="h-5 w-5" />
                  </span>
                  <div>
                    <p className="font-semibold text-ink">{item.title}</p>
                    <p className="mt-2 text-sm text-slate-600">{item.description}</p>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      </SectionCard>
    </div>
  );
}
