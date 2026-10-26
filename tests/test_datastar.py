"""The aiohttp Datastar adapter (myapp/datastar.py) and the Datastar
flows it enables on the notes routes."""

from aiohttp import web

from myapp import accounts, datastar, notes
from myapp.main import make_app

PW = "correct horse battery staple"
DS = {"Datastar-Request": "true"}


# -- Adapter unit tests, on a bare app ----------------------------------------


def _adapter_app():
    async def echo_signals(request):
        return web.json_response(await datastar.read_signals(request))

    async def empty(request):
        return datastar.DatastarResponse()

    async def single(request):
        return datastar.DatastarResponse(
            datastar.ServerSentEventGenerator.patch_signals({"a": 1})
        )

    async def stream(request):
        async def gen():
            yield datastar.ServerSentEventGenerator.patch_signals({"n": 1})
            yield datastar.ServerSentEventGenerator.patch_signals({"n": 2})

        return datastar.DatastarResponse(gen())

    app = web.Application()
    app.router.add_get("/signals", echo_signals)
    app.router.add_post("/signals", echo_signals)
    app.router.add_get("/empty", empty)
    app.router.add_get("/single", single)
    app.router.add_get("/stream", stream)
    return app


async def test_read_signals_from_query_param(aiohttp_client):
    client = await aiohttp_client(_adapter_app())
    resp = await client.get(
        "/signals", params={"datastar": '{"title": "x"}'}, headers=DS
    )
    assert await resp.json() == {"title": "x"}


async def test_read_signals_from_json_body(aiohttp_client):
    client = await aiohttp_client(_adapter_app())
    resp = await client.post("/signals", json={"body": "hi"}, headers=DS)
    assert await resp.json() == {"body": "hi"}


async def test_read_signals_none_without_header(aiohttp_client):
    client = await aiohttp_client(_adapter_app())
    resp = await client.post("/signals", json={"body": "hi"})
    assert await resp.json() is None


async def test_empty_response_is_204(aiohttp_client):
    client = await aiohttp_client(_adapter_app())
    resp = await client.get("/empty")
    assert resp.status == 204
    assert await resp.text() == ""


async def test_single_and_streamed_events(aiohttp_client):
    client = await aiohttp_client(_adapter_app())
    resp = await client.get("/single")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"
    assert "event: datastar-patch-signals" in await resp.text()

    resp = await client.get("/stream")
    text = await resp.text()
    assert text.count("event: datastar-patch-signals") == 2


# -- Integration: notes over Datastar, classic fallback intact -----------------


async def _logged_in_client(aiohttp_client, cfg):
    app = make_app(cfg)
    accounts.create_user(app["db"], "alice", PW)
    client = await aiohttp_client(app)
    await client.post("/login", data={"username": "alice", "password": PW})
    return client, app


async def test_datastar_add_note_cqrs(aiohttp_client, cfg):
    # Mutate + publish; the only direct reply clears the form signals.
    # The list update itself travels over the /events push stream.
    client, app = await _logged_in_client(aiohttp_client, cfg)
    resp = await client.post(
        "/notes", json={"title": "Via DS", "body": "hello"}, headers=DS
    )
    assert resp.status == 200
    text = await resp.text()
    assert "event: datastar-patch-signals" in text
    assert '"title":""' in text
    assert len(notes.list_notes(app["db"], 1)) == 1


async def test_datastar_add_note_validation_error(aiohttp_client, cfg):
    client, app = await _logged_in_client(aiohttp_client, cfg)
    resp = await client.post("/notes", json={"title": "  "}, headers=DS)
    assert resp.status == 204  # error is state now; the push shows it
    assert app["note_errors"][1] == "Title is required."
    assert notes.list_notes(app["db"], 1) == []


async def test_datastar_delete_note_is_204(aiohttp_client, cfg):
    client, app = await _logged_in_client(aiohttp_client, cfg)
    note = notes.create_note(app["db"], 1, "Doomed", "x")
    resp = await client.post(f"/notes/{note['id']}/delete", headers=DS)
    assert resp.status == 204
    assert notes.list_notes(app["db"], 1) == []


async def _read_event(resp, timeout=5):
    """Read one SSE event block (lines up to the blank separator)."""
    import asyncio

    async def _read():
        data = b""
        while not data.endswith(b"\n\n"):
            line = await resp.content.readline()
            if not line:
                break
            data += line
        return data.decode()

    return await asyncio.wait_for(_read(), timeout)


async def test_events_stream_pushes_fat_updates(aiohttp_client, cfg):
    client, app = await _logged_in_client(aiohttp_client, cfg)
    resp = await client.get("/events")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"
    try:
        first = await _read_event(resp)
        assert "event: datastar-patch-elements" in first
        assert 'id="app"' in first
        assert app["bus"].subscriber_count("user:1") == 1

        await client.post("/notes", json={"title": "Pushed"}, headers=DS)
        pushed = await _read_event(resp)
        assert "Pushed" in pushed
        assert "id: " in pushed  # render hash as the SSE event id
    finally:
        resp.close()
    # Disconnecting unsubscribes — no leaked subscriber.
    import asyncio

    for _ in range(50):
        if app["bus"].subscriber_count("user:1") == 0:
            break
        await asyncio.sleep(0.05)
    assert app["bus"].subscriber_count("user:1") == 0


async def test_classic_flow_still_redirects(aiohttp_client, cfg):
    client, app = await _logged_in_client(aiohttp_client, cfg)
    resp = await client.post(
        "/notes", data={"title": "Classic"}, allow_redirects=False
    )
    assert resp.status == 302


async def test_client_bundle_is_vendored_and_loaded(aiohttp_client, cfg):
    client = await aiohttp_client(make_app(cfg))
    resp = await client.get("/static/datastar.js")
    assert resp.status == 200
    assert "javascript" in resp.content_type
    text = await (await client.get("/login")).text()
    assert '<script type="module" src="/static/datastar.js"></script>' in text
