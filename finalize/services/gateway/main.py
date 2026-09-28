"""API gateway - the single public entry point to Finalize.

Clients never talk to individual services. The gateway handles the
cross-cutting concerns once: authentication, correlation ids, routing, and an
aggregated health check. Everything under /api/... is proxied to the owning
service with the /api prefix stripped.
"""
import asyncio
import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import HTTPException, Request, Response

from lscommon.service import correlation_id, create_service, service_url

API_KEYS = set(os.environ.get("API_KEYS", "dev-key").split(","))

# First match wins, so more specific routes come first.
ROUTES = [
    (r"^agreements/[^/]+/comments", "collaboration"),
    (r"^(templates|intake)(/|$)", "template"),
    (r"^(agreements|hub)(/|$)", "agreement"),
    (r"^(matrices|workflows|tasks)(/|$)", "approval"),
    (r"^(playbooks|review|reviews|summarize|ask)(/|$)", "review"),
    (r"^(notifications|comments)(/|$)", "collaboration"),
    (r"^(events|subscriptions|deliveries)(/|$)", "event_bus"),
]
SERVICES = sorted({s for _, s in ROUTES})
client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app):
    global client
    client = httpx.AsyncClient(timeout=10.0)
    yield
    await client.aclose()


app = create_service("gateway", lifespan=lifespan)


@app.get("/health/all")
async def health_all():
    async def probe(name):
        try:
            r = await client.get(f"{service_url(name)}/health", timeout=2.0)
            return name, "ok" if r.status_code == 200 else f"http {r.status_code}"
        except httpx.HTTPError as exc:
            return name, f"down ({exc.__class__.__name__})"

    results = dict(await asyncio.gather(*(probe(s) for s in SERVICES)))
    return {"status": "ok" if all(v == "ok" for v in results.values()) else "degraded", "services": results}


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    if request.headers.get("x-api-key") not in API_KEYS:
        raise HTTPException(401, "missing or invalid X-API-Key")
    service = next((s for pattern, s in ROUTES if re.match(pattern, path)), None)
    if service is None:
        raise HTTPException(404, f"no service handles /{path}")
    try:
        upstream = await client.request(
            request.method,
            f"{service_url(service)}/{path}",
            params=request.query_params,
            content=await request.body(),
            headers={"content-type": request.headers.get("content-type", "application/json"),
                     "x-correlation-id": correlation_id.get()},
        )
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"{service} service unavailable ({exc.__class__.__name__})") from exc
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={"x-served-by": service},
    )
