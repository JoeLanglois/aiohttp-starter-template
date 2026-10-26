"""The security-hardening pass: headers, origin check on all unsafe
methods, username normalization + length caps, __Host- cookie, SSE cap,
rate-limiter hygiene, auth logging."""

import logging
import re
import time

import pytest

from myapp import accounts, web
from myapp.config import Config
from myapp.main import make_app

PW = "correct horse battery staple"


async def _client_with_user(aiohttp_client, cfg, username="alice"):
    app = make_app(cfg)
    accounts.create_user(app["db"], username, PW)
    return await aiohttp_client(app), app


async def _login(client, username="alice", password=PW):
    return await client.post(
        "/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )


# -- Security headers -------------------------------------------------------


def _nonce_of(csp: str) -> str:
    match = re.search(r"'nonce-([^']+)'", csp)
    assert match, f"no nonce in CSP: {csp}"
    return match.group(1)


async def test_headers_on_pages_and_redirects(aiohttp_client, cfg):
    client, _ = await _client_with_user(aiohttp_client, cfg)

    resp = await client.get("/login")
    csp = resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    # Datastar runs in its documented CSP nonce mode: script-src
    # carries a per-response nonce, <html> carries the same nonce in
    # data-nonce, and 'unsafe-eval'/'unsafe-inline' never appear.
    assert "unsafe-eval" not in csp and "unsafe-inline" not in csp
    assert f'data-nonce="{_nonce_of(csp)}"' in await resp.text()
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "same-origin"

    # The authenticated page too (it's the live Datastar one).
    await _login(client)
    resp = await client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    assert f'data-nonce="{_nonce_of(csp)}"' in await resp.text()

    # Redirects are raised as exceptions in handlers; headers must
    # survive that path too.
    resp = await _login(client)
    assert resp.status == 302
    assert "Content-Security-Policy" in resp.headers


async def test_csp_dev_nonces_the_hotswap_snippet(aiohttp_client, tmp_path):
    cfg = Config(db_path=tmp_path / "t.db", secure_cookies=False, dev=True)
    client = await aiohttp_client(make_app(cfg))
    resp = await client.get("/login")
    csp = resp.headers["Content-Security-Policy"]
    # Same policy as prod: with a nonce in script-src, browsers ignore
    # 'unsafe-inline', so the dev snippet must carry the nonce itself.
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    nonce = _nonce_of(csp)
    body = await resp.text()
    assert f'data-nonce="{nonce}"' in body
    assert f'<script nonce="{nonce}">' in body  # the hot-swap snippet


# -- Origin check: all unsafe methods ---------------------------------------


async def test_origin_check_covers_non_post_methods(aiohttp_client, cfg):
    client, _ = await _client_with_user(aiohttp_client, cfg)
    resp = await client.delete(
        "/notes", headers={"Origin": "https://evil.example"}
    )
    assert resp.status == 403
    # Same request without a foreign Origin sails past the middleware
    # and dies at routing: no DELETE route exists (405).
    resp = await client.delete("/notes")
    assert resp.status == 405


# -- Usernames: case-insensitive, capped ------------------------------------


async def test_username_normalized_everywhere(aiohttp_client, cfg, db):
    user = accounts.create_user(db, "  MixedCase ", PW)
    assert user["username"] == "mixedcase"
    assert accounts.authenticate(db, "MIXEDCASE", PW)["id"] == user["id"]
    with pytest.raises(ValueError, match="already taken"):
        accounts.create_user(db, "mixedCASE", PW)

    client, _ = await _client_with_user(aiohttp_client, cfg, "CaseWeb")
    resp = await _login(client, username="CASEWEB")
    assert resp.status == 302


def test_length_caps(db):
    with pytest.raises(ValueError, match="at most"):
        accounts.create_user(db, "x" * 65, PW)
    with pytest.raises(ValueError, match="at most"):
        accounts.create_user(db, "alice", "y" * 1025)


async def test_oversized_password_login_fails_cleanly(aiohttp_client, cfg):
    client, _ = await _client_with_user(aiohttp_client, cfg)
    resp = await _login(client, password="z" * 5000)
    assert resp.status == 401


# -- Session cookie ----------------------------------------------------------


async def test_host_prefix_cookie_when_secure(aiohttp_client, tmp_path):
    cfg = Config(db_path=tmp_path / "t.db", secure_cookies=True)
    client, _ = await _client_with_user(aiohttp_client, cfg)
    resp = await _login(client)
    assert resp.status == 302
    set_cookie = resp.headers["Set-Cookie"]
    assert set_cookie.startswith("__Host-sid=")
    assert "Secure" in set_cookie and "HttpOnly" in set_cookie
    # The middleware reads the same name back (bypass the jar, which
    # won't send Secure cookies over plain http in tests).
    token = set_cookie.split(";", 1)[0].split("=", 1)[1]
    resp = await client.get(
        "/", headers={"Cookie": f"__Host-sid={token}"}, allow_redirects=False
    )
    assert resp.status == 200


# -- SSE stream cap ------------------------------------------------------------


async def test_events_stream_capped_per_user(aiohttp_client, cfg):
    client, app = await _client_with_user(aiohttp_client, cfg)
    await _login(client)
    bus = app["bus"]
    queues = [bus.subscribe("user:1") for _ in range(web.MAX_STREAMS_PER_USER)]
    try:
        resp = await client.get("/events")
        assert resp.status == 429
    finally:
        for q in queues:
            bus.unsubscribe("user:1", q)


# -- Rate limiter hygiene -------------------------------------------------------


def test_rate_limiter_prunes_dead_keys():
    failures: dict[str, list[float]] = {}
    stale_at = time.monotonic() - web._FAIL_WINDOW - 1
    for i in range(web._PRUNE_AT + 10):
        failures[f"10.0.0.1:stale-{i}"] = [stale_at]
    web._record_failure(failures, "10.0.0.1:fresh")
    assert list(failures) == ["10.0.0.1:fresh"]


async def test_case_variants_share_one_rate_bucket(aiohttp_client, cfg):
    client, _ = await _client_with_user(aiohttp_client, cfg)
    codes = []
    for name in ("alice", "ALICE", "Alice", "aLiCe", "alice", "ALICE"):
        resp = await _login(client, username=name, password="wrong-password")
        codes.append(resp.status)
    # Without a normalized key, each variant would get its own 5 tries.
    assert codes == [401, 401, 401, 401, 401, 429]


# -- Auth logging -----------------------------------------------------------------


async def test_login_attempts_are_logged_without_passwords(
    aiohttp_client, cfg, caplog
):
    client, _ = await _client_with_user(aiohttp_client, cfg)
    with caplog.at_level(logging.INFO, logger="myapp.auth"):
        await _login(client, password="wrong-password")
        await _login(client)
    text = caplog.text
    assert "login failed" in text and "login ok" in text
    assert "wrong-password" not in text and PW not in text
