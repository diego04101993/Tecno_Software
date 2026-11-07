import { useEffect, useRef, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Campaign, Videowall, VideowallMediaDelivery, VideowallNode, VideowallPublication } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";

const statusLabels = { PENDING: "Pendiente", PROCESSING: "Procesando", READY: "Lista", ACTIVE: "Activada", FAILED: "Error" };

export function VideowallPublicationsPanel({ wall, nodes, token, canManage, onSaved }: {
  wall: Videowall;
  nodes: VideowallNode[];
  token: string | null;
  canManage: boolean;
  onSaved: (wall: Videowall) => Promise<void> | void;
}) {
  const [delivery, setDelivery] = useState<VideowallMediaDelivery>(wall.media_delivery ?? "legacy");
  const [draftDelivery, setDraftDelivery] = useState<VideowallMediaDelivery>(delivery);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [campaignId, setCampaignId] = useState("");
  const [publications, setPublications] = useState<VideowallPublication[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const scope = `${wall.id}:${wall.client_id}:${token ?? ""}`;
  const liveScope = useRef(scope);
  liveScope.current = scope;
  const serial = useRef(0);
  const mounted = useRef(true);
  const editable = canManage && Boolean(token);
  const geometryReady = wall.render_mode === "multi-node" && wall.rows === 1 && wall.columns === 3
    && wall.total_width > 0 && wall.total_width % 3 === 0 && wall.total_height > 0;
  const nodesReady = nodes.length === 3 && new Set(nodes.map((node) => node.channel_id)).size === 3
    && [1, 2, 3].every((position) => nodes.some((node) => node.videowall_id === wall.id
      && node.position_index === position && node.row_index === 0 && node.column_index === position - 1));

  function current(ticket: number, expectedScope: string) {
    return mounted.current && serial.current === ticket && liveScope.current === expectedScope;
  }

  async function refresh() {
    if (!token) return;
    const ticket = ++serial.current;
    const expectedScope = scope;
    setBusy("refresh");
    setError(null);
    try {
      const [nextPublications, nextCampaigns] = await Promise.all([
        apiRequest<VideowallPublication[]>(`/videowalls/${wall.id}/publications`, { token }),
        apiRequest<Campaign[]>(`/campaigns?client_id=${encodeURIComponent(wall.client_id)}`, { token }),
      ]);
      if (!current(ticket, expectedScope)) return;
      setPublications(nextPublications.filter((publication) => publication.wall_id === wall.id));
      const ownCampaigns = nextCampaigns.filter((campaign) => campaign.client_id === wall.client_id && campaign.is_active);
      setCampaigns(ownCampaigns);
      setCampaignId((value) => ownCampaigns.some((campaign) => campaign.id === value) ? value : "");
    } catch (nextError) {
      if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudo actualizar el estado."));
    } finally {
      if (current(ticket, expectedScope)) setBusy(null);
    }
  }

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; serial.current += 1; };
  }, []);

  useEffect(() => {
    serial.current += 1;
    const savedDelivery = wall.media_delivery ?? "legacy";
    setDelivery(savedDelivery);
    setDraftDelivery(savedDelivery);
    setCampaignId("");
    setCampaigns([]);
    setPublications([]);
    setBusy(null);
    setError(null);
    setNotice(null);
    if (savedDelivery === "node_slice" && token) void refresh();
  }, [wall.id, wall.client_id, wall.media_delivery, token]);

  async function saveMode() {
    if (!editable || busy || draftDelivery === delivery || (draftDelivery === "node_slice" && !geometryReady)) return;
    const ticket = ++serial.current;
    const expectedScope = scope;
    setBusy("mode");
    setError(null);
    setNotice(null);
    try {
      const saved = await apiRequest<Videowall>(`/videowalls/${wall.id}`, {
        method: "PATCH", token, body: { media_delivery: draftDelivery },
      });
      if (!current(ticket, expectedScope)) return;
      setDelivery(saved.media_delivery ?? "legacy");
      setDraftDelivery(saved.media_delivery ?? "legacy");
      setNotice("Modo guardado. Preparar una publicación no la activa.");
      setBusy(null);
      await onSaved(saved);
      if (current(ticket, expectedScope) && saved.media_delivery === "node_slice") await refresh();
    } catch (nextError) {
      if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudo guardar el modo."));
    } finally {
      if (current(ticket, expectedScope)) setBusy(null);
    }
  }

  async function prepare() {
    if (!editable || busy || delivery !== "node_slice" || !geometryReady || !nodesReady
      || !campaigns.some((campaign) => campaign.id === campaignId && campaign.client_id === wall.client_id)) return;
    const ticket = ++serial.current;
    const expectedScope = scope;
    setBusy("prepare");
    setError(null);
    setNotice(null);
    try {
      const publication = await apiRequest<VideowallPublication>(`/videowalls/${wall.id}/publications`, {
        method: "POST", token, body: { campaign_id: campaignId },
      });
      if (!current(ticket, expectedScope)) return;
      setPublications((previous) => [publication, ...previous.filter((item) => item.id !== publication.id)]);
      setNotice("Preparación solicitada. Actualiza el estado hasta que esté lista; la publicación activa se conserva.");
    } catch (nextError) {
      if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudo preparar la publicación."));
    } finally {
      if (current(ticket, expectedScope)) setBusy(null);
    }
  }

  async function activate(publication: VideowallPublication) {
    if (!editable || busy || delivery !== "node_slice" || publication.wall_id !== wall.id
      || publication.status !== "READY" || publication.is_current_active) return;
    const ticket = ++serial.current;
    const expectedScope = scope;
    setBusy(publication.id);
    setError(null);
    setNotice(null);
    try {
      const activated = await apiRequest<VideowallPublication>(`/videowalls/${wall.id}/publications/${publication.id}/activate`, {
        method: "POST", token,
      });
      if (!current(ticket, expectedScope)) return;
      setPublications((previous) => previous.map((item) => item.id === activated.id ? activated : { ...item, is_current_active: false }));
      setNotice("Publicación activada. Los tres nodos usarán su misma línea de tiempo compartida.");
      await onSaved({ ...wall, media_delivery: "node_slice", active_publication_id: activated.id });
    } catch (nextError) {
      if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudo activar la publicación."));
    } finally {
      if (current(ticket, expectedScope)) setBusy(null);
    }
  }

  return <section className="space-y-4 rounded-[24px] border border-slate-200 bg-white p-5" aria-label="Publicaciones de videowall">
    <h3 className="font-semibold text-ink">Modo de entrega del videowall</h3>
    <label className="block text-sm">Modo
      <select name="media_delivery" value={draftDelivery} disabled={!editable || Boolean(busy)}
        onChange={(event) => setDraftDelivery(event.target.value as VideowallMediaDelivery)}>
        <option value="legacy">Legacy</option>
        <option value="node_slice">Android node slices</option>
      </select>
    </label>
    <button type="button" onClick={() => void saveMode()} disabled={!editable || Boolean(busy) || draftDelivery === delivery
      || (draftDelivery === "node_slice" && !geometryReady)} className="rounded-xl border border-slate-300 px-4 py-2">
      {busy === "mode" ? "Guardando modo..." : "Guardar modo"}
    </button>
    {!canManage ? <p className="text-sm text-slate-500">Este rol sólo puede consultar las publicaciones.</p> : null}
    {draftDelivery === "node_slice" || delivery === "node_slice" ? <>
      <p className="text-sm text-slate-600">1 fila × 3 columnas · Canvas {wall.total_width} × {wall.total_height} · Panel {wall.total_width / 3} × {wall.total_height}.</p>
      <p className="text-sm text-slate-600">Cada caja Android necesita su propio canal y código de activación. Los archivos se entregan ya recortados; el Player no debe volver a aplicar crop.</p>
      <p className="text-sm text-amber-800">Requiere Android AVW1 compatible. La aprobación física de 4K sigue pendiente.</p>
      {!geometryReady ? <p role="alert">Guarda primero un videowall distribuido con geometría de 1 fila × 3 columnas.</p> : null}
    </> : <p className="text-sm text-slate-600">Legacy conserva el flujo actual. Los videowalls existentes no cambian automáticamente.</p>}
    {delivery === "node_slice" ? <>
      {!nodesReady ? <p className="text-sm text-amber-800">Configura tres canales distintos en las posiciones 1, 2 y 3 antes de preparar.</p> : null}
      <label className="block text-sm">Campaña compartida
        <select name="publication_campaign_id" value={campaignId} disabled={!editable || Boolean(busy)}
          onChange={(event) => setCampaignId(event.target.value)}>
          <option value="">Selecciona una campaña</option>
          {campaigns.map((campaign) => <option key={campaign.id} value={campaign.id}>{campaign.name}</option>)}
        </select>
      </label>
      <p className="text-xs text-slate-500">El master debe coincidir exactamente con el canvas. Se conserva su proporción; no se estira.</p>
      <div className="flex flex-wrap gap-3">
        <button type="button" onClick={() => void prepare()} disabled={!editable || Boolean(busy) || !campaignId || !geometryReady || !nodesReady}
          className="rounded-xl bg-ink px-4 py-2 text-white">{busy === "prepare" ? "Preparando..." : "Preparar publicación"}</button>
        <button type="button" onClick={() => void refresh()} disabled={!token || Boolean(busy)} className="rounded-xl border border-slate-300 px-4 py-2">
          {busy === "refresh" ? "Actualizando..." : "Actualizar estado"}
        </button>
      </div>
      <p className="text-xs text-slate-500">El audio se conserva sólo en la posición 1. Las posiciones 2 y 3 no llevan audio.</p>
      <div className="space-y-3" aria-live="polite">
        {!busy && publications.length === 0 ? <p className="text-sm text-slate-500">Sin publicaciones preparadas.</p> : null}
        {publications.map((publication) => <article key={publication.id} className="rounded-xl border border-slate-200 p-3" data-publication-id={publication.id}>
          <p className="font-semibold">{campaigns.find((campaign) => campaign.id === publication.campaign_id)?.name ?? publication.campaign_id}</p>
          <p>{publication.is_current_active ? "Activa actual" : statusLabels[publication.status]} · {publication.validated_asset_count}/{publication.expected_asset_count} archivos validados</p>
          <p className="text-xs text-slate-500">{publication.id}</p>
          {publication.loop_duration_ms !== null ? <p className="text-sm">Duración del ciclo: {publication.loop_duration_ms} ms</p> : null}
          {publication.error_message || publication.error_code ? <p role="alert" className="text-sm text-rose-700">{publication.error_message || publication.error_code}</p> : null}
          {publication.status === "ACTIVE" && !publication.is_current_active ? <p className="text-sm text-slate-500">Publicación histórica. Prepara una nueva para volver a activarla.</p> : null}
          {publication.status === "READY" ? <button type="button" onClick={() => void activate(publication)} disabled={!editable || Boolean(busy) || publication.is_current_active}
            className="mt-2 rounded-xl bg-ink px-4 py-2 text-white">{busy === publication.id ? "Activando..." : "Activar publicación"}</button> : null}
        </article>)}
      </div>
    </> : null}
    {error ? <p role="alert" className="text-sm text-rose-700">{error}</p> : null}
    {notice ? <p role="status" className="text-sm text-emerald-700">{notice}</p> : null}
  </section>;
}
