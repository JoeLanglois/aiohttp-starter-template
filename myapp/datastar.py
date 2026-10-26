"""aiohttp adapter for the official Datastar Python SDK (datastar-py).

The SDK ships first-party integrations for Starlette, Quart, FastAPI,
Django, Litestar and Sanic — but not aiohttp. This module mirrors those
integrations (same names, same semantics) on aiohttp primitives:

- `DatastarResponse` — a StreamResponse that emits 0..N SDK events.
  Empty content → 204 No Content (no SSE headers), exactly like the
  other adapters; otherwise 200 + the SDK's `SSE_HEADERS`. Content may
  be a single event, an iterable of events, or an async generator of
  events for long-lived streams.
- `datastar_response` — decorator turning an event-returning aiohttp
  handler into a DatastarResponse-returning one.
- `read_signals(request)` — the request's Datastar signals as a dict
  (from the `datastar` query param on GET/DELETE, or the JSON body
  otherwise); None when the request didn't come from Datastar.
- `is_datastar_request(request)` — header check, for handlers that
  serve both classic form posts and Datastar actions on one route.
- `render_stream(bus, topic, render)` — the Hyperlith fat-update
  pattern as an async generator for DatastarResponse: subscribe to a
  bus topic, yield a full re-render now and after every publish,
  skipping renders that didn't change. Unsubscribes itself when the
  stream ends or the client disconnects. See bus.py.

Everything Datastar-shaped (event formatting, signal encoding) is the
SDK's `ServerSentEventGenerator`, re-exported here like the other
adapters do. `read_signals` delegates to the SDK's own `_read_signals`,
the same private helper every first-party integration uses.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable, Callable, Iterable
from functools import wraps
from inspect import isasyncgen
from typing import Any

from aiohttp import web
from datastar_py import SSE_HEADERS, ServerSentEventGenerator, _read_signals
from datastar_py.sse import DatastarEvent

__all__ = [
    "SSE_HEADERS",
    "DatastarResponse",
    "ServerSentEventGenerator",
    "datastar_response",
    "is_datastar_request",
    "read_signals",
    "render_stream",
]


def is_datastar_request(request: web.Request) -> bool:
    return "Datastar-Request" in request.headers


async def read_signals(request: web.Request) -> dict[str, Any] | None:
    return _read_signals(
        request.method, request.headers, request.query, await request.read()
    )


class DatastarResponse(web.StreamResponse):
    """Respond with 0..N `DatastarEvent`s (see module docstring)."""

    def __init__(
        self,
        content: DatastarEvent
        | Iterable[DatastarEvent]
        | AsyncIterable[DatastarEvent]
        | None = None,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not content:
            super().__init__(status=status or 204, headers=headers)
            self._content = None
        else:
            super().__init__(
                status=status or 200,
                headers={**SSE_HEADERS, **(headers or {})},
            )
            self._content = content

    async def prepare(self, request: web.Request) -> DatastarResponse:
        if not self.prepared:
            await super().prepare(request)
        content, self._content = self._content, None
        if content is not None:
            try:
                if isinstance(content, (DatastarEvent, str)):
                    await self.write(str(content).encode())
                elif isasyncgen(content):
                    async for event in content:
                        await self.write(str(event).encode())
                else:
                    for event in content:
                        await self.write(str(event).encode())
            finally:
                if isasyncgen(content):
                    # Guarantee the generator's own cleanup (e.g.
                    # render_stream unsubscribing from the bus) runs on
                    # disconnect/cancellation too, not just on a clean
                    # finish — a suspended generator would otherwise
                    # wait for garbage collection.
                    try:
                        await content.aclose()
                    except Exception:
                        pass
            await self.write_eof()
        return self


def render_stream(
    bus,
    topic: str,
    render: Callable[[], str],
    *,
    heartbeat_seconds: float | None = 30.0,
) -> AsyncIterable[DatastarEvent | str]:
    """An async generator of fat-update events for DatastarResponse —
    the Hyperlith model:

        return DatastarResponse(render_stream(bus, f"user:{id}", render))

    `render()` returns the fragment to morph (its root element's id is
    the patch target — e.g. the page's whole <main id="app">). The
    current render is yielded immediately, then again after every
    `bus.publish(topic)`, but only when it actually changed: renders
    are compared as strings, and each emitted event carries the
    render's SHA-256 as its SSE id, like Hyperlith. Actions stay
    CQRS-pure — mutate, publish, answer 204 — and every open page
    converges on the new state by itself.

    The subscription lives and dies with the generator: when the
    client disconnects, aiohttp cancels/closes the response,
    DatastarResponse closes this generator, and the finally below
    unsubscribes — no leaked subscribers. A comment heartbeat every
    `heartbeat_seconds` (None to disable) keeps idle connections alive
    through proxies.
    """

    async def gen():
        queue = bus.subscribe(topic)
        last_html: str | None = None
        try:
            while True:
                html = render()
                if html != last_html:
                    last_html = html
                    digest = hashlib.sha256(html.encode()).hexdigest()
                    yield ServerSentEventGenerator.patch_elements(
                        html, event_id=digest
                    )
                try:
                    await asyncio.wait_for(queue.get(), heartbeat_seconds)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(topic, queue)

    return gen()


def datastar_response(func):
    """Wrap an aiohttp handler returning DatastarEvents so it returns a
    DatastarResponse instead. The handler still receives the request."""

    @wraps(func)
    async def wrapper(request: web.Request, *args, **kwargs) -> DatastarResponse:
        return DatastarResponse(await func(request, *args, **kwargs))

    return wrapper
