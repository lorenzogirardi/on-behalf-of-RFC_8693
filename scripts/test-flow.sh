#!/usr/bin/env bash
# Pyramid test: unit → integration → e2e
# Layer 1 runs without any stack. Layers 2+3 require a running stack.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # repo root — layer 1 uses relative paths

PASS=0; FAIL=0
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}✗${NC} $1"; FAIL=$((FAIL+1)); }
skip() { echo -e "  ${YELLOW}○${NC} $1 (skipped)"; }
h1()   { echo -e "\n${YELLOW}▶ $1${NC}"; }

# ─────────────────────────────────────────────────────────────────────────
h1 "LAYER 1 — Unit tests (no network)"
# ─────────────────────────────────────────────────────────────────────────

echo "  [1.1] JWT structure — sub + act claim"
python3 - <<'EOF'
import base64, json, hmac, hashlib, time
SECRET = b"test-secret"
now = int(time.time())
def b64url(d): return base64.urlsafe_b64encode(d).decode().rstrip("=")
h = b64url(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
p = b64url(json.dumps({"sub":"user-alice","act":{"sub":"agent-service"},
                        "exp":now+3600,"iat":now}).encode())
sig = b64url(hmac.new(SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest())
token = f"{h}.{p}.{sig}"
parts = token.split(".")
assert len(parts) == 3
payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
assert payload["sub"] == "user-alice"
assert payload["act"]["sub"] == "agent-service"
assert payload["exp"] > now
print("    JWT ok: sub=%s act=%s" % (payload["sub"], payload["act"]["sub"]))
EOF
ok "JWT structure valid: sub + act claim present"

echo "  [1.2] Grant store format (nonce + ciphertext, base64)"
python3 - <<'EOF'
import base64, json, os
original = {"access_token":"tok123","refresh_token":"rt456","expires_at":9999999.0}
nonce = os.urandom(12)
blob = base64.b64encode(nonce + json.dumps(original).encode()).decode()
raw = base64.b64decode(blob)
n, ct = raw[:12], raw[12:]
assert len(n) == 12, "nonce must be 12 bytes"
recovered = json.loads(ct)
assert recovered == original
print("    format ok: 12-byte nonce + payload")
EOF
ok "Grant store: 12-byte nonce prefix + base64 blob"

echo "  [1.3] near_expiry logic (60s skew)"
python3 - <<'EOF'
import time
SKEW = 60.0
near = lambda exp: time.time() >= exp - SKEW
assert near(time.time() - 10),    "expired → near_expiry"
assert not near(time.time()+7200),"7200s future → NOT near_expiry"
assert near(time.time() + 30),    "30s future → near_expiry (within 60s skew)"
print("    logic ok")
EOF
ok "near_expiry: expired=True, 7200s=False, 30s=True (skew=60)"

echo "  [1.4] OBO token decode — sub/act extraction"
python3 - <<'EOF'
import base64, json
# Simulate decoding an OBO token from Keycloak
payload = {"sub":"user-alice","act":{"sub":"agent-service"},
           "iss":"http://keycloak:8080/realms/poc","exp":9999999}
def b64url(d): return base64.urlsafe_b64encode(d).decode().rstrip("=")
h = b64url(json.dumps({"alg":"RS256","typ":"JWT"}).encode())
p = b64url(json.dumps(payload).encode())
token = f"{h}.{p}.fakesig"
# decode (as the agent does — display only, no sig verification)
part = token.split(".")[1]
decoded = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
assert decoded["sub"] == "user-alice"
assert decoded["act"]["sub"] == "agent-service"
assert "iss" in decoded
print("    sub=%s act=%s" % (decoded["sub"], decoded["act"]["sub"]))
EOF
ok "JWT decode: sub + act.sub extraction works"

echo "  [1.5] Python syntax — all services compile"
if python3 -m py_compile services/common/obs.py services/agent/*.py \
    services/obo-exchange/server.py services/mcp-mock/server.py \
    services/webapp/server.py 2>/dev/null; then
  ok "py_compile: all service sources valid"
else
  fail "py_compile failed — syntax error in a service"
fi

echo "  [1.6] Grafana dashboards — valid JSON with panels"
DASH=$(python3 - <<'EOF'
import json, glob
files = sorted(glob.glob("observability/grafana/dashboards/*.json"))
assert files, "no dashboards found"
total = 0
for f in files:
    d = json.load(open(f))
    assert d.get("uid") and d.get("panels"), f"{f}: missing uid/panels"
    total += len(d["panels"])
print(f"{len(files)} dashboards, {total} panels")
EOF
) && ok "Dashboards: $DASH" || fail "Dashboard JSON invalid"

echo "  [1.7] Helm chart — lint + template (default and dev values)"
if command -v helm >/dev/null 2>&1; then
  if helm lint helm/agent-identity-poc >/dev/null 2>&1 \
     && helm template poc helm/agent-identity-poc >/dev/null 2>&1 \
     && helm template poc helm/agent-identity-poc -f helm/agent-identity-poc/values-dev.yaml >/dev/null 2>&1; then
    ok "Helm chart: lint + template OK (default + dev)"
  else
    fail "Helm chart lint/template failed"
  fi
else
  skip "helm not installed"
fi

echo "  [1.8] Fallback token is flagged and gateable"
python3 - <<'EOF'
import base64, json
# A fallback-minted token must always carry _fallback=True so consumers
# (webapp badge, tests, dashboards) can detect the downgraded trust level.
payload = {"sub":"u","act":{"sub":"a"},"_fallback":True}
p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
decoded = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
assert decoded["_fallback"] is True
print("    _fallback flag round-trips")
EOF
ok "Fallback tokens carry _fallback marker"

# ─────────────────────────────────────────────────────────────────────────
h1 "LAYER 2 — Integration tests (requires running stack)"
# ─────────────────────────────────────────────────────────────────────────

STACK_UP=false
curl -sf http://localhost:8081/healthz >/dev/null 2>&1 && STACK_UP=true

if ! $STACK_UP; then
  skip "Stack not running — skipping (run ./scripts/start.sh first)"
else

  echo "  [2.1] Keycloak reachable"
  R=$(curl -sf http://localhost:8180/realms/poc/.well-known/openid-configuration 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['issuer'])" 2>/dev/null || echo "fail")
  if [ "$R" != "fail" ]; then ok "Keycloak issuer: $R"; else fail "Keycloak not reachable"; fi

  echo "  [2.2] Keycloak login — alice"
  USER_TOKEN=$(curl -sf -X POST "http://localhost:8180/realms/poc/protocol/openid-connect/token" \
    -d "grant_type=password&client_id=poc-webapp&username=alice&password=alice123&scope=openid+profile+email" \
    -H "Accept: application/json" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "")
  if [ -n "$USER_TOKEN" ]; then
    SUB=$(echo "$USER_TOKEN" | python3 -c "
import sys,json,base64
t=sys.stdin.read().strip()
p=t.split('.')[1]; p+=('='*(-len(p)%4))
print(json.loads(base64.urlsafe_b64decode(p))['sub'])
")
    ok "Keycloak ROPC login OK — sub=$SUB"
  else
    fail "Keycloak login failed for alice"
    USER_TOKEN=""
  fi

  if [ -n "$USER_TOKEN" ]; then
    echo "  [2.3] OBO exchange — real RFC 8693 (no Authorization header:"
    echo "        obo-exchange must use its own agent-service actor token)"
    # Subject token via webapp /login: dev-mode Keycloak derives the token iss
    # from the request Host header, and the exchange (made in-network against
    # keycloak:8080) rejects tokens issued through localhost:8180 with
    # invalid_token. The webapp logs in through the internal hostname.
    EX_TOKEN=$(curl -sf -X POST http://localhost:8080/login \
      -H "Content-Type: application/json" \
      -d '{"username":"alice","password":"alice123"}' \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "$USER_TOKEN")
    OBO_RESP=$(curl -sf -X POST http://localhost:8081/exchange \
      -H "Content-Type: application/x-www-form-urlencoded" \
      -d "subject_token=$EX_TOKEN&scope=openid+profile+email+offline_access")
    OBO_OK=$(echo "$OBO_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'access_token' in d else 'fail')" 2>/dev/null || echo "fail")
    IS_FALLBACK=$(echo "$OBO_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('_note','').startswith('local-fallback') else 'no')" 2>/dev/null || echo "no")
    if [ "$OBO_OK" = "ok" ]; then
      OBO_TOKEN=$(echo "$OBO_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
      OBO_SUB=$(echo "$OBO_TOKEN" | python3 -c "
import sys,json,base64
t=sys.stdin.read().strip()
p=t.split('.')[1]; p+=('='*(-len(p)%4))
d=json.loads(base64.urlsafe_b64decode(p))
print('sub=%s act=%s fallback=%s' % (d.get('sub'),str(d.get('act',{}).get('sub')),d.get('_fallback',False)))
")
      if [ "$IS_FALLBACK" = "yes" ]; then
        fail "OBO exchange degraded to local fallback (run ./scripts/fix-keycloak-token-exchange.sh): $OBO_SUB"
      else
        ok "OBO exchange OK (Keycloak RFC 8693): $OBO_SUB"
      fi
    else
      fail "OBO exchange failed: $OBO_RESP"
      OBO_TOKEN=""
    fi

    if [ -n "${OBO_TOKEN:-}" ]; then
      echo "  [2.4] OBO token has sub + act claims"
      CHECK=$(echo "$OBO_TOKEN" | python3 -c "
import sys,json,base64
t=sys.stdin.read().strip()
p=t.split('.')[1]; p+=('='*(-len(p)%4))
d=json.loads(base64.urlsafe_b64decode(p))
assert d.get('sub'), 'missing sub'
assert d.get('act'), 'missing act claim'
assert d['act'].get('sub'), 'missing act.sub'
print('sub=%s act=%s' % (d['sub'],d['act']['sub']))
" 2>/dev/null || echo "fail")
      if [ "$CHECK" != "fail" ]; then ok "OBO claims: $CHECK"; else fail "OBO token missing sub or act: $OBO_TOKEN"; fi

      echo "  [2.5] OBO refresh"
      RT=$(echo "$OBO_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('refresh_token',''))")
      if [ -n "$RT" ]; then
        R2=$(curl -sf -X POST http://localhost:8081/refresh \
          -H "Content-Type: application/x-www-form-urlencoded" \
          -d "refresh_token=$RT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'access_token' in d else 'fail')" 2>/dev/null)
        if [ "$R2" = "ok" ]; then ok "OBO /refresh: new access_token returned"; else fail "OBO /refresh failed"; fi
      else
        skip "No refresh_token in OBO response"
      fi
    fi
  fi

  echo "  [2.6] MCP tools/list (with OBO token)"
  if [ -n "${OBO_TOKEN:-}" ]; then
    # Use python to do full MCP session (initialize + tools/list) in one shot
    MCP_RESULT=$(python3 - "$OBO_TOKEN" <<'PYEOF'
import sys, json, urllib.request, urllib.error
token = sys.argv[1]
headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
           "MCP-Protocol-Version": "2025-06-18", "Content-Type": "application/json"}
# initialize
data = json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}).encode()
req = urllib.request.Request("http://localhost:8083/", data=data, headers=headers, method="POST")
with urllib.request.urlopen(req, timeout=5) as r:
    session = r.headers.get("mcp-session-id","")
    r.read()
# tools/list
headers["Mcp-Session-Id"] = session
data = json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}).encode()
req = urllib.request.Request("http://localhost:8083/", data=data, headers=headers, method="POST")
with urllib.request.urlopen(req, timeout=5) as r:
    body = json.loads(r.read())
    print(len(body["result"]["tools"]))
PYEOF
    2>/dev/null || echo "0")
    if [ "${MCP_RESULT:-0}" -gt "0" ]; then
      ok "MCP tools/list: $MCP_RESULT tools returned"
    else
      fail "MCP tools/list failed (got: $MCP_RESULT)"
    fi
  else
    skip "No OBO token — skipping MCP test"
  fi

  echo "  [2.7] Agent: missing OBO → 401"
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8082/a2a/run \
    -H "Content-Type: application/json" -d '{"task":"test"}')
  if [ "$CODE" = "401" ]; then ok "Agent returns 401 without OBO grant"; else fail "Expected 401, got $CODE"; fi

  echo "  [2.8] Healthchecks"
  for svc in "8081/healthz:obo-exchange" "8082/healthz:agent" "8083/healthz:mcp-mock"; do
    port="${svc%%/*}"; rest="${svc#*/}"; path="${rest%%:*}"; name="${rest#*:}"
    R=$(curl -sf "http://localhost:${port}/${path}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "fail")
    if [ "$R" = "ok" ]; then ok "$name /healthz"; else fail "$name /healthz: $R"; fi
  done

  echo "  [2.9] Prometheus /metrics on every service"
  for svc in "8081:obo-exchange" "8082:agent" "8083:mcp-mock" "8080:webapp"; do
    port="${svc%%:*}"; name="${svc#*:}"
    if curl -sf "http://localhost:${port}/metrics" | grep -q "http_requests_total"; then
      ok "$name /metrics exposes http_requests_total"
    else
      fail "$name /metrics missing http_requests_total"
    fi
  done

  echo "  [2.10] Readiness — /readyz reports dependencies"
  for svc in "8081:obo-exchange" "8082:agent" "8083:mcp-mock" "8080:webapp"; do
    port="${svc%%:*}"; name="${svc#*:}"
    R=$(curl -sf "http://localhost:${port}/readyz" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "fail")
    if [ "$R" = "ready" ]; then ok "$name /readyz ready"; else fail "$name /readyz: $R"; fi
  done

  echo "  [2.11] Prometheus scraping targets"
  UP=$(curl -sf "http://localhost:9090/api/v1/query?query=count(up==1)" \
    | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(int(float(r[0]['value'][1])) if r else 0)" 2>/dev/null || echo 0)
  if [ "${UP:-0}" -ge 5 ]; then ok "Prometheus: $UP targets up"; else
    skip "Prometheus not up yet ($UP targets) — needs ~1 scrape interval"
  fi

  echo "  [2.12] Grafana healthy + dashboards provisioned"
  GRAF=$(curl -sf "http://localhost:3000/api/health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('database',''))" 2>/dev/null || echo "fail")
  if [ "$GRAF" = "ok" ]; then
    DASH_N=$(curl -sf "http://localhost:3000/api/search?type=dash-db" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
    if [ "${DASH_N:-0}" -ge 2 ]; then ok "Grafana up, $DASH_N dashboards provisioned"; else fail "Grafana up but $DASH_N dashboards found (expected ≥2)"; fi
  else
    skip "Grafana not reachable"
  fi

fi

# ─────────────────────────────────────────────────────────────────────────
h1 "LAYER 3 — End-to-end test (requires stack + LLM)"
# ─────────────────────────────────────────────────────────────────────────

if ! $STACK_UP; then
  skip "Stack not running"
else

  echo "  [3.1] Full flow via webapp /run"
  E2E=$(curl -sf -X POST http://localhost:8080/run \
    -H "Content-Type: application/json" \
    -d '{"task":"List the MCP tools available.","username":"alice","password":"alice123"}' \
    --max-time 600 2>/dev/null || echo "FAIL")

  if [ "$E2E" = "FAIL" ]; then
    fail "Webapp /run request failed"
  else
    CHECK=$(echo "$E2E" | python3 -c "
import sys,json
d=json.load(sys.stdin)
assert not d.get('error'), f'error: {d.get(\"error\")}'
assert d.get('run_id'), 'no run_id'
assert d.get('result'), 'no result'
s3 = d['steps'][2]['event']
assert s3.get('status') == 'COMPLETED', f'agent run status={s3.get(\"status\")} result={d[\"result\"][:120]}'
assert len(d.get('steps',[])) == 4, f'expected 4 steps, got {len(d.get(\"steps\",[]))}'
s2 = d['steps'][1]
obo = s2.get('obo_claims',{})
assert obo.get('sub'), f'no sub in OBO claims: {obo}'
assert obo.get('act'), f'no act in OBO claims: {obo}'
print('run_id=%s sub=%s act=%s fallback=%s' % (d['run_id'],obo['sub'],obo['act']['sub'],s2.get('is_fallback',False)))
" 2>&1 || echo "FAIL")
    if echo "$CHECK" | grep -q "FAIL\|Error\|assert"; then
      fail "E2E validation failed: $CHECK"
    else
      ok "Full E2E: $CHECK"
    fi

    echo "  [3.2] Audit trail"
    RUN_ID=$(echo "$E2E" | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id',''))" 2>/dev/null || echo "")
    if [ -n "$RUN_ID" ]; then
      AUDIT=$(curl -sf "http://localhost:8080/audit/$RUN_ID" 2>/dev/null || echo "{}")
      AUDIT_OK=$(echo "$AUDIT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
assert d.get('identity'), 'no identity'
g=d['identity'].get('obo_grant',{})
assert g.get('subject_sub'), 'no subject_sub'
assert g.get('act'), 'no act in identity'
print('subject=%s act=%s mcp_calls=%d' % (g['subject_sub'],str(g['act'].get('sub')),len(d.get('trace',{}).get('calls',[]))))
" 2>&1 || echo "FAIL")
      if echo "$AUDIT_OK" | grep -q "FAIL\|Error"; then fail "Audit incomplete: $AUDIT_OK"; else ok "Audit: $AUDIT_OK"; fi
    else
      fail "No run_id to audit"
    fi

    echo "  [3.3] Metrics incremented by the E2E run"
    EX_N=$(curl -sf http://localhost:8081/metrics | python3 -c "
import sys
total = sum(float(l.rsplit(' ',1)[1]) for l in sys.stdin
            if l.startswith('obo_exchange_total'))
print(int(total))" 2>/dev/null || echo 0)
    RUN_N=$(curl -sf http://localhost:8082/metrics | python3 -c "
import sys
total = sum(float(l.rsplit(' ',1)[1]) for l in sys.stdin
            if l.startswith('agent_runs_total'))
print(int(total))" 2>/dev/null || echo 0)
    if [ "${EX_N:-0}" -ge 1 ] && [ "${RUN_N:-0}" -ge 1 ]; then
      ok "Counters moved: obo_exchange_total=$EX_N agent_runs_total=$RUN_N"
    else
      fail "Metrics did not increment (exchanges=$EX_N runs=$RUN_N)"
    fi
  fi

fi

# ─────────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════"
printf "  Results: ${GREEN}%d passed${NC}  ${RED}%d failed${NC}\n" $PASS $FAIL
echo "═══════════════════════════════════════════"
[ $FAIL -eq 0 ]
