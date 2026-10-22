// node --test frontend/tests/build_configuration.test.mjs
// Execute both real Vite configs and api.ts in memory; never contact the API.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import * as vite from "vite";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repository = resolve(frontend, "..");
const appRequire = createRequire(join(frontend, "package.json"));
const ts = appRequire("typescript");
const { transformSync } = createRequire(appRequire.resolve("vite/package.json"))("esbuild");
const read = (file) => readFileSync(join(repository, file), "utf8");
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const lockHash = sha(read("frontend/pnpm-lock.yaml"));
const official = "https://api.tecnocontrolcloud.com";
const apiSource = read("frontend/src/lib/api.ts");
const fixtureRoot = mkdtempSync(join(tmpdir(), "sec1g-build-tests-"));
const configs = ["js", "ts"].map((extension) => ({
  extension, source: read(`frontend/vite.config.${extension}`),
}));

function evaluateConfig(source, extension) {
  const code = transformSync(source, { loader: extension, format: "cjs", target: "es2022" }).code;
  const module = { exports: {} };
  const require = (name) => name === "vite" ? vite : appRequire(name);
  new Function("require", "module", "exports", code)(require, module, module.exports);
  return module.exports.default;
}

function withApiEnvironment(primary, secondary, fn) {
  const keys = ["VITE_API_BASE_URL", "VITE_API_URL"];
  const saved = keys.map((key) => process.env[key]);
  try {
    for (const [index, value] of [primary, secondary].entries()) {
      if (value === undefined) delete process.env[keys[index]];
      else process.env[keys[index]] = value;
    }
    return fn();
  } finally {
    for (const [index, value] of saved.entries()) {
      if (value === undefined) delete process.env[keys[index]];
      else process.env[keys[index]] = value;
    }
  }
}

function guard(source, extension, primary, secondary, options = {}) {
  return withApiEnvironment(primary, secondary, () => {
    const config = evaluateConfig(source, extension);
    const plugin = config.plugins.flat().find((item) => item.name === "production-api-configuration");
    assert.equal(typeof plugin?.configResolved, "function", "The effective exported config must contain the guard");
    const mode = options.mode ?? "production";
    plugin.configResolved({
      command: options.command ?? "build", mode,
      env: vite.loadEnv(mode, options.root ?? fixtureRoot, "VITE_"),
    });
  });
}

function compileApi(source, env) {
  return transformSync(source, {
    loader: "ts", format: "cjs", target: "es2022", treeShaking: true, minify: true,
    define: Object.fromEntries(["DEV", "PROD", "VITE_API_BASE_URL", "VITE_API_URL"]
      .map((key) => [`import.meta.env.${key}`, JSON.stringify(env[key]) ?? "undefined"])),
  }).code;
}

function runtime(source, env, location = { protocol: "https:", hostname: "qa.invalid" }) {
  const module = { exports: {} };
  runInNewContext(compileApi(source, env), {
    module, exports: module.exports, ...(location ? { window: { location } } : {}),
    fetch() { throw new Error("Network forbidden in configuration tests"); },
  }, { timeout: 1000 });
  return module.exports.API_URL;
}

function instructions(source) {
  return source.replace(/\\\r?\n/g, " ").split(/\r?\n/)
    .map((line) => line.trim()).filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const match = line.match(/^([A-Z]+)\s+(.+)$/);
      assert.ok(match, `Unsupported Dockerfile instruction: ${line}`);
      return { op: match[1], value: match[2].replace(/\s+/g, " ") };
    });
}

// Read a mapping path by indentation, not matches elsewhere in Compose.
function yamlMappingValue(source, keys) {
  const stack = [];
  for (const line of source.split(/\r?\n/)) {
    const match = line.match(/^( *)([\w-]+):(?:\s+(.*))?$/);
    if (!match) continue;
    const indent = match[1].length;
    while (stack.length && stack.at(-1).indent >= indent) stack.pop();
    stack.push({ indent, key: match[2] });
    if (stack.map((entry) => entry.key).join(".") === keys.join(".")) return match[3] ?? "";
  }
  return undefined;
}

const structure = {
  manifest: JSON.parse(read("frontend/package.json")),
  docker: read("frontend/Dockerfile.production"),
  compose: read("docker-compose.production.yml"),
};

function validateStructure({ manifest, docker, compose }) {
  assert.equal(manifest.packageManager, "pnpm@10.17.1");
  assert.equal(manifest.engines.node, "24.22.1");
  const rows = instructions(docker);
  assert.deepEqual(rows.filter((row) => row.op === "FROM").map((row) => row.value), [
    "node:24.22.1-alpine AS build", "caddy:2.11.4-alpine",
  ]);
  const run = rows.filter((row) => row.op === "RUN").map((row) => row.value);
  assert.ok(run.includes("pnpm install --frozen-lockfile"));
  assert.ok(run.includes("pnpm run build"));
  assert.ok(!run.some((command) => /\bnpm\s+(install|ci)\b/.test(command)));
  const bootstrap = run.flatMap((command) => command.split(/\s*&&\s*/));
  assert.ok(bootstrap.includes("corepack enable"));
  assert.ok(bootstrap.includes("corepack prepare pnpm@10.17.1 --activate"));
  assert.ok(bootstrap.includes('test "$(pnpm --version)" = "10.17.1"'));
  const deps = rows.findIndex((row) => row.op === "COPY" &&
    row.value === "package.json pnpm-lock.yaml pnpm-workspace.yaml ./");
  const install = rows.findIndex((row) => row.op === "RUN" && row.value === "pnpm install --frozen-lockfile");
  const source = rows.findIndex((row) => row.op === "COPY" && row.value === ". .");
  const build = rows.findIndex((row) => row.op === "RUN" && row.value === "pnpm run build");
  assert.ok(deps >= 0 && deps < install && install < source && source < build);
  for (const key of ["VITE_API_BASE_URL", "VITE_API_URL"]) {
    const argument = rows.findIndex((row) => row.op === "ARG" && row.value === key);
    const environment = rows.findIndex((row) => row.op === "ENV" && row.value === `${key}=\${${key}}`);
    assert.ok(argument > source && environment > argument && environment < build);
    assert.equal(yamlMappingValue(compose, ["services", "frontend", "build", "args", key]), `\${${key}:-}`);
  }
  assert.equal(yamlMappingValue(compose, ["services", "frontend", "build", "dockerfile"]), "Dockerfile.production");
  assert.ok(rows.some((row) => row.op === "COPY" && row.value === "--from=build /app/dist /srv"));
}

test("canonical package, Docker stages and Compose frontend build arguments", () => validateStructure(structure));
test("pnpm policy approves only esbuild using the pnpm 10.17.1 mechanism", () => {
  const policy = read("frontend/pnpm-workspace.yaml").trim().split(/\r?\n/).map((line) => line.trim());
  assert.deepEqual(policy, ["onlyBuiltDependencies:", "- esbuild"]);
});

const cases = [
  ["missing", undefined, undefined, false],
  ["empty", "", "", false],
  ["whitespace", "  ", " \t ", false],
  ["primary root", official, undefined, true],
  ["primary root slash", official + "/", undefined, true],
  ["primary api", official + "/api", undefined, true],
  ["primary api slash", official + "/api/", undefined, true],
  ["outer whitespace", " " + official + " ", undefined, true],
  ["secondary only", undefined, official, true],
  ["blank primary secondary valid", " ", official, true],
  ["both valid", official, official + "/api", true],
  ["valid primary invalid secondary", official, "https://wrong.invalid", true],
  ["invalid primary valid secondary", "https://wrong.invalid", official, false],
  ["invalid secondary", undefined, "https://wrong.invalid", false],
  ["http", "http://api.tecnocontrolcloud.com", undefined, false],
  ["wrong host", "https://wrong.invalid", undefined, false],
  ["hostname suffix", official + ".wrong.invalid", undefined, false],
  ["localhost", "https://localhost", undefined, false],
  ["loopback", "https://127.0.0.1", undefined, false],
  ["ipv6 loopback", "https://[::1]", undefined, false],
  ["port 8000", official + ":8000", undefined, false],
  ["explicit default port", official + ":443", undefined, false],
  ["username", "https://qa@api.tecnocontrolcloud.com", undefined, false],
  ["password", "https://qa:dummy@api.tecnocontrolcloud.com", undefined, false],
  ["empty credentials", "https://@api.tecnocontrolcloud.com", undefined, false],
  ["query", official + "?qa=1", undefined, false],
  ["empty query", official + "?", undefined, false],
  ["fragment", official + "#qa", undefined, false],
  ["empty fragment", official + "#", undefined, false],
  ["arbitrary path", official + "/other", undefined, false],
  ["normalized dot segments", official + "/other/../api", undefined, false],
  ["encoded dot segments", official + "/other/%2e%2e/api", undefined, false],
  ["internal spaces", "https://api. tecnocontrolcloud.com", undefined, false],
  ["internal tab", "https://api.\ttecnocontrolcloud.com", undefined, false],
  ["invalid URL", "https://", undefined, false],
  ["relative URL", "/api", undefined, false],
];

for (const { source, extension } of configs) {
  for (const [name, primary, secondary, valid] of cases) {
    test(`${extension}: production guard ${name}`, () => {
      const execute = () => guard(source, extension, primary, secondary);
      if (valid) assert.doesNotThrow(execute);
      else assert.throws(execute, /Invalid production API configuration/);
    });
  }
  test(`${extension}: serve is unrestricted, including production mode`, () => {
    guard(source, extension, undefined, undefined, { command: "serve", mode: "development" });
    guard(source, extension, "http://localhost:8000", undefined, { command: "serve" });
  });
  test(`${extension}: build in another mode preserves the explicitly scoped guard`, () => {
    guard(source, extension, undefined, undefined, { mode: "development" });
  });
  test(`${extension}: process env wins over env files, including an empty value`, () => {
    const root = join(fixtureRoot, extension);
    mkdirSync(root, { recursive: true });
    writeFileSync(join(root, ".env.production"), `VITE_API_BASE_URL=${official}\n`);
    guard(source, extension, undefined, undefined, { root });
    assert.throws(() => guard(source, extension, "", undefined, { root }), /Invalid production API/);
    assert.throws(() => guard(source, extension, "https://wrong.invalid", official, { root }), /Invalid production API/);
    guard(source, extension, "", official, { root });
  });
  test(`${extension}: diagnostics do not echo invalid credential-bearing input`, () => {
    assert.throws(() => guard(source, extension, "https://qa:PRIVATE_MARKER@wrong.invalid", undefined), (error) => {
      assert.match(error.message, /Invalid production API/);
      assert.ok(!error.message.includes("PRIVATE_MARKER"));
      return true;
    });
  });
}

test("JS and TS Vite configs have identical executable semantics", () => {
  const decisions = configs.map(({ source, extension }) => cases.map(([name, primary, secondary, valid]) => {
    let accepted;
    try {
      guard(source, extension, primary, secondary);
      accepted = true;
    } catch (error) {
      assert.match(error.message, /Invalid production API configuration/, `${extension}: ${name}`);
      accepted = false;
    }
    assert.equal(accepted, valid, `${extension}: ${name}`);
    return accepted;
  }));
  assert.deepEqual(decisions[0], decisions[1]);
});

for (const value of [official, official + "/", official + "/api", official + "/api/"]) {
  test(`production runtime normalizes ${value}`, () => {
    assert.equal(runtime(apiSource, { PROD: true, DEV: false, VITE_API_BASE_URL: value }), official + "/api");
  });
}
test("runtime preserves primary precedence and legacy secondary", () => {
  assert.equal(runtime(apiSource, { PROD: true, DEV: false, VITE_API_URL: official }), official + "/api");
  assert.equal(runtime(apiSource, { PROD: true, DEV: false, VITE_API_BASE_URL: official, VITE_API_URL: "https://wrong.invalid" }), official + "/api");
});

function assertRuntimeBoundary(source) {
  assert.throws(() => runtime(source, { DEV: false, PROD: true }), /Missing API configuration/);
  assert.equal(runtime(source, { DEV: true, PROD: false }), "https://qa.invalid:8000/api");
  assert.equal(runtime(source, { DEV: true, PROD: false }, { protocol: "http:", hostname: "localhost" }), "http://localhost:8000/api");
  assert.equal(runtime(source, { DEV: true, PROD: false }, null), "http://localhost:8000/api");
  const production = compileApi(source, { DEV: false, PROD: true, VITE_API_BASE_URL: official });
  assert.ok(!production.includes(":8000/api"), "Production code must eliminate development inference");
}
test("development fallback survives and production fallback is eliminated", () => assertRuntimeBoundary(apiSource));

const structureMutations = [
  ["npm install", "docker", "pnpm install --frozen-lockfile", "npm install"],
  ["npm ci", "docker", "pnpm install --frozen-lockfile", "npm ci"],
  ["unfrozen install", "docker", "pnpm install --frozen-lockfile", "pnpm install"],
  ["floating Node", "docker", "node:24.22.1-alpine", "node:24-alpine"],
  ["floating Caddy", "docker", "caddy:2.11.4-alpine", "caddy:2-alpine"],
  ["wrong pnpm bootstrap", "docker", "pnpm@10.17.1", "pnpm@10.18.0"],
  ["missing primary Compose argument", "compose", "VITE_API_BASE_URL: \${VITE_API_BASE_URL:-}", "UNRELATED: unused"],
  ["missing secondary Compose argument", "compose", "VITE_API_URL: \${VITE_API_URL:-}", "UNRELATED: unused"],
];
for (const [name, key, before, after] of structureMutations) {
  test(`mutation rejected: ${name}`, () => {
    assert.ok(structure[key].includes(before));
    assert.throws(() => validateStructure({ ...structure, [key]: structure[key].replace(before, after) }));
  });
}
for (const value of [undefined, "pnpm@10.18.0"]) {
  test(`mutation rejected: packageManager ${String(value)}`, () => {
    assert.throws(() => validateStructure({ ...structure, manifest: { ...structure.manifest, packageManager: value } }));
  });
}

// Mutate semantic AST nodes in each real config, including TypeScript's emitted
// JS. Declarations, optional-chain helpers, formatting and quote style may vary.
function mutateSource(source, extension, replace) {
  const tree = ts.createSourceFile(`mutation.${extension}`, source, ts.ScriptTarget.Latest, true,
    extension === "ts" ? ts.ScriptKind.TS : ts.ScriptKind.JS);
  let changes = 0;
  const result = ts.transform(tree, [(context) => {
    const visit = (node) => {
      const replacement = replace(node, ts.factory);
      if (replacement && replacement !== node) {
        changes++;
        return replacement;
      }
      return ts.visitEachChild(node, visit, context);
    };
    return (node) => ts.visitNode(node, visit);
  }]);
  try {
    assert.ok(changes > 0, "The mutation must change a real policy node");
    return ts.createPrinter().printFile(result.transformed[0]);
  } finally {
    result.dispose();
  }
}

function property(node, receiver, name) {
  return ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) &&
    node.expression.text === receiver && node.name.text === name;
}

function allowOrigin(origin) {
  return (node, factory) => {
    if (ts.isStringLiteral(node) && node.text === official) return factory.createStringLiteral(origin);
    if (ts.isStringLiteral(node) && node.text === new URL(official).host) {
      return factory.createStringLiteral(new URL(origin).host);
    }
    if (new URL(origin).port && property(node, "url", "port")) return factory.createStringLiteral("");
  };
}

const guardMutations = [
  ["guard bypass", (node, factory) => {
    if (ts.isBinaryExpression(node) && ts.isIdentifier(node.left) && node.left.text === "command" &&
      ts.isStringLiteral(node.right) && node.right.text === "build" &&
      node.operatorToken.kind === ts.SyntaxKind.ExclamationEqualsEqualsToken) {
      return factory.updateBinaryExpression(node, node.left,
        factory.createToken(ts.SyntaxKind.EqualsEqualsEqualsToken), node.right);
    }
  }, undefined, undefined, false],
  ["allow wrong host", allowOrigin("https://wrong.invalid"), "https://wrong.invalid", undefined, false],
  ["allow localhost", allowOrigin("https://localhost"), "https://localhost", undefined, false],
  ["allow port 8000", allowOrigin(official + ":8000"), official + ":8000", undefined, false],
  ["remove secondary", (node, factory) => property(node, "env", "VITE_API_URL")
    ? factory.createIdentifier("undefined") : undefined, undefined, official, true],
];
for (const [name, replace, primary, secondary, shouldPass] of guardMutations) {
  test(`mutation rejected: ${name}`, () => {
    for (const { source, extension } of configs) {
      const mutant = mutateSource(source, extension, replace);
      const execute = () => guard(mutant, extension, primary, secondary);
      // Prove a real behavioral regression, rather than a parse/import error.
      if (shouldPass) assert.throws(execute, /Invalid production API configuration/);
      else assert.doesNotThrow(execute);
      const check = () => shouldPass
        ? assert.doesNotThrow(execute)
        : assert.throws(execute, /Invalid production API configuration/);
      assert.throws(check, assert.AssertionError);
    }
  });
}
test("mutation rejected: production uses development fallback", () => {
  const mutant = mutateSource(apiSource, "ts", (node, factory) =>
    ts.isPropertyAccessExpression(node) && node.name.text === "DEV" &&
    ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === "env" &&
    ts.isMetaProperty(node.expression.expression) ? factory.createTrue() : undefined);
  assert.equal(runtime(mutant, { DEV: false, PROD: true }), "https://qa.invalid:8000/api");
  assert.throws(() => assertRuntimeBoundary(mutant), assert.AssertionError);
});
test("mutation rejected: runtime removes legacy secondary", () => {
  const mutant = mutateSource(apiSource, "ts", (node, factory) =>
    ts.isPropertyAccessExpression(node) && node.name.text === "VITE_API_URL" &&
    ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === "env" &&
    ts.isMetaProperty(node.expression.expression) ? factory.createIdentifier("undefined") : undefined);
  assert.throws(() => runtime(mutant, { PROD: true, DEV: false, VITE_API_URL: official }), /Missing API configuration/);
  assert.throws(() => assert.equal(runtime(mutant, { PROD: true, DEV: false, VITE_API_URL: official }), official + "/api"));
});

test("env exclusions cover real files while Git retains template exceptions", () => {
  const git = read(".gitignore").split(/\r?\n/).filter(Boolean);
  const docker = read("frontend/.dockerignore").split(/\r?\n/).filter(Boolean);
  assert.ok(git.includes(".env") && git.includes(".env.*"));
  assert.ok(git.indexOf("!.env.example") > git.indexOf(".env.*"));
  assert.ok(git.indexOf("!.env.production.example") > git.indexOf(".env.*"));
  assert.ok(docker.includes("**/.env") && docker.includes("**/.env.*"));
  assert.ok(!docker.some((pattern) => pattern.startsWith("!")), "No Docker env exclusions may be reopened");
  for (const name of ["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "src"]) assert.ok(!docker.includes(name));
});
test("configuration tests leave the lockfile unchanged", () => {
  assert.equal(sha(read("frontend/pnpm-lock.yaml")), lockHash);
});
