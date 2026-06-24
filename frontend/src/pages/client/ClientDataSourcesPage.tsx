import { Database, FileClock, TableProperties } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { DataSourceConnectorCards } from "../../components/data-sources/DataSourceConnectorCards";
import { DatasetUploadWizard } from "../../components/data-sources/DatasetUploadWizard";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { formatDataSourceType, formatDatasetStatus } from "../../lib/labels";
import { canWriteClientScope } from "../../lib/rbac";
import { buildDatasetDetailPath } from "../../lib/workspace";
import type { DataSource, DataSourceType, Dataset, DatasetImport } from "../../types/domain";

export function ClientDataSourcesPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const navigate = useNavigate();
  const [selectedType, setSelectedType] = useState<DataSourceType>("file_upload");
  const [dataSources, setDataSources] = useState<DataSource[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [error, setError] = useState<string | null>(null);

  const canManage = canWriteClientScope(user?.role);
  const readyDatasets = useMemo(() => datasets.filter((item) => item.status === "ready"), [datasets]);
  const processingDatasets = useMemo(() => datasets.filter((item) => item.status === "processing"), [datasets]);

  async function loadData() {
    if (!token || !clientId) {
      return;
    }

    try {
      const [sourcesResponse, datasetsResponse] = await Promise.all([
        apiRequest<DataSource[]>(`/data-sources?client_id=${clientId}`, { token }),
        apiRequest<Dataset[]>(`/datasets?client_id=${clientId}`, { token }),
      ]);
      setDataSources(sourcesResponse);
      setDatasets(datasetsResponse);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el modulo de data sources");
    }
  }

  useEffect(() => {
    void loadData();
  }, [clientId, token]);

  async function handleCreateDataset(payload: {
    source_type: DataSourceType;
    data_source_name: string;
    dataset_name: string;
    dataset_slug: string;
    description: string;
    file: File;
  }) {
    if (!token || !clientId) {
      return;
    }

    if (datasets.some((item) => item.slug === payload.dataset_slug)) {
      setError("Ya existe un dataset con ese slug dentro del cliente actual.");
      return;
    }

    let createdDataset: Dataset | null = null;
    try {
      const dataSource = await apiRequest<DataSource>("/data-sources", {
        method: "POST",
        token,
        body: {
          client_id: clientId,
          name: payload.data_source_name,
          source_type: payload.source_type,
          description: payload.description || null,
          config_json: {},
          is_active: true,
        },
      });

      createdDataset = await apiRequest<Dataset>("/datasets", {
        method: "POST",
        token,
        body: {
          client_id: clientId,
          data_source_id: dataSource.id,
          name: payload.dataset_name,
          slug: payload.dataset_slug,
          description: payload.description || null,
        },
      });

      const formData = new FormData();
      formData.append("file", payload.file);
      await apiRequest<DatasetImport>(`/datasets/${createdDataset.id}/imports/upload`, {
        method: "POST",
        token,
        formData,
      });

      await loadData();
      navigate(buildDatasetDetailPath(clientId, createdDataset.id));
    } catch (nextError) {
      if (createdDataset) {
        await loadData();
      }
      setError(
        nextError instanceof Error
          ? nextError.message
          : "No se pudo crear el dataset. Si el alta fue parcial, podras retomarlo desde la lista.",
      );
    }
  }

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Datasets" value={String(datasets.length)} hint="Catálogo reusable del cliente" tone="teal" />
        <StatCard label="Origenes" value={String(dataSources.length)} hint="Fuentes registradas" />
        <StatCard label="Listos" value={String(readyDatasets.length)} hint="Version activa disponible" tone="teal" />
        <StatCard label="Procesando" value={String(processingDatasets.length)} hint="Importaciones en curso" tone="orange" />
      </section>

      <DataSourceConnectorCards selectedType={selectedType} onSelect={setSelectedType} />

      <div className="grid gap-5 2xl:grid-cols-[0.82fr_1.18fr]">
        <DatasetUploadWizard mode="create" sourceType={selectedType} disabled={!canManage} onSubmitCreate={handleCreateDataset} />

        <SectionCard
          title="Datasets del cliente"
          subtitle="Cada dataset conserva su versión actual, su historial de importaciones y queda listo para alimentar layouts dinámicos en la siguiente fase."
        >
          <div className="grid gap-4 2xl:grid-cols-2">
            {datasets.length > 0 ? (
              datasets.map((dataset) => {
                const source = dataSources.find((item) => item.id === dataset.data_source_id);
                return (
                  <article key={dataset.id} className="rounded-[26px] border border-slate-200 bg-white p-4">
                    <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                      <div className="min-w-0">
                        <p className="truncate font-display text-2xl text-ink">{dataset.name}</p>
                        <p className="mt-1 text-sm text-slate-500">
                          {formatDatasetStatus(dataset.status)} - {formatDataSourceType(source?.source_type)}
                        </p>
                        <p className="mt-3 text-sm text-slate-700">{dataset.description ?? "Sin descripcion operativa"}</p>
                      </div>
                      <Link
                        className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
                        to={buildDatasetDetailPath(clientId ?? "", dataset.id)}
                      >
                        Ver dataset
                      </Link>
                    </div>

                    <div className="mt-4 grid gap-3 md:grid-cols-3">
                      <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">{dataset.column_count} columna(s)</div>
                      <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">{dataset.row_count} fila(s)</div>
                      <div className="rounded-2xl bg-slate-50 px-4 py-3 text-sm text-slate-700">{dataset.slug}</div>
                    </div>
                  </article>
                );
              })
            ) : (
              <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                Aún no existen datasets para este cliente. Crea el primero desde el wizard y sube un archivo CSV o XLSX.
              </div>
            )}
          </div>
        </SectionCard>
      </div>

      <section className="grid gap-4 2xl:grid-cols-4">
        {[
          {
            title: "Excel y CSV versionados",
            description: "Cada carga queda guardada como importacion historica sin perder el snapshot vigente del dataset.",
            icon: FileClock,
          },
          {
            title: "Esquema detectado",
            description: "Las columnas se detectan automaticamente y quedan listas para mapearse a layouts en la siguiente fase.",
            icon: TableProperties,
          },
          {
            title: "Preparado para conectores",
            description: "La entidad data_source ya soporta futuras variantes como Google Sheets o API sin rehacer el modelo.",
            icon: Database,
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
                  <p className="mt-2 text-sm text-slate-600">{item.description}</p>
                </div>
              </div>
            </article>
          );
        })}
      </section>
    </div>
  );
}
