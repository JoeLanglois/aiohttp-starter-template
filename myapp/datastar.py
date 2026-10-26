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
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Iterable
from functools import wraps
from typing import Any

from aiohttp import web
from datastar_py import SSE_HEADERS, ServerSentEventGenerator, _read_signals
from datastar_py.sse import DatastarEvent

__all__ = [
    "SSE_HEADERS",
    "DatastarResponse",
    "ServerSentEventGenerator",
    "accepts_brotli",
    "datastar_response",
    "is_datastar_request",
    "read_signals",
    "render_stream",
]


def is_datastar_request(request: web.Request) -> bool:
    return "Datastar-Request" in request.headers


def accepts_brotli(request: web.Request) -> bool:
    """True when the client offered `Accept-Encoding: br` (q > 0)."""
    for part in request.headers.get("Accept-Encoding", "").split(","):
        token, _, params = part.strip().partition(";")
        if token.strip().lower() == "br" and "q=0" not in params.replace(" ", ""):
            return True
    return False


async def read_signals(request: web.Request) -> dict[str, Any] | None:
    return _read_signals(
        request.method, request.headers, request.query, await request.read()
    )


class DatastarResponse(web.StreamResponse):
    """Respond with 0..N `DatastarEvent`s (see module docstring).

    With `compress=True` the body is Brotli-encoded (the caller checks
    `accepts_brotli(request)` first). Compression is per-event with a
    flush after each one, so streamed events stay immediately
    deliverable — a buffered compressor would batch the very updates
    SSE exists to push. Uses the `brotli` package; keep it >= 1.2.1
    (older releases carry CVE-2026-111497 in the decoder).
    """

    def __init__(
        self,
        content: DatastarEvent
        | Iterable[DatastarEvent]
        | AsyncIterable[DatastarEvent]
        | None = None,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        compress: bool = False,
    ) -> None:
        self._compress = compress and bool(content)
        if not content:
            super().__init__(status=status or 204, headers=headers)
            self._content = None
        else:
            merged = {**SSE_HEADERS, **(headers or {})}
            if self._compress:
                merged["Content-Encoding"] = "br"
            super().__init__(status=status or 200, headers=merged)
            self._content = content

    async def prepare(self, request: web.Request) -> DatastarResponse:
        if not self.prepared:
            await super().prepare(request)
        content, self._content = self._content, None
        if content is not None:
            encoder = None
            if self._compress:
                try:
                    import brotli
                except ImportError as exc:
                    raise RuntimeError(
                        "DatastarResponse(compress=True) needs the "
                        "brotli package (>=1.2.1)"
                    ) from exc
                encoder = brotli.Compressor(quality=4)

            async def send(data: bytes) -> None:
                if encoder is not None:
                    data = encoder.process(data) + encoder.flush()
                if data:
                    await self.write(data)

            try:
                if isinstance(content, (DatastarEvent, str)):
                    await send(str(content).encode())
                elif isinstance(content, AsyncIterable):
                    async for event in content:
                        await send(str(event).encode())
                else:
                    for event in content:
                        await send(str(event).encode())
                if encoder is not None:
                    tail = encoder.finish()
                    if tail:
                        await self.write(tail)
            finally:
                if isinstance(content, AsyncGenerator):
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
) -> AsyncIterable[DatastarEvent]:
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
                    yield DatastarEvent(": ping\n\n")
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
