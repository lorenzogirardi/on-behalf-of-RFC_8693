#!/usr/bin/env bash
# Fix Keycloak token-exchange permission for exchange-app ← agent-service.
# Runs against localhost:8180 (keycloak exposed port).
set -euo pipefail

KC="http://localhost:8180"
REALM="poc"
ADMIN_USER="${KEYCLOAK_ADMIN:-admin}"
ADMIN_PASS="${KEYCLOAK_ADMIN_PASSWORD:-admin}"

ok()   { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; exit 1; }
info() { echo "  · $1"; }

echo "=== Keycloak token-exchange setup ==="

# ── Admin token ──────────────────────────────────────────────────────────
ADMIN_TOKEN=$(curl -sf -X POST "${KC}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli&grant_type=password&username=${ADMIN_USER}&password=${ADMIN_PASS}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
ok "admin token obtained"

auth_header() { echo "Authorization: Bearer ${ADMIN_TOKEN}"; }

# ── Client IDs ───────────────────────────────────────────────────────────
EXCHANGE_ID=$(curl -sf "${KC}/admin/realms/${REALM}/clients?clientId=exchange-app" \
  -H "$(auth_header)" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'])")
ok "exchange-app id: ${EXCHANGE_ID}"

AGENT_ID=$(curl -sf "${KC}/admin/realms/${REALM}/clients?clientId=agent-service" \
  -H "$(auth_header)" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'])")
ok "agent-service id: ${AGENT_ID}"

# ── Enable fine-grained permissions on exchange-app ──────────────────────
PERMS=$(curl -sf -X PUT "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/management/permissions" \
  -H "$(auth_header)" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}')
RESOURCE_ID=$(echo "$PERMS" | python3 -c "import sys,json; print(json.load(sys.stdin)['resource'])")
ok "fine-grained permissions enabled, resource: ${RESOURCE_ID}"

# ── Get the token-exchange scope permission ID ───────────────────────────
SCOPE_PERM_ID=$(echo "$PERMS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d['scopePermissions']['token-exchange'])
")
ok "token-exchange scope permission id: ${SCOPE_PERM_ID}"

# ── Delete any existing policy with the same name (idempotent) ──────────
EXISTING_POLICY=$(curl -sf "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/policy?name=allow-agent-service-token-exchange" \
  -H "$(auth_header)" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')" 2>/dev/null || echo "")

if [ -n "$EXISTING_POLICY" ]; then
  curl -sf -X DELETE \
    "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/policy/${EXISTING_POLICY}" \
    -H "$(auth_header)" 2>/dev/null || true
  info "deleted existing policy ${EXISTING_POLICY}"
fi

# ── Create client policy: allow agent-service ────────────────────────────
POLICY_BODY="{\"name\":\"allow-agent-service-token-exchange\",\"type\":\"client\",\"logic\":\"POSITIVE\",\"clients\":[\"${AGENT_ID}\"]}"
NEW_POLICY=$(curl -sf -X POST \
  "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/policy/client" \
  -H "$(auth_header)" \
  -H "Content-Type: application/json" \
  -d "${POLICY_BODY}")
POLICY_ID=$(echo "$NEW_POLICY" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
ok "policy created: ${POLICY_ID}"

# ── Fetch the scope permission object (need full body for PUT) ───────────
SCOPE_PERM=$(curl -sf \
  "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/permission/scope/${SCOPE_PERM_ID}" \
  -H "$(auth_header)")
ok "fetched scope permission"

# ── Patch: add our policy to the token-exchange scope permission ─────────
UPDATED=$(echo "$SCOPE_PERM" | python3 -c "
import sys,json
d=json.load(sys.stdin)
existing=d.get('policies',[])
if '${POLICY_ID}' not in existing:
    existing.append('${POLICY_ID}')
d['policies']=existing
print(json.dumps(d))
")
curl -sf -X PUT \
  "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/permission/scope/${SCOPE_PERM_ID}" \
  -H "$(auth_header)" \
  -H "Content-Type: application/json" \
  -d "${UPDATED}" >/dev/null
ok "policy linked to token-exchange scope permission"

# ── Smoke test: does token exchange work now? ────────────────────────────
echo ""
echo "=== Smoke test ==="

USER_TOKEN=$(curl -sf -X POST "${KC}/realms/${REALM}/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=poc-webapp&username=alice&password=alice123&scope=openid" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
info "got alice user token"

AGENT_TOKEN=$(curl -sf -X POST "${KC}/realms/${REALM}/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=agent-service&client_secret=agent-service-secret&scope=openid" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
info "got agent-service actor token"

EXCHANGE_RESP=$(curl -sf -X POST "${KC}/realms/${REALM}/protocol/openid-connect/token" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "client_id=exchange-app" \
  -d "client_secret=exchange-app-secret" \
  -d "subject_token=${USER_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "actor_token=${AGENT_TOKEN}" \
  -d "actor_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "requested_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "scope=openid profile email" 2>/dev/null || echo "{}")

OBO_TOKEN=$(echo "$EXCHANGE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")

if [ -z "$OBO_TOKEN" ]; then
  echo ""
  echo "  ✗ Token exchange FAILED. Keycloak response:"
  echo "$EXCHANGE_RESP" | python3 -m json.tool 2>/dev/null || echo "$EXCHANGE_RESP"
  echo ""
  echo "  This may need a Keycloak restart to pick up the new permission."
  echo "  Try: docker restart poc-keycloak && sleep 15 && bash scripts/fix-keycloak-token-exchange.sh"
  exit 1
fi

echo "$OBO_TOKEN" | python3 -c "
import sys,json,base64
t=sys.stdin.read().strip()
p=t.split('.')[1]; p+='='*(-len(p)%4)
c=json.loads(base64.urlsafe_b64decode(p))
sub=c.get('sub','?')
act=(c.get('act') or {}).get('sub','MISSING')
iss=c.get('iss','?')
alg=json.loads(base64.urlsafe_b64decode(t.split('.')[0]+'==')).get('alg','?')
print(f'  ✓ OBO token: sub={sub}')
print(f'  ✓ act.sub  = {act}')
print(f'  ✓ issuer   = {iss}')
print(f'  ✓ alg      = {alg}  ← RS256 = real Keycloak JWT')
if act == 'MISSING':
    print('  ⚠ act claim missing — Keycloak did not embed act in token')
"

echo ""
echo "=== Done. Restart obo-exchange to clear token cache ==="
echo "  docker restart poc-obo-exchange"
