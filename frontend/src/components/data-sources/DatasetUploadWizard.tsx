import { FormEvent, useEffect, useRef, useState } from "react";

import { SectionCard } from "../SectionCard";
import type { DataSourceType } from "../../types/domain";

type CreateDatasetPayload = {
  source_type: DataSourceType;
  data_source_name: string;
  dataset_name: string;
  dataset_slug: string;
  description: string;
  file: File;
};

type ReimportDatasetPayload = {
  file: File;
};

function slugify(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function DatasetUploadWizard({
  mode,
  sourceType,
  disabled,
  datasetName,
  onSubmitCreate,
  onSubmitReimport,
}: {
  mode: "create" | "reimport";
  sourceType: DataSourceType;
  disabled: boolean;
  datasetName?: string;
  onSubmitCreate?: (payload: CreateDatasetPayload) => Promise<void>;
  onSubmitReimport?: (payload: ReimportDatasetPayload) => Promise<void>;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [slugTouched, setSlugTouched] = useState(false);
  const [sourceTouched, setSourceTouched] = useState(false);
  const [form, setForm] = useState({
    data_source_name: "",
    dataset_name: "",
    dataset_slug: "",
    description: "",
  });

  useEffect(() => {
    if (mode === "reimport" && datasetName) {
      setForm((current) => ({
        ...current,
        dataset_name: datasetName,
        data_source_name: current.data_source_name || datasetName,
      }));
    }
  }, [datasetName, mode]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file || disabled) {
      return;
    }

    setSubmitting(true);
    try {
      if (mode === "create" && onSubmitCreate) {
        await onSubmitCreate({
          source_type: sourceType,
          data_source_name: form.data_source_name.trim() || form.dataset_name.trim(),
          dataset_name: form.dataset_name.trim(),
          dataset_slug: form.dataset_slug.trim(),
          description: form.description.trim(),
          file,
        });
        setForm({
          data_source_name: "",
          dataset_name: "",
          dataset_slug: "",
          description: "",
        });
        setSlugTouched(false);
        setSourceTouched(false);
      }
      if (mode === "reimport" && onSubmitReimport) {
        await onSubmitReimport({ file });
      }
      if (fileRef.current) {
        fileRef.current.value = "";
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SectionCard
      title={mode === "create" ? "Nuevo dataset" : "Nueva versión del dataset"}
      subtitle={
        mode === "create"
          ? "Crea el dataset reutilizable del cliente y sube la primera versión del archivo."
          : "Sube una nueva versión del archivo. El historial de importaciones se conserva y el dataset actualiza su snapshot."
      }
    >
      {disabled ? (
        <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
          Este rol puede consultar datasets, pero no crear ni importar archivos.
        </div>
      ) : (
        <form className="grid gap-4" onSubmit={handleSubmit}>
          {mode === "create" ? (
            <>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre del dataset</label>
                  <input
                    value={form.dataset_name}
                    required
                    onChange={(event) => {
                      const nextName = event.target.value;
                      setForm((current) => ({
                        ...current,
                        dataset_name: nextName,
                        dataset_slug: slugTouched ? current.dataset_slug : slugify(nextName),
                        data_source_name: sourceTouched ? current.data_source_name : nextName,
                      }));
                    }}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Slug tecnico</label>
                  <input
                    value={form.dataset_slug}
                    required
                    onChange={(event) => {
                      setSlugTouched(true);
                      setForm((current) => ({
                        ...current,
                        dataset_slug: slugify(event.target.value),
                      }));
                    }}
                  />
                </div>
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre del origen</label>
                  <input
                    value={form.data_source_name}
                    required
                    onChange={(event) => {
                      setSourceTouched(true);
                      setForm((current) => ({ ...current, data_source_name: event.target.value }));
                    }}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Descripción</label>
                  <input
                    value={form.description}
                    onChange={(event) => setForm((current) => ({ ...current, description: event.target.value }))}
                  />
                </div>
              </div>
            </>
          ) : (
            <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
              <p className="font-semibold text-ink">{datasetName ?? "Dataset actual"}</p>
              <p className="mt-1">El archivo que subas se registrará como una nueva importación y quedará como versión vigente.</p>
            </div>
          )}

          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Archivo CSV o XLSX</label>
            <input ref={fileRef} type="file" accept=".csv,.xlsx" required />
          </div>

          <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" type="submit" disabled={submitting}>
            {submitting ? "Procesando importación..." : mode === "create" ? "Crear dataset e importar" : "Subir nueva versión"}
          </button>
        </form>
      )}
    </SectionCard>
  );
}
