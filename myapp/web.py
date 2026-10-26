"""Web layer (the driving adapter): aiohttp handlers and middleware.

A handler does exactly three things: parse the request, call a context,
render a view. No SQL, no business rules here. If a handler grows a
fourth step, that step probably belongs in a context.
"""

import asyncio
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from tdom import html as render

from . import accounts, datastar, notes, views

# Auth events (login ok / failed / rate-limited) go to this logger —
# the only audit trail a personal app needs. Passwords are never logged.
log = logging.getLogger("myapp.auth")

COOKIE = "sid"
STATIC_DIR = Path(__file__).parent / "static"


def _cookie_name(request) -> str:
    """The session cookie's name. With Secure cookies on, use the
    __Host- prefix: browsers then guarantee the cookie was set by this
    exact host over HTTPS, with Path=/ and no Domain (all true of the
    login handler), so no sibling subdomain can plant or shadow it."""
    return "__Host-sid" if request.app["config"].secure_cookies else COOKIE


# -- Login rate limiting (in-memory; plenty for a personal app) -----------
#
# Keyed by client IP + normalized username. Caveat: behind a reverse
# proxy, request.remote is the proxy's address, so the bucket becomes
# per-username globally — stricter, and one attacker can lock a name out
# for everyone. That trade is acceptable at this scale; if it ever
# isn't, key on a *trusted* X-Forwarded-For instead.

_FAIL_WINDOW = 15 * 60  # seconds
_FAIL_MAX = 5
_PRUNE_AT = 512  # sweep dead keys when the dict grows past this
# The store itself lives on the app (app["login_failures"], see
# main.make_app) — per-app state, not a module global, so two apps in
# one process (tests!) never share a rate-limit bucket.


def _rate_limited(failures: dict, key: str) -> bool:
    now = time.monotonic()
    recent = [t for t in failures.get(key, []) if now - t < _FAIL_WINDOW]
    failures[key] = recent
    return len(recent) >= _FAIL_MAX


def _prune_failures(failures: dict) -> None:
    """Drop keys whose attempts have all aged out — otherwise the dict
    grows by one entry per distinct (IP, username) anyone ever tries."""
    now = time.monotonic()
    dead = [
        key
        for key, stamps in failures.items()
        if not stamps or now - stamps[-1] >= _FAIL_WINDOW
    ]
    for key in dead:
        del failures[key]


def _record_failure(failures: dict, key: str) -> None:
    if len(failures) > _PRUNE_AT:
        _prune_failures(failures)
    failures.setdefault(key, []).append(time.monotonic())


# -- Middleware -------------------------------------------------------------


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@web.middleware
async def origin_check(request, handler):
    """CSRF defence-in-depth next to SameSite=Lax: a state-changing
    request whose Origin header points at another site is not ours,
    reject it. Covers every unsafe method, not just POST — a future
    PUT/DELETE route (Datastar has @put/@delete actions) must not be
    able to slip past by choice of verb."""
    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            raise web.HTTPForbidden(text="Bad origin")
    return await handler(request)


@web.middleware
async def auth_middleware(request, handler):
    token = request.cookies.get(_cookie_name(request))
    request["user"] = accounts.user_for_session(request.app["db"], token)
    return await handler(request)


@web.middleware
async def security_headers(request, handler):
    """Baseline hardening headers on every response — including
    redirects and errors, which our handlers raise as HTTPExceptions,
    hence the try/except: an exception here IS the response.

    The CSP runs Datastar in its documented *nonce mode* (Datastar
    Security Reference, data-star.dev/reference/security): Datastar
    compiles its attribute expressions with the Function constructor,
    which CSP counts as eval — so its default mode needs
    script-src 'unsafe-eval'. Instead, each response gets a fresh
    random nonce: it goes in script-src here and in a data-nonce
    attribute on <html> (views._page), Datastar reads it there and
    applies it when compiling expressions, and 'unsafe-eval' is
    never allowed. The docs' standing rule still applies and our
    views follow it: expressions are static developer-written
    constants; user values travel via signals, never interpolated
    into an expression."""
    nonce = secrets.token_urlsafe(16)
    request["csp_nonce"] = nonce
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        resp = exc
    if not resp.prepared:  # a started stream's headers are already sent
        # One policy for dev and prod. Note what does NOT work here:
        # adding 'unsafe-inline' for the dev hot-swap snippet — with a
        # nonce in the source list, browsers ignore 'unsafe-inline'
        # entirely and the snippet is blocked anyway. The snippet
        # carries the nonce itself (views._dev_snippet).
        script_src = f"'self' 'nonce-{nonce}'"
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src {script_src}; "
            "frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'; object-src 'none'",
        )
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


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
        views.home_page(
            user,
            user_notes,
            dev=request.app["config"].dev,
            nonce=request["csp_nonce"],
        )
    )


async def login_form(request):
    if request.get("user") is not None:
        raise web.HTTPFound("/")
    return html_response(
        views.login_page(
            dev=request.app["config"].dev, nonce=request["csp_nonce"]
        )
    )


async def login(request):
    form = await request.post()
    db, cfg = request.app["db"], request.app["config"]
    username = form.get("username", "")
    password = form.get("password", "")
    # The rate-limit key uses the canonical username, so case variants
    # ("Alice", "ALICE") share one bucket instead of multiplying it.
    failures = request.app["login_failures"]
    key = f"{request.remote}:{accounts.normalize_username(username)}"
    if _rate_limited(failures, key):
        log.warning(
            "login rate-limited user=%s ip=%s", username, request.remote
        )
        return html_response(
            views.login_page(
                "Too many attempts. Try again later.",
                dev=cfg.dev,
                nonce=request["csp_nonce"],
            ),
            status=429,
        )
    user = accounts.user_by_username(db, username)
    # Argon2 verify is ~100-250 ms of pure CPU: run it in a thread (never
    # block the event loop), against a dummy hash when the user doesn't
    # exist, so failures are identical in message and timing. An
    # over-long password is simply wrong: fail it without spending
    # Argon2 time on up to a megabyte of attacker input.
    pw_hash = user["pw_hash"] if user else accounts.DUMMY_HASH
    ok = len(password) <= accounts.MAX_PASSWORD_LEN and await asyncio.to_thread(
        accounts.check_password, password, pw_hash
    )
    if not (user and ok):
        _record_failure(failures, key)
        log.warning("login failed user=%s ip=%s", username, request.remote)
        return html_response(
            views.login_page(
                "Invalid username or password.",
                dev=cfg.dev,
                nonce=request["csp_nonce"],
            ),
            status=401,
        )
    failures.pop(key, None)
    log.info("login ok user=%s ip=%s", user["username"], request.remote)
    token = accounts.start_session(db, user["id"], days=cfg.session_days)
    resp = web.HTTPFound("/")
    resp.set_cookie(
        _cookie_name(request),
        token,
        httponly=True,
        secure=cfg.secure_cookies,
        samesite="Lax",
        path="/",
        max_age=cfg.session_days * 86_400,
    )
    raise resp


async def logout(request):
    name = _cookie_name(request)
    accounts.end_session(request.app["db"], request.cookies.get(name))
    resp = web.HTTPFound("/login")
    resp.del_cookie(name, path="/")
    raise resp


def _topic(user: dict) -> str:
    return f"user:{user['id']}"


def _render_home_main(request, user: dict) -> str:
    """Current <main id="app"> for this user, as a string — the render
    function the push stream fat-morphs. Reads fresh state every call."""
    error = request.app["note_errors"].get(user["id"])
    user_notes = notes.list_notes(request.app["db"], user["id"])
    return render(views.home_main(user_notes, error))


MAX_STREAMS_PER_USER = 10  # tabs are cheap; a flood of streams is not


async def ui_events(request):
    """The push stream (opened by data-init on live pages). Subscribes
    this connection to the user's bus topic and fat-morphs <main> on
    every change until the client disconnects — unsubscribing is the
    stream generator's finally, via the adapter."""
    user = require_user(request)
    topic = _topic(user)
    # Cap concurrent streams per user: each one holds a render closure
    # and a compressor, and every publish fans out to all of them.
    if request.app["bus"].subscriber_count(topic) >= MAX_STREAMS_PER_USER:
        raise web.HTTPTooManyRequests(text="Too many open streams")
    stream = datastar.render_stream(
        request.app["bus"],
        topic,
        lambda: _render_home_main(request, user),
    )
    return datastar.DatastarResponse(
        stream, compress=datastar.accepts_brotli(request)
    )


async def add_note(request):
    user = require_user(request)
    db = request.app["db"]
    if datastar.is_datastar_request(request):
        # CQRS, Hyperlith-style: mutate, publish, answer almost nothing.
        # Open pages (this one included) re-render via the push stream.
        # The one direct reply is patch_signals clearing the form
        # inputs, since the morphed fragment can't reset typed values.
        signals = await datastar.read_signals(request) or {}
        try:
            notes.create_note(
                db, user["id"], signals.get("title", ""), signals.get("body", "")
            )
        except ValueError as exc:
            # The error is UI state: stash it, let the re-render show it.
            request.app["note_errors"][user["id"]] = str(exc)
            request.app["bus"].publish(_topic(user))
            return datastar.DatastarResponse()
        request.app["note_errors"].pop(user["id"], None)
        request.app["bus"].publish(_topic(user))
        return datastar.DatastarResponse(
            datastar.ServerSentEventGenerator.patch_signals(
                {"title": "", "body": ""}
            )
        )
    form = await request.post()
    try:
        notes.create_note(
            db,
            user["id"],
            form.get("title", ""),
            form.get("body", ""),
        )
    except ValueError as exc:
        return web.Response(text=str(exc), status=400)
    request.app["bus"].publish(_topic(user))
    raise web.HTTPFound("/")


async def delete_note(request):
    user = require_user(request)
    notes.delete_note(
        request.app["db"], user["id"], int(request.match_info["id"])
    )
    request.app["bus"].publish(_topic(user))
    if datastar.is_datastar_request(request):
        return datastar.DatastarResponse()  # 204 — the push does the rest
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
# Registered and started only when config.dev is on. The server reuses
# watchfiles (already the supervisor's dependency) with a low debounce,
# so a save is pushed to connected pages over SSE within a few tens of
# milliseconds — no polling, no restart (Python changes are the
# supervisor's job). Pages get a `css` event naming the file, and the
# snippet in views.py swaps in a cache-busted <link>.


def _css_only(change, path: str) -> bool:
    return path.endswith(".css")


async def dev_events(request):
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    # Same per-event Brotli treatment as the Datastar stream (see
    # datastar.DatastarResponse): flush after every event so pushes
    # are never held back by the compressor.
    encoder = None
    if datastar.accepts_brotli(request):
        import brotli

        encoder = brotli.Compressor(quality=4)
        headers["Content-Encoding"] = "br"
    resp = web.StreamResponse(headers=headers)
    await resp.prepare(request)

    async def send(data: bytes) -> None:
        if encoder is not None:
            data = encoder.process(data) + encoder.flush()
        if data:
            await resp.write(data)

    queue: asyncio.Queue = asyncio.Queue()
    request.app["dev_queues"].add(queue)
    try:
        await send(b": connected\n\n")
        while True:
            name = await queue.get()
            await send(f"event: css\ndata: {name}\n\n".encode())
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        request.app["dev_queues"].discard(queue)
    return resp


async def _watch_static_css(app: web.Application) -> None:
    from watchfiles import awatch

    async for changes in awatch(
        STATIC_DIR, watch_filter=_css_only, debounce=50, step=25
    ):
        for name in sorted({Path(p).name for _, p in changes}):
            for queue in list(app["dev_queues"]):
                queue.put_nowait(name)


async def start_dev_watcher(app: web.Application) -> None:
    try:
        import watchfiles  # noqa: F401 — dev dependency; absent in prod installs
    except ImportError:
        print("[dev] watchfiles not installed — CSS hot-swap disabled")
        return
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
    app.router.add_get("/events", ui_events)
    app.router.add_get("/static/{filename:.+}", static_file)
    if app["config"].dev:
        app.router.add_get("/__dev/events", dev_events)
