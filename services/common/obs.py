"""
Shared observability for all POC services.

Adds to a FastAPI app:
  GET /metrics  — Prometheus exposition (RED metrics for every route + whatever
                  domain metrics the service registers itself)
  GET /healthz  — liveness: process is up
  GET /readyz   — readiness: critical dependencies reachable (503 otherwise)

HTTP metrics are labeled with the *route template* (e.g. /admin/instances/{run_id}/trace),
never the raw path, so label cardinality stays bounded.
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

_EXCLUDED_PATHS = {"/metrics", "/healthz", "/readyz"}

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests processed",
    ["service", "method", "path", "status"],
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "method", "path"],
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

ReadinessCheck = Callable[[], Awaitable[dict[str, bool]]]


def setup_observability(
    app: FastAPI,
    service_name: str,
    readiness_check: ReadinessCheck | None = None,
) -> None:
    """Install /metrics, /healthz, /readyz and the RED middleware."""

    @app.middleware("http")
    async def _red_metrics(request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            if path not in _EXCLUDED_PATHS:
                HTTP_REQUESTS.labels(service_name, request.method, path, str(status)).inc()
                HTTP_DURATION.labels(service_name, request.method, path).observe(
                    time.perf_counter() - start
                )

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "service": service_name}

    @app.get("/readyz")
    async def readyz():
        if readiness_check is None:
            return {"status": "ready", "service": service_name}
        deps = await readiness_check()
        ready = all(deps.values())
        body = {"status": "ready" if ready else "not_ready",
                "service": service_name, "dependencies": deps}
        return JSONResponse(body, status_code=200 if ready else 503)
