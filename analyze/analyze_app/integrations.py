"""Integration with Finalize: an anti-corruption layer at the monolith's edge.

When Finalize publishes `agreement.executed`, the signed contract flows into
Analyze automatically. The monolith subscribes to the microservices' event bus
- one system can be a monolith while its neighbours are microservices.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

from . import database, ingestion

log = logging.getLogger("analyze.integrations")
BUS_URL = os.environ.get("FINALIZE_EVENT_BUS_URL")
PUBLIC_URL = os.environ.get("ANALYZE_PUBLIC_URL", "http://127.0.0.1:9000")


def handle_finalize_event(event: dict) -> dict:
    with database.transaction() as conn:
        if conn.execute("SELECT 1 FROM processed_events WHERE id = ?", (event["id"],)).fetchone():
            return {"status": "duplicate-ignored"}
        conn.execute("INSERT INTO processed_events (id, received_at) VALUES (?, ?)",
                     (event["id"], datetime.now(timezone.utc).isoformat()))
        if event["topic"] != "agreement.executed":
            return {"status": "ignored"}
        p = event["payload"]
        # Translate Finalize's model into ours; only what Analyze needs crosses the boundary.
        contract, created = ingestion.ingest(
            conn, text=p["content"], title=p["title"], counterparty=p["counterparty"],
            department=p["metadata"].get("department"), source="finalize", source_ref=p["agreement_id"])
        return {"status": "ingested" if created else "already-present", "contract_id": contract["id"]}


async def subscribe_forever() -> None:
    """Register with the Finalize event bus, retrying until it is reachable."""
    if not BUS_URL:
        return
    body = {"subscriber": "analyze", "topic": "agreement.executed",
            "callback_url": f"{PUBLIC_URL}/integrations/finalize/events"}
    while True:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                (await client.post(f"{BUS_URL}/subscriptions", json=body)).raise_for_status()
            log.info("subscribed to Finalize agreement.executed events")
            return
        except httpx.HTTPError:
            await asyncio.sleep(1.0)
