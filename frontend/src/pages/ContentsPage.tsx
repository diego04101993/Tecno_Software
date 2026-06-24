import { FormEvent, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { SectionCard } from "../components/SectionCard";
import { API_ORIGIN, apiRequest } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatContentType } from "../lib/labels";
import { canAccessGlobalClients, canWriteClientScope } from "../lib/rbac";
import type { Client, ContentItem } from "../types/domain";

function resolveAssetPath(path: string | null) {
  if (!path) {
    return null;
  }
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  return `${API_ORIGIN}${path}`;
}

export function ContentsPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [clients, setClients] = useState<Client[]>([]);
  const [contents, setContents] = useState<ContentItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    client_id: clientId ?? user?.client_id ?? "",
    name: "",
    type: "text",
    source_url: "",
    text_content: "",
    html_content: "",
    duration_seconds: 15,
  });

  const canCreateContent = canWriteClientScope(user?.role);

  function loadData() {
    if (!token) {
      return;
    }

    const contentPath = clientId ? `/contents?client_id=${clientId}` : "/contents";
    Promise.all([apiRequest<Client[]>("/clients", { token }), apiRequest<ContentItem[]>(contentPath, { token })])
      .then(([clientsResponse, contentsResponse]) => {
        setClients(clientsResponse);
        setContents(contentsResponse);
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el contenido");
      });
  }

  useEffect(() => {
    loadData();
  }, [clientId, token]);

  useEffect(() => {
    if (clientId || user?.client_id) {
      setForm((current) => ({ ...current, client_id: clientId ?? user?.client_id ?? current.client_id }));
    }
  }, [clientId, user?.client_id]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !canCreateContent) {
      return;
    }

    try {
      const selectedFile = fileRef.current?.files?.[0];
      if (selectedFile) {
        const data = new FormData();
        data.append("file", selectedFile);
        data.append("name", form.name);
        data.append("client_id", form.client_id || user?.client_id || "");
        data.append("duration_seconds", String(form.duration_seconds));
        await apiRequest<ContentItem>("/contents/upload", { method: "POST", token, formData: data });
      } else {
        await apiRequest<ContentItem>("/contents", {
          method: "POST",
          token,
          body: {
            ...form,
            client_id: form.client_id || user?.client_id,
            source_url: form.source_url || null,
            text_content: form.text_content || null,
            html_content: form.html_content || null,
            metadata_json: {},
          },
        });
      }

      setForm((current) => ({
        ...current,
        name: "",
        source_url: "",
        text_content: "",
        html_content: "",
        duration_seconds: 15,
      }));
      if (fileRef.current) {
        fileRef.current.value = "";
      }
      loadData();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo guardar el contenido");
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
      <SectionCard title="Biblioteca multimedia" subtitle="La biblioteca ahora se consulta desde el workspace del cliente activo.">
        <div className="grid gap-4 md:grid-cols-2">
          {contents.map((content) => {
            const assetPath = resolveAssetPath(content.file_path ?? content.source_url);
            return (
              <article key={content.id} className="rounded-[28px] border border-slate-200 bg-white p-4">
                <div className="overflow-hidden rounded-[22px] bg-slate-100">
                  {content.type === "image" && assetPath ? (
                    <img alt={content.name} className="h-44 w-full object-cover" src={assetPath} />
                  ) : content.type === "video" && assetPath ? (
                    <video className="h-44 w-full object-cover" src={assetPath} controls />
                  ) : (
                    <div className="grid h-44 place-items-center bg-gradient-to-br from-accentSoft to-emberSoft px-4 text-center text-sm text-slate-700">
                      {content.text_content ?? content.source_url ?? "Contenido HTML / URL"}
                    </div>
                  )}
                </div>
                <p className="mt-4 font-semibold text-ink">{content.name}</p>
                <p className="mt-1 text-sm text-slate-500">
                  {formatContentType(content.type)} · {content.duration_seconds}s
                </p>
              </article>
            );
          })}
        </div>
      </SectionCard>

      <SectionCard title="Cargar contenido" subtitle="La carga queda acotada al cliente activo aunque el layout visual siga igual por ahora.">
        {error ? <div className="mb-4 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
        {!canCreateContent ? (
          <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-5 text-sm text-slate-600">
            Este rol puede consultar la biblioteca, pero no cargar ni editar contenido.
          </div>
        ) : (
          <form className="grid gap-4" onSubmit={handleSubmit}>
            {canAccessGlobalClients(user?.role) && !clientId ? (
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Cliente</label>
                <select value={form.client_id} onChange={(event) => setForm({ ...form, client_id: event.target.value })} required>
                  <option value="">Selecciona un cliente</option>
                  {clients.map((client) => (
                    <option key={client.id} value={client.id}>
                      {client.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
              <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Tipo</label>
              <select value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value })}>
                <option value="image">Imagen</option>
                <option value="video">Video</option>
                <option value="url">URL</option>
                <option value="html">HTML</option>
                <option value="text">Texto dinámico</option>
              </select>
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Archivo</label>
              <input ref={fileRef} type="file" />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">URL fuente</label>
              <input value={form.source_url} onChange={(event) => setForm({ ...form, source_url: event.target.value })} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Texto</label>
              <textarea rows={3} value={form.text_content} onChange={(event) => setForm({ ...form, text_content: event.target.value })} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">HTML</label>
              <textarea rows={4} value={form.html_content} onChange={(event) => setForm({ ...form, html_content: event.target.value })} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Duración</label>
              <input
                type="number"
                value={form.duration_seconds}
                onChange={(event) => setForm({ ...form, duration_seconds: Number(event.target.value) })}
              />
            </div>
            <button className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white" type="submit">
              Guardar contenido
            </button>
          </form>
        )}
      </SectionCard>
    </div>
  );
}
