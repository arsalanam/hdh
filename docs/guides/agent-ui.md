# Agent UI — the browser front door

The hdh agent has three front doors: the `hdh agent` CLI, a future MCP server
(#90), and a **React SPA served over HTTP** by the same runtime as the backend
API. This guide runs that third one end to end — the same agent, the same
pipeline, the same grounding guarantee, reached from a browser and signed in
against Keycloak.

Design: [agentic-ui-module.md](../design/agentic-ui-module.md). The identity
rules it enforces: [identity-and-authorization.md](../design/identity-and-authorization.md).

**Requires:** `hdh[agent,api]`, Node (for the SPA build), and the dependency
containers from `just deps` (PostgreSQL + Keycloak). Streaming answers need an
Anthropic API key, as the CLI agent does.

## 1. Start the dependencies

```bash
just deps          # PostgreSQL on :5433, Keycloak on :8080 with the `hdh` realm
```

`just deps` renders `deps/keycloak-realm.json` from your `HDH_AUTH_*` env and
imports it on Keycloak's **first boot**. The realm ships a provider client
(`hdh-web`, auth-code + PKCE) and a set of demo users.

> **If you added a client or user to the realm after Keycloak was already
> running:** `--import-realm` only imports a realm that does not yet exist, so a
> running container keeps the old realm and you get **"Client not found"** at
> login. Force a clean re-import with `just deps-nuke && just deps`. That
> regenerates the demo users' Keycloak IDs, so re-run `just seed-identities`
> afterwards (below).

## 2. Build the SPA

```bash
cd web
npm install
npm run build      # emits web/dist/, which the API serves at /
cd ..
```

## 3. Serve the agent

```bash
hdh serve-agent                       # http://127.0.0.1:8100
hdh serve-agent --port 9000           # a different port
hdh serve-agent --db family_medicine.db   # or set HDH_DB_URL for PostgreSQL
```

`serve-agent` serves the built `web/dist/` at `/` and the API alongside it
(`/ask`, `/ask/stream`, `/me`, `/threads`, `/conversations`, `/notes/upload`;
OpenAPI docs at `/docs`). Every data endpoint requires a valid provider-realm
token; `/health` and the SPA are open. Open **http://127.0.0.1:8100** and sign
in.

Port 8100 is deliberate: it is one of the `hdh-web` client's registered
redirect URIs. If you serve on another port, add that origin to the client's
redirect URIs / web origins in the realm, or login fails with *"Invalid
redirect URI"*.

## 4. Sign in — demo credentials

The `hdh` realm is the **provider** realm (a patient portal would be a separate
realm — the two never mix). Password equals username for every seed user:

| Username | Password | Roles |
|---|---|---|
| `dr.chen` | `dr.chen` | clinician, prescriber |
| `dr.okafor` | `dr.okafor` | prescriber |
| `nurse.reed` | `nurse.reed` | nurse |
| `clerk.diaz` | `clerk.diaz` | clerk |
| `admin` | `admin` | admin |

Use **`dr.chen`** as the default — it can read charts and prescribe/order.

For a signed-in write to carry a real `provider_id` (not just a name), link the
Keycloak users to provider profiles once — it is idempotent:

```bash
hdh seed-identities
```

## 5. What you can do

- **Ask** — type a question; watch the pipeline stages stream (topic gate →
  intent → fetching → assembling → validating), then read the grounded answer
  with its verdict. Answers are attributed to the signed-in provider.
- **Scroll back** — the sidebar is a foldable tree of **threads → runs**; a new
  conversation starts a new thread, and only the current thread is expanded.
- **Attach a note** — enter an MRN and pick a file (text, image, or PDF). It is
  transcribed, comprehended, and charted as **history** with reconciliation
  verdicts. An upload is *always* a note — never a lab-result feed, never an
  order. Audio dictation is not accepted yet (that is #87, the local speech
  service).

## 6. Developing the SPA

For hot-reload, run the API and the Vite dev server side by side:

```bash
hdh serve-agent                    # terminal 1 — the API on :8100
cd web && npm run dev              # terminal 2 — Vite on :5173, proxies API routes
```

Open **http://localhost:5173**. `web/vite.config.ts` proxies every API route
the SPA calls (`/ask`, `/me`, `/threads`, `/conversations`, `/notes`,
`/health`) to `:8100` — add any new route there or it will 404 against the dev
server. Both `:5173` and `:8100` are registered redirect URIs, so login works
in dev too.

The SPA lives in `web/src/`: `App.tsx` (the shell), `api.ts` (the fetch/SSE
layer), `auth.ts` (oidc-client-ts). Point it at a non-default Keycloak with the
build-time `VITE_KEYCLOAK_URL` / `VITE_KEYCLOAK_REALM` env vars.
