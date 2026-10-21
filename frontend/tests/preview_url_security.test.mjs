// Use the existing isolated React harness, without installing dependencies.
// The real private resolver is exported only in the in-memory test bundle.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const deps = process.env.SCREEN_ROTATION_TEST_DEPS;
assert.ok(deps, "Set SCREEN_ROTATION_TEST_DEPS to the existing isolated test dependency directory");
const testRequire = createRequire(join(resolve(deps), "package.json"));
const React = testRequire("react");
const { create, act } = testRequire("react-test-renderer");
assert.equal(React.version, "18.3.1");
const appRequire = createRequire(join(frontend, "package.json"));
const { buildSync } = createRequire(appRequire.resolve("vite/package.json"))("esbuild");
const componentPath = join(frontend, "src/components/PreviewContentSurface.tsx");
const productionBytes = readFileSync(componentPath);
const productionSource = productionBytes.toString("utf8");

function compileComponent(source) {
  const bundle = buildSync({
    stdin: {
      contents: source + "\nexport { resolveUrlPreviewFrame };",
      resolveDir: dirname(componentPath), sourcefile: componentPath, loader: "tsx",
    },
    bundle: true, write: false, platform: "node", format: "cjs", jsx: "automatic",
    external: ["react", "react/jsx-runtime"], define: { "import.meta.env": "{}" },
  });
  const compiled = { exports: {} };
  // Evaluate project code only; URL fixtures are data, never evaluated.
  new Function("require", "module", "exports", bundle.outputFiles[0].text)(testRequire, compiled, compiled.exports);
  return compiled.exports;
}

const production = compileComponent(productionSource);
const genericSandbox = "allow-scripts allow-forms allow-presentation";
const youtubeSandbox = "allow-same-origin allow-scripts allow-presentation";
const canonical = "https://www.youtube.com/embed/M7lc1UVf-VE?rel=0&modestbranding=1";
const forbidden = [
  "allow-popups", "allow-popups-to-escape-sandbox", "allow-downloads", "allow-modals",
  "allow-top-navigation", "allow-top-navigation-by-user-activation",
];

function assertPreview(module, sourceUrl, expectedSrc, kind = "generic", compact = false) {
  const content = Object.freeze({
    id: "qa-content", name: "YouTube trusted-youtube", type: "url", source_url: sourceUrl, file_path: null,
    html_content: null, text_content: null, duration_seconds: 10, metadata_json: Object.freeze({}),
  });
  const entry = Object.freeze({
    id: "qa-preview", content, contentLabel: "YouTube", duration_seconds: 10, zone_key: "main",
  });
  let view;
  act(() => { view = create(React.createElement(module.PreviewContentSurface, { entry, compact })); });
  try {
    const frames = view.root.findAllByType("iframe");
    assert.equal(frames.length, expectedSrc === null ? 0 : 1, "Unexpected iframe count");
    if (expectedSrc !== null) {
      assert.deepEqual(module.resolveUrlPreviewFrame(sourceUrl), { url: expectedSrc, kind }, "URL/classification contract");
      const props = frames[0].props;
      assert.equal(props.src, expectedSrc, "Exact iframe URL");
      assert.equal(props.sandbox, kind === "trusted-youtube" ? youtubeSandbox : genericSandbox, "Exact sandbox contract");
      assert.equal(props.referrerPolicy, "strict-origin-when-cross-origin");
      const permissions = new Set(props.sandbox.split(/\s+/));
      for (const denied of forbidden) assert.equal(permissions.has(denied), false, denied);
      assert.equal(permissions.has("allow-same-origin"), kind === "trusted-youtube");
      assert.equal(permissions.has("allow-forms"), kind === "generic");
      if (kind === "trusted-youtube") {
        const output = new URL(props.src);
        assert.equal(output.origin, "https://www.youtube.com");
        assert.equal(output.username + output.password + output.port + output.hash, "");
        assert.equal(output.search, "?rel=0&modestbranding=1");
      }
    } else if (sourceUrl) {
      assert.ok(view.root.findAll((node) => typeof node.type === "string")
        .some((node) => node.children.includes(sourceUrl)), "Rejected URLs remain literal text");
    }
    for (const node of view.root.findAll((node) => typeof node.type === "string")) {
      assert.equal(Object.hasOwn(node.props, "srcDoc"), false);
      assert.equal(Object.hasOwn(node.props, "dangerouslySetInnerHTML"), false);
    }
    assert.equal(content.source_url, sourceUrl, "Rendering must not rewrite stored URLs");
  } finally {
    act(() => view.unmount());
  }
}

const genericCases = [
  ["HTTP", "http://example.test/page", "http://example.test/page"],
  ["HTTPS", "https://example.test/page", "https://example.test/page"],
  ["Uppercase generic", "HTTP://EXAMPLE.TEST/page", "http://example.test/page"],
  ["Generic whitespace", " \t\r\nhttps://example.test/page\r\n ", "https://example.test/page"],
  ["Same-origin generic policy (browser isolation checked separately)", "http://localhost:8899/fixture", "http://localhost:8899/fixture"],
  ["Redirect generic policy (browser isolation checked separately)", "http://localhost:8900/redirect", "http://localhost:8900/redirect"],
  ["YouTube nocookie remains generic", "https://www.youtube-nocookie.com/embed/M7lc1UVf-VE", "https://www.youtube-nocookie.com/embed/M7lc1UVf-VE"],
];
for (const [name, input, expected] of genericCases) {
  test(name, () => { for (const compact of [false, true]) assertPreview(production, input, expected, "generic", compact); });
}

const trustedInputs = [
  ["Watch", "https://youtube.com/watch?v=M7lc1UVf-VE"],
  ["WWW watch", "https://www.youtube.com/watch?v=M7lc1UVf-VE"],
  ["Mobile watch", "https://m.youtube.com/watch?v=M7lc1UVf-VE"],
  ["Short", "https://youtu.be/M7lc1UVf-VE"],
  ["Direct embed WWW", "https://www.youtube.com/embed/M7lc1UVf-VE"],
  ["Direct embed bare host", "https://youtube.com/embed/M7lc1UVf-VE"],
  ["Direct embed mobile", "https://m.youtube.com/embed/M7lc1UVf-VE"],
  ["Mixed case", "hTtPs://WWW.YouTube.COM/watch?v=M7lc1UVf-VE"],
  ["HTTPS default port", "https://youtube.com:443/watch?v=M7lc1UVf-VE"],
  ["HTTP default port upgraded", "http://youtube.com:80/watch?v=M7lc1UVf-VE"],
  ["Surrounding whitespace", " \t\r\nhttps://youtube.com/watch?v=M7lc1UVf-VE\r\n\t "],
  ["Fullwidth host parses to youtube.com", "https://ｙｏｕｔｕｂｅ.com/watch?v=M7lc1UVf-VE"],
  ["Watch drops extra query and fragment", "https://youtube.com/watch?next=https://evil.test&v=M7lc1UVf-VE&origin=https://evil.test&rel=1#fragment"],
  ["Embed drops extra query", "https://www.youtube.com/embed/M7lc1UVf-VE?redirect=https://evil.test&origin=https://evil.test#fragment"],
  ["Short ID comes only from exact path", "https://youtu.be/M7lc1UVf-VE?v=invalid&next=https://evil.test#fragment"],
];
for (const [name, input] of trustedInputs) {
  test("Trusted: " + name, () => {
    for (const compact of [false, true]) assertPreview(production, input, canonical, "trusted-youtube", compact);
  });
}
test("Fullwidth host has the permitted effective hostname", () => {
  assert.equal(new URL("https://ｙｏｕｔｕｂｅ.com/watch?v=M7lc1UVf-VE").hostname, "youtube.com");
});
for (const id of ["abcdefghijk", "0123456789_", "aB3_dE6-fG9"]) {
  test("Valid 11-character ID: " + id, () => {
    assertPreview(production, "https://youtube.com/watch?v=" + id,
      "https://www.youtube.com/embed/" + id + "?rel=0&modestbranding=1", "trusted-youtube");
  });
}

// Expected generic serialization uses URL(), not a copy of the production classification.
const untrustedInputs = [
  ["Host suffix", "https://youtube.com.evil.test/watch?v=M7lc1UVf-VE"],
  ["WWW suffix", "https://www.youtube.com.evil.test/watch?v=M7lc1UVf-VE"],
  ["YouTube as userinfo", "https://youtube.com@evil.test/watch?v=M7lc1UVf-VE"],
  ["YouTube in query", "https://evil.test/?youtube.com/watch?v=abc"],
  ["YouTube in fragment", "https://evil.test/#youtube.com/watch?v=abc"],
  ["Short suffix", "https://youtu.be.evil.test/M7lc1UVf-VE"],
  ["Short as userinfo", "https://youtu.be@evil.test/M7lc1UVf-VE"],
  ["Unlisted WWW short", "https://www.youtu.be/M7lc1UVf-VE"],
  ["Unlisted WWW mobile", "https://www.m.youtube.com/watch?v=M7lc1UVf-VE"],
  ["IDN Cyrillic o", "https://yоutube.com/watch?v=M7lc1UVf-VE"],
  ["Trailing dot", "https://youtube.com./watch?v=M7lc1UVf-VE"],
  ["Username", "https://user@youtube.com/watch?v=M7lc1UVf-VE"],
  ["Username and password", "https://user:pass@youtube.com/watch?v=M7lc1UVf-VE"],
  ["Password alone", "https://:pass@youtube.com/watch?v=M7lc1UVf-VE"],
  ["Empty userinfo", "https://@youtube.com/watch?v=M7lc1UVf-VE"],
  ["Nonstandard port", "https://youtube.com:8443/watch?v=M7lc1UVf-VE"],
  ["Nondefault HTTP port", "http://youtube.com:443/watch?v=M7lc1UVf-VE"],
  ["Embed userinfo", "https://user@www.youtube.com/embed/M7lc1UVf-VE"],
  ["Embed port", "https://www.youtube.com:8443/embed/M7lc1UVf-VE"],
  ["Short userinfo", "https://user@youtu.be/M7lc1UVf-VE"],
  ["Short port", "https://youtu.be:8443/M7lc1UVf-VE"],
  ["Unsupported path with valid v", "https://youtube.com/arbitrary?v=M7lc1UVf-VE"],
  ["Watch trailing slash", "https://youtube.com/watch/?v=M7lc1UVf-VE"],
  ["No watch ID", "https://youtube.com/watch"],
  ["Duplicate v", "https://youtube.com/watch?v=M7lc1UVf-VE&v=abcdefghijk"],
  ["Duplicate encoded v", "https://youtube.com/watch?v=M7lc1UVf-VE&%76=abcdefghijk"],
  ["Encoded query key", "https://youtube.com/watch?%76=M7lc1UVf-VE"],
  ["Encoded valid query ID", "https://youtube.com/watch?v=%4D7lc1UVf-VE"],
  ["Encoded valid path ID", "https://youtu.be/%4D7lc1UVf-VE"],
  ["Embed extra segment", "https://www.youtube.com/embed/M7lc1UVf-VE/extra"],
  ["Embed empty", "https://www.youtube.com/embed/"],
  ["Embed traversal", "https://www.youtube.com/embed/../evil"],
  ["Embed encoded traversal", "https://www.youtube.com/embed/%2e%2e/evil"],
  ["Traversal normalized to valid watch", "https://youtube.com/unused/../watch?v=M7lc1UVf-VE"],
  ["Traversal normalized to valid embed", "https://youtube.com/unused/../embed/M7lc1UVf-VE"],
  ["Short extra segment", "https://youtu.be/M7lc1UVf-VE/extra"],
  ["Short traversal", "https://youtu.be/../redirect"],
  ["Short encoded slash", "https://youtu.be/M7lc1UVf-VE%2Fextra"],
  ["Short normalizes to valid ID", "https://youtu.be/unused/../M7lc1UVf-VE"],
  ["Embedded TAB in hostname", "https://you\ttube.com/watch?v=M7lc1UVf-VE"],
  ["Embedded newline in path ID", "https://youtu.be/M7lc1\nUVf-VE"],
  ["Backslash normalized to embed", "https://youtube.com\\embed\\M7lc1UVf-VE"],
  ["Raw traversal watch ID", "https://youtube.com/watch?v=../redirect"],
  ["Raw query injection short ID", "https://youtube.com/watch?v=abc?x=1"],
  ["Raw fragment short ID", "https://youtube.com/watch?v=abc#fragment"],
  ["Raw ampersand short ID", "https://youtube.com/watch?v=abc&x=1"],
];
for (const id of [
  "", "abc", "M7lc1UVf-VEE", "/", "../redirect", "..%2Fredirect", "%2e%2e/redirect", "..\\redirect",
  "abc?x=1", "abc#fragment", "abc&x=1", "abc=x", "\"quote'", "日本語", "M7lc1UVf VE", "%4D7lc1UVf-VE",
  "M7lc1UVf-VE\n", "M7lc1UVf-VE\r", "M7lc1UVf-VE\u0000", "\tM7lc1UVf-VE", "M7lc1UVf-VE\u007f",
]) untrustedInputs.push(["Invalid watch ID " + JSON.stringify(id), "https://youtube.com/watch?v=" + encodeURIComponent(id)]);
for (const [name, input] of untrustedInputs) {
  test("Generic, never trusted: " + name, () => {
    assertPreview(production, input, new URL(input.trim()).toString());
  });
}

const blockedInputs = [
  ["Protocol-relative", "//example.test/page"],
  ["JavaScript", "javascript:window.__SEC1E_EXECUTED=true"],
  ["Mixed-case JavaScript", "JaVaScRiPt:window.__SEC1E_EXECUTED=true"],
  ["HTML data", "data:text/html,<script>window.__SEC1E_EXECUTED=true</script>"],
  ["Base64 data", "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="],
  ["Blob", "blob:http://localhost:8899/fake"], ["File", "file:///qa-fixture"],
  ["FTP", "ftp://example.test/page"], ["About", "about:blank"], ["VBScript", "vbscript:qa"],
  ["Encoded scheme", "java%73cript:qa"], ["Relative", "/media/qa.svg"],
  ["RTMP", "rtmp://example.test/live"], ["RTSP", "rtsp://example.test/live"],
  ["HLS", "https://example.test/live.m3u8"], ["DASH", "https://example.test/live.mpd"],
  ["YouTube live stream", "https://youtube.com/live/M7lc1UVf-VE"],
  ["Short live stream", "https://youtu.be/live/M7lc1UVf-VE"], ["Twitch", "https://twitch.tv/fictitious"],
  ["Invalid host", "https://you tube.com/watch?v=M7lc1UVf-VE"], ["Empty", ""], ["Absent", null],
];
for (const [name, input] of blockedInputs) {
  test("No iframe: " + name, () => {
    for (const compact of [false, true]) assertPreview(production, input, null, "generic", compact);
  });
}

// Each mutation targets one exact expression and must fail behavior, not crash.
const idGuard = "videoId !== null && videoId.length === 11 && /^[A-Za-z0-9_-]{11}$/.test(videoId)";
const tick = String.fromCharCode(96);
const mutations = [
  ["A generic same-origin", 'const GENERIC_URL_SANDBOX = "' + genericSandbox + '"',
    'const GENERIC_URL_SANDBOX = "allow-same-origin ' + genericSandbox + '"',
    "https://example.test/", "https://example.test/", "generic", /Exact sandbox contract/],
  ["B trusted popups", 'const YOUTUBE_URL_SANDBOX = "' + youtubeSandbox + '"',
    'const YOUTUBE_URL_SANDBOX = "' + youtubeSandbox + ' allow-popups"',
    "https://youtube.com/watch?v=M7lc1UVf-VE", canonical, "trusted-youtube", /Exact sandbox contract/],
  ["C unsafe hostname substring", '["youtube.com", "www.youtube.com", "m.youtube.com"].includes(hostname)',
    '["youtube.com", "www.youtube.com", "m.youtube.com"].some((allowed) => hostname.includes(allowed))',
    "https://youtube.com.evil.test/watch?v=M7lc1UVf-VE", "https://youtube.com.evil.test/watch?v=M7lc1UVf-VE", "generic", /URL\/classification contract/],
  ["D remove ID validation", idGuard, "videoId !== null",
    "https://youtube.com/watch?v=short", "https://youtube.com/watch?v=short", "generic", /URL\/classification contract/],
  ["E allow traversal ID", idGuard, 'videoId !== null && (videoId.includes("..") || (videoId.length === 11 && /^[A-Za-z0-9_-]{11}$/.test(videoId)))',
    "https://youtube.com/watch?v=../redirect", "https://youtube.com/watch?v=../redirect", "generic", /URL\/classification contract/],
  ["F retain arbitrary query", "url: " + tick + "https://www.youtube.com/embed/$" + "{videoId}?rel=0&modestbranding=1" + tick,
    "url: " + tick + "https://www.youtube.com/embed/$" + "{videoId}$" + "{parsed.search}" + tick,
    "https://youtube.com/watch?v=M7lc1UVf-VE&next=https://evil.test", canonical, "trusted-youtube", /URL\/classification contract/],
  ["G JavaScript iframe", "if (!/^https?:\\/\\//i.test(trimmed)) {",
    "if (!/^(?:https?:\\/\\/|javascript:)/i.test(trimmed)) {",
    "javascript:window.__SEC1E_EXECUTED=true", null, "generic", /Unexpected iframe count/],
  ["Missing sandbox", 'sandbox={frame.kind === "trusted-youtube" ? YOUTUBE_URL_SANDBOX : GENERIC_URL_SANDBOX}', "",
    "https://example.test/", "https://example.test/", "generic", /Exact sandbox contract/],
  ["Generic top navigation", 'const GENERIC_URL_SANDBOX = "' + genericSandbox + '"',
    'const GENERIC_URL_SANDBOX = "' + genericSandbox + ' allow-top-navigation"',
    "https://example.test/", "https://example.test/", "generic", /Exact sandbox contract/],
];
for (const [name, before, after, input, expected, kind, failure] of mutations) {
  test("Mutation rejected: " + name, () => {
    assert.equal(productionSource.split(before).length - 1, 1, "Mutation targets exactly one expression");
    const mutant = compileComponent(productionSource.replace(before, after));
    assert.throws(() => assertPreview(mutant, input, expected, kind), (error) => {
      assert.ok(error instanceof assert.AssertionError, "A runtime error is not a killed mutation");
      assert.match(error.message, failure);
      return true;
    });
  });
}

// React rendering cannot prove browser isolation. Consume real local-browser observations
// bound to this component SHA-256. Missing browser evidence is explicitly SKIP, not PASS.
const browserEvidencePath = process.env.SECURITY1E_BROWSER_EVIDENCE;
function browserEvidence() {
  const evidence = JSON.parse(readFileSync(browserEvidencePath, "utf8"));
  assert.equal(evidence.componentSha256, createHash("sha256").update(productionBytes).digest("hex"));
  assert.ok(evidence.browser.name && evidence.browser.version && evidence.browser.os);
  return evidence;
}
function assertBrowserIsolation(sample) {
  assert.equal(sample.sandbox, genericSandbox);
  assert.equal(sample.sandboxAfterReload, genericSandbox);
  assert.equal(sample.parentSentinel, "PARENT_UNCHANGED");
  const accesses = sample.events.filter((e) => e.action === "access");
  assert.ok(accesses.some((e) => e.phase === "initial"));
  assert.ok(accesses.some((e) => e.phase === "after_reload"));
  for (const e of accesses) {
    assert.equal(e.script_executed, true);
    assert.equal(e.origin, "null");
    for (const key of ["parent_dom", "parent_dom_write", "parent_token", "own_localStorage", "own_sessionStorage", "cookie_read", "cookie_write"]) {
      assert.equal(e[key].status, "BLOCKED", key);
    }
    assert.equal(e.frame_element.result, "null");
  }
  for (const phase of ["automatic", "click"]) {
    const nav = sample.events.find((e) => e.action === "navigation" && e.phase === phase);
    assert.ok(nav, phase);
    if (phase === "click") assert.equal(nav.trusted, true);
    assert.equal(nav.top.status, "BLOCKED");
    assert.equal(nav.parent.status, "BLOCKED");
  }
  assert.ok(sample.events.some((e) => e.action === "popup" && e.trusted && e.opened.result === false));
  assert.ok(sample.events.some((e) => e.action === "target_blank" && e.trusted));
  assert.equal(sample.popupRequests, 0);
  assert.equal(sample.topNavigationRequests, 0);
  assert.ok(sample.events.some((e) => e.action === "escape_attempt" && e.removal.result.removed === false && e.parent_lookup.status === "BLOCKED"));
}
test("Browser evidence: same-origin isolation, popups, top navigation and forms", { skip: !browserEvidencePath }, () => {
  const sample = browserEvidence().sameOrigin;
  assertBrowserIsolation(sample);
  assert.ok(sample.events.some((e) => e.form_submitted === true && e.parent_dom.status === "BLOCKED" && e.parent_token.status === "BLOCKED"));
});
test("Browser evidence: cross-origin redirect to parent stays isolated", { skip: !browserEvidencePath }, () => {
  const sample = browserEvidence().redirect;
  assert.notEqual(new URL(sample.initialUrl).origin, new URL(sample.parentUrl).origin);
  assert.equal(new URL(sample.documentUrl).origin, new URL(sample.parentUrl).origin);
  assertBrowserIsolation(sample);
});
test("Browser evidence: real YouTube initializes with the minimal sandbox", { skip: !browserEvidencePath }, () => {
  const sample = browserEvidence().youtube;
  assert.equal(sample.src, canonical);
  assert.equal(sample.sandbox, youtubeSandbox);
  assert.match(sample.snapshot, /Reproductor de video de YouTube|YouTube video player/);
  assert.match(sample.snapshot, /Reproducir video|Play video/);
  assert.match(sample.snapshot, /thumbnail-image/);
  assert.equal(sample.errors.length, 0, "Record and investigate any browser errors");
});
