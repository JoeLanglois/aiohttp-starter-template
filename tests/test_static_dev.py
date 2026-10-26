"""Static files and the dev-only hot-swap machinery."""

import asyncio

from myapp.config import Config
from myapp.main import make_app
from myapp.web import STATIC_DIR


def dev_cfg(tmp_path):
    return Config(db_path=tmp_path / "test.db", secure_cookies=False, dev=True)


async def test_static_css_served_in_prod(aiohttp_client, cfg):
    client = await aiohttp_client(make_app(cfg))
    resp = await client.get("/static/app.css")
    assert resp.status == 200
    assert resp.content_type == "text/css"
    # Production keeps normal caching semantics — no no-store stamp.
    assert "no-store" not in resp.headers.get("Cache-Control", "")


async def test_static_no_store_in_dev(aiohttp_client, tmp_path):
    client = await aiohttp_client(make_app(dev_cfg(tmp_path)))
    resp = await client.get("/static/app.css")
    assert resp.status == 200
    assert resp.headers.get("Cache-Control") == "no-store"


async def test_static_missing_and_traversal_404(aiohttp_client, cfg):
    client = await aiohttp_client(make_app(cfg))
    assert (await client.get("/static/nope.css")).status == 404
    assert (await client.get("/static/../pyproject.toml")).status == 404


async def test_dev_events_404_outside_dev(aiohttp_client, cfg):
    client = await aiohttp_client(make_app(cfg))
    assert (await client.get("/__dev/events")).status == 404


async def test_dev_page_links_css_and_snippet(aiohttp_client, tmp_path):
    client = await aiohttp_client(make_app(dev_cfg(tmp_path)))
    text = await (await client.get("/login")).text()
    assert 'href="/static/app.css"' in text
    assert 'EventSource("/__dev/events")' in text
    assert "{{" not in text  # t-string brace escaping rendered correctly


async def test_prod_page_has_css_but_no_snippet(aiohttp_client, cfg):
    client = await aiohttp_client(make_app(cfg))
    text = await (await client.get("/login")).text()
    assert 'href="/static/app.css"' in text
    assert "EventSource" not in text


async def test_css_change_pushes_sse_event(aiohttp_client, tmp_path):
    client = await aiohttp_client(make_app(dev_cfg(tmp_path)))
    css = STATIC_DIR / "app.css"
    original = css.read_text()
    try:
        resp = await client.get("/__dev/events")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        await asyncio.sleep(0.4)  # let the watcher's first snapshot land
        css.write_text(original + "\n/* hot-swap test change */\n")

        async def read_until_event() -> bytes:
            data = b""
            while b"app.css" not in data:
                line = await resp.content.readline()
                if not line:
                    break
                data += line
            return data

        data = await asyncio.wait_for(read_until_event(), timeout=5)
        assert b"event: css" in data
        resp.close()
    finally:
        css.write_text(original)
