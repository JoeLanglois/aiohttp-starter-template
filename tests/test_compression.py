"""Brotli compression of the SSE streams + the CVE-2026-111497 pin."""

import asyncio

import brotli
from aiohttp.test_utils import make_mocked_request

from myapp import accounts, datastar, notes
from myapp.main import make_app

PW = "correct horse battery staple"
DS = {"Datastar-Request": "true"}


def test_brotli_is_patched_against_node2shell():
    # CVE-2026-111497 ("Node2Shell"): OOB write in the Brotli C decoder,
    # every release <= 1.2.0. The fix is 1.2.1. This test is the tripwire
    # that keeps a future dependency shuffle from silently downgrading.
    version = tuple(int(p) for p in brotli.__version__.split(".")[:3])
    assert version >= (1, 2, 1)


def test_accepts_brotli_parsing():
    def req(ae):
        headers = {} if ae is None else {"Accept-Encoding": ae}
        return make_mocked_request("GET", "/", headers=headers)

    assert datastar.accepts_brotli(req("gzip, deflate, br"))
    assert datastar.accepts_brotli(req("br"))
    assert not datastar.accepts_brotli(req("gzip"))
    assert not datastar.accepts_brotli(req("br;q=0"))
    assert not datastar.accepts_brotli(req(None))


async def _logged_in_client(aiohttp_client, cfg):
    app = make_app(cfg)
    accounts.create_user(app["db"], "alice", PW)
    client = await aiohttp_client(app)
    await client.post("/login", data={"username": "alice", "password": PW})
    return client, app


async def _decoded_until(resp, decompressor, needle: str, timeout=5) -> str:
    """Read raw (still-encoded) chunks, decompressing incrementally,
    until `needle` appears in the decoded text. Proves both that events
    are decodable mid-stream (per-event flush) and that they arrive.
    One decompressor per stream — brotli state is continuous."""
    text = ""

    async def _pump():
        nonlocal text
        while needle not in text:
            chunk = await resp.content.readany()
            if not chunk:
                break
            text += decompressor.process(chunk).decode()

    await asyncio.wait_for(_pump(), timeout)
    return text


async def test_events_stream_is_brotli_and_stays_live(aiohttp_client, cfg):
    client, app = await _logged_in_client(aiohttp_client, cfg)
    resp = await client.get(
        "/events", headers={"Accept-Encoding": "br"}, auto_decompress=False
    )
    assert resp.status == 200
    assert resp.headers.get("Content-Encoding") == "br"
    decompressor = brotli.Decompressor()
    try:
        first = await _decoded_until(resp, decompressor, 'id="app"')
        assert "datastar-patch-elements" in first

        await client.post(
            "/notes", json={"title": "Compressed"}, headers=DS
        )
        pushed = await _decoded_until(resp, decompressor, "Compressed")
        assert "Compressed" in pushed
    finally:
        resp.close()


async def test_events_stream_identity_without_br(aiohttp_client, cfg):
    client, app = await _logged_in_client(aiohttp_client, cfg)
    resp = await client.get("/events", headers={"Accept-Encoding": "identity"})
    assert resp.headers.get("Content-Encoding") is None
    line = await asyncio.wait_for(resp.content.readline(), 5)
    assert line.startswith(b"event: datastar-patch-elements")
    resp.close()


async def test_dev_events_stream_is_brotli(aiohttp_client, tmp_path):
    from myapp.config import Config

    cfg = Config(db_path=tmp_path / "t.db", secure_cookies=False, dev=True)
    client = await aiohttp_client(make_app(cfg))
    resp = await client.get(
        "/__dev/events",
        headers={"Accept-Encoding": "br"},
        auto_decompress=False,
    )
    assert resp.headers.get("Content-Encoding") == "br"
    text = await _decoded_until(resp, brotli.Decompressor(), ": connected")
    assert ": connected" in text
    resp.close()
