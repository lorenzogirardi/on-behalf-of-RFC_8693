# Helm deployment — agent-identity-poc

Every component is **standalone and externally wireable** — same principle as
pointing the stack at an existing Keycloak. Any bundled piece (Keycloak,
Redis, LiteLLM, mcp-mock) can be disabled and replaced by an URL in `values`.

## Build & push images

Images are built from the repo root (shared `services/` build context):

```bash
REG=ghcr.io/lorenzogirardi/on-behalf-of-rfc_8693
for svc in obo-exchange agent mcp-mock webapp; do
  docker build -t $REG/$svc:0.1.0 -f services/$svc/Dockerfile services/
  docker push $REG/$svc:0.1.0
done
```

## Install

Self-contained cluster demo (bundled Keycloak + Redis, fallback allowed):

```bash
helm install poc ./helm/agent-identity-poc \
  -f helm/agent-identity-poc/values-dev.yaml \
  --set image.tag=0.1.0 \
  --namespace agent-identity --create-namespace
```

Against **existing** infrastructure (the intended production shape):

```bash
helm install poc ./helm/agent-identity-poc \
  --set image.tag=0.1.0 \
  --set config.kcIssuer=https://keycloak.mycompany.com/realms/agents \
  --set config.redisUrl=redis://redis.shared-cache.svc:6379 \
  --set litellm.enabled=false \
  --set services.agent.env.LLM_BASE_URL=https://llm-gateway.mycompany.com/v1 \
  --set secrets.create=false \
  --set secrets.existingSecret=agent-identity-secrets \
  --namespace agent-identity --create-namespace
```

Keycloak realm prerequisites for an external IdP are exactly the steps in
`services/keycloak-setup/setup.sh` (clients `poc-webapp` / `agent-service` /
`exchange-app`, audience mapper, token-exchange permission).

## Scaling model

| Component | State | Scaling |
|---|---|---|
| obo-exchange | none (actor-token cache re-mints) | HPA, default 2–6 replicas |
| agent | grants/traces in shared Redis (AES-GCM) | HPA, default 2–6 replicas |
| mcp-mock | per-session dict, TTL-bound | replicas fine; demo singleton |
| webapp | cosmetic event feed per replica | demo singleton |

Readiness (`/readyz`) gates traffic on real dependencies: obo-exchange needs
Keycloak discovery; agent needs Redis + obo-exchange. A replica that loses
Redis is pulled from rotation instead of degrading to in-memory state.

## Production checklist (your responsibility, not the chart's)

- `config.allowLocalFallback: "false"` (default) — never allow HMAC fallback tokens
- TLS: ingress TLS at minimum; mesh mTLS recommended (`X-OBO-Refresh-Token`
  is credential material and must not cross plaintext links)
- Secrets via External Secrets Operator / Sealed Secrets (`secrets.create=false`)
- NetworkPolicy: only the gateway and agent may reach obo-exchange
- Prometheus scraping: pods carry `prometheus.io/*` annotations; import the
  dashboards from `observability/grafana/dashboards/`
