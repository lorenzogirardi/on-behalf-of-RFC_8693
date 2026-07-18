#!/usr/bin/env bash
# Keycloak realm bootstrap — runs once at stack startup, then exits.
#
# Configures everything needed for RFC 8693 Token Exchange:
#   1. Import realm 'poc' (users, clients) from realm-poc.json
#   2. Enable authz on exchange-app (required for fine-grained permissions)
#   3. Enable fine-grained management permissions on exchange-app
#   4. Add audience mapper on poc-webapp → tokens include exchange-app in 'aud'
#   5. Create client policy: allow agent-service to exchange
#   6. Link policy to token-exchange scope permission
#   7. Smoke-test the full exchange flow
set -euo pipefail

KC="http://keycloak:8080"
REALM="poc"
ADMIN_USER="${KEYCLOAK_ADMIN:-admin}"
ADMIN_PASS="${KEYCLOAK_ADMIN_PASSWORD:-admin}"

ok()   { echo "[setup] ✓ $*"; }
info() { echo "[setup] · $*"; }
fail() { echo "[setup] ✗ $*"; exit 1; }

echo "[setup] Waiting for Keycloak..."
until curl -sf "${KC}/realms/master" >/dev/null 2>&1; do sleep 3; done
ok "Keycloak is up"

# ── 1. Admin token ───────────────────────────────────────────────────────
ADMIN_TOKEN=$(curl -sf -X POST "${KC}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli&grant_type=password&username=${ADMIN_USER}&password=${ADMIN_PASS}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
ok "admin token obtained"

H="Authorization: Bearer ${ADMIN_TOKEN}"

# ── 2. Import realm ──────────────────────────────────────────────────────
REALM_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" -H "$H" "${KC}/admin/realms/${REALM}" || echo "000")
if [ "$REALM_STATUS" = "200" ]; then
  ok "realm '${REALM}' already exists — skipping import"
else
  curl -sf -X POST "${KC}/admin/realms" \
    -H "$H" -H "Content-Type: application/json" -d @/setup/realm-poc.json
  ok "realm '${REALM}' imported"
fi

# ── 3. Get client IDs ────────────────────────────────────────────────────
EXCHANGE_ID=$(curl -sf "${KC}/admin/realms/${REALM}/clients?clientId=exchange-app" \
  -H "$H" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
ok "exchange-app: ${EXCHANGE_ID}"

AGENT_ID=$(curl -sf "${KC}/admin/realms/${REALM}/clients?clientId=agent-service" \
  -H "$H" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
ok "agent-service: ${AGENT_ID}"

WEBAPP_ID=$(curl -sf "${KC}/admin/realms/${REALM}/clients?clientId=poc-webapp" \
  -H "$H" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
ok "poc-webapp: ${WEBAPP_ID}"

# ── 4. Enable authz on exchange-app (needed for fine-grained permissions) ─
CLIENT=$(curl -sf "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}" -H "$H")
AUTHZ_ENABLED=$(echo "$CLIENT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('authorizationServicesEnabled',False))")
if [ "$AUTHZ_ENABLED" != "True" ] && [ "$AUTHZ_ENABLED" != "true" ]; then
  UPDATED=$(echo "$CLIENT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
d['authorizationServicesEnabled']=True
d['serviceAccountsEnabled']=True
print(json.dumps(d))
")
  curl -sf -X PUT "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}" \
    -H "$H" -H "Content-Type: application/json" -d "$UPDATED"
  ok "authz services enabled on exchange-app"
else
  ok "authz services already enabled on exchange-app"
fi

# ── 5. Enable fine-grained management permissions ────────────────────────
PERMS=$(curl -sf -X PUT "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/management/permissions" \
  -H "$H" -H "Content-Type: application/json" -d '{"enabled": true}')
SCOPE_PERM_ID=$(echo "$PERMS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d.get('scopePermissions',{}).get('token-exchange',''))
")
if [ -z "$SCOPE_PERM_ID" ]; then
  fail "token-exchange scope permission not found — KC_FEATURES=token-exchange not active?"
fi
ok "token-exchange scope permission id: ${SCOPE_PERM_ID}"

# ── 6. Add audience mapper on poc-webapp (user token includes exchange-app in aud) ──
EXISTING_MAPPER=$(curl -sf "${KC}/admin/realms/${REALM}/clients/${WEBAPP_ID}/protocol-mappers/models" \
  -H "$H" | python3 -c "
import sys,json
for m in json.load(sys.stdin):
    if m.get('name')=='exchange-app-audience':
        print(m['id'])
        break
" 2>/dev/null || echo "")
if [ -z "$EXISTING_MAPPER" ]; then
  curl -sf -X POST "${KC}/admin/realms/${REALM}/clients/${WEBAPP_ID}/protocol-mappers/models" \
    -H "$H" -H "Content-Type: application/json" \
    -d '{"name":"exchange-app-audience","protocol":"openid-connect","protocolMapper":"oidc-audience-mapper","consentRequired":false,"config":{"included.client.audience":"exchange-app","id.token.claim":"false","access.token.claim":"true"}}'
  ok "audience mapper added to poc-webapp"
else
  ok "audience mapper already exists on poc-webapp"
fi

# ── 7. Create client policy: allow agent-service to perform exchange ──────
POLICY_NAME="allow-agent-service-token-exchange"

# Delete stale policy if exists
OLD=$(curl -sf "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/policy?name=${POLICY_NAME}" \
  -H "$H" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')" 2>/dev/null || echo "")
if [ -n "$OLD" ]; then
  curl -sf -X DELETE \
    "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/policy/${OLD}" \
    -H "$H" 2>/dev/null || true
  info "removed stale policy ${OLD}"
fi

NEW_POLICY=$(curl -sf -X POST \
  "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/policy/client" \
  -H "$H" -H "Content-Type: application/json" \
  -d "{\"name\":\"${POLICY_NAME}\",\"type\":\"client\",\"logic\":\"POSITIVE\",\"clients\":[\"${AGENT_ID}\"]}")
POLICY_ID=$(echo "$NEW_POLICY" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
ok "policy created: ${POLICY_ID}"

# ── 8. Link policy to token-exchange scope permission ────────────────────
SCOPE_PERM=$(curl -sf \
  "${KC}/admin/realms/${REALM}/clients/${EXCHANGE_ID}/authz/resource-server/permission/scope/${SCOPE_PERM_ID}" \
  -H "$H")
UPDATED_PERM=$(echo "$SCOPE_PERM" | python3 -c "
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
  -H "$H" -H "Content-Type: application/json" -d "$UPDATED_PERM" >/dev/null
ok "policy linked to token-exchange scope permission"

# ── 9. Smoke test ─────────────────────────────────────────────────────────
info "smoke testing RFC 8693 token exchange..."
USER_TOKEN=$(curl -sf -X POST "${KC}/realms/${REALM}/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=poc-webapp&username=alice&password=alice123&scope=openid" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
AGENT_TOKEN=$(curl -sf -X POST "${KC}/realms/${REALM}/protocol/openid-connect/token" \
  -d "grant_type=client_credentials&client_id=agent-service&client_secret=agent-service-secret&scope=openid" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

OBO_RESP=$(curl -sf -X POST "${KC}/realms/${REALM}/protocol/openid-connect/token" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "client_id=exchange-app" \
  -d "client_secret=exchange-app-secret" \
  -d "subject_token=${USER_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "actor_token=${AGENT_TOKEN}" \
  -d "actor_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "requested_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "scope=openid profile email" 2>/dev/null || echo "{}")

python3 - <<PYEOF
import sys,json,base64
resp = json.loads('''${OBO_RESP}''')
if 'access_token' not in resp:
    print(f"[setup] ✗ Token exchange FAILED: {resp}")
    sys.exit(1)
t = resp['access_token']
p = t.split('.')[1]; p += '='*(-len(p)%4)
c = json.loads(base64.urlsafe_b64decode(p))
sub  = c.get('sub','?')
act  = (c.get('act') or {}).get('sub','MISSING')
alg  = json.loads(base64.urlsafe_b64decode(t.split('.')[0]+'==')).get('alg','?')
print(f"[setup] ✓ Token exchange OK  sub={sub}  act={act}  alg={alg}")
PYEOF

echo "[setup]"
echo "[setup] ════════════════════════════════════════"
echo "[setup]  Realm:   ${REALM}"
echo "[setup]  IdP:     ${KC}/realms/${REALM}"
echo "[setup]  Users:   alice/alice123  bob/bob123"
echo "[setup]  RFC8693: ENABLED (Keycloak native)"
echo "[setup] ════════════════════════════════════════"
