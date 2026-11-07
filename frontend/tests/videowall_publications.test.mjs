// Isolated React renderer; all HTTP requests below use synthetic local fixtures.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const deps = process.env.SCREEN_ROTATION_TEST_DEPS;
assert.ok(deps, "Set SCREEN_ROTATION_TEST_DEPS to isolated React/test-renderer 18.3.1");
const testRequire = createRequire(join(resolve(deps), "package.json"));
const React = testRequire("react");
const { create, act } = testRequire("react-test-renderer");
assert.equal(React.version, "18.3.1");
const appRequire = createRequire(join(frontend, "package.json"));
const viteRequire = createRequire(appRequire.resolve("vite/package.json"));
const { buildSync } = viteRequire("esbuild");
const scratch = mkdtempSync(join(resolve(deps), "videowall-publications-"));
const bundle = join(scratch, "panel.cjs");
buildSync({ entryPoints: [join(frontend, "src/components/dashboard/VideowallPublicationsPanel.tsx")],
  outfile: bundle, bundle: true, platform: "node", format: "cjs", jsx: "automatic",
  external: ["react", "react/jsx-runtime"], define: { "import.meta.env": JSON.stringify({ DEV: true, PROD: false }) } });
const { VideowallPublicationsPanel: Panel } = testRequire(bundle);
const originalFetch = globalThis.fetch;
after(() => {
  globalThis.fetch = originalFetch;
  assert.equal(dirname(resolve(scratch)), resolve(deps));
  rmSync(scratch, { recursive: true, force: true });
});

function wall(extra = {}) { return { id: "wall-a", client_id: "client-a", name: "Wall A", render_mode: "multi-node",
  rows: 1, columns: 3, total_width: 5760, total_height: 1080, output_width: 1920, output_height: 1080,
  primary_channel_id: null, sync_mode: "play_at_timestamp", start_tolerance_ms: 250, ...extra }; }
function nodes(w = wall()) { return [1, 2, 3].map((position) => ({ id: `node-${position}`, videowall_id: w.id,
  channel_id: `channel-${w.id}-${position}`, position_index: position, row_index: 0, column_index: position - 1 })); }
function publication(extra = {}) { return { id: "pub-a", wall_id: "wall-a", campaign_id: "campaign-a", status: "READY",
  error_code: null, error_message: null, expected_asset_count: 3, validated_asset_count: 3,
  loop_duration_ms: 10000, loop_start_epoch_ms: null, created_at: "2026-11-04T00:00:00+00:00", updated_at: null,
  activated_at: null, is_current_active: false, ...extra }; }
function response(value, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
function text(node) { return typeof node === "string" ? node : (node.children ?? []).map(text).join(""); }
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }

async function mount({ w = wall({ media_delivery: "node_slice" }), pubs = [], nodeList, canManage = true, token = "fixture-only", handler } = {}) {
  const requests = [], saved = [];
  let props = { wall: w, nodes: nodeList ?? nodes(w), canManage, token, onSaved: (value) => saved.push(value) };
  globalThis.fetch = async (url, options) => {
    assert.ok(String(url).startsWith("http://localhost:8000/api/"));
    const request = { path: String(url).replace("http://localhost:8000/api", ""), method: options.method,
      body: options.body ? JSON.parse(options.body) : undefined };
    requests.push(request);
    if (handler) { const result = await handler(request); if (result !== undefined) return result; }
    if (request.method === "GET" && request.path.endsWith("/publications")) return response(pubs);
    if (request.method === "GET" && request.path.startsWith("/campaigns?")) return response([
      { id: "campaign-a", client_id: props.wall.client_id, name: "Campaign A", is_active: true },
      { id: "foreign-campaign", client_id: "foreign-client", name: "Foreign", is_active: true },
      { id: "inactive", client_id: props.wall.client_id, name: "Inactive", is_active: false },
    ]);
    if (request.method === "PATCH") return response({ ...props.wall, ...request.body });
    if (request.path.endsWith("/activate")) return response(publication({ status: "ACTIVE", is_current_active: true, loop_start_epoch_ms: 1793750410000 }));
    if (request.method === "POST" && request.path.endsWith("/publications")) return response(publication({ status: "PENDING", validated_asset_count: 0, loop_duration_ms: null }), 201);
    assert.fail(`Unexpected fixture request: ${request.method} ${request.path}`);
  };
  let view;
  await act(async () => { view = create(React.createElement(Panel, props)); });
  return { view, requests, saved,
    button: (label) => view.root.findAllByType("button").find((button) => text(button) === label),
    select: (name) => view.root.findByProps({ name }),
    writes: () => requests.filter((request) => request.method !== "GET"),
    content: () => text(view.root),
    update: async (next) => { props = { ...props, ...next }; await act(async () => view.update(React.createElement(Panel, props))); },
    close: async () => act(async () => view.unmount()),
  };
}
async function click(button) { assert.ok(button, "Expected button"); await act(async () => button.props.onClick()); }
async function change(select, value) { await act(async () => select.props.onChange({ target: { value } })); }

test("legacy mounts unchanged without fetching publications or writing", async () => {
  const panel = await mount({ w: wall() });
  assert.equal(panel.select("media_delivery").props.value, "legacy");
  assert.equal(panel.button("Guardar modo").props.disabled, true);
  assert.deepEqual(panel.requests, []);
  assert.match(panel.content(), /no cambian automáticamente/);
  await panel.close();
});

test("mode switch sends only explicit media_delivery and loads own active campaigns", async () => {
  const panel = await mount({ w: wall() });
  await change(panel.select("media_delivery"), "node_slice");
  assert.equal(panel.writes().length, 0);
  await click(panel.button("Guardar modo"));
  assert.deepEqual(panel.writes(), [{ path: "/videowalls/wall-a", method: "PATCH", body: { media_delivery: "node_slice" } }]);
  assert.deepEqual(panel.select("publication_campaign_id").findAllByType("option").map((item) => item.props.value), ["", "campaign-a"]);
  assert.equal(panel.saved[0].media_delivery, "node_slice");
  await panel.close();
});

test("normal geometry cannot be silently converted to node slices", async () => {
  const panel = await mount({ w: wall({ rows: 2, columns: 2 }) });
  await change(panel.select("media_delivery"), "node_slice");
  assert.equal(panel.button("Guardar modo").props.disabled, true);
  await click(panel.button("Guardar modo"));
  assert.deepEqual(panel.writes(), []);
  assert.match(panel.content(), /Guarda primero/);
  await panel.close();
});

test("FullHD and 4K show exact 1x3 dimensions and no physical 4K approval", async () => {
  for (const [width, height, panelWidth] of [[5760, 1080, 1920], [11520, 2160, 3840]]) {
    const panel = await mount({ w: wall({ total_width: width, total_height: height, media_delivery: "node_slice" }) });
    assert.ok(panel.content().includes(`Canvas ${width} × ${height} · Panel ${panelWidth} × ${height}`));
    assert.match(panel.content(), /aprobación física de 4K sigue pendiente/);
    assert.match(panel.content(), /no debe volver a aplicar crop/);
    await panel.close();
  }
});

test("preparation requires three different channels in historical positions 1,2,3", async () => {
  for (const invalid of [nodes().slice(0, 2), nodes().map((node) => ({ ...node, channel_id: "same" })), nodes().map((node) => ({ ...node, position_index: node.position_index - 1 }))]) {
    const panel = await mount({ nodeList: invalid });
    await change(panel.select("publication_campaign_id"), "campaign-a");
    assert.equal(panel.button("Preparar publicación").props.disabled, true);
    await click(panel.button("Preparar publicación"));
    assert.deepEqual(panel.writes(), []);
    await panel.close();
  }
});

test("prepare submits campaign only and preserves the current publication", async () => {
  const panel = await mount({ pubs: [publication({ id: "old", status: "ACTIVE", is_current_active: true })] });
  await change(panel.select("publication_campaign_id"), "campaign-a");
  await click(panel.button("Preparar publicación"));
  assert.deepEqual(panel.writes(), [{ path: "/videowalls/wall-a/publications", method: "POST", body: { campaign_id: "campaign-a" } }]);
  assert.match(panel.content(), /Pendiente/);
  assert.match(panel.content(), /Activa actual/);
  assert.equal(panel.button("Activar publicación"), undefined);
  await panel.close();
});

test("manual refresh observes processing then ready without automatic activation", async () => {
  let ready = false;
  const panel = await mount({ handler: (request) => request.method === "GET" && request.path.endsWith("/publications")
    ? response([publication({ status: ready ? "READY" : "PROCESSING", validated_asset_count: ready ? 3 : 1 })]) : undefined });
  assert.match(panel.content(), /Procesando.*1\/3/);
  assert.equal(panel.button("Activar publicación"), undefined);
  ready = true;
  await click(panel.button("Actualizar estado"));
  assert.match(panel.content(), /Lista.*3\/3/);
  assert.ok(panel.button("Activar publicación"));
  assert.deepEqual(panel.writes(), []);
  await panel.close();
});

test("only READY activates with no client epoch or geometry payload", async () => {
  const panel = await mount({ pubs: [publication(), publication({ id: "old", status: "ACTIVE", is_current_active: true })] });
  await click(panel.button("Activar publicación"));
  assert.deepEqual(panel.writes(), [{ path: "/videowalls/wall-a/publications/pub-a/activate", method: "POST", body: undefined }]);
  assert.equal(panel.saved[0].active_publication_id, "pub-a");
  assert.match(panel.content(), /Activa actual/);
  assert.match(panel.content(), /Publicación histórica/);
  assert.equal(panel.button("Activar publicación"), undefined);
  await panel.close();
});

test("failed preparation displays backend error and never offers activation", async () => {
  const panel = await mount({ pubs: [publication({ status: "FAILED", error_code: "DIMENSIONS", error_message: "El master no coincide con 5760×1080" })] });
  assert.match(panel.content(), /El master no coincide/);
  assert.equal(panel.button("Activar publicación"), undefined);
  await panel.close();
});

test("read-only and missing-token users cannot write even by directly invoking handlers", async () => {
  for (const restrictions of [{ canManage: false }, { token: null }]) {
    const panel = await mount({ ...restrictions, pubs: [publication()] });
    assert.equal(panel.select("media_delivery").props.disabled, true);
    await change(panel.select("media_delivery"), "legacy");
    await click(panel.button("Guardar modo"));
    await click(panel.button("Preparar publicación"));
    if (panel.button("Activar publicación")) await click(panel.button("Activar publicación"));
    assert.deepEqual(panel.writes(), []);
    if (restrictions.token === null) assert.deepEqual(panel.requests, []);
    await panel.close();
  }
});

test("backend error keeps campaign and previous active publication and permits retry", async () => {
  let fails = true;
  const panel = await mount({ pubs: [publication({ id: "old", status: "ACTIVE", is_current_active: true })],
    handler: (request) => request.method === "POST" && fails ? response({ detail: "Preparación rechazada" }, 409) : undefined });
  await change(panel.select("publication_campaign_id"), "campaign-a");
  await click(panel.button("Preparar publicación"));
  assert.match(panel.content(), /Preparación rechazada/);
  assert.match(panel.content(), /Activa actual/);
  assert.equal(panel.select("publication_campaign_id").props.value, "campaign-a");
  assert.equal(panel.button("Preparar publicación").props.disabled, false);
  fails = false;
  await click(panel.button("Preparar publicación"));
  assert.match(panel.content(), /Pendiente/);
  await panel.close();
});

test("unrelated wall edits preserve selected campaign and unsaved delivery choice", async () => {
  const panel = await mount();
  await change(panel.select("publication_campaign_id"), "campaign-a");
  await change(panel.select("media_delivery"), "legacy");
  await panel.update({ wall: wall({ name: "Renamed", media_delivery: "node_slice" }) });
  assert.equal(panel.select("media_delivery").props.value, "legacy");
  assert.equal(panel.select("publication_campaign_id").props.value, "campaign-a");
  assert.deepEqual(panel.writes(), []);
  await panel.close();
});

test("late load for wall A cannot expose its publications in wall B", async () => {
  const pending = deferred();
  const panel = await mount({ handler: (request) => request.path === "/videowalls/wall-a/publications" ? pending.promise : undefined });
  const other = wall({ id: "wall-b", client_id: "client-b", media_delivery: "node_slice" });
  await panel.update({ wall: other, nodes: nodes(other) });
  await act(async () => pending.resolve(response([publication({ id: "stale-A" })])));
  assert.ok(!panel.content().includes("stale-A"));
  assert.deepEqual(panel.saved, []);
  await panel.close();
});

test("late preparation for wall A cannot update wall B", async () => {
  const pending = deferred();
  const panel = await mount({ handler: (request) => request.method === "POST" ? pending.promise : undefined });
  await change(panel.select("publication_campaign_id"), "campaign-a");
  await click(panel.button("Preparar publicación"));
  const other = wall({ id: "wall-b", client_id: "client-b", media_delivery: "node_slice" });
  await panel.update({ wall: other, nodes: nodes(other) });
  await act(async () => pending.resolve(response(publication({ id: "stale-preparation" }), 201)));
  assert.ok(!panel.content().includes("stale-preparation"));
  assert.deepEqual(panel.saved, []);
  await panel.close();
});

test("late activation for wall A cannot notify or replace wall B", async () => {
  const pending = deferred();
  const panel = await mount({ pubs: [publication()], handler: (request) => request.path.endsWith("/activate") ? pending.promise : undefined });
  await click(panel.button("Activar publicación"));
  const other = wall({ id: "wall-b", client_id: "client-b", media_delivery: "node_slice" });
  await panel.update({ wall: other, nodes: nodes(other) });
  await act(async () => pending.resolve(response(publication({ status: "ACTIVE", is_current_active: true }))));
  assert.deepEqual(panel.saved, []);
  assert.ok(!panel.content().includes("Publicación activada."));
  await panel.close();
});

test("both existing interfaces share the panel and dashboard passes real write permissions", () => {
  for (const path of ["src/pages/VideowallPage.tsx", "src/components/dashboard/VideowallEditDrawer.tsx"]) {
    assert.match(readFileSync(join(frontend, path), "utf8"), /<VideowallPublicationsPanel/);
  }
  assert.match(readFileSync(join(frontend, "src/pages/client/ClientOverviewPage.tsx"), "utf8"), /<VideowallEditDrawer\s+open=\{Boolean\(videowallEditTarget\)\}\s+canManage=\{canManageInfrastructure\}/);
  assert.match(readFileSync(join(frontend, "src/components/dashboard/VideowallEditDrawer.tsx"), "utf8"), /if \(!token \|\| !canManage\)/);
});
