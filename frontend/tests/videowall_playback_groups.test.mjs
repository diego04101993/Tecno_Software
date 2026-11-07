// React renderer only: synthetic requests, no backend, Player, or activation calls.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const deps = process.env.SCREEN_ROTATION_TEST_DEPS;
assert.ok(deps, "Use isolated React/test-renderer 18.3.1 dependencies");
const testRequire = createRequire(join(resolve(deps), "package.json"));
const React = testRequire("react"), { create, act } = testRequire("react-test-renderer");
assert.equal(React.version, "18.3.1");
const appRequire = createRequire(join(frontend, "package.json"));
const { buildSync, transformSync } = createRequire(appRequire.resolve("vite/package.json"))("esbuild");
const scratch = mkdtempSync(join(resolve(deps), "vw-flexible-"));
const bundle = join(scratch, "groups.cjs");
buildSync({ entryPoints: [join(frontend, "src/components/dashboard/VideowallPlaybackGroupsPanel.tsx")], outfile: bundle,
  bundle: true, platform: "node", format: "cjs", jsx: "automatic", external: ["react", "react/jsx-runtime"],
  define: { "import.meta.env": JSON.stringify({ DEV: true, PROD: false }) } });
const { VideowallPlaybackGroupsPanel: Panel } = testRequire(bundle);
const originalFetch = globalThis.fetch;
after(() => { globalThis.fetch = originalFetch; assert.equal(dirname(resolve(scratch)), resolve(deps)); rmSync(scratch, { recursive: true, force: true }); });

function wall(extra = {}) { return { id: "wall-a", client_id: "client-a", name: "Physical wall", render_mode: "multi-node", rows: 1, columns: 2,
  total_width: 3840, total_height: 1080, output_width: 1920, output_height: 1080, playback_groups_enabled: true, ...extra }; }
function nodes(w = wall()) { return [1, 2, 3].map((position) => ({ id: `node-${position}`, videowall_id: w.id, channel_id: `channel-${position}`,
  layout_enabled: position <= w.columns, position_index: position, row_index: 0, column_index: position - 1, x: (position - 1) * 1920, y: 0, width: 1920, height: 1080 })); }
function group(extra = {}) { const ids = extra.node_ids ?? ["node-1", "node-2"]; return { id: "sync-saved", client_id: "client-a", branch_id: "branch-a", wall_id: "wall-a",
  name: "Sync saved", mode: "SYNCHRONIZED", campaign_id: "campaign-sync", active_publication_id: "old-publication", rows: 1, columns: ids.length,
  total_width: ids.length * 1920, total_height: 1080, output_width: 1920, output_height: 1080,
  members: ids.map((id, index) => ({ id: `member-${id}`, group_id: extra.id ?? "sync-saved", node_id: id, channel_id: id.replace("node", "channel"),
    position_index: index + 1, row_index: 0, column_index: index, physical_position_index: Number(id.split("-")[1]), physical_row_index: 0, physical_column_index: Number(id.split("-")[1]) - 1 })), ...extra }; }
function publication(extra = {}) { return { id: "publication-ready", wall_id: "wall-a", playback_group_id: "sync-saved", campaign_id: "campaign-sync", status: "READY",
  is_current_active: false, error_code: null, error_message: null, expected_asset_count: 2, validated_asset_count: 2, loop_duration_ms: 10000, loop_start_epoch_ms: null, ...extra }; }
function response(value, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
function text(node) { return typeof node === "string" ? node : (node.children ?? []).map(text).join(""); }
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
async function mount({ w = wall(), groupList = [group()], pubs = [], canManage = true, token = "synthetic-only", handler } = {}) {
  let currentGroups = groupList, nodeList = nodes(w);
  let props = { wall: w, nodes: nodeList, channels: nodeList.map((node, index) => ({ id: node.channel_id, name: `Screen ${index + 1}` })), token, canManage,
    onSaved: (saved) => saves.push(saved) };
  const requests = [], saves = [];
  globalThis.fetch = async (url, options) => {
    const base = "http://localhost:8000/api";
    assert.ok(String(url).startsWith(base + "/"));
    const request = { path: String(url).slice(base.length), method: options.method, body: options.body ? JSON.parse(options.body) : undefined };
    assert.ok(!/activate$/.test(request.path) || request.path.includes("/publications/"), "No device activation request");
    requests.push(request);
    if (handler) { const result = await handler(request); if (result !== undefined) return result; }
    if (request.method === "GET" && request.path.endsWith("/playback-groups")) return response(currentGroups);
    if (request.method === "GET" && request.path.endsWith("/nodes")) return response(nodeList);
    if (request.method === "GET" && request.path.endsWith("/publications")) return response(pubs);
    if (request.method === "GET" && request.path.startsWith("/campaigns?")) return response([
      { id: "campaign-sync", name: "Sync campaign", client_id: props.wall.client_id, is_active: true },
      { id: "campaign-1", name: "Individual 1", client_id: props.wall.client_id, is_active: true },
      { id: "campaign-2", name: "Individual 2", client_id: props.wall.client_id, is_active: true },
      { id: "foreign", name: "Foreign", client_id: "other", is_active: true },
      { id: "disabled", name: "Disabled", client_id: props.wall.client_id, is_active: false },
    ]);
    if (request.method === "PUT" && request.path.endsWith("/physical-layout")) {
      const saved = { ...props.wall, ...request.body, playback_groups_enabled: true };
      nodeList = nodeList.map((node) => { const item = request.body.nodes.find((entry) => entry.node_id === node.id); return item ? { ...node, ...item,
        position_index: item.row_index * saved.columns + item.column_index + 1, layout_enabled: true } : { ...node, layout_enabled: false }; });
      return response(saved);
    }
    if (request.method === "PUT" && request.path.endsWith("/playback-groups")) {
      const active = request.body.groups.map((draft, index) => group({ ...draft, id: draft.id ?? `new-${index}` }));
      currentGroups = [...active, ...currentGroups.filter((saved) => !active.some((item) => item.id === saved.id)).map((saved) => ({ ...saved, members: [] }))];
      return response(currentGroups);
    }
    if (request.method === "POST" && request.path.endsWith("/publications")) return response(publication({ status: "PENDING", ...request.body }), 201);
    if (request.method === "POST" && request.path.endsWith("/activate")) return response(publication({ status: "ACTIVE", is_current_active: true, loop_start_epoch_ms: 100000 }));
    assert.fail(`Unexpected synthetic request ${request.method} ${request.path}`);
  };
  let view;
  await act(async () => { view = create(React.createElement(Panel, props)); });
  return { view, requests, saves, content: () => text(view.root), control: (name) => view.root.findByProps({ name }),
    button: (label) => view.root.findAllByType("button").find((button) => text(button) === label),
    writes: () => requests.filter((request) => request.method !== "GET"),
    update: async (next) => { props = { ...props, ...next }; await act(async () => view.update(React.createElement(Panel, props))); },
    close: async () => act(async () => view.unmount()) };
}
async function click(button) { assert.ok(button, "Expected button"); await act(async () => button.props.onClick()); }
async function change(control, value, checkbox = false) { await act(async () => control.props.onChange({ target: checkbox ? { checked: value } : { value } })); }

test("legacy remains collapsed and performs no requests or implicit opt-in", async () => {
  const panel = await mount({ w: wall({ playback_groups_enabled: false }) });
  assert.deepEqual(panel.requests, []);
  assert.ok(panel.button("Configurar layout y grupos"));
  await click(panel.button("Configurar layout y grupos"));
  assert.equal(panel.writes().length, 0);
  assert.match(panel.content(), /no solicita otra activación/);
  await panel.close();
});

test("1x3 to1x2 uses stable node IDs and omits preserved third node without delete or activation", async () => {
  const panel = await mount({ w: wall({ columns: 3, total_width: 5760 }) });
  await change(panel.control("physical_columns"), "2");
  await click(panel.button("Guardar distribución física"));
  assert.deepEqual(panel.writes(), [{ path: "/videowalls/wall-a/physical-layout", method: "PUT", body: { rows: 1, columns: 2,
    total_width: 3840, total_height: 1080, output_width: 1920, output_height: 1080,
    nodes: [{ node_id: "node-1", row_index: 0, column_index: 0 }, { node_id: "node-2", row_index: 0, column_index: 1 }] } }]);
  assert.match(panel.content(), /Screen 3.*Se conservarán/);
  assert.equal(panel.saves[0].playback_groups_enabled, true);
  await panel.close();
});

test("duplicate physical assignment is blocked without a write", async () => {
  const panel = await mount();
  await change(panel.control("physical_node_2"), "node-1");
  assert.equal(panel.button("Guardar distribución física").props.disabled, true);
  await click(panel.button("Guardar distribución física"));
  assert.deepEqual(panel.writes(), []);
  await panel.close();
});

test("two individual preset saves distinct campaigns in one atomic group replacement", async () => {
  const panel = await mount();
  await click(panel.button("Dos individuales"));
  const selectors = panel.view.root.findAllByType("select").filter((item) => item.props.name?.startsWith("group_campaign_"));
  assert.deepEqual(selectors[0].findAllByType("option").map((option) => option.props.value), ["", "campaign-sync", "campaign-1", "campaign-2"]);
  await change(selectors[0], "campaign-1"); await change(selectors[1], "campaign-2");
  await click(panel.button("Guardar grupos"));
  assert.deepEqual(panel.writes()[0], { path: "/videowalls/wall-a/playback-groups", method: "PUT", body: { groups: [
    { name: "Individual 1", mode: "INDIVIDUAL", campaign_id: "campaign-1", node_ids: ["node-1"] },
    { name: "Individual 2", mode: "INDIVIDUAL", campaign_id: "campaign-2", node_ids: ["node-2"] },
  ] } });
  assert.match(panel.content(), /Pantalla completa. No necesita publicación sincronizada/);
  assert.ok(!panel.writes().some((request) => request.method === "DELETE" || request.path.includes("/activate")));
  await panel.close();
});

test("return to sync reuses historical group ID and campaign without device activation or client epoch", async () => {
  const historical = group({ members: [], rows: 0, columns: 0, total_width: 0 });
  const panel = await mount({ groupList: [historical, group({ id: "individual-1", mode: "INDIVIDUAL", node_ids: ["node-1"], campaign_id: "campaign-1" }),
    group({ id: "individual-2", mode: "INDIVIDUAL", node_ids: ["node-2"], campaign_id: "campaign-2" })] });
  await click(panel.button("Sincronizar 1 + 2")); await click(panel.button("Guardar grupos"));
  assert.deepEqual(panel.writes()[0].body, { groups: [{ id: "sync-saved", name: "Sync saved", mode: "SYNCHRONIZED", campaign_id: "campaign-sync", node_ids: ["node-1", "node-2"] }] });
  assert.equal(panel.writes().length, 1);
  assert.ok(!JSON.stringify(panel.writes()).includes("epoch"));
  await panel.close();
});

test("mixed1x3 separates individual physical1 from synced physical2,3 and displays local1,2", async () => {
  const panel = await mount({ w: wall({ columns: 3, total_width: 5760 }), groupList: [group({ id: "individual", name: "Individual", mode: "INDIVIDUAL", node_ids: ["node-1"], campaign_id: "campaign-1" }),
    group({ node_ids: ["node-2", "node-3"] })] });
  assert.match(panel.content(), /Posición física 2 → posición local 1/);
  assert.match(panel.content(), /Posición física 3 → posición local 2/);
  await click(panel.button("Guardar grupos"));
  assert.deepEqual(panel.writes()[0].body.groups.map((item) => [item.mode, item.node_ids]), [["INDIVIDUAL", ["node-1"]], ["SYNCHRONIZED", ["node-2", "node-3"]]]);
  await panel.close();
});

test("nonrectangular, overlapping and individual-multiple memberships cannot be saved", async () => {
  const cases = [
    [group({ node_ids: ["node-1", "node-3"] })],
    [group(), group({ id: "overlap", mode: "INDIVIDUAL", node_ids: ["node-1"] })],
    [group({ mode: "INDIVIDUAL" })],
  ];
  for (const groupList of cases) {
    const panel = await mount({ w: wall({ columns: 3, total_width: 5760 }), groupList });
    assert.equal(panel.button("Guardar grupos").props.disabled, true);
    await click(panel.button("Guardar grupos"));
    assert.deepEqual(panel.writes(), []);
    await panel.close();
  }
});

test("group publication prepare and activate retain the approved API and send no geometry or epoch", async () => {
  const panel = await mount({ pubs: [publication()] });
  await click(panel.button("Preparar publicación de Sync saved"));
  assert.deepEqual(panel.writes()[0], { path: "/videowalls/wall-a/publications", method: "POST", body: { campaign_id: "campaign-sync", playback_group_id: "sync-saved" } });
  await click(panel.button("Actualizar grupos y publicaciones"));
  await click(panel.button("Activar publicación de Sync saved"));
  assert.deepEqual(panel.writes()[1], { path: "/videowalls/wall-a/publications/publication-ready/activate", method: "POST", body: undefined });
  assert.match(panel.content(), /Activa actual/);
  await panel.close();
});

test("failed publication has no activation and leaves group and existing configuration usable", async () => {
  const panel = await mount({ pubs: [publication({ status: "FAILED", error_message: "Master incompatible" })] });
  assert.match(panel.content(), /Master incompatible/);
  assert.equal(panel.button("Activar publicación de Sync saved"), undefined);
  await panel.close();
});

test("read-only or missing-token direct handlers cannot write", async () => {
  for (const restriction of [{ canManage: false }, { token: null }]) {
    const panel = await mount({ ...restriction, pubs: [publication()] });
    await click(panel.button("Guardar distribución física")); await click(panel.button("Guardar grupos"));
    await click(panel.button("Sincronizar 1 + 2"));
    if (panel.button("Preparar publicación de Sync saved")) await click(panel.button("Preparar publicación de Sync saved"));
    if (panel.button("Activar publicación de Sync saved")) await click(panel.button("Activar publicación de Sync saved"));
    assert.deepEqual(panel.writes(), []);
    if (restriction.token === null) assert.deepEqual(panel.requests, []);
    await panel.close();
  }
});

test("failed group save preserves draft campaign and allows a safe retry", async () => {
  let fails = true;
  const panel = await mount({ handler: (request) => request.method === "PUT" && fails ? response({ detail: "Grupo rechazado" }, 409) : undefined });
  await change(panel.control("group_campaign_sync-saved"), "campaign-1");
  await click(panel.button("Guardar grupos"));
  assert.match(panel.content(), /Grupo rechazado/);
  assert.equal(panel.control("group_campaign_sync-saved").props.value, "campaign-1");
  fails = false; await click(panel.button("Guardar grupos"));
  assert.equal(panel.writes().length, 2);
  await panel.close();
});

test("refresh and unrelated wall rename preserve draft physical dimensions and group campaign", async () => {
  const panel = await mount();
  await change(panel.control("physical_columns"), "3");
  await change(panel.control("group_campaign_sync-saved"), "campaign-1");
  await panel.update({ wall: wall({ name: "Renamed" }) });
  await click(panel.button("Actualizar grupos y publicaciones"));
  assert.equal(panel.control("physical_columns").props.value, 3);
  assert.equal(panel.control("group_campaign_sync-saved").props.value, "campaign-1");
  assert.deepEqual(panel.writes(), []);
  await panel.close();
});

test("foreign groups and publications do not appear in the current wall", async () => {
  const panel = await mount({ groupList: [group(), group({ id: "foreign-group", client_id: "other", name: "Foreign secret" })],
    pubs: [publication({ playback_group_id: "foreign-group", error_message: "Foreign publication" })] });
  assert.ok(!panel.content().includes("Foreign secret"));
  assert.ok(!panel.content().includes("Foreign publication"));
  await panel.close();
});

test("late load for another tenant cannot populate or save the selected wall", async () => {
  const pending = deferred();
  const panel = await mount({ handler: (request) => request.path === "/videowalls/wall-a/playback-groups" ? pending.promise : undefined });
  await panel.update({ wall: wall({ id: "wall-b", client_id: "client-b" }), nodes: [] });
  await act(async () => pending.resolve(response([group({ name: "Stale secret" })])));
  assert.ok(!panel.content().includes("Stale secret")); assert.deepEqual(panel.saves, []);
  await panel.close();
});

test("late group save and publication activation cannot notify a different wall", async () => {
  for (const action of ["save", "activate"]) {
    const pending = deferred();
    const panel = await mount({ pubs: [publication()], handler: (request) => (action === "save" ? request.method === "PUT" : request.path.endsWith("/activate")) ? pending.promise : undefined });
    await click(panel.button(action === "save" ? "Guardar grupos" : "Activar publicación de Sync saved"));
    await panel.update({ wall: wall({ id: "wall-b", client_id: "client-b" }), nodes: [] });
    await act(async () => pending.resolve(response(action === "save" ? [group()] : publication({ status: "ACTIVE" }))));
    assert.deepEqual(panel.saves, []); assert.ok(!panel.content().includes("Publicación activada para este grupo"));
    await panel.close();
  }
});

test("both interfaces keep legacy path and integrate the shared flexible editor", () => {
  for (const path of ["src/pages/VideowallPage.tsx", "src/components/dashboard/VideowallEditDrawer.tsx"]) {
    const source = readFileSync(join(frontend, path), "utf8");
    assert.match(source, /<VideowallPlaybackGroupsPanel/); assert.match(source, /!.*playback_groups_enabled.*VideowallPublicationsPanel/);
  }
  const drawer = readFileSync(join(frontend, "src/components/dashboard/VideowallEditDrawer.tsx"), "utf8");
  assert.match(drawer, /body: activeVideowall.playback_groups_enabled \? \{ name: name.trim\(\) \} :/);
});

test("actual dashboard save handler refreshes only the matching open drawer identity and topology", async () => {
  const source = readFileSync(join(frontend, "src/pages/client/ClientOverviewPage.tsx"), "utf8");
  const actual = source.match(/async function handleVideowallSaved\([\s\S]*?(?=\n  async function handleVideowallMonitorSaved)/)?.[0];
  assert.ok(actual);
  const code = transformSync(`${actual}\nmodule.exports = handleVideowallSaved;`, { loader: "ts", format: "cjs" }).code;
  for (const selectedId of ["wall-a", "wall-other"]) {
    let selected = { branch: { id: "branch-stable" }, videowall: wall({ id: selectedId, columns: 3, playback_groups_enabled: false }) };
    const old = selected, saved = wall({ columns: 2, playback_groups_enabled: true });
    let loads = 0;
    const module = { exports: {} };
    new Function("setVideowallEditTarget", "loadDashboard", "setFeedback", "module", code)(
      (change) => { selected = change(selected); }, async () => { loads += 1; }, () => {}, module);
    await module.exports(saved);
    assert.equal(loads, 1);
    if (selectedId === "wall-a") { assert.equal(selected.videowall, saved); assert.equal(selected.branch, old.branch); }
    else assert.equal(selected, old);
  }
});
