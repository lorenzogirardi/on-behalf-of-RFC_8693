# Agent Identity POC — RFC 8693 On-Behalf-Of delegation for AI agents

![How it works](how-its-work.png)

## Purpose

When an AI agent acts autonomously on behalf of a human, the systems it touches
usually see only the agent's service account. They can answer *"is this agent
allowed?"* — but not *"is **this user** allowed to do this **via** this agent?"*

This POC demonstrates the fix: **RFC 8693 Token Exchange (On-Behalf-Of)**.
A human logs in, and a broker exchanges their token for a delegated token that
carries **both identities**:

```json
{
  "sub": "8c8af53c-...",              // the human (alice)
  "act": { "sub": "agent-service" },  // the agent acting for her
  "iss": "http://localhost:8180/realms/poc"
}
```

Every downstream hop — the LLM proxy, every MCP tool call — receives this token.
That enables per-user policy, per-user audit, per-user rate limiting, and
revocation, even when the action is executed by an autonomous agent hours later.

Key security properties demonstrated:

- The agent **never sees the user's raw credential** — only the delegated (OBO) token.
- The token-exchange client secret is held by **one small broker service** only
  (skeleton-key pattern), never by the agent.
- OBO grants are stored **encrypted at rest** (AES-256-GCM) in Redis.
- Tokens are **real RS256 JWTs signed by Keycloak** — not mocks.
- Every MCP call is **traced with the identity it presented** (audit endpoints).

This is a demo/educational stack — not production code. Primary audience:
platform engineers evaluating agent identity patterns.

> Deep dive: [ARCHITECTURE.md](ARCHITECTURE.md) (diagrams, token anatomy,
> authorization layers) and [MANUAL.md](MANUAL.md) (component-by-component
> explanation).

## What's in the box

Everything runs locally in Docker/Podman — no cloud, no VPN, no TLS setup.

| Port | Container | Role |
|---|---|---|
| 8180 | `poc-keycloak` | Real IdP (Keycloak 24), RFC 8693 token exchange, realm `poc` |
| 8081 | `poc-obo-exchange` | OBO broker — sole holder of the exchange-app secret |
| 8082 | `poc-agent` | AI agent: tool-calling loop, grant store, audit endpoints |
| 8083 | `poc-mcp-mock` | MCP Streamable HTTP server with 4 demo tools |
| 4000 | `poc-litellm` | OpenAI-compatible LLM proxy (Ollama / OpenAI / Anthropic) |
| 8080 | `poc-webapp` | Identity-flow visualizer UI (simulates the gateway) |
| 6379 | `poc-redis` | Grant store (AES-256-GCM encrypted OBO grants) |
| 9090 | `poc-prometheus` | Metrics — scrapes every service + Keycloak + Redis |
| 3000 | `poc-grafana` | Dashboards (anonymous admin, auto-provisioned) |

Demo users: `alice/alice123`, `bob/bob123`. Keycloak admin: `admin/admin`.
All secrets in this repo are demo values for the local stack only.

## How to use it

### Prerequisites

- Docker (with `docker compose`) or Podman (with `podman-compose`)
- An LLM. Default is local [Ollama](https://ollama.com):

```bash
ollama pull gemma4-12b-qat   # or any chat model
```

### Start

```bash
# Optional: create .env to override defaults (all have working fallbacks)
# AGENT_MODEL=gpt-4o-mini            # OpenAI instead of Ollama
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
# OLLAMA_API_BASE=http://host.containers.internal:11434
# AGENT_STATE_AEAD_KEY=<base64 32B>  # keep grant encryption key stable across restarts

chmod +x scripts/*.sh
./scripts/start.sh
```

First boot takes ~2 minutes (Keycloak JVM boot + realm bootstrap + image pulls).
Subsequent starts: ~10 seconds.

### Explore

- **http://localhost:8080** — the visualizer. Log in as alice, submit a task,
  and watch each step: login → token exchange → agent run → audit trail, with
  every JWT decoded on screen. Step 2 should show `fallback=False` and
  `alg=RS256` — meaning Keycloak performed the real RFC 8693 exchange.
- **http://localhost:8180/admin** — Keycloak console (`admin/admin`, realm `poc`).
- **http://localhost:3000** — Grafana with two provisioned dashboards:
  *Agent Identity — Delegation Flow* (exchange rate, **fallback ratio**, run
  outcomes, per-tool MCP traffic, hop latencies) and *Agent Identity — Service
  RED* (rate/errors/duration per service). Prometheus raw at
  **http://localhost:9090**.

### Verify

```bash
./scripts/test-flow.sh   # test pyramid: unit → integration → E2E
```

Expected with the full stack up: `31 passed  0 failed` (8 unit checks run
even without the stack). Key assertions: `fallback=False` (real Keycloak
RS256 exchange, not the local HMAC fallback) and metrics counters actually
incrementing after the E2E run.

### Watch the identity flow in logs

```bash
./scripts/logs.sh agent
# [OBO] run=abc123 sub=8c8af53c act=agent-service has_refresh=True
# [MCP] run=abc123 tools/list sub=8c8af53c act=agent-service ok=True
```

Audit a run (operator-only endpoints):

```bash
curl http://localhost:8082/admin/instances/$RUN_ID/identity | python3 -m json.tool
curl http://localhost:8082/admin/instances/$RUN_ID/trace    | python3 -m json.tool
```

### Stop

```bash
./scripts/stop.sh
```

## How to work on it

### Repo layout

```
ARCHITECTURE.md          ← diagrams, auth enforcement guide, token anatomy
MANUAL.md                ← technical explanation of each component
docs/CRITICAL_REVIEW.md  ← EA/SRE review: findings, fixes, deferred roadmap
docker-compose.yml       ← full stack definition
config/litellm.yaml      ← LiteLLM model routing
observability/           ← prometheus.yml + Grafana provisioning/dashboards
helm/agent-identity-poc  ← Helm chart: k8s deploy, every component optional

services/
  keycloak-setup/
    realm-poc.json       ← realm definition (users, clients, mappers)
    setup.sh             ← idempotent bootstrap incl. RFC 8693 permission setup
  obo-exchange/server.py ← RFC 8693 broker + local HMAC fallback
  agent/
    agent.py             ← FastAPI server + sync tool-calling loop + HITL stubs
    obo.py               ← OBOClient: actor-token cache, exchange, refresh
    grant_store.py       ← AES-256-GCM grant store on Redis
  mcp-mock/server.py     ← MCP session handler + 4 demo tools
  webapp/
    server.py            ← orchestrates login → exchange → agent → audit
    static/index.html    ← visualizer UI

scripts/
  start.sh / stop.sh / logs.sh
  test-flow.sh                     ← the test pyramid (31 checks with stack up)
  fix-keycloak-token-exchange.sh   ← repairs the RFC 8693 permission if setup failed
```

### Observability

Every Python service exposes:

- `GET /metrics` — Prometheus (RED metrics per route + domain metrics:
  `obo_exchange_total{result}`, `agent_runs_total{status}`,
  `agent_mcp_requests_total{tool}`, `mcp_tool_calls_total`,
  `webapp_flows_total{fallback}`, …)
- `GET /healthz` — liveness (process up)
- `GET /readyz` — readiness (critical dependencies reachable; 503 otherwise)

The one metric to watch: **`obo_exchange_total{result="fallback"}`** — any
nonzero rate means Keycloak stopped doing real RFC 8693 exchanges and the
broker minted locally-signed demo tokens instead. The Grafana *Delegation
Flow* dashboard turns this into a red ratio stat.

![Webapp — identity delegation chain](docs/screenshots/webapp-flow.png)

![Grafana — delegation flow dashboard](docs/screenshots/grafana-identity-flow.png)

![Grafana — service RED dashboard](docs/screenshots/grafana-service-red.png)

### Kubernetes (Helm)

`helm/agent-identity-poc` deploys the stack with every component optional and
externally wireable — point `config.kcIssuer` at an existing Keycloak,
`config.redisUrl` at an existing Redis, disable `litellm` in favor of your
LLM gateway. Default values fail closed (`ALLOW_LOCAL_FALLBACK=false`), run
non-root with read-only rootfs, and ship HPAs for obo-exchange and agent.
See [helm/README.md](helm/README.md).

### Development loop

Services are plain Python/FastAPI containers. After editing a service:

```bash
docker compose up -d --build agent    # rebuild + restart one service
./scripts/logs.sh agent               # follow its logs
./scripts/test-flow.sh                # confirm nothing broke
```

Keycloak realm changes go in `services/keycloak-setup/realm-poc.json` and/or
`setup.sh`; recreate the stack (`./scripts/stop.sh && ./scripts/start.sh`) to
re-run the bootstrap.

### Switching the LLM

Edit `config/litellm.yaml` and set `AGENT_MODEL` in `.env`:

```bash
AGENT_MODEL=gpt-4o-mini        # OpenAI (needs OPENAI_API_KEY)
AGENT_MODEL=claude-haiku       # Anthropic (needs ANTHROPIC_API_KEY)
AGENT_MODEL=ollama/llama3.2    # Ollama (use ollama_chat/ prefix in litellm.yaml)
```

Note: `gemma4-12b-qat` is a thinking model — it answers in `reasoning_content`
unless `think: false` is set. The shipped config already handles this.

### What is intentionally NOT enforced (good first contributions)

The POC demonstrates identity **transport**, not enforcement. Each missing layer
is documented with implementation steps in
[ARCHITECTURE.md §Authorization](ARCHITECTURE.md):

1. **MCP per-tool role checks** — mcp-mock logs `sub`/`act` but never blocks.
   Add `realm_access.roles` checks on sensitive tools.
2. **Gateway CEL rules** — the webapp simulates the gateway without policy.
3. **Custom scopes** — mint OBO tokens with `mcp:read` / `mcp:write` and
   enforce them in the tool server.
4. **HITL gate** — `ENABLE_HITL=0`; enable it to pause the workflow on
   sensitive tool calls until a human approves.

### Troubleshooting

| Symptom | Fix |
|---|---|
| Webapp Step 2 shows ⚠ `fallback=True` | `./scripts/fix-keycloak-token-exchange.sh && docker restart poc-obo-exchange` |
| LiteLLM `No connected db` | Remove `master_key` from `config/litellm.yaml` (needs PostgreSQL) |
| Agent returns 401 on direct `/a2a/run` | Missing OBO grant — go through the webapp, it performs the exchange first |
| Empty LLM answers with gemma4 | Thinking model — ensure `ollama_chat/` prefix + `think: false` in litellm.yaml |

More gotchas in [MANUAL.md §Troubleshooting](MANUAL.md).
