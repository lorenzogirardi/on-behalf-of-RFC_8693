"""
Agent POC — sincronous tool-calling loop con OBO identity.
Identico alla versione di produzione ma senza Dapr (Redis diretto).
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from grant_store import GrantStore
from obo import Grant, OBOClient

GATEWAY_BASE = os.getenv("GATEWAY_BASE", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://litellm:4000/v1")
MCP_BASE_URL = os.getenv("MCP_BASE_URL", "http://mcp-mock:8083")
DEFAULT_MODEL = os.getenv("AGENT_MODEL", "gpt-4o-mini")
MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "6"))
MCP_PROTOCOL_VERSION = "2025-06-18"

_obo = OBOClient.from_env()
_store = GrantStore.from_env(_obo)
_http = httpx.Client(timeout=60.0)


def _decode_jwt(token: str) -> dict:
    try:
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {"_note": "opaque token"}


def bearer(h: str) -> str:
    p = "Bearer "
    return h[len(p):].strip() if h and h[:len(p)].lower() == p.lower() else ""


# ── MCP ───────────────────────────────────────────────────────────────────

def _mcp_post(token: str, payload: dict, session: str | None = None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    resp = _http.post(MCP_BASE_URL, json=payload, headers=headers)
    resp.raise_for_status()
    return resp


def _mcp_parse(resp) -> dict:
    if "text/event-stream" in resp.headers.get("content-type", ""):
        data = [l[5:].strip() for l in resp.text.splitlines() if l.startswith("data:")]
        return json.loads(data[-1]) if data else {}
    return resp.json()


def _mcp_call(token: str, method: str, params: dict) -> dict:
    init = _mcp_post(token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": MCP_PROTOCOL_VERSION,
                   "capabilities": {}, "clientInfo": {"name": "agent-poc", "version": "1"}},
    })
    session = init.headers.get("mcp-session-id")
    _mcp_post(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session=session)
    resp = _mcp_post(token, {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
                     session=session)
    return _mcp_parse(resp)


def _mcp_with_trace(run_id: str, token: str, method: str, params: dict) -> dict:
    claims = _decode_jwt(token)
    act = claims.get("act") or {}
    entry = {
        "hop": "mcp", "method": method, "tool": params.get("name"),
        "ts": int(time.time()),
        "presented_identity": {"sub": claims.get("sub"), "act_sub": act.get("sub")},
        "token_exp": claims.get("exp"),
    }
    try:
        result = _mcp_call(token, method, params)
        entry["ok"] = True
        print(f"[MCP] run={run_id} {method} tool={params.get('name')} "
              f"sub={claims.get('sub')} act={act.get('sub')} ok=True", flush=True)
        return result
    except Exception as e:
        entry["ok"] = False
        entry["error"] = str(e)
        print(f"[MCP] run={run_id} {method} FAILED: {e}", flush=True)
        raise
    finally:
        _store.append_trace(run_id, entry)


# ── Tool loop ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are the Valerie platform assistant. Use ListMcpTools to discover "
    "available in-cluster MCP tools, then CallMcpTool to invoke one. "
    "Keep answers concise and state which tools you used."
)

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "ListMcpTools",
        "description": "List the tools available from the in-cluster MCP servers.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "CallMcpTool",
        "description": "Call an in-cluster MCP tool by name with JSON arguments.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "arguments_json": {"type": "string"},
            },
            "required": ["name"],
        },
    }},
]


def _live_token(state: dict) -> str:
    grant = state["grant"]
    if grant.near_expiry() and grant.refresh_token:
        grant = _obo.refresh(grant)
        state["grant"] = grant
        _store.save(state["run_id"], grant)
        print(f"[OBO] run={state['run_id']} refreshed grant", flush=True)
    return state["grant"].access_token


def _exec_tool(state: dict, name: str, arguments: str) -> str:
    run_id = state["run_id"]
    token = _live_token(state)
    args = json.loads(arguments) if arguments else {}
    if name == "ListMcpTools":
        result = _mcp_with_trace(run_id, token, "tools/list", {})
        names = [t.get("name", "?") for t in result.get("result", {}).get("tools", [])]
        return "Available MCP tools: " + (", ".join(names) if names else "(none)")
    if name == "CallMcpTool":
        result = _mcp_with_trace(run_id, token, "tools/call",
                                 {"name": args.get("name"),
                                  "arguments": json.loads(args.get("arguments_json", "{}") or "{}")})
        return json.dumps(result.get("result", result))
    return f"unknown tool: {name}"


def run_task(run_id: str, grant: Grant, task: str) -> str:
    from openai import OpenAI
    state = {"grant": grant, "run_id": run_id}
    llm_http = httpx.Client(timeout=60.0)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    for _ in range(MAX_TURNS):
        token = _live_token(state)
        client = OpenAI(api_key=token, base_url=LLM_BASE_URL, http_client=llm_http)
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL, messages=messages, tools=TOOL_SCHEMAS, tool_choice="auto",
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append({
            "role": "assistant", "content": msg.content or "",
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            out = _exec_tool(state, tc.function.name, tc.function.arguments)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    return "(stopped: max turns)"


# ── FastAPI ───────────────────────────────────────────────────────────────

def _grant_from_headers(headers) -> Grant | None:
    obo_access = bearer(headers.get("authorization", ""))
    if not obo_access:
        return None
    claims = _decode_jwt(obo_access)
    expires_at = float(claims.get("exp") or 0) or (time.time() + 3600)
    return Grant(access_token=obo_access,
                 refresh_token=headers.get("x-obo-refresh-token", ""),
                 expires_at=expires_at)


app = FastAPI(title="agent-poc")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "agent-poc"}


@app.post("/a2a/run")
async def a2a_run(request: Request):
    grant = _grant_from_headers(request.headers)
    if grant is None:
        return JSONResponse({"error": "missing OBO grant"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    task = body.get("task") or "List the MCP tools you have."
    run_id = uuid.uuid4().hex
    _store.save(run_id, grant)
    claims = _decode_jwt(grant.access_token)
    print(f"[OBO] run={run_id} sub={claims.get('sub')} act={(claims.get('act') or {}).get('sub')} "
          f"has_refresh={bool(grant.refresh_token)}", flush=True)
    # Emit identity event for webapp
    ev = json.dumps({
        "ts": int(time.time()), "stage": "agent.run_start",
        "run_id": run_id,
        "sub": claims.get("sub"), "act": (claims.get("act") or {}).get("sub"),
        "task": task[:100],
    })
    print(f"[IDENTITY_EVENT] {ev}", flush=True)
    try:
        result = await run_in_threadpool(run_task, run_id, grant, task)
        status = "COMPLETED"
    except Exception as e:
        result, status = str(e), "FAILED"
        print(f"[RUN] run={run_id} FAILED: {e}", flush=True)
    return {"instance_id": run_id, "runtime_status": status, "result": result}


@app.get("/admin/instances/{run_id}/identity")
async def admin_identity(run_id: str):
    grant = _store.load(run_id)
    if grant is None:
        return JSONResponse({"error": f"no grant for run {run_id}"}, status_code=404)
    claims = _decode_jwt(grant.access_token)
    act = claims.get("act") or {}
    return {
        "instance_id": run_id,
        "obo_grant": {
            "subject_sub": claims.get("sub"),
            "act": act,
            "scope": claims.get("scope"),
            "expires_at": grant.expires_at,
            "near_expiry": grant.near_expiry(),
            "has_refresh_token": bool(grant.refresh_token),
            "means": f"agent {act.get('sub')} acting on behalf of user {claims.get('sub')}",
        },
    }


@app.get("/admin/instances/{run_id}/trace")
async def admin_trace(run_id: str):
    return {"instance_id": run_id, "calls": _store.get_trace(run_id)}


if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", "8082"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
