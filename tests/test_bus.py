"""The pub/sub bus (myapp/bus.py) and the adapter's render_stream."""

import asyncio


from myapp import datastar
from myapp.bus import Bus


def test_publish_nudges_subscribers():
    bus = Bus()
    queue = bus.subscribe("t")
    bus.publish("t")
    assert queue.qsize() == 1


def test_publish_coalesces():
    bus = Bus()
    queue = bus.subscribe("t")
    bus.publish("t")
    bus.publish("t")
    bus.publish("t")
    assert queue.qsize() == 1  # latest state wins; no pile-up


def test_topics_are_isolated_and_unsubscribe_works():
    bus = Bus()
    qa = bus.subscribe("a")
    bus.subscribe("b")
    bus.publish("a")
    assert qa.qsize() == 1
    assert bus.subscriber_count("b") == 1
    bus.unsubscribe("b", bus._topics["b"].copy().pop())
    assert bus.subscriber_count("b") == 0
    bus.publish("b")  # no subscribers: a no-op, not an error


async def _next_event(gen, timeout=2):
    return await asyncio.wait_for(gen.__anext__(), timeout)


async def test_render_stream_initial_change_and_dedup():
    bus = Bus()
    state = {"html": "<main id='app'>one</main>"}
    gen = datastar.render_stream(
        bus, "t", lambda: state["html"], heartbeat_seconds=None
    )
    first = await _next_event(gen)
    assert "one" in first
    assert "event: datastar-patch-elements" in first

    # A publish with unchanged state produces no new event. (Race the
    # pending __anext__ instead of timing it out: cancelling an async
    # generator's __anext__ would close the generator.)
    pending = asyncio.ensure_future(gen.__anext__())
    bus.publish("t")
    await asyncio.wait({pending}, timeout=0.3)
    assert not pending.done()

    state["html"] = "<main id='app'>two</main>"
    bus.publish("t")
    second = await asyncio.wait_for(pending, timeout=2)
    assert "two" in second
    await gen.aclose()


async def test_render_stream_unsubscribes_on_close():
    bus = Bus()
    gen = datastar.render_stream(
        bus, "t", lambda: "<main id='app'>x</main>", heartbeat_seconds=None
    )
    await _next_event(gen)
    assert bus.subscriber_count("t") == 1
    await gen.aclose()
    assert bus.subscriber_count("t") == 0


async def test_render_stream_heartbeat():
    bus = Bus()
    gen = datastar.render_stream(
        bus, "t", lambda: "<main id='app'>x</main>", heartbeat_seconds=0.1
    )
    await _next_event(gen)  # initial render
    beat = await _next_event(gen)
    assert beat == ": ping\n\n"
    await gen.aclose()
