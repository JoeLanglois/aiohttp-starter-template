"""A few end-to-end smoke tests through the real HTTP layer.
Everything else is covered at the context level; keep this file small."""

from myapp import accounts
from myapp.main import make_app

PW = "correct horse battery staple"


async def _client_with_user(aiohttp_client, cfg):
    app = make_app(cfg)
    accounts.create_user(app["db"], "alice", PW)
    return await aiohttp_client(app)


async def _login(client, password=PW):
    return await client.post(
        "/login",
        data={"username": "alice", "password": password},
        allow_redirects=False,
    )


async def test_anonymous_is_redirected_to_login(aiohttp_client, cfg):
    client = await aiohttp_client(make_app(cfg))
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login"


async def test_login_logout_flow(aiohttp_client, cfg):
    client = await _client_with_user(aiohttp_client, cfg)

    resp = await _login(client, password="wrong wrong wrong")
    assert resp.status == 401

    resp = await _login(client)
    assert resp.status == 302
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie

    resp = await client.get("/")
    assert resp.status == 200
    assert "alice" in await resp.text()

    resp = await client.post("/logout", allow_redirects=False)
    assert resp.status == 302
    # The session is dead server-side too, not just cookie-less.
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login"


async def test_notes_round_trip_and_escaping(aiohttp_client, cfg):
    client = await _client_with_user(aiohttp_client, cfg)
    await _login(client)

    resp = await client.post(
        "/notes",
        data={"title": "<b>bold?</b>", "body": "hello"},
        allow_redirects=False,
    )
    assert resp.status == 302

    resp = await client.get("/")
    text = await resp.text()
    assert "&lt;b&gt;bold?&lt;/b&gt;" in text  # escaped by tdom
    assert "<b>bold?</b>" not in text


async def test_cross_origin_post_is_rejected(aiohttp_client, cfg):
    client = await _client_with_user(aiohttp_client, cfg)
    await _login(client)
    resp = await client.post(
        "/notes",
        data={"title": "csrf attempt"},
        headers={"Origin": "https://evil.example"},
        allow_redirects=False,
    )
    assert resp.status == 403
