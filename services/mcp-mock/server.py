"""
MCP Mock Server — implementa MCP Streamable HTTP con tool realistici.
Simula tool DevOps che un agente userebbe in produzione.
"""
import json
import logging
import os
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter, Gauge

from obs import setup_observability

PORT = int(os.getenv("PORT", "8083"))
SESSION_TTL = float(os.getenv("MCP_SESSION_TTL_SECONDS", "3600"))
SESSION_MAX = int(os.getenv("MCP_SESSION_MAX", "1000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mcp-mock")

app = FastAPI(title="mcp-mock")
setup_observability(app, "mcp-mock")

TOOL_CALLS = Counter("mcp_tool_calls_total", "MCP tool invocations", ["tool"])
ACTIVE_SESSIONS = Gauge("mcp_sessions_active", "MCP sessions currently tracked")

TOOLS = [
    {
        "name": "echo",
        "description": "Ritorna il messaggio che gli passi. Utile per testare la connessione.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "Testo da echeggiare"}},
            "required": ["message"],
        },
    },
    {
        "name": "list_deployments",
        "description": "Lista i deployment attivi nel cluster (mock).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace K8s (default: default)"}
            },
        },
    },
    {
        "name": "get_service_health",
        "description": "Controlla lo stato di un servizio nel cluster.",
        "inputSchema": {
            "type": "object",
            "properties": {"service": {"type": "string", "description": "Nome del servizio"}},
            "required": ["service"],
        },
    },
    {
        "name": "list_pr_reviews",
        "description": "Lista le PR in attesa di review nel repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Nome repository"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
            },
        },
    },
]

SESSIONS: dict[str, dict] = {}


def _prune_sessions() -> None:
    """TTL-expire sessions and hard-cap the dict so memory stays bounded."""
    now = time.time()
    for sid in [s for s, v in SESSIONS.items() if now - v["created"] > SESSION_TTL]:
        SESSIONS.pop(sid, None)
    if len(SESSIONS) > SESSION_MAX:
        for sid in sorted(SESSIONS, key=lambda s: SESSIONS[s]["created"])[:len(SESSIONS) - SESSION_MAX]:
            SESSIONS.pop(sid, None)
    ACTIVE_SESSIONS.set(len(SESSIONS))


def _bearer_claims(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not token:
        return {}
    try:
        import base64
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


def _exec_tool(name: str, arguments: dict) -> str:
    if name == "echo":
        return f"Echo: {arguments.get('message', '(empty)')}"
    if name == "list_deployments":
        ns = arguments.get("namespace", "default")
        return json.dumps([
            {"name": "litellm", "namespace": ns, "replicas": 1, "status": "Running"},
            {"name": "agent-poc", "namespace": ns, "replicas": 1, "status": "Running"},
            {"name": "mcp-mock", "namespace": ns, "replicas": 1, "status": "Running"},
        ])
    if name == "get_service_health":
        svc = arguments.get("service", "unknown")
        return json.dumps({"service": svc, "status": "healthy", "latency_ms": 12, "uptime": "99.9%"})
    if name == "list_pr_reviews":
        repo = arguments.get("repo", "my-repo")
        return json.dumps([
            {"id": 42, "title": "feat: add OBO token refresh", "author": "lorenzo", "state": "open"},
            {"id": 43, "title": "fix: MCP session header", "author": "giovanni", "state": "open"},
        ])
    return f"Tool '{name}' not found"


@app.post("/")
async def mcp_endpoint(request: Request):
    claims = _bearer_claims(request)
    sub = claims.get("sub", "unknown")
    act = (claims.get("act") or {}).get("sub", "no-act")
    session_id = request.headers.get("mcp-session-id")

    body = await request.json()
    method = body.get("method", "")
    id_ = body.get("id")

    log.info(f"[MCP] {method} sub={sub} act={act} session={session_id}")

    if method == "initialize":
        _prune_sessions()
        new_session = f"mcp-session-{int(time.time() * 1000)}"
        SESSIONS[new_session] = {"sub": sub, "act": act, "created": time.time()}
        ACTIVE_SESSIONS.set(len(SESSIONS))
        return JSONResponse(
            {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mcp-mock-poc", "version": "1.0"},
            }},
            headers={"mcp-session-id": new_session},
        )

    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {"tools": TOOLS}})

    if method == "tools/call":
        params = body.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        result_text = _exec_tool(name, arguments)
        TOOL_CALLS.labels(name or "unknown").inc()
        log.info(f"[MCP] tools/call name={name} by sub={sub} act={act} → {result_text[:80]}")
        return JSONResponse({"jsonrpc": "2.0", "id": id_,
                             "result": {"content": [{"type": "text", "text": result_text}]}})

    return JSONResponse({"jsonrpc": "2.0", "id": id_,
                         "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
