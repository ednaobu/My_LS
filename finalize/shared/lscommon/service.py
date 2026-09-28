"""Service bootstrap, service discovery and synchronous service-to-service calls."""
import contextvars
import logging
import os
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request

# The correlation id travels with every HTTP call and every event so one
# business action can be traced across all services (poor man's tracing).
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

DEFAULT_PORTS = {
    "gateway": 8000,
    "template": 8001,
    "agreement": 8002,
    "approval": 8003,
    "review": 8004,
    "collaboration": 8005,
    "event_bus": 8010,
}


_client: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    """One pooled client per process. Building a client costs ~50ms (TLS context),
    so creating one per call silently adds that to every hop."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


def service_url(name: str) -> str:
    """Service discovery via environment variables (Docker/K8s DNS in production)."""
    return os.environ.get(f"{name.upper()}_SERVICE_URL", f"http://127.0.0.1:{DEFAULT_PORTS[name]}")


def get_logger(service: str) -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return logging.getLogger(service)


def create_service(name: str, **fastapi_kwargs) -> FastAPI:
    app = FastAPI(title=f"Finalize {name} service", **fastapi_kwargs)

    @app.middleware("http")
    async def propagate_correlation_id(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:16]
        token = correlation_id.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id.reset(token)
        response.headers["x-correlation-id"] = cid
        return response

    @app.get("/health")
    def health():
        return {"service": name, "status": "ok"}

    return app


async def call(service: str, method: str, path: str, **kwargs):
    """Request/response call to another service.

    Network failures are turned into 503s so the caller sees *which* dependency
    failed - the price of synchronous coupling between services.
    """
    headers = {"x-correlation-id": correlation_id.get()}
    try:
        response = await http_client().request(method, service_url(service) + path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"{service} service unavailable ({exc.__class__.__name__})") from exc
    if response.status_code >= 500:
        raise HTTPException(502, f"{service} service error: {response.text}")
    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"{service} service: {response.json().get('detail')}")
    return response.json()
