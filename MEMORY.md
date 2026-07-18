# Session Memory — Agent Identity POC

Tutto quello fatto in questa sessione (2026-07-02 / 2026-07-03).
Da leggere integralmente quando si riprende il lavoro su un nuovo computer.

---

## Origine del progetto

Creato come POC locale del pattern di identity delegation implementato in
`gd_platformengineering-valerie/agentgateway/src/` (produzione su EKS, usa
Zitadel + agentgateway Envoy-based). L'obiettivo era dimostrare il concetto
senza VPN/cluster/certificati interni usando solo Docker locale.

---

## Cosa è stato costruito

### Stack Docker completo (Podman-compatible)
Partendo da zero in `/Users/lorenzo.girardi/Storage/004-work/agent-identity-poc/`:

- **Keycloak 24** in Docker (sostituisce Zitadel di produzione) — real IdP con RFC 8693
- **obo-exchange** Python/FastAPI — broker OBO, unico holder del `exchange-app` secret
- **agent** Python/FastAPI — tool-calling loop sincrono, grant store Redis AES-256-GCM
- **mcp-mock** Python/FastAPI — MCP Streamable HTTP con 4 tool demo
- **LiteLLM** proxy — routing verso Ollama (gemma4-12b-qat locale)
- **webapp** dark UI — visualizzatore step-by-step del delegation chain
- **Redis** — grant store encrypted

### Documentazione
- `ARCHITECTURE.md` — 6 diagrammi Mermaid (sequence, flowchart, mindmap, statediagram, trust boundary, startup sequence) + sezione authorization completa
- `MANUAL.md` — spiegazione tecnica di ogni componente (JWT, RFC 8693, extAuth, MCP sessione, Dapr workflow)
- `CLAUDE.md` — questo file + file corrente

---

## Decisioni tecniche prese

### Perché Keycloak e non Zitadel
Zitadel in produzione richiede configurazione manuale (console + Management API),
certificati TLS, VPN. Keycloak 24 con `start-dev` parte in Docker senza config
esterna e ha RFC 8693 nativo con `KC_FEATURES=token-exchange`.

### Perché il fallback HMAC locale in obo-exchange
Keycloak RFC 8693 richiede 6 step di configurazione (realm import, authz enable,
audience mapper, policy, scope permission link). Se un passo fallisce all'avvio,
obo-exchange usa un fallback HMAC-SHA256 con struttura JWT identica. La webapp
mostra ⚠ badge. `fallback=False` è il target — verificato nel test E2E.

### LiteLLM senza master_key
`master_key` in litellm.yaml richiede un database PostgreSQL collegato.
Per il POC non serve — rimosso. Senza master_key LiteLLM non richiede auth
sulle call `/v1/chat/completions`.

### gemma4-12b-qat = thinking model
Il modello mette tutto in `reasoning_content`, non `content`. LiteLLM config
usa `ollama_chat/` (non `ollama/`) + `extra_body.options.think: false` per
forzare l'output in `content`. Verificato funzionante.

### Healthcheck senza curl
Le immagini Docker usate non hanno curl/wget preinstallato:
- Keycloak: usa `bash /dev/tcp` trick
- python:3.12-slim: usa `python3 -c "import urllib.request; urlopen(...)"`
- LiteLLM: usa `/usr/bin/python3` (non `python3` nel PATH) + endpoint `/health/liveliness`

### act.sub = nome client, non UUID
Il token Keycloak per `agent-service` ha `sub=UUID` e `azp=agent-service`.
obo-exchange usa `azp` (authorized party = client name) invece di `sub`
per costruire il claim `act.sub` nel fallback locale — risultato leggibile.

---

## Bug risolti

1. **keycloak-setup non partiva** — dipendeva da `service_completed_successfully` su container che non finiva mai. Fix: `depends_on: keycloak: service_healthy`.

2. **`apt-get install curl` falliva** — mancava `apt-get update` prima. Fix: aggiunto nel compose command.

3. **mcp-mock usciva con exit 0** — mancava il main block `if __name__ == "__main__": uvicorn.run(...)`. Fix: aggiunto.

4. **obo-exchange crash** — `httpx` non nel requirements.txt. Fix: aggiunto.

5. **LiteLLM 400 "No connected db"** — `master_key` nel config richiede DB. Fix: rimosso.

6. **gemma4 content vuoto** — thinking model, risposta in `reasoning_content`. Fix: `ollama_chat/` + `think: false`.

7. **Keycloak token exchange 403 "not within audience"** — user token di `poc-webapp` non includeva `exchange-app` in `aud`. Fix: audience mapper sul client poc-webapp.

8. **act.sub = stessa UUID del sub** — webapp passava user JWT come Authorization header su `/exchange` → obo-exchange usava user token come actor. Fix: rimosso Authorization header, obo-exchange usa le sue credenziali interne.

9. **Keycloak permission RFC 8693 fallisce** — `authorizationServicesEnabled` non era abilitato su exchange-app prima di tentare di creare la policy. Fix: step 4 in setup.sh abilita authz esplicitamente.

---

## Stato finale (2026-07-03 ~17:00)

```
16/16 test passano
fallback=False (Keycloak RS256 reale)
sub=8c8af53c (alice UUID Keycloak)
act=agent-service (nome leggibile)
alg=RS256

2 commit:
  cbf8454  feat: agent identity POC initial
  c43a8c9  docs: rewrite MANUAL.md + expand ARCHITECTURE.md
```

### Cosa funziona
- Login reale Keycloak (ROPC per demo, PKCE disponibile)
- RFC 8693 token exchange reale (non fallback)
- OBO token RS256 firmato da Keycloak con `sub=utente act=agent-service`
- Agent riceve solo OBO grant (mai il token utente grezzo)
- Grant store AES-256-GCM in Redis
- Tool-calling loop: LLM → MCP → risultato
- Audit: `/admin/instances/{id}/identity` + `/admin/instances/{id}/trace`
- Test E2E via webapp

### Cosa non è attivo (by design, documentato)
- Gateway CEL enforcement — webapp bypassa il gateway
- MCP per-tool role check — mcp-mock logga ma non blocca
- HITL (`ENABLE_HITL=0`)
- TLS / mutual TLS
- Dapr sidecar (Redis diretto)

---

## Prossimi step possibili

1. **Aggiungere enforcement al mcp-mock** — leggere `realm_access.roles` dal token, bloccare tool sensibili se manca `platform-admin`. Vedi `ARCHITECTURE.md §Authorization` per codice.

2. **Aggiungere ruoli Keycloak ad alice** — in Keycloak admin aggiungere ruolo `platform-admin` ad alice e verificare che il tool call funzioni. Rimuoverlo e verificare il 403.

3. **Abilitare HITL** — impostare `ENABLE_HITL=1` nel docker-compose, verificare che il workflow si sospenda su `CallMcpTool` e riprenda con `POST /a2a/hitl/runs/{id}/decision`.

4. **Aggiungere scope personalizzati** — creare scope `mcp:read` / `mcp:write` in Keycloak, aggiungerli all'exchange, verificare enforcement nel mcp-mock.

5. **Push su un remote** — il repo è solo locale (git init). Aggiungere remote e pushare per condivisione team.

---

## Comandi utili da ricordare

```bash
# Avvio (da dentro la cartella)
./scripts/start.sh

# Verifica tutto funziona
./scripts/test-flow.sh

# Se RFC 8693 mostra fallback=True
./scripts/fix-keycloak-token-exchange.sh && docker restart poc-obo-exchange

# Logs in tempo reale di un servizio
./scripts/logs.sh agent        # [OBO]/[MCP] lines
./scripts/logs.sh obo-exchange # [EXCHANGE]/[REFRESH] lines

# Audit manuale di un run
curl http://localhost:8082/admin/instances/$RUN_ID/identity | python3 -m json.tool
curl http://localhost:8082/admin/instances/$RUN_ID/trace    | python3 -m json.tool

# Keycloak admin
open http://localhost:8180/admin   # admin/admin — realm: poc

# Token exchange manuale (debug)
USER_TOKEN=$(curl -sf -X POST http://localhost:8180/realms/poc/protocol/openid-connect/token \
  -d "grant_type=password&client_id=poc-webapp&username=alice&password=alice123&scope=openid" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sf -X POST http://localhost:8081/exchange \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "subject_token=$USER_TOKEN" \
  | python3 -c "
import sys,json,base64
d=json.load(sys.stdin)
t=d['access_token']
p=t.split('.')[1]; p+='='*(-len(p)%4)
c=json.loads(base64.urlsafe_b64decode(p))
print(f'sub={c[\"sub\"]}  act={c.get(\"act\",{}).get(\"sub\")}  fallback={c.get(\"_fallback\",False)}')
"

# Stop
./scripts/stop.sh
```
