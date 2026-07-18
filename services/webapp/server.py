"""
Webapp — identity flow visualizer.
User login via Keycloak ROPC (demo) or PKCE (button in UI).
Exposes:
  GET  /            → UI
  POST /login       → username+password → Keycloak → user JWT
  POST /run         → full flow: login → OBO exchange → agent → audit
  GET  /audit/{id}  → identity + trace for a run
  GET  /healthz
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

PORT           = int(os.getenv("PORT", "8080"))
AGENT_URL      = os.getenv("AGENT_URL", "http://agent:8082")
OBO_URL        = os.getenv("OBO_URL", "http://obo-exchange:8081")
KC_ISSUER      = os.getenv("KC_ISSUER", "http://keycloak:8080/realms/poc")
KC_TOKEN_URL   = KC_ISSUER.rstrip("/") + "/protocol/openid-connect/token"
KC_CLIENT_ID   = "poc-webapp"     # public client, no secret

app = FastAPI(title="agent-identity-visualizer")
_events: list[dict] = []


def _decode_jwt(token: str) -> dict:
    try:
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


def _push(stage: str, **kw) -> dict:
    ev = {"id": uuid.uuid4().hex[:8], "ts": int(time.time()), "stage": stage, **kw}
    _events.append(ev)
    if len(_events) > 200:
        _events.pop(0)
    return ev


def _token_preview(token: str) -> dict:
    c = _decode_jwt(token)
    return {"sub": c.get("sub"), "act": c.get("act"), "iss": c.get("iss"),
            "exp": c.get("exp"), "scope": c.get("scope"),
            "raw_truncated": token[:48] + "..."}


@app.post("/login")
async def login(request: Request):
    """Keycloak ROPC (Resource Owner Password Credentials) — demo only."""
    body = await request.json()
    username = body.get("username", "alice")
    password = body.get("password", "alice123")

    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.post(KC_TOKEN_URL,
                            data={"grant_type": "password",
                                  "client_id": KC_CLIENT_ID,
                                  "username": username,
                                  "password": password,
                                  "scope": "openid profile email"},
                            headers={"Accept": "application/json"})
    if resp.status_code != 200:
        return JSONResponse({"error": "login_failed",
                             "detail": resp.json().get("error_description", resp.text[:200])},
                            status_code=401)

    data = resp.json()
    token = data["access_token"]
    claims = _decode_jwt(token)
    _push("user.login", sub=claims.get("sub"), email=claims.get("email"),
          username=username, note=f"Keycloak login: {username}")
    return JSONResponse({"access_token": token,
                         "claims": claims,
                         "preview": _token_preview(token)})


@app.post("/run")
async def run_task(request: Request):
    body = await request.json()
    task     = body.get("task", "List the MCP tools you have.")
    username = body.get("username", "alice")
    password = body.get("password", "alice123")

    steps = []

    # ── STEP 1: Keycloak login ───────────────────────────────────────────
    async with httpx.AsyncClient(timeout=10) as c:
        login_resp = await c.post(KC_TOKEN_URL,
                                  data={"grant_type": "password",
                                        "client_id": KC_CLIENT_ID,
                                        "username": username,
                                        "password": password,
                                        "scope": "openid profile email"},
                                  headers={"Accept": "application/json"})
    if login_resp.status_code != 200:
        return JSONResponse({"error": "login_failed",
                             "detail": login_resp.json().get("error_description", "")},
                            status_code=401)

    user_token  = login_resp.json()["access_token"]
    user_claims = _decode_jwt(user_token)
    ev1 = _push("user.login", sub=user_claims.get("sub"), email=user_claims.get("email"),
                note=f"Keycloak ROPC login: {username} → user JWT issued")
    steps.append({"step": 1, "name": "User Login (Keycloak)",
                  "event": ev1, "token_preview": _token_preview(user_token),
                  "claims": user_claims,
                  "note": f"Keycloak issued a real signed JWT for user '{username}'. "
                          f"This token identifies the human — sub={user_claims.get('sub')}"})

    # ── STEP 2: OBO Exchange ─────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=10) as c:
        obo_resp = await c.post(f"{OBO_URL}/exchange",
                                data={"subject_token": user_token,
                                      "scope": "openid profile email offline_access"},
                                headers={"Content-Type": "application/x-www-form-urlencoded",
                                         "Accept": "application/json"})
    if obo_resp.status_code != 200:
        return JSONResponse({"error": "obo_failed", "detail": obo_resp.text[:300]}, status_code=500)

    obo_data   = obo_resp.json()
    obo_token  = obo_data["access_token"]
    obo_claims = _decode_jwt(obo_token)
    is_fallback = obo_data.get("_note", "").startswith("local-fallback")
    ev2 = _push("gateway.exchange",
                sub=obo_claims.get("sub"),
                act=(obo_claims.get("act") or {}).get("sub"),
                fallback=is_fallback,
                note="obo-exchange: user JWT → OBO token {sub=user, act=agent}")
    steps.append({"step": 2, "name": "OBO Token Exchange",
                  "event": ev2, "token_preview": _token_preview(obo_token),
                  "obo_claims": obo_claims, "is_fallback": is_fallback,
                  "note": ("obo-exchange called Keycloak RFC 8693 token exchange. "
                           if not is_fallback else
                           "⚠ Keycloak token-exchange not configured — local fallback used. "
                           "Structure is identical; signature is local HMAC instead of Keycloak RS256. ") +
                          f"Result: sub={obo_claims.get('sub')}, "
                          f"act={(obo_claims.get('act') or {}).get('sub')}"})

    # ── STEP 3: Agent execution ──────────────────────────────────────────
    async with httpx.AsyncClient(timeout=120) as c:
        agent_resp = await c.post(f"{AGENT_URL}/a2a/run",
                                  json={"task": task},
                                  headers={"Authorization": f"Bearer {obo_token}",
                                           "X-OBO-Refresh-Token": obo_data.get("refresh_token", ""),
                                           "X-OBO-Expires-In": str(obo_data.get("expires_in", 3600))})
    if agent_resp.status_code != 200:
        return JSONResponse({"error": "agent_failed", "detail": agent_resp.text[:300]}, status_code=500)

    agent_data = agent_resp.json()
    run_id = agent_data.get("instance_id")
    ev3 = _push("agent.completed", run_id=run_id, status=agent_data.get("runtime_status"),
                note="Agent ran the task — OBO token presented on every LLM + MCP hop")
    steps.append({"step": 3, "name": "Agent Execution",
                  "event": ev3, "run_id": run_id,
                  "note": "Every call the agent made (LLM /v1, MCP tools) carried the OBO token. "
                          "The agent never saw the user's original Keycloak token."})

    # ── STEP 4: Audit ────────────────────────────────────────────────────
    identity, trace = {}, []
    async with httpx.AsyncClient(timeout=10) as c:
        id_r = await c.get(f"{AGENT_URL}/admin/instances/{run_id}/identity")
        tr_r = await c.get(f"{AGENT_URL}/admin/instances/{run_id}/trace")
    if id_r.status_code == 200:
        identity = id_r.json()
    if tr_r.status_code == 200:
        trace = tr_r.json().get("calls", [])
    ev4 = _push("audit.fetched", run_id=run_id, mcp_calls=len(trace),
                note=f"Operator audit: {len(trace)} MCP hop(s) traced with delegated identity")
    steps.append({"step": 4, "name": "Audit Trail",
                  "event": ev4, "identity": identity, "trace": trace,
                  "note": "These endpoints are NOT exposed through the gateway. "
                          "Only operators with in-cluster access can read this."})

    return JSONResponse({
        "run_id": run_id,
        "result": agent_data.get("result"),
        "steps": steps,
        "recent_events": _events[-20:],
    })


@app.get("/audit/{run_id}")
async def audit(run_id: str):
    async with httpx.AsyncClient(timeout=10) as c:
        id_r = await c.get(f"{AGENT_URL}/admin/instances/{run_id}/identity")
        tr_r = await c.get(f"{AGENT_URL}/admin/instances/{run_id}/trace")
    return JSONResponse({
        "identity": id_r.json() if id_r.status_code == 200 else {},
        "trace": tr_r.json() if tr_r.status_code == 200 else {},
    })


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("/app/static/index.html") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
