// R3: exercise real password forms with isolated React and mocked local requests.
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const deps = process.env.SCREEN_ROTATION_TEST_DEPS;
assert.ok(deps, "Set SCREEN_ROTATION_TEST_DEPS to isolated React/test-renderer 18.3.1");
const testRequire = createRequire(join(resolve(deps), "package.json"));
const React = testRequire("react");
const { create, act } = testRequire("react-test-renderer");
const appRequire = createRequire(join(frontend, "package.json"));
const { buildSync } = createRequire(appRequire.resolve("vite/package.json"))("esbuild");
assert.equal(React.version, "18.3.1");

const sources = {
  client: ["components/clients/ClientUsersPanel.tsx", "ClientUsersPanel"],
  team: ["components/team/TeamUserFormDialog.tsx", "TeamUserFormDialog"],
  page: ["pages/ClientsPage.tsx", "ClientsPage"],
  teamPage: ["pages/TeamUsersPage.tsx", "TeamUsersPage"],
};
const user = { id: "fixture-user", full_name: "Existing user", email: "existing@example.com",
  role: "staff_operator", status: "active", is_active: true, client_id: null, branch_id: null, permissions: [] };
const originalFetch = globalThis.fetch;
after(() => { globalThis.fetch = originalFetch; });

function compile(kind, mutate = (source) => source) {
  const [path, name] = sources[kind];
  const file = join(frontend, "src", path);
  let source = mutate(readFileSync(file, "utf8"));
  // Only the auth boundary is mocked; the actual form and request code run.
  source = source.replace(/import \{ useAuth \} from "[^"]+";/,
    'const useAuth = () => ({ token: "fixture-only", user: { role: "super_admin", permissions: [] } });');
  const result = buildSync({
    stdin: { contents: source, loader: "tsx", sourcefile: file, resolveDir: dirname(file) },
    bundle: true, write: false, platform: "node", format: "cjs", jsx: "automatic",
    external: ["react", "react/jsx-runtime", "react-router-dom"],
    define: { "import.meta.env": JSON.stringify({ DEV: true, PROD: false }) },
  });
  const module = { exports: {} };
  const require = (id) => id === "react-router-dom" ? {
    Link: ({ children }) => React.createElement("a", null, children),
    useNavigate: () => () => {}, useParams: () => ({}), useLocation: () => ({ pathname: "/" }),
  } : testRequire(id);
  new Function("require", "module", "exports", result.outputFiles[0].text)(require, module, module.exports);
  return module.exports[name];
}
const components = Object.fromEntries(Object.keys(sources).map((kind) => [kind, compile(kind)]));

async function mount(kind, { edit = false, component = components[kind] } = {}) {
  const requests = [], submissions = [];
  globalThis.fetch = async (url, options = {}) => {
    const address = new URL(url);
    assert.equal(address.origin, "http://localhost:8000", "No external requests permitted");
    const method = options.method ?? "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ path: address.pathname, method, body });
    let payload = [];
    if (method === "GET" && address.pathname.endsWith("/users")) payload = edit ? [user] : [];
    if (method !== "GET") payload = address.pathname === "/api/clients" ?
      { id: "fixture-client", name: "Fixture", status: "active" } : user;
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const props = kind === "client" ? { clientId: "fixture-client", token: "fixture-only" } :
    kind === "team" ? { open: true, mode: edit ? "edit" : "create", initialUser: edit ? user : null,
      onCancel() {}, onSubmit: (payload) => submissions.push(payload) } : {};
  let view;
  await act(async () => { view = create(React.createElement(component, props)); });
  const button = (label) => view.root.findAllByType("button").find((node) => node.children.join("") === label);
  if (edit && kind !== "team") {
    await act(async () => button("Editar").props.onClick());
  }
  return {
    view, requests, submissions,
    input: () => view.root.findByProps({ type: "password" }),
    async change(value) { await act(async () => this.input().props.onChange({ target: { value } })); },
    async submit() {
      await act(async () => {
        if (kind === "team" || kind === "teamPage") button(edit ? "Guardar cambios" : "Crear usuario").props.onClick();
        else view.root.findByType("form").props.onSubmit({ preventDefault() {} });
      });
    },
    writes: () => requests.filter((r) => r.method !== "GET"),
    async close() { await act(async () => view.unmount()); },
  };
}

for (const kind of ["client", "team", "page"]) {
  test(kind + ": starts empty and requires an explicit new password", async () => {
    const form = await mount(kind);
    try {
      assert.equal(form.input().props.value, "");
      await form.submit();
      assert.equal(form.writes().length + form.submissions.length, 0);
    } finally { await form.close(); }
  });

  test(kind + ": rejects 14/129/whitespace before submitting", async () => {
    const form = await mount(kind);
    try {
      for (const value of ["a".repeat(14), "a".repeat(129), " ".repeat(15)]) {
        await form.change(value);
        await form.submit();
        assert.equal(form.writes().length + form.submissions.length, 0);
      }
    } finally { await form.close(); }
  });

  test(kind + ": accepts 15/128 Unicode code points and preserves exact spaces", async () => {
    for (const value of ["abcdefghijklmno", "ab".repeat(64), "🚀".repeat(128), "  fixture frase larga  "]) {
      const form = await mount(kind);
      try {
        await form.change(value);
        await form.submit();
        const sent = kind === "team" ? form.submissions[0] :
          form.writes().find((request) => request.path.endsWith("/users"))?.body;
        assert.ok(sent);
        assert.equal(sent.password, value);
      } finally { await form.close(); }
    }
  });
}

test("client edit: no password sends null; explicit valid change preserves spaces", async () => {
  const form = await mount("client", { edit: true });
  try {
    assert.equal(form.input().props.value, "");
    await form.submit();
    assert.equal(form.writes()[0].body.password, null);
  } finally { await form.close(); }
  const changed = await mount("client", { edit: true });
  try {
    const value = "  edited fixture phrase  ";
    await changed.change(value);
    await changed.submit();
    assert.equal(changed.writes()[0].body.password, value);
  } finally { await changed.close(); }
});

test("team edit: actual parent converts unchanged blank to null", async () => {
  const form = await mount("teamPage", { edit: true });
  try {
    assert.equal(form.input().props.value, "");
    await form.submit();
    const patch = form.writes().find((request) => request.method === "PATCH");
    assert.ok(patch);
    assert.equal(patch.body.password, null);
  } finally { await form.close(); }
});

test("page: client without initial user does not require a password", async () => {
  const form = await mount("page");
  try {
    const checkbox = form.view.root.findAllByProps({ type: "checkbox" })[0];
    await act(async () => checkbox.props.onChange({ target: { checked: false } }));
    await form.submit();
    assert.equal(form.writes().length, 1);
    assert.equal(form.writes()[0].path, "/api/clients");
  } finally { await form.close(); }
});

test("R3 mutation: reintroducing a fixed frontend password is detected in all three forms", async () => {
  let detected = 0;
  for (const kind of ["client", "team", "page"]) {
    const component = compile(kind, (source) => {
      const pattern = kind === "page" ? /temporary_password: "",/ : /password: "",/;
      assert.equal(source.match(pattern)?.length, 1);
      return source.replace(pattern, (match) => match.replace('""', '"mutation-only-default"'));
    });
    const form = await mount(kind, { component });
    try {
      assert.throws(() => assert.equal(form.input().props.value, ""), assert.AssertionError);
      detected++;
    } finally { await form.close(); }
  }
  assert.equal(detected, 3);
});
