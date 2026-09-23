// Run with SCREEN_ROTATION_TEST_DEPS pointing to isolated React 18.3.1/test-renderer 18.3.1 dependencies.
// No browser, backend server, network request, production config or app lockfile changes.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createRequire } from "node:module";
import { mkdtempSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const deps = process.env.SCREEN_ROTATION_TEST_DEPS;
assert.ok(deps, "Set SCREEN_ROTATION_TEST_DEPS to the isolated test dependency directory");
const testRequire = createRequire(join(resolve(deps), "package.json"));
const React = testRequire("react");
const { create, act } = testRequire("react-test-renderer");
assert.equal(React.version, "18.3.1");
const appRequire = createRequire(join(frontend, "package.json"));
const viteRequire = createRequire(appRequire.resolve("vite/package.json"));
const { buildSync } = viteRequire("esbuild");
const scratch = mkdtempSync(join(resolve(deps), "screen-rotation-"));
const bundle = join(scratch, "drawer.cjs");
buildSync({
  entryPoints: [join(frontend, "src/components/dashboard/ScreenEditDrawer.tsx")],
  outfile: bundle, bundle: true, platform: "node", format: "cjs", jsx: "automatic",
  external: ["react", "react/jsx-runtime"], define: { "import.meta.env": "{}" },
});
const { ScreenEditDrawer } = testRequire(bundle);
const originalFetch = globalThis.fetch;
after(() => {
  globalThis.fetch = originalFetch;
  assert.equal(dirname(resolve(scratch)), resolve(deps));
  rmSync(scratch, { recursive: true, force: true });
});

function fixture(rotation, overrides = {}) {
  return {
    id: "fixture-screen", client_id: "client-a", branch_id: "branch-a", name: "Original screen",
    mode: "normal", device_mode: "single", orientation: "vertical", screen_count: 1,
    resolution_width: 1080, resolution_height: 1920, canvas_width: 700, canvas_height: 1100,
    notes: "Original notes", hardware_identifier: "fixture-hardware", output_mapping_json: {},
    device_outputs: [{ id: "output-0", display_order: 0, rotation }], ...overrides,
  };
}

async function mount(channel) {
  const requests = [];
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "http://localhost:8000/api/channels/fixture-screen");
    assert.equal(options.method, "PATCH");
    requests.push(JSON.parse(options.body));
    return new Response(JSON.stringify(channel), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  let view;
  await act(async () => {
    view = create(React.createElement(ScreenEditDrawer, {
      open: true, token: "fixture-only", channel, branch: { id: "branch-a", name: "Branch A" },
      onClose: () => {}, onSaved: () => {},
    }));
  });
  return {
    view, requests,
    rotation: () => view.root.findByProps({ name: "rotation" }),
    submit: async () => act(async () => view.root.findByType("form").props.onSubmit({ preventDefault() {} })),
    close: async () => act(async () => view.unmount()),
  };
}

test("loads persisted primary output by display_order, independent of array order", async () => {
  const form = await mount(fixture(270, { device_outputs: [
    { id: "output-1", display_order: 1, rotation: 90 }, { id: "output-0", display_order: 0, rotation: 270 },
  ] }));
  assert.equal(form.rotation().props.value, 270);
  assert.deepEqual(form.rotation().findAllByType("option").map((option) => option.props.value), [0, 90, 180, 270]);
  await form.close();
});

test("renaming and editing notes omit rotation, dimensions, mode and mapping", async () => {
  const form = await mount(fixture(270));
  await act(async () => {
    form.view.root.findAllByType("input").find((input) => input.props.value === "Original screen")
      .props.onChange({ target: { value: "Renamed screen" } });
    form.view.root.findByType("textarea").props.onChange({ target: { value: "Edited notes" } });
  });
  await form.submit();
  assert.equal(form.requests.length, 1);
  assert.equal(form.requests[0].name, "Renamed screen");
  assert.equal(form.requests[0].notes, "Edited notes");
  for (const field of ["rotation", "orientation", "resolution_width", "resolution_height", "canvas_width", "canvas_height",
    "mode", "device_mode", "screen_count", "output_mapping_json"]) {
    assert.equal(Object.hasOwn(form.requests[0], field), false, `Unexpected field: ${field}`);
  }
  assert.equal(form.rotation().props.value, 270);
  await form.close();
});

test("all four explicit selections are numbers and do not alter logical orientation or dimensions", async () => {
  for (const angle of [0, 90, 180, 270]) {
    const form = await mount(fixture(270));
    await act(async () => form.rotation().props.onChange({ target: { value: String(angle) } }));
    await form.submit();
    assert.equal(form.requests[0].rotation, angle);
    for (const field of ["orientation", "resolution_width", "resolution_height", "canvas_width", "canvas_height", "output_mapping_json"]) {
      assert.equal(Object.hasOwn(form.requests[0], field), false, field);
    }
    await form.close();
  }
});

test("historical vertical screen with zero rotation stays at zero and sends no implicit rotation", async () => {
  const form = await mount(fixture(0));
  assert.equal(form.rotation().props.value, 0);
  await form.submit();
  assert.equal(Object.hasOwn(form.requests[0], "rotation"), false);
  await form.close();
});

test("missing primary output is shown as unconfigured, never presented as a persisted zero", async () => {
  const form = await mount(fixture(0, { device_outputs: [] }));
  assert.equal(form.rotation().props.value, "");
  await form.submit();
  assert.equal(Object.hasOwn(form.requests[0], "rotation"), false);
  await act(async () => form.rotation().props.onChange({ target: { value: "0" } }));
  await form.submit();
  assert.equal(form.requests[1].rotation, 0);
  await form.close();
});

test("out-of-scope editors omit rotation and preserve device mode on metadata-only saves", async () => {
  for (const [mode, device_mode] of [["expanded", "mirror"], ["expanded", "extended"], ["expanded", "independent"],
    ["videowall", "single"], ["audio", "single"], ["touch", "single"]]) {
    const form = await mount(fixture(180, { mode, device_mode }));
    assert.equal(form.view.root.findAllByProps({ name: "rotation" }).length, 0);
    await form.submit();
    for (const field of ["rotation", "mode", "device_mode", "canvas_width", "canvas_height", "output_mapping_json"]) {
      assert.equal(Object.hasOwn(form.requests[0], field), false, `${mode}/${device_mode}: ${field}`);
    }
    await form.close();
  }
});
