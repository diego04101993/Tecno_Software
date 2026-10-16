// Reuse the existing isolated React 18.3.1/test-renderer harness via SCREEN_ROTATION_TEST_DEPS.
// Bundle the real component in memory: no server, network, backend or temporary files.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire } from "node:module";
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
const { buildSync } = createRequire(appRequire.resolve("vite/package.json"))("esbuild");
const bundle = buildSync({
  entryPoints: [join(frontend, "src/components/PreviewContentSurface.tsx")],
  bundle: true, write: false, platform: "node", format: "cjs", jsx: "automatic",
  external: ["react", "react/jsx-runtime"], define: { "import.meta.env": "{}" },
});
const compiled = { exports: {} };
// Evaluate only the compiled project module, never the content fixture.
new Function("require", "module", "exports", bundle.outputFiles[0].text)(testRequire, compiled, compiled.exports);
const { PreviewContentSurface } = compiled.exports;

function fixture(type, value) {
  return Object.freeze({
    id: "qa-preview", duration_seconds: 10, zone_key: "main", contentLabel: "QA preview",
    content: Object.freeze({
      id: "qa-content", name: "QA content", type, file_path: null, source_url: null,
      html_content: type === "html" ? value : null,
      text_content: type === "text" ? value : null,
      duration_seconds: 10, metadata_json: Object.freeze({}),
    }),
  });
}

function assertLiteralContent(type, value, expected = value, compact = false) {
  const entry = fixture(type, value);
  let view;
  act(() => { view = create(React.createElement(PreviewContentSurface, { entry, compact })); });
  try {
    const body = type === "html"
      ? view.root.findByType("pre")
      : view.root.findAllByType("p").find((node) => node.children.length === 1 && node.children[0] === expected);
    assert.ok(body, "The preview must have a visible content area");
    assert.deepEqual(body.children, expected === "" ? [] : [expected], "Content must remain literal JSX text");
    // Scope to the content area: the component's own header contains a legitimate p and SVG icon.
    assert.equal(body.findAll((node) => typeof node.type === "string").length, 1, "No payload elements may be created");
    for (const node of view.root.findAll((node) => typeof node.type === "string")) {
      assert.equal(Object.hasOwn(node.props, "dangerouslySetInnerHTML"), false);
      assert.equal(Object.hasOwn(node.props, "srcDoc"), false);
      assert.equal(Object.keys(node.props).some((key) => /^on/i.test(key)), false, "No payload event handlers");
    }
    assert.equal(entry.content[type === "html" ? "html_content" : "text_content"], value, "Stored value stays intact");
  } finally {
    act(() => view.unmount());
  }
}

const cases = [
  ["HTML_BASIC_TEXT: h1 and p stay literal", "<h1>Título</h1><p>Hola</p>"],
  ["IMG_ONERROR_SAFE: no image or error handler", '<img src=x onerror="window.__XSS=1">'],
  ["SVG_ONLOAD_SAFE: no payload SVG or load handler", '<svg onload="window.__XSS=2"></svg>'],
  ["SCRIPT_SAFE: no script element", "<script>window.__XSS=3</script>"],
  ["IFRAME_SAFE: no iframe from html_content", '<iframe src="data:text/html,<script>window.__XSS=4</script>"></iframe>'],
  ["JAVASCRIPT_URL_SAFE: no active anchor", '<a href="javascript:alert(1)">x</a>'],
  ["MALFORMED_HTML_SAFE: nested and malformed markup stays text", '<div><p><svg><g onload="window.__XSS=5"></div><img src=x onerror=alert(1)'],
  ["MIXED_CASE_SAFE: tags and entities remain unchanged", '<ImG sRc=x oNeRrOr="window.__XSS=6">&lt;script&gt;alert(1)&lt;/script&gt;&#60;svg onload=alert(2)&#62; &amp;'],
];

for (const [name, payload] of cases) {
  test(name, () => {
    for (const compact of [false, true]) assertLiteralContent("html", payload, payload, compact);
  });
}

test("HTML multiline and long content remains complete", () => {
  const payload = "\n<h1>Literal</h1>\r\n" + "<p>Long content & entities</p>\n".repeat(200);
  assertLiteralContent("html", payload);
});

test("HTML empty string stays empty; absent HTML uses a plain-text fallback", () => {
  assertLiteralContent("html", "");
  for (const value of [null, undefined]) assertLiteralContent("html", value, "Sin HTML disponible.");
});

test("TEXT_REACT_ESCAPE_REGRESSION: dangerous text stays literal in both preview sizes", () => {
  for (const compact of [false, true]) {
    assertLiteralContent("text", "<img src=x onerror=alert(1)>", undefined, compact);
  }
});
