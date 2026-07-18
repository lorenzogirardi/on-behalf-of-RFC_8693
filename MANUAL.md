# Agent Identity POC — Manuale

> Diagrammi Mermaid, flusso tecnico, tabella componenti → **[ARCHITECTURE.md](ARCHITECTURE.md)**

Audience: tech team con esperienza su container e API REST, non necessariamente su OAuth/OIDC.
Ogni concetto ha una spiegazione immediata + i dettagli tecnici.

---

## I componenti — cosa sono e perché esistono

### 1. Keycloak (IdP)

**In breve:** server che gestisce chi sei e cosa puoi fare. Rilascia token firmati.

Funziona come un **authorization server OAuth2/OIDC**: espone `/token`, `/authorize`,
`.well-known/openid-configuration`. Ogni applicazione lo chiama per verificare identità
senza gestire password o sessioni proprie.

Cosa fa in questo stack:
- Emette JWT firmati RS256 per utenti (ROPC/PKCE) e service account (client_credentials)
- Esegue **RFC 8693 Token Exchange**: riceve `subject_token` (utente) + `actor_token`
  (agente) e ritorna un token delegato `{sub=utente, act=agente}`
- Gestisce il realm `poc` con 3 client: `poc-webapp`, `agent-service`, `exchange-app`

```
GET  /realms/poc/.well-known/openid-configuration  ← discovery
POST /realms/poc/protocol/openid-connect/token     ← tutti i grant
```

**Porta locale:** `http://localhost:8180` · Admin: `admin/admin` · Realm: `poc`

> Nota: token exchange richiede `KC_FEATURES=token-exchange` + fine-grained authz
> sul client `exchange-app` + audience mapper su `poc-webapp`. `keycloak-setup`
> configura tutto automaticamente all'avvio.

---

### 2. JWT (JSON Web Token)

**In breve:** stringa base64url-encoded in 3 parti (header.payload.signature).
Stateless — chi lo riceve può verificare firma e leggere i claim senza chiamare il server.

```
eyJhbGciOiJSUzI1NiJ9  .  eyJzdWIiOiI4YzhhZjU...  .  dGhpcyBpcyBhIHNpZ25hdHVyZQ
      header                    payload                       signature
   {alg: RS256}          {sub, act, iss, exp, scope}     RSASSA-PKCS1-v1.5
```

Payload decodificato (base64url → JSON):
```json
{
  "sub": "8c8af53c-...",              ← UUID Keycloak dell'utente alice
  "act": { "sub": "agent-service" }, ← chi agisce per alice — il claim OBO
  "iss": "http://keycloak:8080/realms/poc",
  "exp": 1751234567,                 ← unix timestamp di scadenza
  "aud": "exchange-app",             ← chi può consumare questo token
  "scope": "openid profile email"
}
```

La firma è RS256 (Keycloak firma con chiave privata, chiunque verifica con la pubkey
esposta su `/.well-known/jwks.json`). Nel fallback locale è HS256 — stessa struttura,
firma diversa.

**`sub`** = soggetto del token (chi ha autenticato).
**`act.sub`** = chi sta agendo per conto del soggetto. Claim aggiunto da RFC 8693.

---

### 3. OBO — On-Behalf-Of (RFC 8693 Token Exchange)

**In breve:** scambia un token utente per un token delegato che porta entrambe le identità.
Serve perché l'agente deve agire come l'utente senza MAI avere le sue credenziali.

**Il problema senza OBO:**
```
User → Gateway → Agent
Agent chiama MCP: Authorization: Bearer <agent-service-token>
MCP vede: sub=agent-service
MCP non sa chi è l'utente → non può fare policy per-utente
```

**Con OBO:**
```
User → Gateway → [exchange: user-JWT + agent-creds → OBO JWT] → Agent
Agent chiama MCP: Authorization: Bearer <OBO-token>
MCP vede: sub=alice, act=agent-service
MCP può applicare: "alice ha ruolo platform-admin? alice è owner di questo namespace?"
```

**Flusso RFC 8693:**
```http
POST /realms/poc/protocol/openid-connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
client_id=exchange-app
client_secret=<exchange-app-secret>        ← solo obo-exchange lo ha
subject_token=<user-JWT>                   ← chi delega
subject_token_type=urn:ietf:params:oauth:token-type:access_token
actor_token=<agent-JWT>                    ← chi riceve la delega
actor_token_type=urn:ietf:params:oauth:token-type:access_token
requested_token_type=urn:ietf:params:oauth:token-type:access_token
scope=openid profile email
```

Risposta Keycloak:
```json
{
  "access_token": "<OBO-JWT con sub=alice, act.sub=agent-service, alg=RS256>",
  "refresh_token": "<RT — rotates on use>",
  "expires_in": 3600
}
```

**Perché il refresh token?** I task agent durano ore. Il `refresh_token` permette
di rinnovare l'`access_token` senza che alice sia presente. L'`act` claim sopravvive
al refresh. Il RT ruota a ogni uso (rotate-on-use) — invalidare il vecchio previene
replay attacks.

---

### 4. obo-exchange

**In breve:** microservizio che è l'unico holder del secret `exchange-app`. Nessun altro
componente può mintare token delegati direttamente.

Architettura:
```
agent → obo-exchange /exchange → Keycloak (RFC 8693) → OBO token
agent → obo-exchange /refresh  → Keycloak (refresh grant) → nuovo OBO token
gateway → obo-exchange /authz  → exchange inline → OBO headers iniettati
```

Il design **skeleton-key pattern** ha senso perché:
- Se l'agente avesse il secret, un agente compromesso potrebbe mintare token
  arbitrari per qualsiasi utente
- obo-exchange è un servizio piccolo, senza logica di business, facile da auditare
- La superficie d'attacco è minimizzata: un solo servizio, un solo secret

In produzione obo-exchange è deployato in un namespace separato con network policy
che lo rende raggiungibile solo da gateway e agent.

---

### 5. agentgateway (nel POC: sostituito dalla webapp)

**In breve:** reverse proxy Envoy con un filter `extAuth` che intercetta ogni request,
valida il JWT, chiama obo-exchange, e sostituisce l'header prima di forwardare.

Flusso nel gateway reale:
```
Client request (Authorization: Bearer <user-JWT>)
    ↓
Envoy listener
    ↓
extAuth filter → chiama obo-exchange /authz
    ↓
obo-exchange risponde: {allow: true, headers: {Authorization: Bearer <OBO-JWT>}}
    ↓
Envoy sostituisce Authorization header
    ↓
Upstream (agent) riceve solo OBO token — non ha mai visto il user-JWT
```

Il PDP (Policy Decision Point) valuta regole CEL:
```
jwt.iss == "https://idp.internal/realms/poc" &&
jwt.exp > now() &&
jwt.roles.exists(r, r == "ai-platform-user")
```

Nel POC la webapp chiama obo-exchange direttamente (stesso risultato, meno infrastruttura).

---

### 6. LiteLLM

**In breve:** proxy OpenAI-compatible che traduce verso qualsiasi provider LLM.

L'agente usa `openai.OpenAI(api_key=obo_token, base_url="http://litellm:4000/v1")`.
LiteLLM riceve la request, legge il model name, e chiama il provider corretto:

```
model: ollama_chat/gemma4-12b-qat → Ollama /api/chat
model: gpt-4o-mini               → OpenAI /v1/chat/completions
model: claude-haiku               → Anthropic API
model: anthropic.claude-...       → AWS Bedrock
```

L'`api_key` passata (il token OBO) non viene usata da LiteLLM per autenticarsi
verso Ollama — serve solo al gateway come bearer per autorizzare la call.

Config in `config/litellm.yaml`. Senza `master_key` nessun DB richiesto.

---

### 7. MCP — Model Context Protocol

**In breve:** protocollo JSON-RPC 2.0 su HTTP che standardizza come un LLM chiama tool.

Sessione MCP:
```http
POST /mcp  {"method": "initialize", "params": {...}}
→ Header: Mcp-Session-Id: abc123

POST /mcp  {"method": "tools/list", "params": {}}
→ [{name: "echo"}, {name: "list_deployments"}, ...]

POST /mcp  {"method": "tools/call", "params": {"name": "echo", "arguments": {"message": "hi"}}}
→ {result: {content: [{type: "text", text: "Echo: hi"}]}}
```

Ogni request porta `Authorization: Bearer <OBO-token>`. Il server MCP:
1. Decodifica il JWT (no signature check se già validato dal gateway)
2. Legge `sub` e `act.sub` — sa chi è l'utente e chi è l'agente
3. Esegue il tool o ritorna 403 se l'utente non ha i permessi

Nel POC il mcp-mock logga l'identità ma non enforcia — vedi `ARCHITECTURE.md §Authorization`.

---

### 8. Dapr (nel POC: non presente)

**In breve:** runtime sidecar che astrae state store, pub/sub e workflow da un unico
HTTP API su `localhost:3500`.

Perché viene usato in produzione (non nel POC):
- `grant_store.py` in produzione chiama `http://localhost:3500/v1.0/state/statestore`
  invece di Redis diretto → il backend (Redis/DynamoDB/CosmosDB) è intercambiabile
- Il **durable workflow** per HITL usa Dapr Workflow: l'orchestratore è deterministic
  e può fare `wait_for_external_event("decision")` che si serializza su disco.
  Il pod può crashare e ripartire — il workflow riprende dallo stesso punto.

Nel POC `grant_store.py` parla direttamente a Redis su `redis://redis:6379`.

---

### 9. HITL — Human in the Loop

**In breve:** il workflow si sospende su disco quando l'agente vuole eseguire un'operazione
sensibile, finché un umano approva o rigetta.

Come funziona tecnicamente:
```python
# agent.py — orchestratore Dapr Workflow
decision = yield ctx.wait_for_external_event("decision")
# ← il processo si serializza qui, non consuma CPU

# Quando l'umano chiama:
# POST /a2a/hitl/runs/{id}/decision {"action": "approve"}
# il workflow riprende dalla riga dopo yield
```

La serializzazione avviene su Redis via Dapr. La durata è illimitata — l'umano
può rispondere dopo 5 minuti o 5 ore. Il token OBO viene rinnovato automaticamente
nel frattempo da `_live_token()`.

**Non attivo nel POC** (`ENABLE_HITL=0`) — tutto gira sincrono su `/a2a/run`.

---

## Struttura

```
agent-identity-poc/
├── docker-compose.yml
├── .env.example
├── config/litellm.yaml
├── services/
│   ├── keycloak-setup/     ← realm import + RFC 8693 permission setup (bash)
│   ├── obo-exchange/       ← RFC 8693 broker (Python/FastAPI)
│   ├── mcp-mock/           ← MCP server con 4 tool demo (Python/FastAPI)
│   ├── agent/
│   │   ├── agent.py        ← tool-calling loop + FastAPI endpoints
│   │   ├── obo.py          ← OBOClient: actor_token cache + exchange/refresh
│   │   └── grant_store.py  ← AES-256-GCM grant store su Redis
│   └── webapp/
│       ├── server.py       ← backend che orchestra il flusso
│       └── static/index.html ← identity flow visualizer UI
└── scripts/
    ├── start.sh
    ├── stop.sh
    ├── logs.sh
    ├── test-flow.sh                  ← pyramid: unit → integration → E2E
    └── fix-keycloak-token-exchange.sh ← ripara la permission RFC 8693 se serve
```

---

## Avvio

```bash
# Prerequisiti: Podman o Docker, Ollama con un modello
ollama pull gemma4-12b-qat   # o qualsiasi modello chat

cp .env.example .env
# lascia vuoto per Ollama, oppure aggiungi OPENAI_API_KEY / ANTHROPIC_API_KEY

chmod +x scripts/*.sh
./scripts/start.sh

# Keycloak impiega ~30s al primo avvio (JVM + DB init)
# Successivamente: ~5s (tutto cachato)
```

**Apri:** `http://localhost:8080` — identity flow visualizer  
**Keycloak admin:** `http://localhost:8180/admin` (admin/admin)  
**Utenti demo:** alice/alice123, bob/bob123

```bash
./scripts/test-flow.sh   # 16 test: unit + integration + E2E
```

---

## Osservare il flusso

### Webapp — step 2 (Gateway OBO Exchange)

Mostra la trasformazione:
```
User JWT   alg=RS256  sub=8c8af53c (alice)
   ↓ obo-exchange → Keycloak RFC 8693
OBO Token  alg=RS256  sub=8c8af53c (alice)  act.sub=agent-service
   ↓ iniettato come Authorization header
Agent riceve SOLO il token delegato
```

`fallback=False` = Keycloak ha fatto il vero exchange RS256.  
`fallback=True` = Keycloak non configurato, token HMAC locale (stessa struttura).

### Logs

```bash
./scripts/logs.sh agent
# [OBO] run=abc123 sub=8c8af53c act=agent-service has_refresh=True
# [MCP] run=abc123 tools/list sub=8c8af53c act=agent-service ok=True
```

### Audit

```bash
RUN_ID="<instance_id dal risultato>"
curl http://localhost:8082/admin/instances/$RUN_ID/identity | python3 -m json.tool
curl http://localhost:8082/admin/instances/$RUN_ID/trace    | python3 -m json.tool
```

---

## Differenze POC vs Produzione

| Aspetto | POC | Produzione |
|---|---|---|
| IdP | Keycloak in Docker (RS256) | Keycloak o Zitadel su EKS |
| Gateway | webapp simula extAuth | agentgateway (Envoy + extAuth filter) |
| Token validation | nessuna (OBO trusted) | PDP CEL rules su ogni hop |
| HITL | disabilitato | Dapr durable workflow |
| Grant store | Redis diretto | Redis via Dapr state API |
| LLM | Ollama locale via LiteLLM | Bedrock via agentgateway /v1 |
| TLS | nessuno | mutual TLS + internal CA |
| Audit | /admin/* in-process | servizio operatore separato |

---

## Troubleshooting

**Agent 401:**
```bash
# Manca l'OBO grant — non chiamare /a2a/run direttamente.
# La webapp fa l'exchange prima. Controlla i log obo-exchange.
./scripts/logs.sh obo-exchange
```

**LiteLLM `No connected db`:**
```bash
# master_key nel litellm.yaml richiede un DB PostgreSQL.
# Rimuoverlo risolve — il POC non ha DB per LiteLLM.
grep master_key config/litellm.yaml   # non deve esserci
```

**Keycloak token exchange fallback:**
```bash
# setup.sh ha fallito la configurazione permission RFC 8693.
# Applica manualmente:
./scripts/fix-keycloak-token-exchange.sh
docker restart poc-obo-exchange
```

**Ollama content vuoto (gemma4 thinking model):**
```bash
# gemma4-12b-qat mette la risposta in reasoning_content, non content.
# LiteLLM config deve usare ollama_chat/ e think: false.
# Già configurato in config/litellm.yaml.
docker restart poc-litellm
```
