"""Web layer (the driving adapter): aiohttp handlers and middleware.

A handler does exactly three things: parse the request, call a context,
render a view. No SQL, no business rules here. If a handler grows a
fourth step, that step probably belongs in a context.
"""

import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from tdom import html as render

from . import accounts, notes, views

COOKIE = "sid"
STATIC_DIR = Path(__file__).parent / "static"

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
    return html_response(
        views.home_page(user, user_notes, dev=request.app["config"].dev)
    )


async def login_form(request):
    if request.get("user") is not None:
        raise web.HTTPFound("/")
    return html_response(views.login_page(dev=request.app["config"].dev))


async def login(request):
    form = await request.post()
    db, cfg = request.app["db"], request.app["config"]
    username = form.get("username", "")
    key = f"{request.remote}:{username}"
    if _rate_limited(key):
        return html_response(
            views.login_page(
                "Too many attempts. Try again later.", dev=cfg.dev
            ),
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
            views.login_page("Invalid username or password.", dev=cfg.dev),
            status=401,
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


# -- Static files -------------------------------------------------------------


async def static_file(request):
    """Serve files from myapp/static/. FileResponse gives us sendfile,
    ranges, content type, ETag/Last-Modified in production. In dev mode we
    stamp `Cache-Control: no-store` on every static response, so the
    browser never keeps a copy at all — every request (and every CSS
    hot-swap) fetches the file as it exists on disk right now."""
    path = (STATIC_DIR / request.match_info["filename"]).resolve()
    if STATIC_DIR.resolve() not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    if request.app["config"].dev:
        resp.headers["Cache-Control"] = "no-store"
    return resp


# -- Dev-only CSS hot swap ----------------------------------------------------
#
# Registered and started only when config.dev is on. A background task
# polls the static dir's CSS mtimes (0.25 s — no watcher dependency in the
# server, and Python-file changes are already handled by the supervisor
# restarting us). Connected pages get an SSE `css` event naming the file,
# and the snippet in views.py swaps in a cache-busted <link>.

_CSS_POLL_SECONDS = 0.25


async def dev_events(request):
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)
    queue: asyncio.Queue = asyncio.Queue()
    request.app["dev_queues"].add(queue)
    try:
        await resp.write(b": connected\n\n")
        while True:
            name = await queue.get()
            await resp.write(f"event: css\ndata: {name}\n\n".encode())
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        request.app["dev_queues"].discard(queue)
    return resp


async def _watch_static_css(app: web.Application) -> None:
    def snapshot() -> dict[Path, float]:
        return {p: p.stat().st_mtime for p in STATIC_DIR.glob("*.css")}

    seen = snapshot()
    while True:
        await asyncio.sleep(_CSS_POLL_SECONDS)
        current = snapshot()
        changed = [p.name for p, m in current.items() if seen.get(p) != m]
        seen = current
        for name in changed:
            for queue in list(app["dev_queues"]):
                queue.put_nowait(name)


async def start_dev_watcher(app: web.Application) -> None:
    app["dev_watcher"] = asyncio.create_task(_watch_static_css(app))


async def stop_dev_watcher(app: web.Application) -> None:
    task = app.get("dev_watcher")
    if task is not None:
        task.cancel()


def register_routes(app: web.Application) -> None:
    app.router.add_get("/", home)
    app.router.add_get("/login", login_form)
    app.router.add_post("/login", login)
    app.router.add_post("/logout", logout)
    app.router.add_post("/notes", add_note)
    app.router.add_post("/notes/{id:\\d+}/delete", delete_note)
    app.router.add_get("/static/{filename:.+}", static_file)
    if app["config"].dev:
        app.router.add_get("/__dev/events", dev_events)
