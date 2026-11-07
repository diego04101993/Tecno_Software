import { useEffect, useRef, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Campaign, Channel, Videowall, VideowallNode, VideowallPlaybackGroup, VideowallPlaybackGroupMode, VideowallPublication } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";

type GroupDraft = { key: string; id?: string; name: string; mode: VideowallPlaybackGroupMode; campaign_id: string; node_ids: string[] };
const statuses = { PENDING: "Pendiente", PROCESSING: "Procesando", READY: "Lista", ACTIVE: "Activada", FAILED: "Error" };
const positive = (value: number) => Number.isSafeInteger(value) && value > 0;
const placed = (node: VideowallNode, wall: Videowall) => node.videowall_id === wall.id && node.layout_enabled !== false
  && node.row_index >= 0 && node.row_index < wall.rows && node.column_index >= 0 && node.column_index < wall.columns;
const fromGroup = (group: VideowallPlaybackGroup): GroupDraft => ({ key: group.id, id: group.id, name: group.name,
  mode: group.mode, campaign_id: group.campaign_id, node_ids: group.members.map((member) => member.node_id) });

function groupProblem(drafts: GroupDraft[], nodes: VideowallNode[], wall: Videowall, campaigns: Campaign[]) {
  const used = new Set<string>();
  for (const group of drafts) {
    if (!group.name.trim()) return "Cada grupo necesita un nombre.";
    if (!campaigns.some((campaign) => campaign.id === group.campaign_id && campaign.client_id === wall.client_id && campaign.is_active)) return "Selecciona una campaña activa propia para cada grupo.";
    const members = group.node_ids.map((id) => nodes.find((node) => node.id === id && placed(node, wall)));
    if (members.some((node) => !node)) return "Guarda primero la distribución física de los miembros seleccionados.";
    if (group.mode === "INDIVIDUAL" && members.length !== 1) return "Un grupo individual debe tener exactamente una pantalla.";
    if (group.mode === "SYNCHRONIZED" && members.length < 2) return "Un grupo sincronizado necesita al menos dos pantallas.";
    for (const id of group.node_ids) {
      if (used.has(id)) return "Una pantalla no puede pertenecer a dos grupos.";
      used.add(id);
    }
    if (group.mode === "SYNCHRONIZED") {
      const cells = members as VideowallNode[];
      const width = Math.max(...cells.map((node) => node.column_index)) - Math.min(...cells.map((node) => node.column_index)) + 1;
      const height = Math.max(...cells.map((node) => node.row_index)) - Math.min(...cells.map((node) => node.row_index)) + 1;
      if (width * height !== cells.length || new Set(cells.map((node) => `${node.row_index}:${node.column_index}`)).size !== cells.length) return "El grupo sincronizado debe formar un rectángulo contiguo, sin huecos.";
    }
  }
  return null;
}

export function VideowallPlaybackGroupsPanel({ wall, nodes, channels, token, canManage, onSaved }: {
  wall: Videowall; nodes: VideowallNode[]; channels: Channel[]; token: string | null; canManage: boolean;
  onSaved: (wall: Videowall) => Promise<void> | void;
}) {
  const [expanded, setExpanded] = useState(Boolean(wall.playback_groups_enabled));
  const [liveWall, setLiveWall] = useState(wall);
  const [liveNodes, setLiveNodes] = useState(nodes);
  const [rows, setRows] = useState(wall.rows);
  const [columns, setColumns] = useState(wall.columns);
  const [outputWidth, setOutputWidth] = useState(wall.output_width);
  const [outputHeight, setOutputHeight] = useState(wall.output_height);
  const [slots, setSlots] = useState<Record<number, string>>({});
  const [groups, setGroups] = useState<VideowallPlaybackGroup[]>([]);
  const [drafts, setDrafts] = useState<GroupDraft[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [publications, setPublications] = useState<VideowallPublication[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const scope = `${wall.id}:${wall.client_id}:${token ?? ""}`;
  const latestScope = useRef(scope); latestScope.current = scope;
  const serial = useRef(0), mounted = useRef(true), draftCounter = useRef(0);
  const editable = canManage && Boolean(token);
  const layoutValid = [rows, columns, outputWidth, outputHeight].every(positive) && rows * columns <= 256;
  const slotValues = layoutValid ? Array.from({ length: rows * columns }, (_, index) => slots[index] ?? "").filter(Boolean) : [];
  const uniqueSlots = new Set(slotValues).size === slotValues.length;
  const activeNodes = liveNodes.filter((node) => placed(node, liveWall)).sort((a, b) => a.position_index - b.position_index);
  const draftError = groupProblem(drafts, liveNodes, liveWall, campaigns);
  const geometryKey = `${wall.rows}:${wall.columns}:${wall.total_width}:${wall.total_height}:${wall.output_width}:${wall.output_height}`;

  function current(ticket: number, expectedScope: string) { return mounted.current && serial.current === ticket && latestScope.current === expectedScope; }
  function loadLayout(nextWall: Videowall, nextNodes: VideowallNode[]) {
    setLiveWall(nextWall); setLiveNodes(nextNodes);
    setRows(nextWall.rows); setColumns(nextWall.columns); setOutputWidth(nextWall.output_width); setOutputHeight(nextWall.output_height);
    setSlots(Object.fromEntries(nextNodes.filter((node) => placed(node, nextWall)).map((node) => [node.row_index * nextWall.columns + node.column_index, node.id])));
  }
  async function refresh(resetDrafts = false, nextWall = liveWall) {
    if (!token) return;
    const ticket = ++serial.current, expectedScope = scope;
    setBusy("refresh"); setError(null);
    try {
      const [nextGroups, nextCampaigns, nextNodes, nextPublications] = await Promise.all([
        apiRequest<VideowallPlaybackGroup[]>(`/videowalls/${wall.id}/playback-groups`, { token }),
        apiRequest<Campaign[]>(`/campaigns?client_id=${encodeURIComponent(wall.client_id)}`, { token }),
        apiRequest<VideowallNode[]>(`/videowalls/${wall.id}/nodes`, { token }),
        apiRequest<VideowallPublication[]>(`/videowalls/${wall.id}/publications`, { token }),
      ]);
      if (!current(ticket, expectedScope)) return;
      const own = nextGroups.filter((group) => group.wall_id === wall.id && group.client_id === wall.client_id);
      setGroups(own); setCampaigns(nextCampaigns.filter((campaign) => campaign.client_id === wall.client_id && campaign.is_active));
      setPublications(nextPublications.filter((publication) => publication.wall_id === wall.id && own.some((group) => group.id === publication.playback_group_id)));
      const ownedNodes = nextNodes.filter((node) => node.videowall_id === wall.id);
      if (resetDrafts) {
        loadLayout(nextWall, ownedNodes);
        setDrafts(own.filter((group) => group.members.length > 0).map(fromGroup));
      } else setLiveNodes(ownedNodes);
    } catch (nextError) { if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudo cargar el layout y los grupos.")); }
    finally { if (current(ticket, expectedScope)) setBusy(null); }
  }
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; serial.current += 1; }; }, []);
  useEffect(() => {
    serial.current += 1; setExpanded(Boolean(wall.playback_groups_enabled));
    loadLayout(wall, nodes); setGroups([]); setDrafts([]); setCampaigns([]); setPublications([]); setBusy(null); setError(null); setNotice(null);
  }, [scope]);
  useEffect(() => {
    serial.current += 1;
    if (expanded && token) void refresh(true, wall);
  }, [scope, expanded, geometryKey, wall.playback_groups_enabled]);

  async function saveLayout() {
    if (!editable || busy || !layoutValid || !uniqueSlots || slotValues.length === 0) return;
    const assignments = Object.entries(slots).filter(([index, id]) => id && Number(index) < rows * columns)
      .map(([index, node_id]) => ({ node_id, row_index: Math.floor(Number(index) / columns), column_index: Number(index) % columns }));
    if (assignments.some((item) => !liveNodes.some((node) => node.id === item.node_id && node.videowall_id === wall.id))) return;
    const ticket = ++serial.current, expectedScope = scope;
    setBusy("layout"); setError(null); setNotice(null);
    try {
      const saved = await apiRequest<Videowall>(`/videowalls/${wall.id}/physical-layout`, { method: "PUT", token,
        body: { rows, columns, total_width: columns * outputWidth, total_height: rows * outputHeight,
          output_width: outputWidth, output_height: outputHeight, nodes: assignments } });
      if (!current(ticket, expectedScope)) return;
      setLiveWall(saved);
      setNotice("Distribución guardada. Los canales y Players se conservan; configura los grupos y sus campañas.");
      await onSaved(saved);
      if (current(ticket, expectedScope)) await refresh(true, saved);
    } catch (nextError) { if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudo guardar la distribución.")); }
    finally { if (current(ticket, expectedScope)) setBusy(null); }
  }
  function editGroup(key: string, change: Partial<GroupDraft>) {
    if (!editable || busy) return;
    setDrafts((previous) => previous.map((draft) => draft.key === key ? { ...draft, ...change } : draft));
  }
  function preset(mode: VideowallPlaybackGroupMode) {
    if (!editable || busy || liveWall.rows !== 1 || liveWall.columns !== 2 || activeNodes.length !== 2) return;
    if (mode === "SYNCHRONIZED") {
      const candidates = groups.filter((group) => group.mode === mode);
      const saved = candidates.length === 1 ? candidates[0] : undefined;
      setDrafts([{ ...(saved ? fromGroup(saved) : { key: `draft-${++draftCounter.current}`, name: "Sincronizado 1 + 2", mode, campaign_id: "" }), node_ids: activeNodes.map((node) => node.id) }]);
    } else setDrafts(activeNodes.map((node, index) => {
      const saved = groups.find((group) => group.mode === mode && group.members.some((member) => member.node_id === node.id))
        ?? groups.find((group) => group.mode === mode && group.name === `Individual ${index + 1}` && group.members.length === 0);
      return { ...(saved ? fromGroup(saved) : { key: `draft-${++draftCounter.current}`, name: `Individual ${index + 1}`, mode, campaign_id: "" }), node_ids: [node.id] };
    }));
  }
  async function saveGroups() {
    if (!editable || busy || draftError || drafts.length === 0) return;
    const ticket = ++serial.current, expectedScope = scope;
    setBusy("groups"); setError(null); setNotice(null);
    try {
      const saved = await apiRequest<VideowallPlaybackGroup[]>(`/videowalls/${wall.id}/playback-groups`, { method: "PUT", token,
        body: { groups: drafts.map(({ id, name, mode, campaign_id, node_ids }) => ({ ...(id ? { id } : {}), name: name.trim(), mode, campaign_id, node_ids })) } });
      if (!current(ticket, expectedScope)) return;
      const own = saved.filter((group) => group.wall_id === wall.id && group.client_id === wall.client_id);
      setGroups(own); setDrafts(own.filter((group) => group.members.length > 0).map(fromGroup));
      setLiveWall((previous) => ({ ...previous, playback_groups_enabled: true }));
      setNotice("Grupos guardados sin reactivar dispositivos. Los individuales usan su campaña completa; los sincronizados requieren una publicación activa.");
      await onSaved({ ...liveWall, playback_groups_enabled: true });
    } catch (nextError) { if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudieron guardar los grupos.")); }
    finally { if (current(ticket, expectedScope)) setBusy(null); }
  }
  async function prepare(group: VideowallPlaybackGroup) {
    if (!editable || busy || group.mode !== "SYNCHRONIZED" || group.members.length < 2 || group.wall_id !== wall.id || group.client_id !== wall.client_id) return;
    const ticket = ++serial.current, expectedScope = scope;
    setBusy(group.id); setError(null); setNotice(null);
    try {
      const publication = await apiRequest<VideowallPublication>(`/videowalls/${wall.id}/publications`, { method: "POST", token,
        body: { campaign_id: group.campaign_id, playback_group_id: group.id } });
      if (!current(ticket, expectedScope)) return;
      if (publication.wall_id !== wall.id || publication.playback_group_id !== group.id) throw new Error("La publicación no pertenece al grupo solicitado.");
      setPublications((previous) => [publication, ...previous.filter((item) => item.id !== publication.id)]);
      setNotice("Preparación solicitada. Actualiza el estado y activa únicamente cuando esté lista.");
    } catch (nextError) { if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudo preparar la publicación del grupo.")); }
    finally { if (current(ticket, expectedScope)) setBusy(null); }
  }
  async function activate(publication: VideowallPublication) {
    const group = groups.find((item) => item.id === publication.playback_group_id && item.mode === "SYNCHRONIZED" && item.members.length >= 2);
    if (!editable || busy || !group || publication.wall_id !== wall.id || publication.status !== "READY" || publication.is_current_active) return;
    const ticket = ++serial.current, expectedScope = scope;
    setBusy(publication.id); setError(null); setNotice(null);
    try {
      const activated = await apiRequest<VideowallPublication>(`/videowalls/${wall.id}/publications/${publication.id}/activate`, { method: "POST", token });
      if (!current(ticket, expectedScope)) return;
      if (activated.wall_id !== wall.id || activated.playback_group_id !== group.id) throw new Error("La publicación no pertenece al grupo solicitado.");
      setPublications((previous) => previous.map((item) => item.id === activated.id ? activated : item.playback_group_id === group.id ? { ...item, is_current_active: false } : item));
      setGroups((previous) => previous.map((item) => item.id === group.id ? { ...item, active_publication_id: activated.id } : item));
      setNotice("Publicación activada para este grupo. Los Players adoptarán el runtime mediante su refresco habitual.");
    } catch (nextError) { if (current(ticket, expectedScope)) setError(getApiErrorMessage(nextError, "No se pudo activar la publicación.")); }
    finally { if (current(ticket, expectedScope)) setBusy(null); }
  }

  const nodeName = (node: VideowallNode) => channels.find((channel) => channel.id === node.channel_id)?.name ?? node.channel_id;
  return <section className="space-y-4 rounded-[24px] border border-slate-200 bg-white p-5" aria-label="Layout físico y grupos de reproducción">
    <h3 className="font-semibold text-ink">Layout físico y grupos de reproducción</h3>
    <p className="text-sm text-slate-600">Reubicar o agrupar conserva los canales y Players existentes; no solicita otra activación.</p>
    {!expanded ? <button type="button" disabled={!token} onClick={() => setExpanded(true)} className="rounded-xl border px-4 py-2">Configurar layout y grupos</button> : <>
      {!canManage ? <p className="text-sm text-slate-500">Este rol sólo puede consultar la configuración.</p> : null}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[{ name: "physical_rows", label: "Filas físicas", value: rows, setter: setRows }, { name: "physical_columns", label: "Columnas físicas", value: columns, setter: setColumns },
          { name: "physical_output_width", label: "Ancho por pantalla", value: outputWidth, setter: setOutputWidth }, { name: "physical_output_height", label: "Alto por pantalla", value: outputHeight, setter: setOutputHeight }].map((field) =>
          <label key={field.name} className="text-sm">{field.label}<input name={field.name} type="number" min={1} value={field.value} disabled={!editable || Boolean(busy)} onChange={(event) => field.setter(Number(event.target.value))} /></label>)}
      </div>
      <p className="text-sm">Canvas físico: {columns * outputWidth} × {rows * outputHeight}. Guardar la distribución habilita los grupos; los nodos sin grupo pueden quedar sin campaña.</p>
      {layoutValid ? <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {Array.from({ length: rows * columns }, (_, index) => <label key={index} className="rounded-xl border border-slate-200 p-3 text-sm">Posición física {index + 1}
          <select name={`physical_node_${index + 1}`} value={slots[index] ?? ""} disabled={!editable || Boolean(busy)} onChange={(event) => setSlots((previous) => ({ ...previous, [index]: event.target.value }))}>
            <option value="">Sin asignar</option>{liveNodes.map((node) => <option key={node.id} value={node.id}>{nodeName(node)}</option>)}
          </select></label>)}
      </div> : <p role="alert">Usa dimensiones enteras positivas y un máximo de 256 posiciones para este editor.</p>}
      {!uniqueSlots ? <p role="alert">Cada nodo puede ocupar una sola posición física.</p> : null}
      <p className="text-xs text-slate-500">Fuera del layout: {liveNodes.filter((node) => !slotValues.includes(node.id)).map(nodeName).join(", ") || "ninguno"}. Se conservarán sin eliminar su identidad.</p>
      <button type="button" onClick={() => void saveLayout()} disabled={!editable || Boolean(busy) || !layoutValid || !uniqueSlots || slotValues.length === 0} className="rounded-xl border px-4 py-2">{busy === "layout" ? "Guardando distribución..." : "Guardar distribución física"}</button>
      <hr className="border-slate-200" />
      <p className="text-sm text-slate-600">Selecciona miembros sobre la distribución ya guardada. La posición local se calcula dentro de cada grupo; no cambia su posición física.</p>
      <div className="flex flex-wrap gap-2">
        {liveWall.rows === 1 && liveWall.columns === 2 ? <>
          <button type="button" disabled={!editable || Boolean(busy) || activeNodes.length !== 2} onClick={() => preset("SYNCHRONIZED")} className="rounded-xl border px-4 py-2">Sincronizar 1 + 2</button>
          <button type="button" disabled={!editable || Boolean(busy) || activeNodes.length !== 2} onClick={() => preset("INDIVIDUAL")} className="rounded-xl border px-4 py-2">Dos individuales</button>
        </> : null}
        <button type="button" disabled={!editable || Boolean(busy)} onClick={() => { if (editable && !busy) setDrafts((previous) => [...previous, { key: `draft-${++draftCounter.current}`, name: `Grupo ${previous.length + 1}`, mode: "SYNCHRONIZED", campaign_id: "", node_ids: [] }]); }} className="rounded-xl border px-4 py-2">Añadir grupo</button>
        <button type="button" disabled={!token || Boolean(busy)} onClick={() => void refresh(false)} className="rounded-xl border px-4 py-2">Actualizar grupos y publicaciones</button>
      </div>
      {drafts.map((draft) => <article key={draft.key} data-group-key={draft.key} className="space-y-3 rounded-xl border border-slate-200 p-4">
        <label className="block text-sm">Nombre del grupo<input name={`group_name_${draft.key}`} value={draft.name} disabled={!editable || Boolean(busy)} onChange={(event) => editGroup(draft.key, { name: event.target.value })} /></label>
        <label className="block text-sm">Reproducción<select name={`group_mode_${draft.key}`} value={draft.mode} disabled={!editable || Boolean(busy)} onChange={(event) => editGroup(draft.key, { mode: event.target.value as VideowallPlaybackGroupMode })}>
          <option value="INDIVIDUAL">Individual</option><option value="SYNCHRONIZED">Sincronizado</option></select></label>
        <label className="block text-sm">Campaña<select name={`group_campaign_${draft.key}`} value={draft.campaign_id} disabled={!editable || Boolean(busy)} onChange={(event) => editGroup(draft.key, { campaign_id: event.target.value })}>
          <option value="">Selecciona una campaña</option>{campaigns.map((campaign) => <option key={campaign.id} value={campaign.id}>{campaign.name}</option>)}</select></label>
        <div className="flex flex-wrap gap-3">{activeNodes.map((node) => <label key={node.id} className="text-sm"><input type="checkbox" name={`group_member_${draft.key}_${node.id}`} checked={draft.node_ids.includes(node.id)} disabled={!editable || Boolean(busy)}
          onChange={(event) => editGroup(draft.key, { node_ids: event.target.checked ? [...draft.node_ids, node.id] : draft.node_ids.filter((id) => id !== node.id) })} /> Posición física {node.position_index}: {nodeName(node)}</label>)}</div>
        <p className="text-xs text-slate-500">{draft.mode === "INDIVIDUAL" ? "Campaña completa, sin recorte ni dependencia de sincronización." : "Miembros en rectángulo contiguo. El master debe coincidir con el canvas del grupo."}</p>
        <button type="button" disabled={!editable || Boolean(busy)} onClick={() => { if (editable && !busy) setDrafts((previous) => previous.filter((item) => item.key !== draft.key)); }} className="text-sm text-rose-700">Quitar del conjunto</button>
      </article>)}
      {draftError ? <p role="alert" className="text-sm text-amber-800">{draftError}</p> : null}
      <button type="button" disabled={!editable || Boolean(busy) || Boolean(draftError) || drafts.length === 0} onClick={() => void saveGroups()} className="rounded-xl bg-ink px-4 py-2 text-white">{busy === "groups" ? "Guardando grupos..." : "Guardar grupos"}</button>
      {groups.some((group) => group.members.length === 0) ? <div className="space-y-2"><p className="text-sm">Grupos guardados sin miembros: puedes reutilizarlos al regresar a sincronizado.</p>
        {groups.filter((group) => group.members.length === 0 && !drafts.some((draft) => draft.id === group.id)).map((group) => <button key={group.id} type="button" disabled={!editable || Boolean(busy)}
          onClick={() => { if (editable && !busy) setDrafts((previous) => [...previous, fromGroup(group)]); }} className="mr-2 rounded-xl border px-3 py-2 text-sm">Usar {group.name}</button>)}
      </div> : null}
      {groups.some((group) => group.members.length > 0) ? <h4 className="font-semibold text-ink">Configuración guardada</h4> : null}
      {groups.filter((group) => group.members.length > 0).map((group) => <article key={group.id} data-saved-group-id={group.id} className="space-y-2 rounded-xl bg-slate-50 p-4">
        <h4 className="font-semibold">{group.name} · {group.mode === "INDIVIDUAL" ? "Individual" : "Sincronizado"}</h4>
        <p className="text-sm">Campaña: {campaigns.find((campaign) => campaign.id === group.campaign_id)?.name ?? group.campaign_id} · Canvas {group.total_width} × {group.total_height}</p>
        {group.members.map((member) => <p key={member.id} className="text-xs text-slate-600">Posición física {member.physical_position_index} → posición local {member.position_index}</p>)}
        {group.mode === "INDIVIDUAL" ? <p className="text-sm">Pantalla completa. No necesita publicación sincronizada.</p> : <>
          <button type="button" disabled={!editable || Boolean(busy)} onClick={() => void prepare(group)} className="rounded-xl border px-3 py-2 text-sm">Preparar publicación de {group.name}</button>
          {publications.filter((publication) => publication.playback_group_id === group.id).map((publication) => <div key={publication.id} data-publication-id={publication.id} className="rounded-xl border p-3 text-sm">
            <p>{publication.is_current_active ? "Activa actual" : statuses[publication.status]} · {publication.validated_asset_count}/{publication.expected_asset_count} archivos validados</p>
            {publication.error_message || publication.error_code ? <p role="alert">{publication.error_message || publication.error_code}</p> : null}
            {publication.status === "READY" ? <button type="button" disabled={!editable || Boolean(busy) || publication.is_current_active} onClick={() => void activate(publication)} className="mt-2 rounded-xl border px-3 py-2">Activar publicación de {group.name}</button> : null}
          </div>)}
        </>}
      </article>)}
    </>}
    {error ? <p role="alert" className="text-sm text-rose-700">{error}</p> : null}
    {notice ? <p role="status" className="text-sm text-emerald-700">{notice}</p> : null}
  </section>;
}
