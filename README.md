# myapp starter template

A small, boring, secure-by-default starting point for personal web apps:

- **aiohttp** for HTTP (async, stdlib-adjacent, plenty fast at this scale)
- **SQLite via sqlite-utils** for persistence (tables as dicts, bundled
  forward-only migrations)
- **tdom** for templates (Python 3.14 t-strings: real HTML inline,
  auto-escaped, composed as values)
- **Username/password auth done properly** (Argon2id + server-side
  sessions), with a CLI instead of a public signup page
- **Datastar included** (vendored client + the official Python SDK),
  with an aiohttp adapter — the demo notes UI updates over SSE with no
  page reload, and still works with JS off

Python **3.14+** is required (tdom uses t-strings).

## Quick start

```bash
uv sync                                   # create .venv, install deps
uv run python -m myapp.cli create-user alice
uv run python -m myapp                    # http://127.0.0.1:8080
uv run python -m myapp --dev              # same, + auto-reload on .py changes
uv run pytest                             # context tests + HTTP smoke tests
```

Dev mode (`--dev`) supervises the server with
[watchfiles](https://watchfiles.helpmanual.io/): any `.py` change under
the project restarts it (measured save → serving new code: ~0.5 s). Two
settings make that instant rather than theoretical: the supervisor
escalates SIGINT → SIGKILL after 1 s, and the server itself shuts down
fast — aiohttp's `run_app` defaults wait up to **60 s** for connections
and handlers to drain (an open SSE stream or websocket makes Ctrl-C look
hung), so `main.py` sets `shutdown_timeout=0.1` and
`handler_cancellation=True`. Measured: total shutdown is roughly
2 × `shutdown_timeout` — ~0.3 s worst case even with a never-ending
handler in flight, ~0.1 s normally. (And never set it to 0: aiohttp
treats that as "no timeout" and hangs.) Nothing in this stack needs a
long drain; its writes are local SQLite.

Log in, add a note, delete it, log out. That's the whole demo feature —
its job is to show where code goes, then be replaced by your real one.

## Architecture: mini-hexagonal, Phoenix-context style

Three kinds of code, one direction of dependencies:

```
        web.py  (handlers, middleware)      views.py (tdom templates)
            \            parses, delegates, renders        /
             \                                            /
              ───►  accounts.py   notes.py  ◄── contexts
                         │  business rules + SQL, plain dicts in/out
                         ▼
                    db.py (open + migrations)   SQLite file
        main.py wires it all together; cli.py is a second adapter
        calling the same contexts as the web layer.
```

### The three rules (the entire architecture)

1. **Dependencies point one way.** `web.py` imports contexts; contexts
   never import aiohttp, tdom, or see a `request`. `main.py` is the only
   module that knows every layer.
2. **Contexts are the only public API.** Handlers, the CLI, and other
   contexts call `accounts.authenticate(...)`,
   `notes.create_note(...)` — never each other's tables or SQL directly.
3. **The db handle is the first argument.** `notes.create_note(db, ...)`
   is the whole dependency-injection framework. In tests you pass a
   database opened on a temp file; no mocks, no container.

### What deliberately does NOT exist

- No repository layer — contexts talk to sqlite-utils directly, like
  Phoenix contexts talk to the Repo. Rows are already dicts.
- No DTOs, mappers, entity classes, unit-of-work, or event bus.
- No interface/`Protocol` per collaborator. Introduce one only when a
  *second* implementation actually shows up (a real one, or a test fake
  for an external side effect like email). SQLite is not a port: you'll
  never swap it, and rule 1 already shields you from the realistic
  change (the web framework).

### Adding a feature

1. Add a context module (or extend one): functions `verb_noun(db, ...)`
   containing the rules and the SQL, raising `ValueError` with a
   user-readable message for domain errors.
2. Add a view function returning a `Template` (compose Templates into
   Templates; render once, in `web.html_response`).
3. Add a thin handler (parse → call context → render/redirect) and one
   route in `web.register_routes`.
4. Add context tests; extend `tests/test_web.py` only for smoke-level
   behaviour.

When a context file passes ~400–500 lines, turn it into a package and
re-export the same public functions from its `__init__.py`. The API
doesn't change, only the filing.

## Static files

Put files in `myapp/static/`; they're served at `/static/<name>`, and
the default stylesheet (`app.css`) is already linked from every page.
Serving is aiohttp's `FileResponse` (sendfile, ranges,
ETag/Last-Modified), so production caching behaves normally.

## Dev extras: CSS hot-swap, no browser cache

Under `--dev`, the supervisor exports `APP_DEV=1` to the server, which
turns on three dev-only behaviors:

- **No-store static**: every `/static/` response carries
  `Cache-Control: no-store`, so the browser never keeps a cached copy —
  what loads is always what's on disk right now.
- **CSS hot-swap**: the server watches `myapp/static/` with
  [watchfiles](https://watchfiles.helpmanual.io/) (the supervisor's own
  dependency, reused) at a low 50 ms debounce and pushes an SSE event to
  open pages via `/__dev/events` — measured save → event in ~30 ms. A
  snippet injected into pages (dev only) swaps the changed stylesheet
  for a cache-busted URL, so new styles apply instantly — no page
  reload, no server restart. Python changes still restart via the
  supervisor, as above.
- The SSE endpoint and the snippet **don't exist outside dev mode**.

## Datastar

The client is vendored at `myapp/static/datastar.js` (v1.0.4, pinned —
no CDN, works offline) and loaded as a module on every page. Server
side uses the official Python SDK (`datastar-py`, v1.0.3) through
`myapp/datastar.py`, an **aiohttp adapter written for this template**
(the SDK ships Starlette/Quart/FastAPI/Django integrations, but not
aiohttp). It mirrors theirs:

- `DatastarResponse(events…)` — 0..N SDK events; empty → 204, otherwise
  200 + SSE headers. Accepts a single event, a list, or an async
  generator for open-ended streams.
- `read_signals(request)` — signals dict (query param on GET, JSON body
  otherwise), or None for non-Datastar requests.
- `is_datastar_request(request)` and a `datastar_response` decorator.

The notes demo shows the pattern: the add/delete forms carry
`data-on:submit="@post(...)"` and `data-bind` inputs. With JS on,
Datastar posts signals to the *same routes*; the handlers detect it.
With JS off, the identical forms submit classically and get redirects.
One route table, both worlds. For one-shot replies, handlers can also
return SDK events directly in the action's response (the demo uses this
only to clear the form inputs via `patch_signals`).

### Server push: the Hyperlith model

The demo's updates don't actually come from the action responses —
they're pushed, Hyperlith-style:

- Pages opt in with `data-init="@get('/events')"` (see `views._page`'s
  `live` flag), opening a long-lived SSE stream per tab.
- `myapp/bus.py` is a tiny in-process pub/sub: `subscribe(topic)`
  returns a size-1 coalescing queue, `publish(topic)` nudges every
  subscriber, `unsubscribe` cleans up. One topic per user here.
- Actions are CQRS-pure: mutate via the context, `bus.publish(...)`,
  answer 204 (validation errors become state the next render shows).
- The stream is the adapter's `render_stream(bus, topic, render)`:
  it yields `render()` — the *whole* `<main id="app">`, fat morph, no
  server-side diffing — immediately and after every publish, skipping
  renders that didn't change (string compare; the SHA-256 of the HTML
  rides along as the SSE event id, as Hyperlith does).
- When a client disconnects, the response closes the generator and
  its `finally` unsubscribes: no leaked subscribers, ever. A 30 s
  heartbeat comment keeps idle streams alive through proxies.

Single process, state re-read from SQLite on every render — the whole
thing is `bus.py` (~50 lines) plus one generator in the adapter.

## Auth, concretely

- Passwords: Argon2id via `pwdlib` (`PasswordHash.recommended()`),
  salt+params embedded in the stored string. Minimum length 12, no
  composition rules, no forced rotation.
- Login verify runs `accounts.check_password` in a worker thread —
  Argon2 is ~100–250 ms of CPU and must never block the event loop.
  Unknown usernames verify against a dummy hash, and failures are
  identical in message and timing. 5 failures per IP+username within
  15 minutes → 429.
- Sessions: `secrets.token_urlsafe(32)` in an
  `HttpOnly; Secure*; SameSite=Lax` cookie; only its SHA-256 is stored
  (`accounts._token_hash`), with an absolute 30-day expiry. Logout and
  password changes delete the server-side row: revocation is real.
  (*`Secure` only when `APP_SECURE_COOKIES=1` — see Production.)
- POSTs with a foreign `Origin` header are rejected (middleware), as
  defence-in-depth next to `SameSite=Lax`.
- No signup, password-reset, or email flows. The CLI is the admin:
  `create-user`, `reset-password`, `logout-everywhere`.

## Configuration (environment)

| Variable             | Default     | Meaning                                  |
|----------------------|-------------|------------------------------------------|
| `APP_DB`             | `app.db`    | SQLite file path                         |
| `APP_HOST`           | `127.0.0.1` | Bind host                                |
| `APP_PORT`           | `8080`      | Bind port                                |
| `APP_SECURE_COOKIES` | `0`         | `1` = Secure session cookies (HTTPS)     |
| `APP_SESSION_DAYS`   | `30`        | Absolute session lifetime                |
| `APP_DEV`            | `0`         | Dev extras; set for you by `--dev`       |

## Production checklist (the short version)

- Put it behind a TLS-terminating reverse proxy (e.g. Caddy) and set
  `APP_SECURE_COOKIES=1`. Session cookies over plain HTTP are the one
  thing standing between this template and a stolen session.
- Backups = copy the `.db` file (WAL mode: use `sqlite3 app.db .backup
  backup.db` or stop the app first).
- That's… mostly it. It's one process and one file.
