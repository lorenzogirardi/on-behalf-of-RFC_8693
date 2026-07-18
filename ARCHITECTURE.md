# Agent Identity POC — Architecture & Flow

## What this demonstrates

When an AI agent acts autonomously on behalf of a human, every system it touches
can see **both** identities — who the human is, and which agent is acting.
This enables fine-grained policy (allow/deny/audit) on individual agent actions,
with an optional Human-in-the-Loop gate for sensitive operations.

---

## Components

```
┌─────────────────────────────────────────────────────────────────────┐
│  LOCAL STACK (all Docker containers, no cloud, no VPN)              │
│                                                                     │
│  Keycloak :8180   obo-exchange :8081   LiteLLM :4000                │
│  Redis    :6379   mcp-mock     :8083   Agent   :8082                │
│  Webapp   :8080                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

| Component | Role | Why it exists |
|---|---|---|
| **Keycloak** | Identity Provider (IdP) | Issues signed JWTs for users and services. Runs RFC 8693 token exchange. Source of truth for identity. |
| **obo-exchange** | OBO broker | Only service that holds the `exchange-app` client secret. Calls Keycloak to mint delegated tokens. Agent never touches this secret. |
| **Agent** | AI worker | Receives only the OBO grant, never the user's raw token. Calls LLM and MCP tools presenting the delegated identity on every hop. |
| **LiteLLM** | LLM proxy | OpenAI-compatible `/v1` endpoint. Routes to Ollama (local), OpenAI, Anthropic, or Bedrock. |
| **mcp-mock** | Tool server | MCP Streamable HTTP server with 4 demo tools (echo, list_deployments, get_service_health, list_pr_reviews). |
| **Redis** | Grant store | Stores OBO grants encrypted at rest (AES-256-GCM). Agent loads grants by `run_id`. |
| **Webapp** | Demo UI | Visualizes the full delegation chain step-by-step. Shows real JWT tokens decoded. |

---

## Keycloak client topology

```
poc-webapp          public PKCE app     ← human user logs in here
agent-service       service account     ← AI agent's own identity (actor)
exchange-app        confidential        ← holds skeleton key; runs RFC 8693
```

Trust relationship (configured by `keycloak-setup`):

```
poc-webapp tokens   include audience: exchange-app
agent-service       granted token-exchange permission on exchange-app
exchange-app        runs the RFC 8693 exchange (subject=user, actor=agent)
```

---

## Identity flow — step by step

```mermaid
sequenceDiagram
    actor Human as 👤 Human (alice)
    participant KC as Keycloak
    participant GW as Gateway / Webapp
    participant OBO as obo-exchange
    participant Agent as 🤖 Agent
    participant LLM as LiteLLM /v1
    participant MCP as MCP tools

    Note over Human,KC: Step 1 — Login
    Human->>KC: POST /token (ROPC or PKCE)<br/>username=alice password=alice123
    KC-->>Human: JWT {sub=alice, aud=[exchange-app], alg=RS256}

    Note over Human,OBO: Step 2 — Submit task (gateway intercepts)
    Human->>GW: POST /task<br/>Authorization: Bearer <user JWT>
    GW->>OBO: POST /exchange<br/>subject_token=<user JWT>
    Note right of OBO: obo-exchange holds<br/>exchange-app secret
    OBO->>KC: RFC 8693 token exchange<br/>subject=alice, actor=agent-service
    KC-->>OBO: OBO JWT {sub=alice, act={sub=agent-service}, alg=RS256}
    OBO-->>GW: OBO JWT + refresh_token
    GW->>Agent: POST /a2a/run<br/>Authorization: Bearer <OBO JWT>
    Note right of GW: User JWT never<br/>reaches the agent

    Note over Agent,MCP: Step 3 — Agent executes task
    Agent->>Agent: Store OBO grant (AES-256-GCM in Redis)
    loop Tool-calling loop (max 6 turns)
        Agent->>LLM: POST /v1/chat/completions<br/>Authorization: Bearer <OBO JWT>
        Note right of LLM: PDP sees sub=alice<br/>act=agent-service
        LLM-->>Agent: next action (tool call or final answer)
        opt LLM requests tool call
            Agent->>MCP: POST /mcp tools/call<br/>Authorization: Bearer <OBO JWT>
            Note right of MCP: Every hop carries<br/>sub=alice, act=agent-service
            MCP-->>Agent: tool result
        end
    end
    Agent-->>Human: {status: COMPLETED, result: "..."}

    Note over Agent,MCP: Step 4 — Audit (operator only, not exposed to user)
    Agent->>Agent: GET /admin/instances/{id}/identity → who held the grant
    Agent->>Agent: GET /admin/instances/{id}/trace   → every MCP call + identity
```

---

## Token anatomy

### User JWT (from Keycloak, RS256)
```json
{
  "sub":   "8c8af53c-bcfc-4960-8874-bfb859aba5e0",
  "aud":   "exchange-app",
  "iss":   "http://localhost:8180/realms/poc",
  "email": "alice@poc.local",
  "exp":   1751234567
}
```

### OBO token (from Keycloak via RFC 8693, RS256)
```json
{
  "sub":   "8c8af53c-bcfc-4960-8874-bfb859aba5e0",
  "act":   { "sub": "agent-service" },
  "iss":   "http://localhost:8180/realms/poc",
  "scope": "openid profile email",
  "exp":   1751234567
}
```

**`sub`** = who owns the action (the human).  
**`act.sub`** = who is executing (the agent service account).  
Every downstream system that validates this token can enforce rules on both.

---

## What the gateway does (simulated by webapp in this POC)

```mermaid
flowchart LR
    A([User Request\nBearer user-JWT]) --> B{Gateway PDP}
    B -->|validate JWT\nsignature + expiry| C[obo-exchange\nPOST /exchange]
    C -->|RFC 8693\nKeycloak| D([OBO token\nsub=user\nact=agent])
    D --> E[Replace Authorization\nheader]
    E --> F([Agent receives\nonly OBO token])
    style D fill:#1a4d2e,color:#3dd68c
    style A fill:#1a2d4d,color:#4f8ef7
    style F fill:#1a4d2e,color:#3dd68c
```

In production this gateway is **agentgateway** (Envoy-based proxy with a PDP).
The `extAuth` filter intercepts every `/a2a` request, calls obo-exchange `/authz`,
replaces the `Authorization` header, then forwards to the agent.

---

## Security properties

```mermaid
mindmap
  root((Agent Identity\nPOC))
    Delegation
      sub=user on every hop
      act=agent on every hop
      Token signed RS256 by Keycloak
    Isolation
      Agent never holds user raw credential
      exchange-app secret held only by obo-exchange
      Grant encrypted at rest AES-256-GCM in Redis
    Renewability
      OBO token short-lived 1h
      Refresh token rotates on use
      Renewal offline — no human present needed
    Auditability
      Every MCP call traced with identity
      Operator-only /admin endpoints
      IDENTITY_EVENT log lines with decoded claims
    Human-in-the-Loop
      Durable workflow pauses on sensitive tool call
      Human approves or rejects
      Workflow resumes — no token re-auth needed
```

---

## Trust boundary

```mermaid
flowchart TB
    subgraph client["CLIENT SIDE"]
        U([Human\nalice])
    end
    subgraph gateway["GATEWAY"]
        G[gw-a2a PDP\nvalidates JWT]
        O[obo-exchange\nholds skeleton-key]
    end
    subgraph backend["AGENT BACKEND"]
        A[Agent]
        R[(Redis\ngrant store\nAES-256-GCM)]
        L[LiteLLM]
        M[MCP tools]
    end
    subgraph idp["IDENTITY PROVIDER"]
        K[Keycloak\nRFC 8693]
    end

    U -->|user JWT\nsub=alice| G
    G -->|user JWT| O
    O -->|exchange| K
    K -->|OBO JWT\nsub=alice\nact=agent-service| O
    O -->|OBO JWT| G
    G -->|OBO JWT| A
    A -->|save grant| R
    A -->|OBO JWT| L
    A -->|OBO JWT| M

    style O fill:#4d2a00,color:#f7934f
    style K fill:#002a4d,color:#4f8ef7
    style R fill:#002a1a,color:#3dd68c
```

**Red zone** (obo-exchange): sole holder of `exchange-app` client secret.  
**Blue zone** (Keycloak): sole issuer of signed tokens.  
**Green zone** (Redis): encrypted grant storage; token material never in plaintext.

---

## OBO token lifecycle

```mermaid
stateDiagram-v2
    [*] --> Minted: user submits task\ngateway calls /exchange

    Minted --> Active: stored in Redis\nagent receives headers

    Active --> NearExpiry: time.now >= exp - 60s\nagent detects in _live_token()

    NearExpiry --> Refreshed: agent calls obo-exchange /refresh\nact claim preserved\nrefresh token rotates

    Refreshed --> Active: new grant saved to Redis\nrun continues without human

    Active --> Revoked: user session ends\nor logout

    Active --> Completed: task finishes\n_store has grant for audit

    Revoked --> [*]
    Completed --> [*]
```

---

## Stack startup sequence

```mermaid
sequenceDiagram
    participant DC as docker compose up
    participant KC as Keycloak
    participant KCS as keycloak-setup
    participant OBO as obo-exchange
    participant A as agent
    participant W as webapp

    DC->>KC: start container
    KC-->>KC: JVM boot + DB init (~30s)
    KC-->>DC: healthy (/health/ready = UP)

    DC->>KCS: start (depends: KC healthy)
    KCS->>KC: import realm poc
    KCS->>KC: enable authz on exchange-app
    KCS->>KC: add audience mapper to poc-webapp
    KCS->>KC: create allow-agent-service policy
    KCS->>KC: link policy → token-exchange scope
    KCS->>KC: smoke test RFC 8693
    KC-->>KCS: exchange OK sub=alice act=agent-service
    KCS-->>DC: exit 0

    DC->>OBO: start (depends: KC healthy)
    OBO-->>DC: healthy

    DC->>A: start (depends: OBO healthy + redis + mcp + litellm)
    A-->>DC: healthy

    DC->>W: start (depends: agent healthy + OBO healthy)
    W-->>DC: healthy

    Note over DC,W: Full stack ready — open http://localhost:8080
```

---

## Authorization: how the system decides what alice can do

The OBO token carries `sub=alice` — but knowing *who* alice is does not automatically
mean she can run every tool. Authorization happens at multiple independent layers.

### Layer 1 — Gateway PDP (before the request reaches MCP)

The gateway validates the JWT signature and evaluates CEL rules on the token claims.
Example rules (agentgateway config):

```yaml
# Allow /mcp only if alice has the 'ai-platform-user' role
- path: /mcp
  policy: jwt.roles.exists(r, r == "ai-platform-user")

# Block write tools for read-only users
- path: /mcp
  policy: >
    request.method == "POST" &&
    params.name in ["apply_terraform","merge_pr","delete_namespace"]
    ? jwt.roles.exists(r, r == "platform-admin")
    : true
```

If the policy returns false → **403 before the request touches any tool server**.

### Layer 2 — MCP server enforces per-tool rules

Each MCP tool server receives the full OBO token and can read its claims:

```python
# Example: mcp-mock enforcing roles on sensitive tools
SENSITIVE_TOOLS = {"delete_deployment", "apply_terraform", "merge_pr"}

def _exec_tool(name, arguments, bearer_claims):
    if name in SENSITIVE_TOOLS:
        roles = bearer_claims.get("realm_access", {}).get("roles", [])
        if "platform-admin" not in roles:
            raise PermissionError(
                f"tool '{name}' requires platform-admin — "
                f"sub={bearer_claims['sub']} has roles={roles}"
            )
```

This works because `sub=alice` is in the token — the MCP server can look up
alice's roles, group memberships, or any custom attribute Keycloak injected.

### Layer 3 — Scope negotiation at exchange time

The OBO token is minted with a specific `scope`. If alice's session does not have
`mcp:write` in scope, the token exchange can exclude it:

```
subject_token=alice-jwt
scope=openid profile email mcp:read     ← no mcp:write
→ OBO token has scope="openid profile email mcp:read"
```

The MCP server checks `scope` claim → refuses write operations without `mcp:write`.

### Layer 4 — HITL gate (Human in the Loop)

For tools flagged as sensitive regardless of role, the agent **pauses** and waits
for explicit human approval before executing. The workflow resumes only after the
human who launched the run calls `POST /a2a/hitl/runs/{id}/decision` with
`{"action":"approve"}`.

```
agent wants to call: delete_namespace
↓
HITL gate: pause workflow
↓ emit: {state: "input-required", tool: "delete_namespace", arguments: {...}}
↓ human sees notification
↓ human decides: approve / reject
↓ workflow resumes (approve) or tells LLM "call was rejected" (reject)
```

### Summary: who decides what

```mermaid
flowchart TD
    A([OBO token\nsub=alice\nact=agent-service]) --> B{Gateway PDP\nCEL rules on JWT}
    B -->|deny| Z1([403 — blocked at edge\nnever reaches tool server])
    B -->|allow| C{MCP server\nper-tool role check}
    C -->|missing role| Z2([error: Forbidden\nreturned to agent])
    C -->|scope missing| Z3([error: Insufficient scope])
    C -->|sensitive tool| D{HITL gate}
    D -->|human rejects| Z4([agent told: rejected\nadjusts plan])
    D -->|human approves| E([tool executes\nresult returned])
    C -->|allowed| E

    style Z1 fill:#4d0000,color:#f76f6f
    style Z2 fill:#4d0000,color:#f76f6f
    style Z3 fill:#4d0000,color:#f76f6f
    style Z4 fill:#4d2200,color:#f7934f
    style E fill:#004d1a,color:#3dd68c
```

### Come funziona ORA nel POC (stato attuale)

Il POC dimostra il **trasporto** dell'identità, non l'enforcement.

| Layer | Stato nel POC | Cosa manca |
|---|---|---|
| Gateway PDP | Webapp simula extAuth senza CEL rules | Nessuna policy applicata sul path /mcp |
| MCP server | Logga `sub` e `act` — non blocca nulla | Nessun controllo ruoli sui tool |
| Scope | OBO include `openid profile email` | Nessun scope custom `mcp:read/write` |
| HITL | Disabilitato (`ENABLE_HITL=0`) | Nessun gate su tool sensibili |

**Cosa è verificato:**
- Il token che arriva al MCP ha `sub=alice act=agent-service` ✓
- L'agente non ha mai il token grezzo di alice ✓
- Ogni call MCP è tracciata con l'identità presentata ✓
- L'audit trail mostra chi ha fatto cosa ✓

### Come aggiungere enforcement (prossimi step)

**Step 1 — Ruoli Keycloak → token claims**

In Keycloak Admin: `Clients → poc-webapp → Client scopes → roles → add mapper "realm roles"`.
I ruoli di alice appaiono nel token come `realm_access.roles`.

**Step 2 — MCP server legge i ruoli**

```python
# services/mcp-mock/server.py — in _exec_tool()
SENSITIVE_TOOLS = {"delete_deployment", "apply_terraform", "merge_pr"}

if name in SENSITIVE_TOOLS:
    roles = claims.get("realm_access", {}).get("roles", [])
    if "platform-admin" not in roles:
        return JSONResponse({"jsonrpc":"2.0","id":id_,
            "error":{"code":-32603,
                     "message": f"Forbidden: {claims.get('sub')} lacks role platform-admin"}})
```

**Step 3 — Gateway CEL rule (agentgateway in produzione)**

```yaml
auth:
  proxy:
    rbac:
      enabled: true
      rules:
        - path: /mcp
          cel: jwt.realm_access.roles.exists(r, r == "ai-platform-user")
```

**Step 4 — HITL per tool ad alto rischio**

```python
# services/agent/agent.py
SENSITIVE_TOOLS = {"delete_deployment", "apply_terraform"}

# nel workflow agent_workflow:
if inner_tool in SENSITIVE_TOOLS:
    ctx.set_custom_status(json.dumps({"state":"input-required","gate":{...}}))
    decision = yield ctx.wait_for_external_event("decision")
    if decision.get("action") == "reject":
        # racconta al LLM che è stato rifiutato
        continue
```

### Flusso enforcement completo (quando tutti gli step sono attivi)

```
alice chiama:  POST /a2a/run {"task": "elimina il namespace staging"}
     ↓
[1] gateway valida JWT alice → ok, alice ha ruolo ai-platform-user → passa
     ↓
[2] gateway extAuth → OBO token {sub=alice, act=agent-service, scope=openid profile email}
     ↓
[3] agente gira il task, LLM decide di chiamare delete_namespace
     ↓
[4] HITL gate: delete_namespace è in SENSITIVE_TOOLS → workflow si ferma
    emit: {state: "input-required", tool: "delete_namespace", args: {ns: "staging"}}
     ↓
[5] alice riceve notifica: "l'agente vuole eseguire delete_namespace — approvi?"
     ↓
[5a] alice approva → workflow riprende
     ↓
[6] agente chiama MCP tools/call delete_namespace con OBO token
     ↓
[7] MCP server decodifica token:
    sub=alice → ha ruolo platform-admin? 
    NO → 403 Forbidden (alice non è admin, non può cancellare namespace)
     ↓
[8] agente riceve errore, lo dice a LLM, LLM risponde a alice:
    "Non hai i permessi per eliminare il namespace staging"
```

### The key insight

Without OBO, every MCP call shows `sub=agent-service`.
The tool server can only answer: *"is this agent allowed?"*

With OBO, every MCP call shows `sub=alice, act=agent-service`.
The tool server can answer: *"is alice allowed to do this via this agent?"*

The distinction matters for:
- **Different users → different permissions** (alice can deploy to staging, bob can deploy to prod)
- **Audit** (the action is attributed to alice, not to a generic service account)
- **Revocation** (alice's session ends → all in-flight tool calls using her sub become unauthorized)
- **Rate limiting** (per-user quota, not per-agent quota)

---

## Differences: POC vs Production

| Aspect | POC | Production |
|---|---|---|
| IdP | Keycloak in Docker | Keycloak or Zitadel on EKS |
| Gateway | webapp simulates extAuth | agentgateway (Envoy) with extAuth filter |
| TLS | none (HTTP) | Internal CA, mutual TLS |
| User login | ROPC (password grant) | PKCE + browser |
| LLM | Ollama local | AWS Bedrock via gateway /v1 |
| HITL | disabled (`ENABLE_HITL=0`) | Dapr durable workflow |
| Grant store | Redis direct | Redis via Dapr state API |
| Audit endpoints | /admin/* in-process | Separate operator service |

---

## Quickstart

```bash
# 1. Prerequisites: Podman or Docker, Ollama with a model
ollama pull gemma4-12b-qat   # or any chat model

# 2. Configure LLM
cp .env.example .env
# edit .env — set OPENAI_API_KEY or ANTHROPIC_API_KEY, or leave blank for Ollama

# 3. Start
./scripts/start.sh

# 4. Open
open http://localhost:8080          # identity flow visualizer
open http://localhost:8180/admin    # Keycloak (admin/admin)

# 5. Test (unit → integration → E2E)
./scripts/test-flow.sh

# 6. Fix Keycloak token-exchange if needed (usually auto via keycloak-setup)
./scripts/fix-keycloak-token-exchange.sh
```
