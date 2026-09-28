"""Asynchronous messaging between services.

Publishing uses the *transactional outbox* pattern: an event is first written
to the service's own database, then relayed to the event bus by a background
loop. If the bus is down, nothing is lost - the relay simply retries.

Consuming is *idempotent*: the bus delivers at-least-once, so every consumer
records the ids of events it has processed and ignores duplicates.
"""
import asyncio
import fnmatch
import os
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, HTTPException

from .service import correlation_id, get_logger, http_client, service_url
from .store import Store, new_id, now_iso

Handler = Callable[[dict], Awaitable[None]]


class Events:
    def __init__(self, service: str, store: Store):
        self.service = service
        self.store = store
        self.handlers: dict[str, Handler] = {}
        self.log = get_logger(service)
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ---- publishing -------------------------------------------------------
    def publish(self, topic: str, payload: dict) -> dict:
        event = {
            "id": new_id("evt"),
            "topic": topic,
            "source": self.service,
            "occurred_at": now_iso(),
            "correlation_id": correlation_id.get(),
            "payload": payload,
        }
        self.store.put("outbox", event)
        self._wake.set()
        return event

    async def _relay_outbox(self) -> None:
        pending = self.store.list("outbox")
        if not pending:
            return
        for event in pending:
            try:
                response = await http_client().post(f"{service_url('event_bus')}/publish", json=event, timeout=3.0)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                self.log.warning("event bus unreachable, %s stays in outbox (%s)", event["topic"], exc)
                return
            self.store.delete("outbox", event["id"])

    # ---- consuming --------------------------------------------------------
    def on(self, topic_pattern: str):
        def register(handler: Handler) -> Handler:
            self.handlers[topic_pattern] = handler
            return handler

        return register

    def mount(self, app: FastAPI) -> None:
        """Expose POST /events: the endpoint the bus pushes messages to."""

        @app.post("/events", include_in_schema=False)
        async def receive(event: dict):
            if self.store.get("processed_events", event["id"]):
                return {"status": "duplicate-ignored"}
            handler = next(
                (h for pattern, h in self.handlers.items() if fnmatch.fnmatch(event["topic"], pattern)), None
            )
            if handler is None:
                raise HTTPException(404, f"{self.service} has no handler for {event['topic']}")
            token = correlation_id.set(event.get("correlation_id") or "-")
            try:
                await handler(event)  # an exception -> 500 -> the bus retries
            finally:
                correlation_id.reset(token)
            self.store.put("processed_events", {"id": event["id"], "topic": event["topic"], "at": now_iso()})
            return {"status": "processed"}

    async def _subscribe(self) -> bool:
        callback = os.environ.get("SELF_URL", service_url(self.service)) + "/events"
        try:
            for topic in self.handlers:
                response = await http_client().post(
                    f"{service_url('event_bus')}/subscriptions",
                    json={"subscriber": self.service, "topic": topic, "callback_url": callback},
                    timeout=3.0,
                )
                response.raise_for_status()
        except httpx.HTTPError:
            return False
        self.log.info("subscribed to %s", sorted(self.handlers))
        return True

    # ---- lifecycle --------------------------------------------------------
    async def _run(self) -> None:
        subscribed = not self.handlers
        while True:
            if not subscribed:
                subscribed = await self._subscribe()
            await self._relay_outbox()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
