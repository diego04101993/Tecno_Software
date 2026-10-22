// Uses the same isolated React/test-renderer dependencies as screen_rotation_form.test.mjs.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const deps = process.env.SCREEN_ROTATION_TEST_DEPS;
assert.ok(deps, "Set SCREEN_ROTATION_TEST_DEPS to isolated React 18.3.1/test-renderer 18.3.1 dependencies");
const testRequire = createRequire(join(resolve(deps), "package.json"));
const React = testRequire("react");
const { create, act } = testRequire("react-test-renderer");
assert.equal(React.version, "18.3.1");
const appRequire = createRequire(join(frontend, "package.json"));
const { buildSync } = createRequire(appRequire.resolve("vite/package.json"))("esbuild");
const scratch = mkdtempSync(join(resolve(deps), "led-custom-"));
const bundle = join(scratch, "led.cjs");
buildSync({
  stdin: { contents: `
    export { ScreenEditDrawer } from './src/components/dashboard/ScreenEditDrawer';
    export { ScreenCreateDrawer } from './src/components/dashboard/ScreenCreateDrawer';
    export { buildOutputMappingDraft } from './src/components/dashboard/LedOutputMappingPanel';
    export { buildLedCustomMapping } from './src/lib/ledCustomMapping';
  `, resolveDir: frontend, loader: "tsx" },
  outfile: bundle, bundle: true, platform: "node", format: "cjs", jsx: "automatic",
  external: ["react", "react/jsx-runtime"], define: { "import.meta.env": JSON.stringify({ DEV: true, PROD: false }) },
});
const { ScreenEditDrawer, ScreenCreateDrawer, buildLedCustomMapping, buildOutputMappingDraft } = testRequire(bundle);
const expected = JSON.parse(readFileSync(join(frontend, "../backend/tests/fixtures/led_custom_516x860.json"), "utf8"));
const originalFetch = globalThis.fetch;
after(() => {
  globalThis.fetch = originalFetch;
  assert.equal(dirname(resolve(scratch)), resolve(deps));
  rmSync(scratch, { recursive: true, force: true });
});

const dimensions = { hdmiWidth: 1920, hdmiHeight: 1080, width: 516, height: 860, x: 0, y: 0 };
function fixture(mapping = expected) {
  return {
    id: "led-screen", client_id: "client-a", branch_id: "branch-a", name: "LED original",
    mode: "normal", device_mode: "single", orientation: "horizontal", screen_count: 1,
    resolution_width: 1920, resolution_height: 1080, canvas_width: 1920, canvas_height: 1080,
    notes: "Original notes", hardware_identifier: null, output_mapping_json: structuredClone(mapping),
    device_outputs: [{ id: "output-0", display_order: 0, rotation: 270 }],
  };
}

async function mount(kind = "edit", channel = fixture()) {
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    if (method === "GET") {
      assert.equal(kind, "create");
      assert.equal(url, "http://localhost:8000/api/videowalls?client_id=client-a&branch_id=branch-a");
      return Response.json([]);
    }
    assert.equal(url, kind === "create" ? "http://localhost:8000/api/channels" : "http://localhost:8000/api/channels/led-screen");
    assert.equal(method, kind === "create" ? "POST" : "PATCH");
    const body = JSON.parse(options.body);
    requests.push(body);
    return Response.json({ ...channel, ...body }, { status: kind === "create" ? 201 : 200 });
  };
  const Component = kind === "create" ? ScreenCreateDrawer : ScreenEditDrawer;
  const props = { open: true, token: "fixture-only", channel, clientId: "client-a",
    branch: { id: "branch-a", name: "Branch A" }, onClose() {}, onSaved() {}, onCreated() {} };
  let view;
  await act(async () => { view = create(React.createElement(Component, props)); });
  return {
    view, requests,
    input: (name) => view.root.findByProps({ name }),
    change: async (name, value) => act(async () => view.root.findByProps({ name }).props.onChange({ target: { value: String(value) } })),
    choose: async (label) => act(async () => view.root.findAllByType("button").find((button) => button.props.children === label).props.onClick()),
    submit: async () => act(async () => view.root.findByType("form").props.onSubmit({ preventDefault() {} })),
    reopen: async () => {
      await act(async () => view.update(React.createElement(Component, { ...props, open: false })));
      await act(async () => view.update(React.createElement(Component, props)));
    },
    close: async () => act(async () => view.unmount()),
  };
}

test("pure preset matches the shared exact contract and survives frontend normalization", () => {
  assert.deepEqual(buildLedCustomMapping(dimensions), expected);
  assert.deepEqual(buildOutputMappingDraft(expected, 1920, 1080), expected);
});

test("preset rejects invalid types, fractions, non-finite dimensions and out-of-bounds regions", () => {
  for (const key of ["hdmiWidth", "hdmiHeight", "width", "height"]) {
    for (const invalid of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, true, false, null, "516"]) {
      assert.throws(() => buildLedCustomMapping({ ...dimensions, [key]: invalid }), undefined, `${key}=${invalid}`);
    }
  }
  for (const key of ["x", "y"]) {
    for (const invalid of [-1, 0.5, NaN, Infinity, true, null, "0"]) {
      assert.throws(() => buildLedCustomMapping({ ...dimensions, [key]: invalid }));
    }
  }
  for (const invalid of [{ width: 1921 }, { height: 1081 }, { x: 1405 }, { y: 221 }]) {
    assert.throws(() => buildLedCustomMapping({ ...dimensions, ...invalid }));
  }
  assert.equal(buildLedCustomMapping({ ...dimensions, x: 1404, y: 220 }).viewport_x, 1404);
});

test("create form submits LED516x860 while Channel resolution/canvas remain HDMI1920x1080", async () => {
  const form = await mount("create");
  await act(async () => form.view.root.findByProps({ placeholder: "Ej. Pantalla caja 1" })
    .props.onChange({ target: { value: "LED new" } }));
  await form.choose("LED personalizada");
  await form.change("led_width", 516);
  await form.change("led_height", 860);
  await form.submit();
  assert.equal(form.requests.length, 1);
  const body = form.requests[0];
  assert.deepEqual(body.output_mapping_json, expected);
  assert.deepEqual([body.resolution_width, body.resolution_height, body.canvas_width, body.canvas_height], [1920, 1080, 1920, 1080]);
  assert.equal(body.rotation, 0);
  assert.equal(body.orientation, "horizontal");
  await form.close();
});

test("reopening persisted custom keeps its values and requires explicit preset selection", async () => {
  const form = await mount();
  for (let attempt = 0; attempt < 2; attempt++) {
    const button = form.view.root.findAllByType("button").find((button) => button.props.children === "LED personalizada");
    assert.equal(button.props["aria-pressed"], false);
    assert.equal(form.view.root.findAllByProps({ name: "led_width" }).length, 0);
    const summary = form.view.root.findByProps({ "data-testid": "persisted-mapping-summary" });
    assert.ok(JSON.stringify(summary.findAllByType("p").map((p) => p.props.children)).includes("516"));
    await form.submit();
    assert.equal(Object.hasOwn(form.requests.at(-1), "output_mapping_json"), false);
    await form.choose("LED personalizada");
    assert.equal(form.input("led_width").props.value, "516");
    assert.equal(form.input("led_height").props.value, "860");
    await form.reopen();
  }
  await form.close();
});

test("changing LED dimensions, position and HDMI reconstructs the entire preset in both drawers", async () => {
  for (const kind of ["create", "edit"]) {
    const form = await mount(kind);
    await form.choose("LED personalizada");
    await form.change("led_width", 516);
    await form.change("led_height", 860);
    await form.change("led_width", 600);
    await form.change("led_height", 900);
    await form.change("led_x", 20);
    await form.change("led_y", 30);
    await form.change("resolution_width", 1600);
    await form.change("resolution_height", 1000);
    await form.submit();
    assert.equal(form.requests.length, 1);
    assert.deepEqual(form.requests[0].output_mapping_json, buildLedCustomMapping({ hdmiWidth: 1600, hdmiHeight: 1000, width: 600, height: 900, x: 20, y: 30 }));
    assert.deepEqual([form.requests[0].canvas_width, form.requests[0].canvas_height], [1600, 1000]);
    await form.close();
  }
});

test("explicit LED preset removes a historical residual 1920x1080 slice", async () => {
  const historical = structuredClone(expected);
  historical.slices[0] = { ...historical.slices[0], source_width: 1920, source_height: 1080, output_width: 1920, output_height: 1080 };
  const form = await mount("edit", fixture(historical));
  await form.choose("LED personalizada");
  await form.submit();
  assert.deepEqual(form.requests[0].output_mapping_json, expected);
  await form.close();
});

test("name, notes and rotation changes omit mapping for LED, advanced custom and sliced", async () => {
  const sliced = buildOutputMappingDraft({ ...expected, profile: "led_wide", mode: "sliced", mapping_mode: "sliced", slice_count: 2 }, 1920, 1080);
  const advanced = structuredClone(expected);
  advanced.slices[0].output_x = -25;
  advanced.slices[0].scale_x = 1.5;
  for (const mapping of [expected, advanced, sliced]) {
    const form = await mount("edit", fixture(mapping));
    await act(async () => {
      form.view.root.findAllByType("input").find((input) => input.props.value === "LED original")
        .props.onChange({ target: { value: "Renamed LED" } });
      form.view.root.findByType("textarea").props.onChange({ target: { value: "Edited notes" } });
    });
    await form.change("rotation", 90);
    await form.submit();
    assert.equal(form.requests[0].name, "Renamed LED");
    assert.equal(form.requests[0].notes, "Edited notes");
    assert.equal(form.requests[0].rotation, 90);
    for (const field of ["output_mapping_json", "canvas_width", "canvas_height", "resolution_width", "resolution_height", "orientation"]) {
      assert.equal(Object.hasOwn(form.requests[0], field), false, field);
    }
    await form.close();
  }
});

test("NORMAL clears custom flags, dimensions and slices without touching rotation", async () => {
  const form = await mount();
  await form.choose("Pantalla normal");
  await form.submit();
  const mapping = form.requests[0].output_mapping_json;
  assert.equal(mapping.enabled, false);
  assert.deepEqual([mapping.profile, mapping.mode, mapping.mapping_mode], ["normal", "normal", "normal"]);
  assert.deepEqual(mapping.slices, []);
  for (const field of ["physical_width", "source_canvas_width", "canvas_width", "viewport_width", "output_width"]) assert.equal(mapping[field], 1920);
  for (const field of ["physical_height", "source_canvas_height", "canvas_height", "viewport_height", "output_height"]) assert.equal(mapping[field], 1080);
  assert.equal(Object.hasOwn(form.requests[0], "rotation"), false);
  await form.close();
});

test("invalid drafts and HDMI shrink block submission; correcting the input enables it", async () => {
  for (const kind of ["create", "edit"]) {
    const form = await mount(kind);
    await form.choose("LED personalizada");
    await form.change("led_width", 516);
    await form.change("led_height", 860);
    for (const [field, invalid, valid] of [["led_width", "", 516], ["led_width", 0, 516], ["led_height", 1.5, 860],
      ["led_x", -1, 0], ["led_y", 221, 0], ["resolution_height", 720, 1080]]) {
      await form.change(field, invalid);
      assert.equal(form.view.root.findByProps({ type: "submit" }).props.disabled, true);
      await form.submit();
      assert.equal(form.requests.length, 0);
      await form.change(field, valid);
    }
    await form.submit();
    assert.equal(form.requests.length, 1);
    assert.deepEqual(form.requests[0].output_mapping_json, expected);
    await form.close();
  }
});
