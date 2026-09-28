"""Event bus: a minimal push-based pub/sub broker.

Plays the role Kafka / RabbitMQ / SNS+SQS play in production. Services
subscribe with a topic pattern and a callback URL; published events are
persisted, then pushed to every matching subscriber with retries. Deliveries
that keep failing are parked as "dead_letter" and can be redelivered.
"""
import asyncio
import fnmatch
from contextlib import asynccontextmanager

import httpx
from fastapi import HTTPException
from pydantic import BaseModel

from lscommon.service import create_service, get_logger
from lscommon.store import Store, db_path, new_id, now_iso

MAX_ATTEMPTS = 5
store = Store(db_path("event_bus"))
log = get_logger("event_bus")
_in_flight: set[asyncio.Task] = set()
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app):
    global _client
    _client = httpx.AsyncClient(timeout=5.0)
    yield
    await _client.aclose()


app = create_service("event_bus", lifespan=lifespan)


class Subscription(BaseModel):
    subscriber: str
    topic: str
    callback_url: str


class Event(BaseModel):
    id: str
    topic: str
    source: str
    occurred_at: str
    correlation_id: str = "-"
    payload: dict


@app.post("/subscriptions")
def subscribe(sub: Subscription):
    doc = {"id": f"{sub.subscriber}:{sub.topic}", **sub.model_dump(), "created_at": now_iso()}
    return store.put("subscriptions", doc)


@app.get("/subscriptions")
def list_subscriptions():
    return store.list("subscriptions")


@app.post("/publish", status_code=202)
async def publish(event: Event):
    if store.get("events", event.id):
        return {"status": "duplicate-ignored", "event_id": event.id}
    store.put("events", {**event.model_dump(), "received_at": now_iso()})
    targets = [s for s in store.list("subscriptions") if fnmatch.fnmatch(event.topic, s["topic"])]
    for sub in targets:
        delivery = {
            "id": new_id("dlv"),
            "event_id": event.id,
            "topic": event.topic,
            "subscriber": sub["subscriber"],
            "callback_url": sub["callback_url"],
            "status": "pending",
            "attempts": 0,
            "last_error": None,
        }
        store.put("deliveries", delivery)
        _schedule(delivery, event.model_dump())
    return {"status": "accepted", "event_id": event.id, "subscribers": [s["subscriber"] for s in targets]}


def _schedule(delivery: dict, event: dict) -> None:
    task = asyncio.get_running_loop().create_task(_deliver(delivery, event))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


async def _deliver(delivery: dict, event: dict) -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        delivery["attempts"] = attempt
        try:
            response = await _client.post(delivery["callback_url"], json=event)
            response.raise_for_status()
            delivery.update(status="delivered", delivered_at=now_iso(), last_error=None)
            store.put("deliveries", delivery)
            return
        except httpx.HTTPError as exc:
            delivery["last_error"] = f"{exc.__class__.__name__}: {exc}"
            store.put("deliveries", delivery)
            await asyncio.sleep(0.2 * 2 ** (attempt - 1))  # exponential backoff
    delivery["status"] = "dead_letter"
    store.put("deliveries", delivery)
    log.error("dead-lettered %s -> %s: %s", event["topic"], delivery["subscriber"], delivery["last_error"])


@app.get("/events")
def list_events(topic: str | None = None, correlation_id: str | None = None):
    return store.list("events", topic=topic, correlation_id=correlation_id)


@app.get("/deliveries")
def list_deliveries(status: str | None = None, subscriber: str | None = None):
    return store.list("deliveries", status=status, subscriber=subscriber)


@app.post("/deliveries/{delivery_id}/redeliver", status_code=202)
async def redeliver(delivery_id: str):
    delivery = store.get("deliveries", delivery_id)
    if not delivery:
        raise HTTPException(404, "delivery not found")
    delivery.update(status="pending", attempts=0)
    store.put("deliveries", delivery)
    event = store.get("events", delivery["event_id"])
    event.pop("received_at", None)
    _schedule(delivery, event)
    return delivery
