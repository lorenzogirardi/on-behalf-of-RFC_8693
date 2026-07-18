"""
OBO Exchange — RFC 8693 Token Exchange against a real Keycloak instance.

Endpoints:
  POST /exchange  — mint OBO token: subject_token (user JWT) + actor (agent creds) → OBO {sub=user, act=agent}
  POST /refresh   — renew via refresh_token (act preserved, token rotates)
  POST /authz     — ext_authz entrypoint: receive user bearer, return OBO headers
  GET  /healthz   — liveness
  GET  /readyz    — readiness (Keycloak discovery reachable)
  GET  /metrics   — Prometheus

This service is the SOLE holder of the exchange-app client secret.
The agent only holds its own client credentials (agent-service).

Local HMAC fallback (used when Keycloak rejects the exchange) is gated behind
ALLOW_LOCAL_FALLBACK. Keep it "true" only for local demos; in any shared
environment set "false" so a Keycloak misconfiguration fails closed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from urllib.parse import parse_qs

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge, Histogram

from obs import setup_observability

PORT = int(os.getenv("PORT", "8081"))

# Keycloak config (all from env, no hardcoded references)
KC_ISSUER       = os.getenv("KC_ISSUER", "http://keycloak:8080/realms/poc")
KC_TOKEN_URL    = os.getenv("KC_TOKEN_URL", f"{KC_ISSUER}/protocol/openid-connect/token")
EXCHANGE_CLIENT_ID     = os.getenv("EXCHANGE_APP_CLIENT_ID", "exchange-app")
EXCHANGE_CLIENT_SECRET = os.getenv("EXCHANGE_APP_CLIENT_SECRET", "exchange-app-secret")
AGENT_CLIENT_ID        = os.getenv("AGENT_CLIENT_ID", "agent-service")
AGENT_CLIENT_SECRET    = os.getenv("AGENT_CLIENT_SECRET", "agent-service-secret")
DEFAULT_SCOPE          = os.getenv("DEFAULT_SCOPE", "openid profile email offline_access")
EMIT_EVENTS            = os.getenv("IDENTITY_EVENTS", "true").lower() == "true"
ALLOW_LOCAL_FALLBACK   = os.getenv("ALLOW_LOCAL_FALLBACK", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("obo-exchange")

app = FastAPI(title="obo-exchange")
_http = httpx.AsyncClient(timeout=15.0)

EXCHANGES = Counter("obo_exchange_total", "OBO token exchanges", ["result"])
REFRESHES = Counter("obo_refresh_total", "OBO token refreshes", ["result"])
EXCHANGE_DURATION = Histogram(
    "obo_exchange_duration_seconds", "Keycloak token-exchange round-trip",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
KC_REACHABLE = Gauge("obo_keycloak_reachable", "1 if Keycloak discovery endpoint answers")


async def _check_ready() -> dict[str, bool]:
    ok = False
    try:
        r = await _http.get(f"{KC_ISSUER}/.well-known/openid-configuration", timeout=3)
        ok = r.status_code == 200
    except Exception:
        ok = False
    KC_REACHABLE.set(1 if ok else 0)
    return {"keycloak": ok}


setup_observability(app, "obo-exchange", readiness_check=_check_ready)

# ── Actor token cache (agent-service client_credentials) ─────────────────

_actor_lock = asyncio.Lock()
_actor_token: str | None = None
_actor_exp: float = 0.0


async def _get_actor_token() -> str:
    global _actor_token, _actor_exp
    async with _actor_lock:
        if _actor_token and time.time() < _actor_exp - 60:
            return _actor_token
        resp = await _http.post(KC_TOKEN_URL,
                                data={"grant_type": "client_credentials",
                                      "client_id": AGENT_CLIENT_ID,
                                      "client_secret": AGENT_CLIENT_SECRET,
                                      "scope": "openid"},
                                headers={"Accept": "application/json"})
        resp.raise_for_status()
        body = resp.json()
        _actor_token = body["access_token"]
        _actor_exp = time.time() + float(body.get("expires_in", 3600))
        return _actor_token


# ── JWT helpers (decode only — signature verified by Keycloak) ───────────

def _decode_jwt(token: str) -> dict:
    try:
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


def _emit_event(stage: str, sub: str, act: str, note: str = ""):
    if not EMIT_EVENTS:
        return
    ev = json.dumps({
        "ts": int(time.time()), "stage": stage,
        "sub": sub, "act": act, "note": note,
    })
    print(f"[IDENTITY_EVENT] {ev}", flush=True)


async def _read_form(request: Request) -> dict:
    body = await request.body()
    return {k: v[0] for k, v in parse_qs(body.decode()).items()}


# ── Local fallback minting (HMAC, demo only) ──────────────────────────────

def _mint_local_obo(user_sub: str, agent_sub: str, scope: str) -> dict:
    """
    Mint a locally-signed OBO JWT pair (access + refresh) when Keycloak
    token-exchange is not configured. Structure is identical to what Keycloak
    would return; signature is HS256 with a demo secret. Never for production.
    """
    import hashlib, hmac as _hmac, uuid
    SECRET = b"local-fallback-secret-not-for-production"
    now = int(time.time())

    def b64url(d): return base64.urlsafe_b64encode(d).decode().rstrip("=")
    h = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64url(json.dumps({
        "iss": KC_ISSUER, "sub": user_sub, "act": {"sub": agent_sub},
        "scope": scope, "jti": uuid.uuid4().hex,
        "iat": now, "exp": now + 3600,
        "token_type": "Bearer", "_fallback": True,
    }).encode())
    sig = b64url(_hmac.new(SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest())

    rp = b64url(json.dumps({
        "iss": KC_ISSUER, "sub": user_sub, "act": {"sub": agent_sub},
        "jti": uuid.uuid4().hex, "iat": now, "exp": now + 86400,
        "token_type": "refresh", "_fallback": True,
    }).encode())
    rsig = b64url(_hmac.new(SECRET, f"{h}.{rp}".encode(), hashlib.sha256).digest())

    return {
        "access_token": f"{h}.{p}.{sig}",
        "refresh_token": f"{h}.{rp}.{rsig}",
        "token_type": "Bearer", "expires_in": 3600, "scope": scope,
        "_note": "local-fallback: Keycloak token-exchange not enabled on exchange-app",
    }


def _local_obo_fallback(subject_token: str, actor_token: str, scope: str) -> dict:
    user_claims  = _decode_jwt(subject_token)
    actor_claims = _decode_jwt(actor_token)
    user_sub  = user_claims.get("sub", "unknown-user")
    # Use azp (authorized party = client_id name) for readability; fall back to AGENT_CLIENT_ID.
    # actor_claims.get("sub") is a Keycloak UUID, not the client name.
    agent_sub = actor_claims.get("azp") or actor_claims.get("clientId") or AGENT_CLIENT_ID

    log.warning(f"[FALLBACK OBO] sub={user_sub} act={agent_sub} (local signature, not Keycloak-issued)")
    _emit_event("gateway.exchange.fallback", sub=user_sub, act=agent_sub,
                note="Local fallback OBO — Keycloak token-exchange not configured")
    return _mint_local_obo(user_sub, agent_sub, scope)


# ── Routes ────────────────────────────────────────────────────────────────

@app.post("/exchange")
async def exchange(request: Request):
    """
    RFC 8693 Token Exchange (Keycloak native).

    Flow:
      1. Extract subject_token (user JWT) from body
      2. Get agent actor_token via client_credentials (cached)
      3. POST to Keycloak /token with grant_type=token-exchange
         subject_token + actor_token + exchange-app credentials
      4. Keycloak returns OBO token {sub=user, act=agent}
      5. Relay response (including refresh_token if offline_access granted)
    """
    form = await _read_form(request)

    subject_token = form.get("subject_token", "")
    if not subject_token:
        return JSONResponse({"error": "invalid_request", "error_description": "subject_token required"},
                            status_code=400)

    # Actor token from Authorization header or cached client_credentials
    auth = request.headers.get("authorization", "")
    actor_token = auth.removeprefix("Bearer ").strip() or form.get("actor_token", "")
    if not actor_token:
        try:
            actor_token = await _get_actor_token()
        except Exception as e:
            log.error(f"failed to get actor token: {e}")
            EXCHANGES.labels("error").inc()
            return JSONResponse({"error": "server_error", "error_description": "cannot mint actor token"},
                                status_code=500)

    scope = form.get("scope", DEFAULT_SCOPE)

    # Keycloak RFC 8693 token exchange
    kc_form = {
        "grant_type":           "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id":            EXCHANGE_CLIENT_ID,
        "client_secret":        EXCHANGE_CLIENT_SECRET,
        "subject_token":        subject_token,
        "subject_token_type":   "urn:ietf:params:oauth:token-type:access_token",
        "actor_token":          actor_token,
        "actor_token_type":     "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "scope":                scope,
    }
    if form.get("audience"):
        kc_form["audience"] = form["audience"]

    t0 = time.perf_counter()
    resp = await _http.post(KC_TOKEN_URL,
                            data=kc_form,
                            headers={"Accept": "application/json",
                                     "Content-Type": "application/x-www-form-urlencoded"})
    EXCHANGE_DURATION.observe(time.perf_counter() - t0)

    if resp.status_code != 200:
        log.warning(f"Keycloak exchange rejected: {resp.status_code} {resp.text[:200]}")
        if not ALLOW_LOCAL_FALLBACK:
            EXCHANGES.labels("error").inc()
            return JSONResponse(
                {"error": "exchange_failed",
                 "error_description": "Keycloak rejected the token exchange and local fallback is disabled",
                 "keycloak_status": resp.status_code},
                status_code=502)
        log.warning("Falling back to local OBO token (Keycloak token-exchange not configured)")
        EXCHANGES.labels("fallback").inc()
        return JSONResponse(_local_obo_fallback(subject_token, actor_token, scope))

    body_json = resp.json()
    user_claims = _decode_jwt(subject_token)
    obo_claims  = _decode_jwt(body_json.get("access_token", ""))
    user_sub    = user_claims.get("sub", "unknown")
    act_sub     = (obo_claims.get("act") or {}).get("sub") or AGENT_CLIENT_ID

    log.info(f"[EXCHANGE] sub={user_sub} act={act_sub}")
    EXCHANGES.labels("keycloak").inc()
    _emit_event("gateway.exchange", sub=user_sub, act=act_sub,
                note=f"Keycloak token exchange: {user_sub} → OBO(sub={user_sub}, act={act_sub})")

    return JSONResponse(body_json)


@app.post("/refresh")
async def refresh_token(request: Request):
    """
    Renew via refresh_token grant against Keycloak (exchange-app credentials).
    If token is a local fallback (no Keycloak RT), mint a new local OBO.
    """
    form = await _read_form(request)
    rt = form.get("refresh_token", "")
    if not rt:
        return JSONResponse({"error": "invalid_request", "error_description": "refresh_token required"},
                            status_code=400)

    rt_claims = _decode_jwt(rt)

    # Local fallback refresh token
    if rt_claims.get("_fallback") or rt_claims.get("token_type") == "refresh":
        if not ALLOW_LOCAL_FALLBACK:
            REFRESHES.labels("error").inc()
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "local fallback tokens are disabled"},
                                status_code=400)
        user_sub  = rt_claims.get("sub", "unknown")
        agent_sub = (rt_claims.get("act") or {}).get("sub", AGENT_CLIENT_ID)
        scope = form.get("scope", DEFAULT_SCOPE)
        log.info(f"[REFRESH/FALLBACK] sub={user_sub} act={agent_sub}")
        REFRESHES.labels("fallback").inc()
        return JSONResponse(_mint_local_obo(user_sub, agent_sub, scope))

    # Real Keycloak refresh
    kc_form = {
        "grant_type":    "refresh_token",
        "refresh_token": rt,
        "client_id":     EXCHANGE_CLIENT_ID,
        "client_secret": EXCHANGE_CLIENT_SECRET,
    }
    if scope := form.get("scope", ""):
        kc_form["scope"] = scope

    resp = await _http.post(KC_TOKEN_URL, data=kc_form,
                            headers={"Accept": "application/json",
                                     "Content-Type": "application/x-www-form-urlencoded"})
    if resp.status_code != 200:
        log.warning(f"Keycloak refresh rejected: {resp.status_code}")
        REFRESHES.labels("error").inc()
        return JSONResponse(resp.json(), status_code=resp.status_code)

    body_json = resp.json()
    obo = _decode_jwt(body_json.get("access_token", ""))
    log.info(f"[REFRESH] sub={obo.get('sub')} act={(obo.get('act') or {}).get('sub')}")
    REFRESHES.labels("keycloak").inc()
    return JSONResponse(body_json)


@app.post("/authz")
async def authz(request: Request):
    """
    ext_authz entrypoint for a gateway sidecar.
    Receives user bearer → exchanges → returns OBO headers to inject.
    """
    auth = request.headers.get("authorization", "")
    user_token = auth.removeprefix("Bearer ").strip()
    if not user_token:
        return JSONResponse({"error": "missing bearer"}, status_code=401)

    # Inline exchange
    actor_token = await _get_actor_token()
    kc_form = {
        "grant_type":           "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id":            EXCHANGE_CLIENT_ID,
        "client_secret":        EXCHANGE_CLIENT_SECRET,
        "subject_token":        user_token,
        "subject_token_type":   "urn:ietf:params:oauth:token-type:access_token",
        "actor_token":          actor_token,
        "actor_token_type":     "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "scope":                DEFAULT_SCOPE,
    }
    t0 = time.perf_counter()
    resp = await _http.post(KC_TOKEN_URL, data=kc_form,
                            headers={"Accept": "application/json",
                                     "Content-Type": "application/x-www-form-urlencoded"})
    EXCHANGE_DURATION.observe(time.perf_counter() - t0)
    if resp.status_code != 200:
        if not ALLOW_LOCAL_FALLBACK:
            EXCHANGES.labels("error").inc()
            return JSONResponse({"allow": False, "reason": "exchange_failed"}, status_code=502)
        EXCHANGES.labels("fallback").inc()
        obo_data = _local_obo_fallback(user_token, actor_token, DEFAULT_SCOPE)
    else:
        EXCHANGES.labels("keycloak").inc()
        obo_data = resp.json()

    return JSONResponse({
        "allow": True,
        "headers": {
            "Authorization": f"Bearer {obo_data['access_token']}",
            "X-OBO-Refresh-Token": obo_data.get("refresh_token", ""),
            "X-OBO-Expires-In": str(obo_data.get("expires_in", 3600)),
        },
    })


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
