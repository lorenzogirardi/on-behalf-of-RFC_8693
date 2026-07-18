# Critical Review — Enterprise Architecture & Platform SRE

Reviewed: 2026-07-18. Scope: full codebase at commit `54a93be`.
Verdict up front: **the core pattern is sound and correctly demonstrated** —
skeleton-key broker, real RFC 8693 against Keycloak, encrypted grant store,
identity propagated on every hop. What follows is what stands between "good
demo" and "something you can run more than one replica of, observe, and trust."

Legend: 🔴 must fix · 🟠 should fix · 🟡 acceptable for a POC, document it ·
✅ addressed by the hardening change-set that accompanies this review.

---

## 1. Architecture

### 1.1 🔴✅ Fail-open security downgrade (silent HMAC fallback)
`obo-exchange` falls back to a **locally-signed HMAC token with a secret
hardcoded in the repo** whenever Keycloak returns non-200 on the exchange
(`services/obo-exchange/server.py`). Any Keycloak outage, misconfiguration, or
even an *invalid subject token* silently produces a "valid-looking" delegated
token. This is fail-open: the system degrades to a weaker trust model without
any consumer being forced to notice.
**Fix applied:** fallback is now gated behind `ALLOW_LOCAL_FALLBACK`
(default `true` locally for demo ergonomics, **must be `false` in Kubernetes**
— the prod overlay sets it), counted in the `obo_exchange_total{result="fallback"}`
metric, and alertable via the fallback-ratio panel in Grafana.

### 1.2 🟡 No signature verification downstream
Agent, mcp-mock, and webapp all base64-decode JWTs without verifying the RS256
signature against Keycloak's JWKS. Documented as intentional (the POC shows
identity *transport*, not enforcement), but it means the audit trail records
*claimed* identity, not *proven* identity. Roadmap: JWKS verification in
mcp-mock is the highest-value next increment (`ARCHITECTURE.md §Authorization`
already sketches it).

### 1.3 🟡 Refresh token travels in a custom header over plaintext HTTP
`X-OBO-Refresh-Token` is bearer-equivalent credential material. Fine on a
local bridge network; in Kubernetes this requires TLS (mesh mTLS or ingress
TLS) before it is acceptable. Noted in `k8s/README.md`.

### 1.4 🟠✅ Config naming drift (`ZITADEL_*` on a Keycloak stack)
The agent read `ZITADEL_ISSUER` / `ZITADEL_AGENT_CLIENT_*` — leftovers from
the production stack this POC mirrors. Misleading for anyone wiring an
external IdP. **Fix applied:** canonical names are now `KC_ISSUER`,
`AGENT_CLIENT_ID`, `AGENT_CLIENT_SECRET` (identical to obo-exchange); the old
`ZITADEL_*` names still work as fallbacks so nothing breaks.

### 1.5 🟡 ROPC login
The webapp collects the user's password directly (ROPC). Acceptable
exclusively as a demo shortcut; PKCE is the documented production path.

### 1.6 🟡 Keycloak legacy token-exchange
`KC_FEATURES=token-exchange,admin-fine-grained-authz` is the *legacy preview*
implementation. Keycloak 26.2+ ships standard (v2) token exchange. The
`setup.sh` permission dance (steps 2–6) exists only to serve the legacy
feature; migrating removes most of that bootstrap fragility. Tracked as
roadmap, not fixed here — realm import format differs.

## 2. Scalability & statelessness (Kubernetes readiness)

### 2.1 🔴✅ Every service was locked to one process by accident, not design
Audit of state per service:

| Service | State | Multi-replica safe? |
|---|---|---|
| obo-exchange | actor-token cache (per-process, re-mintable) | ✅ yes |
| agent | grants + traces in Redis, AES-GCM sealed | ✅ yes — **but** silent in-memory fallback when Redis is down breaks it (grant saved on replica A, audit read hits replica B) |
| mcp-mock | `SESSIONS` dict, unbounded, never expired | ⚠️ memory leak + replica-affinity |
| webapp | `_events` list per process | 🟡 cosmetic only (recent-events panel) |

**Fixes applied:** mcp-mock sessions now have a TTL and a hard cap; the
agent's in-memory fallback logs loudly and is reported as not-ready via
`/readyz` (Redis is a readiness dependency, so Kubernetes stops routing to a
replica that lost Redis instead of silently splitting state). Webapp events
stay per-replica — cosmetic, documented.

### 2.2 🟠✅ Trace writes had a read-modify-write race
`GrantStore.append_trace` did GET → append → SET on a JSON blob. Two
concurrent tool calls in the same run lose entries; with >1 agent replica it
is guaranteed lossy. **Fix applied:** traces are now a Redis list
(`RPUSH`/`LRANGE`) — atomic appends, no lost updates.

### 2.3 🟠✅ Sync HTTP calls inside async handlers (obo-exchange)
Handlers were `async def` but used a synchronous `httpx.Client`, blocking the
event loop for the full Keycloak round-trip. One slow KC response stalls
*every* in-flight exchange. **Fix applied:** `httpx.AsyncClient` +
`asyncio.Lock` for the actor-token cache.

### 2.4 🟠✅ No readiness signal — only liveness
All services exposed `/healthz` returning static `ok`, so an agent replica
with no Redis and no obo-exchange still received traffic. **Fix applied:**
every service now has `/healthz` (liveness: process up) and `/readyz`
(readiness: critical dependencies reachable), wired into both compose
healthchecks and the Kubernetes probes.

### 2.5 🟡 No retries, backoff, or circuit breaking
A transient KC blip fails the exchange rather than retrying. For a POC the
metric visibility (error-rate panels) is the right first step; retry policy
belongs in the gateway/mesh layer in production.

## 3. Operability (SRE)

### 3.1 🔴✅ Zero metrics
No `/metrics` anywhere; the only signals were unstructured `print()` lines.
You could not answer "what is the fallback ratio", "p95 exchange latency", or
"which MCP tool is failing" without grepping logs. **Fix applied:** all four
Python services expose Prometheus `/metrics` (shared `services/common/obs.py`
middleware — RED metrics on every route, path-templated to avoid cardinality
explosion) plus domain metrics:

| Service | Domain metrics |
|---|---|
| obo-exchange | `obo_exchange_total{result=keycloak\|fallback\|error}`, `obo_refresh_total{result}`, `obo_exchange_duration_seconds`, `obo_keycloak_reachable` |
| agent | `agent_runs_total{status}`, `agent_run_duration_seconds`, `agent_llm_requests_total{status}` + duration, `agent_mcp_requests_total{method,tool,status}` + duration, `agent_grant_refresh_total{result}` |
| mcp-mock | `mcp_tool_calls_total{tool}`, `mcp_sessions_active` |
| webapp | `webapp_flows_total{status,fallback}`, `webapp_flow_duration_seconds` |

Keycloak metrics are enabled (`KC_METRICS_ENABLED=true`) and Redis is scraped
via `redis_exporter`. Prometheus (`:9090`) and Grafana (`:3000`) join the
compose stack with two provisioned dashboards: **Identity Flow** (exchanges,
fallback ratio, run outcomes, per-tool MCP traffic) and **Service RED**
(rate/errors/duration per service, templated).

### 3.2 🟠✅ `print()` logging
Replaced with the `logging` module across the agent (same message shapes, so
`logs.sh` grepping and the `[IDENTITY_EVENT]` convention still hold).

### 3.3 🟠✅ Containers ran as root, unpinned, no EXPOSE
**Fix applied:** all four Dockerfiles now create and switch to a non-root UID
(10001), set `PYTHONUNBUFFERED=1`, declare `EXPOSE`, and share the
`services/` build context so `common/obs.py` is copied in — no duplicated
code, each image still builds standalone.

### 3.4 🟡 keycloak-setup does `apt-get install curl` at boot
Runtime package installation makes first boot network-dependent and
nondeterministic. Tolerable locally; in Kubernetes the realm should be
imported via `keycloak.config-cli` or baked into a custom image. Documented,
not changed (the setup container is not part of the k8s target — there you
point at an *existing* Keycloak).

### 3.5 🟡 No CI, no lint
`test-flow.sh` is solid for a POC (24 checks after this change-set). GitHub
Actions running layer-1 unit tests + `docker compose config` validation would
be the cheap next step.

## 4. What "Kubernetes-ready" means here (and what was delivered)

Every component is independently deployable and externally wireable — the same
property Keycloak already had (point `KC_ISSUER` anywhere):

- **`k8s/base/`** — Deployments + Services for obo-exchange, agent, mcp-mock,
  webapp, litellm. All configuration via one ConfigMap (`poc-config`) and one
  Secret (`poc-secrets`): external Keycloak URL, external Redis URL, LLM
  backend. Non-root security context, read-only root filesystem, resource
  requests/limits, liveness `/healthz` + readiness `/readyz`,
  `prometheus.io/*` scrape annotations.
- **`k8s/overlays/dev/`** — adds in-cluster Keycloak + Redis for a
  self-contained cluster demo (fallback allowed).
- **`k8s/overlays/prod-example/`** — external IdP/Redis, `ALLOW_LOCAL_FALLBACK=false`,
  2 replicas + HPA for obo-exchange and agent. Template, not a turnkey prod
  deploy: TLS, NetworkPolicy, and real secret management (ESO/Vault) are
  called out as your responsibility in `k8s/README.md`.

Scale model: obo-exchange and agent are stateless against shared Redis —
horizontal by replica count; the HPA targets CPU. mcp-mock and webapp are
demo-grade singletons that also happen to scale (sessions are re-initialized
per MCP conversation; webapp event feed is per-replica cosmetic).

## 4b. Found *by* the new observability (post-review validation)

The dashboards paid for themselves within hours — four latent defects became
visible that log-grepping had never surfaced:

1. **Test 2.3 silently exercised the HMAC fallback on every run** (fixed,
   `c676e28`). It passed the user JWT as `Authorization`, so obo-exchange used
   it as actor_token and Keycloak rejected the exchange. Additionally,
   dev-mode Keycloak derives the token `iss` from the request Host header:
   tokens minted via `localhost:8180` are rejected as `invalid_token` by the
   in-network exchange at `keycloak:8080`. The fallback-ratio stat sat at 50%
   and pointed straight at it. The test now logs in through the webapp
   (internal issuer) and **fails** when the exchange degrades.
2. **Real RS256 grants were not renewable** (fixed, `c676e28`). Keycloak
   omits `refresh_token` when `requested_token_type=access_token` — only
   fallback tokens carried an RT, so the POC's core "renewability" property
   worked only on the degraded path. obo-exchange now requests the
   `refresh_token` type when the scope includes `offline_access`; test 2.5
   exercises the real Keycloak refresh.
3. **webapp /run 500 on agent timeout** (fixed, `2d41f78`) — unhandled
   `httpx.ReadTimeout` under concurrent runs; now a clean 504/502.
4. **mcp-mock 500 on `"params": null`** (fixed, `2d41f78`) — LLM-driven
   JSON-RPC clients send explicit nulls; `.get(k, {})` does not cover them.

## 5. Deferred (explicitly out of scope, in priority order)

1. JWKS signature verification + per-tool RBAC in mcp-mock (§1.2)
2. Keycloak standard token-exchange (v2) migration (§1.6)
3. OpenTelemetry traces (run_id → trace_id propagation is trivially mappable)
4. TLS everywhere / mesh mTLS; NetworkPolicy manifests
5. CI pipeline; image signing/SBOM
6. HITL activation behind a durable workflow
