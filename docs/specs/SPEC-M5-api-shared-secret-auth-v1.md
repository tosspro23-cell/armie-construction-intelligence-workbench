# SPEC-M5 — Shared-secret authentication for the public API

## Objective

Require a shared secret on every `/api/v1/*` request, so `armiem3-web`'s public FQDN
no longer accepts anonymous requests. Closes D-012 Finding 1 ("the public API has no
authentication, authorization, or rate limiting"), tracked open in
`docs/decisions/REVIEW_REQUIRED.md` since SPEC-M3.

## Rationale

Today, any internet caller can reach `armiem3-web`'s public FQDN and call
`/api/v1/chat`, consuming Azure OpenAI quota with no owner check, and can query or
cancel *any* request by ID (`/api/v1/requests/{id}`, `/api/v1/requests/{id}/cancel`)
with no check that they originated it. This was scoped out of SPEC-M3 (a vertical-slice
deploy) and out of SPEC-M4 (persistence only) deliberately, but the app is now
actually live with real Azure OpenAI/Postgres behind it (SPEC-M4's deployment), so the
exposure is no longer theoretical.

The owner chose a shared-secret model over Microsoft Entra ID login or IP allowlisting
(explicit choice, this spec's OD): this is a demo/portfolio reference implementation
with no real multi-user/multi-tenant requirement, and a shared secret closes the
anonymous-access gap with a scope proportional to that — not a login system this
project has no other use for.

**Explicit, honest limitation of this model** (D-004 discipline: don't imply a
guarantee this doesn't provide): a single shared secret authenticates *possession of
the secret*, not *identity*. Anyone holding it can still query or cancel any other
holder's requests — this spec closes "random internet strangers can't get in at all,"
it does not add per-caller ownership checks on `/api/v1/requests/*`. That remains a
known gap, acceptable for this project's actual usage pattern (the owner, and anyone
they hand the key to for a demo), not fixed here.

## Verified current-state assumptions

- `apps/api/app/main.py` — no route declares any `Depends(...)` for auth; `app.add_middleware(CORSMiddleware, ...)` is the only cross-cutting concern currently registered. Confirmed live: `az containerapp auth show` returns `{}` for both `armiem3-web` and `armiem3-api` (Container Apps' built-in Easy Auth is not enabled), and `az containerapp ingress access-restriction list` returns `[]` for `armiem3-web` (no IP restriction).
- `apps/web/src/main.tsx`'s `api()` helper (`main.tsx:18-21`) and `IfcViewer.tsx:80`'s direct `fetch("/api/v1/project/viewer-elements")` both use `fetch`, so both can carry a custom header.
- **Two endpoints are reached via raw `<img src=...>` tags, which cannot carry custom headers**: `/api/v1/project/pdf/pages/{page}.png` (`main.tsx:190`) and `/api/v1/evidence/{filename}` (`main.tsx:194`). A header-only auth scheme breaks these unless the frontend fetches them as blobs instead of using `<img src>` directly. `/api/v1/project/ifc` and `/api/v1/project/pdf` (the raw source files) are not currently fetched by the frontend at all (grep-verified) — they still get the same auth requirement as everything else for consistency, they just have no current caller to update.
- `apps/web/default.conf.template` proxies `/api/*` straight through to the API container; it adds no auth logic today and this spec does not add any there either — the check belongs in the FastAPI app (one enforcement point, consistent with how this project already centralizes other cross-cutting logic, e.g. `providers/factory.py`).
- No existing test in `tests/` routes through FastAPI's actual HTTP dispatch for `app.main.app` (grep-verified: the only `TestClient` usage in the test suite is `tests/_fastapi_instrumentation_timing_repro.py`'s own separate, minimal app, unrelated to auth). Every existing test that exercises `main.chat()`/`main.resume()` calls the decorated function directly as a plain coroutine, which does not run FastAPI's dependency-injection pipeline — so a dependency registered at `FastAPI(dependencies=[...])` protects real HTTP requests without requiring changes to any existing test.
- `infra/bicep/apps.bicep` declares no health/liveness probe (Container Apps' own default TCP probe applies); no consumer depends on `/api/v1/health` being reachable without the secret, so this spec does not carve out an exception for it — one uniform rule, not a maintained allowlist of "exempt" routes.

## Allowed scope

**A. `require_api_key` dependency + config surface.**
- New setting `api_shared_secret: str | None = None` (`apps/api/app/config.py`), same
  opt-in-when-unset pattern as `otel_exporter_connection_string`/`database_url`: unset
  means local development stays exactly as open as it is today, no key required, no
  developer has to set one up to run this project locally.
- `apps/api/app/security.py` (new): `require_api_key(authorization: str | None = Header(None))`
  — when `settings.api_shared_secret` is set, requires `Authorization: Bearer <secret>`
  matching it exactly (constant-time comparison, `secrets.compare_digest`), else `401`;
  when unset, the dependency is a no-op (preserves the local-dev-open default above).
- Registered once, app-wide: `app = FastAPI(..., dependencies=[Depends(require_api_key)])`
  — every route requires it uniformly, including any added later, with no per-route
  boilerplate and no allowlist to maintain.

**B. Frontend: a lightweight key gate, and blob-fetching the two `<img>` endpoints.**
- A minimal gate screen (shown when a request 401s) asking for the shared secret once;
  stored in `localStorage`, attached as `Authorization: Bearer <key>` by the existing
  `api()` helper and `IfcViewer.tsx`'s direct `fetch` call.
- The PDF-page and evidence-crop `<img src=...>` usages become a small `useAuthedImage(url)`
  hook: `fetch(url, {headers: {Authorization: ...}})` → `URL.createObjectURL(blob)` → set
  as `<img src={objectUrl}>`, with `URL.revokeObjectURL` on cleanup/URL change. One
  mechanism (the header) for every request, no secret ever appears in a URL, browser
  history, or an access log.

**C. Azure: the secret as a Container Apps native secret, not a new Key Vault resource.**
- `infra/bicep/apps.bicep` gains a `@secure() param apiSharedSecret string` (no default
  — required, the same "no silent wrong default" discipline as
  `azureOpenAiTextDeployment`, SPEC-M3 Finding 3), stored as a Container Apps `secrets`
  entry and surfaced to the API container as `API_SHARED_SECRET` via `secretRef` (not a
  plain `value`) — Container Apps stores/redacts this at the platform level, distinct
  from a bare environment variable string.
- **Not Key Vault, an explicit scope decision (this spec's OD, not silently assumed):**
  SPEC-M3 deferred Key Vault because Managed Identity eliminated every secret Phase 1
  needed. This is the first secret Managed Identity cannot eliminate (a browser/end user
  is not an Azure identity), but introducing a whole Key Vault resource, its own access
  policy, and a `secretRef` pointing at it is disproportionate to "one shared demo
  key" — a genuine production system serving real external users would want Key
  Vault + rotation; this stays a Container-Apps-native secret, matching this project's
  narrow-scope-per-milestone discipline elsewhere (e.g. SPEC-M4's "no ORM for two
  tables").
- `azure-deploy.yml` gains a required `api_shared_secret` input (`required: true`, no
  default — unlike `database_url`, this one must not be easy to leave blank, since blank
  would silently mean "no auth" in a fresh deploy). Threaded through the same job-level
  `env:` → shell-variable mechanism as every other input (shell-injection discipline,
  SPEC-M3 Finding 5).

**D. Tests.**
- Unit tests for `require_api_key`: unset secret → always allows; set secret → rejects
  missing/wrong `Authorization`, accepts the exact match; timing-safe comparison used
  (asserted by code inspection/reference, not a timing measurement, which would be
  flaky).
- A `TestClient(app)`-based test (the first one this project's suite adds for the real
  `app.main.app`, per the verified-current-state note above) proving the dependency
  actually rejects an unauthenticated real HTTP request end-to-end, not just the
  dependency function in isolation — this is the one behaviour a direct
  `main_module.chat(...)` call cannot exercise, since it bypasses FastAPI's dependency
  injection entirely (see Verified current-state assumptions).

## Explicitly excluded scope

- **Per-caller ownership/authorization** on `/api/v1/requests/*` (a shared-secret holder
  can still act on any other holder's requests) — see Rationale's explicit limitation.
- **Rate limiting / abuse throttling** — the owner explicitly chose shared-secret auth
  over the "anonymous demo + rate limiting" alternative; not bundled in here.
- **Microsoft Entra ID / real per-user login** — the owner's explicit alternative not
  chosen (see Rationale). Would replace this milestone, not extend it, if ever wanted.
- **Azure Key Vault** — see §C; deferred as disproportionate to one shared demo secret.
- **Secret rotation** — a Container Apps native secret can be updated by re-running the
  deploy workflow with a new value; no automated rotation schedule is built.

## Affected surfaces

`apps/api/app/config.py`, `apps/api/app/security.py` (new), `apps/api/app/main.py`,
`apps/web/src/main.tsx`, `apps/web/src/IfcViewer.tsx`, `.env.example`,
`infra/bicep/apps.bicep`, `.github/workflows/azure-deploy.yml`, new tests.

## Invariants

All prior invariants unchanged. New: no route is reachable without
`Authorization: Bearer <secret>` once `api_shared_secret`/`API_SHARED_SECRET` is set;
local development with it unset is unaffected (matches the opt-in pattern of every
other optional setting in this project).

## Acceptance criteria

- `PYTHONPATH=apps/api python3 -m pytest -q` passes, including the new `TestClient`
  end-to-end auth test.
- `(cd apps/web && npm run build)` passes; a manual local browser check confirms the
  key gate appears, accepts a correct key, and the drawing/evidence images still render
  (proving the blob-fetch replacement works, not just that the header is sent).
- `ruff check --select F,E9,I,F401 apps/api tests` clean.
- `infra/bicep/apps.bicep` validated with `az bicep build`.
- If deployed: a request to `armiem3-web`'s public FQDN without the header returns
  `401`; the same request with the correct header succeeds exactly as before this spec.

## Documentation requirements

`D-015` (architecture decision record): the shared-secret model, the Container-Apps-
native-secret-not-Key-Vault decision, the `<img>`/blob-fetch mechanism, and the
explicit per-caller-ownership limitation. `PROJECT_STATE.md` M5 entry.
`docs/decisions/REVIEW_REQUIRED.md`'s Finding 1 entry marked resolved (with the
explicit remaining limitation carried forward, not silently dropped).

## Git / stop conditions

One commit per subsection (A–D), spec committed alone first. Branch:
`feat/m5-api-shared-secret-auth`. Stop and report rather than proceeding if: the
`<img>`-to-blob-fetch conversion turns out to break the drawing zoom/pan interaction in
a way not obvious from reading the code (verify by actually running the frontend, not
just compiling it); or a route is found that must remain anonymous for a reason not
covered by "no consumer depends on it" (e.g. a health-check integration this spec's
author was not told about).

## Owner decisions

- **OD-28 (owner-confirmed).** Shared-secret authentication, not Microsoft Entra ID
  login or IP-based restriction. Rationale: this is a demo/portfolio project with no
  real multi-tenant requirement; a shared secret closes the anonymous-access gap
  proportionally, without building a login system this project has no other use for.
- **OD-29 (this spec's default, flagged for owner review).** The secret is stored as a
  Container Apps native secret, not Azure Key Vault — disproportionate infrastructure
  for one shared demo key (see §C). Revisit if this project ever needs per-user
  secrets, rotation, or a real production posture.

## §I addendum — CodeQL finding on the frontend storage choice

CodeQL (enabled earlier this session) flagged `apiClient.tsx`'s `setStoredApiKey` as a
high-severity `js/clear-text-storage-of-sensitive-data` finding on the PR opening this
spec's §B commit range: the shared secret is stored unencrypted in browser storage,
readable by any script on the page (an XSS vulnerability would expose it). Genuine
finding, not a false positive — investigated rather than dismissed reflexively.

Fixed the cheap, real part: switched `localStorage` → `sessionStorage` (cleared when the
tab/browser closes instead of sitting on disk indefinitely — meaningfully better on a
shared/public machine, no UX regression against this spec's own tested behaviour, since
`sessionStorage` still survives a same-tab reload). This does **not** resolve the
underlying CodeQL rule — any script-readable storage carries the same XSS-exposure
property regardless of which Web Storage API is used; only an httpOnly cookie issued by
a real server-side session (which this milestone deliberately does not build — OD-28
already chose shared-secret auth specifically to avoid a login/session system) removes
it.

**Accepted, not built around, for this specific security model**: the alert is dismissed
on the repository (with this reasoning recorded there and here) because this secret's
entire security property is "possession = access" for every legitimate holder already —
see this spec's Rationale on per-caller ownership not being a goal here. Client-side
exposure of it to an XSS attacker does not grant that attacker anything beyond what any
of this project's own intended users (the owner, or anyone they hand the key to) already
has by design. This reasoning would not hold for a real per-user credential or an
API key with differentiated privilege per holder — it holds specifically because this
model has neither.
