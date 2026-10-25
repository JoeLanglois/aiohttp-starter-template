"""Web layer (the driving adapter): aiohttp handlers and middleware.

A handler does exactly three things: parse the request, call a context,
render a view. No SQL, no business rules here. If a handler grows a
fourth step, that step probably belongs in a context.
"""

import asyncio
import time
from urllib.parse import urlparse

from aiohttp import web
from tdom import html as render

from . import accounts, notes, views

COOKIE = "sid"

# -- Login rate limiting (in-memory; plenty for a personal app) -----------

_FAIL_WINDOW = 15 * 60  # seconds
_FAIL_MAX = 5
_failures: dict[str, list[float]] = {}


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failures.get(key, []) if now - t < _FAIL_WINDOW]
    _failures[key] = recent
    return len(recent) >= _FAIL_MAX


def _record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.monotonic())


# -- Middleware -------------------------------------------------------------


@web.middleware
async def origin_check(request, handler):
    """CSRF defence-in-depth next to SameSite=Lax: a POST whose Origin
    header points at another site is not ours, reject it."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            raise web.HTTPForbidden(text="Bad origin")
    return await handler(request)


@web.middleware
async def auth_middleware(request, handler):
    token = request.cookies.get(COOKIE)
    request["user"] = accounts.user_for_session(request.app["db"], token)
    return await handler(request)


# -- Helpers ------------------------------------------------------------------


def html_response(template, status: int = 200) -> web.Response:
    # The single place Templates become strings.
    return web.Response(
        text=str(render(template)), content_type="text/html", status=status
    )


def require_user(request) -> dict:
    user = request.get("user")
    if user is None:
        raise web.HTTPFound("/login")
    return user


# -- Handlers -----------------------------------------------------------------


async def home(request):
    user = require_user(request)
    user_notes = notes.list_notes(request.app["db"], user["id"])
    return html_response(views.home_page(user, user_notes))


async def login_form(request):
    if request.get("user") is not None:
        raise web.HTTPFound("/")
    return html_response(views.login_page())


async def login(request):
    form = await request.post()
    db, cfg = request.app["db"], request.app["config"]
    username = form.get("username", "")
    key = f"{request.remote}:{username}"
    if _rate_limited(key):
        return html_response(
            views.login_page("Too many attempts. Try again later."),
            status=429,
        )
    user = accounts.user_by_username(db, username)
    # Argon2 verify is ~100-250 ms of pure CPU: run it in a thread (never
    # block the event loop), against a dummy hash when the user doesn't
    # exist, so failures are identical in message and timing.
    pw_hash = user["pw_hash"] if user else accounts.DUMMY_HASH
    ok = await asyncio.to_thread(
        accounts.check_password, form.get("password", ""), pw_hash
    )
    if not (user and ok):
        _record_failure(key)
        return html_response(
            views.login_page("Invalid username or password."), status=401
        )
    _failures.pop(key, None)
    token = accounts.start_session(db, user["id"], days=cfg.session_days)
    resp = web.HTTPFound("/")
    resp.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=cfg.secure_cookies,
        samesite="Lax",
        path="/",
        max_age=cfg.session_days * 86_400,
    )
    raise resp


async def logout(request):
    accounts.end_session(request.app["db"], request.cookies.get(COOKIE))
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE, path="/")
    raise resp


async def add_note(request):
    user = require_user(request)
    form = await request.post()
    try:
        notes.create_note(
            request.app["db"],
            user["id"],
            form.get("title", ""),
            form.get("body", ""),
        )
    except ValueError as exc:
        return web.Response(text=str(exc), status=400)
    raise web.HTTPFound("/")


async def delete_note(request):
    user = require_user(request)
    notes.delete_note(
        request.app["db"], user["id"], int(request.match_info["id"])
    )
    raise web.HTTPFound("/")


def register_routes(app: web.Application) -> None:
    app.router.add_get("/", home)
    app.router.add_get("/login", login_form)
    app.router.add_post("/login", login)
    app.router.add_post("/logout", logout)
    app.router.add_post("/notes", add_note)
    app.router.add_post("/notes/{id:\\d+}/delete", delete_note)
