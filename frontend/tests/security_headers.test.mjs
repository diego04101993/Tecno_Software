// Run with: node --test frontend/tests/security_headers.test.mjs
// No server, dependencies, environment files or application startup required.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const repository = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const caddySource = readFileSync(resolve(repository, "frontend/Caddyfile.production"), "utf8");
const mediaSource = readFileSync(resolve(repository, "backend/app/services/media_streaming.py"), "utf8");
const svgPolicy = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:";
const expectedDirectives = {
  "default-src": ["'none'"],
  "script-src": ["'self'"],
  "style-src": ["'self'", "https://fonts.googleapis.com"],
  "style-src-attr": ["'none'"],
  "img-src": ["'self'", "http:", "https:", "data:", "blob:"],
  "font-src": ["https://fonts.gstatic.com"],
  "connect-src": ["'self'", "https://api.tecnocontrolcloud.com"],
  "media-src": ["'self'", "http:", "https:", "data:", "blob:"],
  "frame-src": ["http:", "https:"],
  "object-src": ["'none'"],
  "base-uri": ["'none'"],
  "form-action": ["'self'"],
  "frame-ancestors": ["'none'"],
  "worker-src": ["'none'"],
  "manifest-src": ["'none'"],
};
const sensitiveQueryKeys = [
  "token", "access_token", "player_token", "issued_player_token",
  "authorization", "password", "channel_code", "activation_code",
];
const expectedGlobalOptions = `{
    log {
        format filter {
            wrap json
            fields {
                request>uri query {
${sensitiveQueryKeys.map((key) => `                    replace ${key} [REDACTED]`).join("\n")}
                }
            }
        }
    }
    servers {
        trusted_proxies static private_ranges
    }
}
`;

// Parse block structure, preserving quoted values, {$ENV} and {path} placeholders.
// This deliberately supports the versioned file's grammar, not all Caddy syntax.
// Unknown syntax fails closed; Caddy's own validate is a separate integration check.
function parseCaddy(source) {
  const tokenPattern = /[ \t\r]+|#[^\r\n]*|\n|\{[^\s{}]+\}|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"#]+/gy;
  const tokens = [];
  let offset = 0;
  while (offset < source.length) {
    tokenPattern.lastIndex = offset;
    const match = tokenPattern.exec(source);
    assert.ok(match, `Unsupported Caddy syntax at offset ${offset}`);
    offset = tokenPattern.lastIndex;
    const raw = match[0];
    if (/^[ \t\r#]/.test(raw)) continue;
    tokens.push(raw.startsWith('"') ? { value: JSON.parse(raw) } : raw);
  }
  let index = 0;
  function block(nested = false) {
    const nodes = [];
    while (index < tokens.length) {
      if (tokens[index] === "\n") { index++; continue; }
      if (tokens[index] === "}") {
        assert.ok(nested, "Unexpected closing block");
        index++;
        return nodes;
      }
      const words = [];
      while (index < tokens.length && !["\n", "{", "}"].includes(tokens[index])) {
        const token = tokens[index++];
        words.push(typeof token === "object" ? token.value : token);
      }
      const node = { words, children: null };
      if (tokens[index] === "{") { index++; node.children = block(true); }
      else assert.ok(words.length, "Empty directive");
      nodes.push(node);
    }
    assert.equal(nested, false, "Unclosed block");
    return nodes;
  }
  return block();
}

function parsePolicy(value) {
  const directives = {};
  for (const part of value.split(";").map((entry) => entry.trim()).filter(Boolean)) {
    const [name, ...sources] = part.split(/\s+/);
    assert.equal(Object.hasOwn(directives, name), false, `Duplicate directive: ${name}`);
    directives[name] = sources;
  }
  return directives;
}

function inspectConfiguration(source, svgSource = mediaSource) {
  const sites = parseCaddy(source);
  assert.equal(sites.length, 3, "Only global options, dashboard and API sites are expected");
  const findSite = (address) => {
    const matches = sites.filter((site) => site.words.length === 1 && site.words[0] === address);
    assert.equal(matches.length, 1, `Exactly one ${address} site is required`);
    return matches[0];
  };
  const app = findSite("{$APP_DOMAIN}");
  const api = findSite("{$API_DOMAIN}");
  const global = sites.filter((site) => site.words.length === 0);
  assert.deepEqual(global, parseCaddy(expectedGlobalOptions), "Global options allow only the approved log redaction and trusted proxies; no shared headers");
  assert.deepEqual(api.children, parseCaddy("reverse_proxy backend:8000\n"), "API proxy scope must remain unchanged");

  const headerBlocks = app.children.filter((node) => node.words[0] === "header");
  assert.equal(headerBlocks.length, 1, "Dashboard needs one unconditional header block");
  assert.deepEqual(headerBlocks[0].words, ["header"], "No route or status matcher may narrow the headers");
  assert.ok(headerBlocks[0].children, "Header block required");
  assert.deepEqual(app.children.filter((node) => node.words[0] !== "header"), parseCaddy("root * /srv\nencode zstd gzip\ntry_files {path} /index.html\nfile_server\n"), "Serving, encoding and SPA fallback must remain unchanged");

  const headers = {};
  for (const node of headerBlocks[0].children) {
    assert.equal(node.children, null, "No nested header operations");
    assert.equal(node.words.length, 2, "Each header must have one value");
    const [name, value] = node.words;
    const key = name.toLowerCase();
    assert.equal(Object.hasOwn(headers, key), false, `Duplicate header: ${name}`);
    headers[key] = value;
  }
  assert.deepEqual(Object.keys(headers).sort(), ["content-security-policy", "referrer-policy", "x-content-type-options"], "Exactly the three approved headers; enforcement only, no Report-Only or reporting endpoint");
  assert.equal(headers["x-content-type-options"], "nosniff");
  assert.equal(headers["referrer-policy"], "strict-origin-when-cross-origin");
  const directives = parsePolicy(headers["content-security-policy"]);
  assert.deepEqual(directives, expectedDirectives, "The complete audited policy must remain exact");

  const svgMatch = svgSource.match(/^SVG_CONTENT_SECURITY_POLICY = "([^"\r\n]*)"\r?$/m);
  assert.ok(svgMatch, "Independent SVG policy definition must remain present");
  assert.equal(svgMatch[1], svgPolicy, "Do not replace SVG's enforced policy with the dashboard policy");
  return { headers, directives };
}

test("production Caddyfile scopes exactly three approved headers to dashboard; API unchanged and global log redaction exact", () => {
  inspectConfiguration(caddySource);
});

for (const [name, expected] of Object.entries(expectedDirectives)) {
  test(`production enforced directive: ${name}`, () => {
    assert.deepEqual(inspectConfiguration(caddySource).directives[name], expected);
  });
}

test("parser respects comments, quoted braces and whitespace without losing host scope", () => {
  const ast = parseCaddy('# fake {$API_DOMAIN} {\n{$APP_DOMAIN} {\nheader {\nExample "literal { value } # not comment"\n}\n}\n');
  assert.deepEqual(ast[0].words, ["{$APP_DOMAIN}"]);
  assert.deepEqual(ast[0].children[0].children[0].words, ["Example", "literal { value } # not comment"]);
  assert.throws(() => parseCaddy("{$APP_DOMAIN} {\n"), /Unclosed block/);
  inspectConfiguration(caddySource.replaceAll("    ", "\t") + "\n# Comments are not directives\n");
});

function replaceOnce(source, from, to) {
  assert.equal(source.split(from).length - 1, 1, `Mutation anchor must be unique: ${from}`);
  return source.replace(from, to);
}

const mutations = [
  ["A: CSP remains Report-Only", (source) => replaceOnce(source, 'Content-Security-Policy "', 'Content-Security-Policy-Report-Only "')],
  ["B: enforcement and Report-Only coexist", (source) => {
    const header = source.match(/^        Content-Security-Policy "[^"\r\n]*"/m)?.[0];
    assert.ok(header, "CSP header mutation anchor");
    return replaceOnce(source, header, header + "\n" + header.replace("Content-Security-Policy", "Content-Security-Policy-Report-Only"));
  }],
  ["C: allow inline scripts", (source) => replaceOnce(source, "script-src 'self';", "script-src 'self' 'unsafe-inline';")],
  ["D: allow unsafe-eval", (source) => replaceOnce(source, "script-src 'self';", "script-src 'self' 'unsafe-eval';")],
  ["E: broaden connect-src with wildcard", (source) => replaceOnce(source, "connect-src 'self' https://api.tecnocontrolcloud.com;", "connect-src *;")],
  ["F: remove API from connect-src", (source) => replaceOnce(source, "connect-src 'self' https://api.tecnocontrolcloud.com;", "connect-src 'self';")],
  ["G: remove Google Fonts stylesheet origin", (source) => replaceOnce(source, "style-src 'self' https://fonts.googleapis.com;", "style-src 'self';")],
  ["H: remove Google Fonts asset origin", (source) => replaceOnce(source, "font-src https://fonts.gstatic.com;", "font-src 'none';")],
  ["I: break generic embeds with YouTube-only frame-src", (source) => replaceOnce(source, "frame-src http: https:;", "frame-src https://www.youtube.com;")],
  ["J: remove frame-ancestors", (source) => replaceOnce(source, "frame-ancestors 'none';", "")],
  ["K: relax object-src", (source) => replaceOnce(source, "object-src 'none';", "object-src 'self';")],
  ["L: relax base-uri", (source) => replaceOnce(source, "base-uri 'none';", "base-uri 'self';")],
  ["M: move headers to API site", (source) => {
    const header = source.match(/^    header \{\r?\n[\s\S]*?^    \}/m)?.[0];
    assert.ok(header, "Header block mutation anchor");
    return replaceOnce(replaceOnce(source, header, ""), "{$API_DOMAIN} {", "{$API_DOMAIN} {\n" + header);
  }],
  ["N: change Referrer-Policy", (source) => replaceOnce(source, 'Referrer-Policy "strict-origin-when-cross-origin"', 'Referrer-Policy "no-referrer"')],
  ["O: remove nosniff", (source) => replaceOnce(source, '        X-Content-Type-Options "nosniff"', "")],
  ["add an unauthorized reporting endpoint", (source) => replaceOnce(source, "manifest-src 'none';", "manifest-src 'none'; report-uri /csp-report;")],
  ["duplicate a CSP directive", (source) => replaceOnce(source, "script-src 'self';", "script-src 'self'; script-src https:;")],
  ["duplicate an enforced CSP header", (source) => replaceOnce(source, 'Content-Security-Policy "', 'Content-Security-Policy "default-src *"\n        Content-Security-Policy "')],
  ["also attach dashboard CSP to API", (source) => replaceOnce(source, "reverse_proxy backend:8000", 'header Content-Security-Policy "default-src *"\n    reverse_proxy backend:8000')],
  ["relax inline style attributes", (source) => replaceOnce(source, "style-src-attr 'none';", "style-src-attr 'unsafe-inline';")],
];

for (const [name, mutate] of mutations) {
  test(`mutation rejected: ${name}`, () => {
    const changed = mutate(caddySource);
    assert.notEqual(changed, caddySource, "Mutation must change the fixture");
    assert.throws(() => inspectConfiguration(changed), { name: "AssertionError" });
  });
}

// Keep the logging allowance exact: it must not bypass any header/CSP checks.
const logMutations = [
  ...sensitiveQueryKeys.map((key) => [`remove ${key} redaction`, (source) => replaceOnce(source, `replace ${key} [REDACTED]`, "")]),
  ["remove global log filter", (source) => {
    const block = source.match(/^    log \{\r?\n[\s\S]*?^    \}/m)?.[0];
    assert.ok(block, "Global log block mutation anchor");
    return replaceOnce(source, block, "");
  }],
  ["filter a different log field", (source) => replaceOnce(source, "request>uri query", "request>url query")],
  ["expose a token through its replacement", (source) => replaceOnce(source, "replace token [REDACTED]", "replace token {query.token}")],
  ["add shared headers beside approved logging", (source) => replaceOnce(source, "    servers {", '    header Content-Security-Policy "default-src *"\n    servers {')],
];

for (const [name, mutate] of logMutations) {
  test(`logging mutation rejected: ${name}`, () => {
    const changed = mutate(caddySource);
    assert.notEqual(changed, caddySource, "Mutation must change the fixture");
    assert.throws(() => inspectConfiguration(changed), /Global options/);
  });
}

test("mutation rejected: modify independent SVG CSP as if it were the dashboard policy", () => {
  const changed = replaceOnce(mediaSource, svgPolicy, "default-src 'self'");
  assert.throws(() => inspectConfiguration(caddySource, changed), /SVG/);
});

test("all mutation checks leave the production files byte-identical to their test inputs", () => {
  assert.equal(readFileSync(resolve(repository, "frontend/Caddyfile.production"), "utf8"), caddySource);
  assert.equal(readFileSync(resolve(repository, "backend/app/services/media_streaming.py"), "utf8"), mediaSource);
});
