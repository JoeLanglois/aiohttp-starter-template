"""Views: pure functions from plain data to tdom templates.

Every function returns an unrendered Template. Compose by interpolating
Templates into Templates (values are auto-escaped; Templates are not) and
let web.py call tdom.html() exactly once, at the boundary. Nothing here
touches the db, aiohttp, or a request object.
"""

from string.templatelib import Template

# Dev-only live-CSS snippet: subscribes to the server's SSE endpoint and,
# when a stylesheet changes, points the matching <link> at a cache-busted
# URL so the browser fetches the new file without a page reload. Injected
# by _page only when dev=True — it never ships to production pages.
# (Braces are doubled because this is a t-string.)
_DEV_SNIPPET = t"""
<script>
const _dev_es = new EventSource("/__dev/events");
_dev_es.addEventListener("css", (e) => {{
  document.querySelectorAll('link[rel="stylesheet"]').forEach((link) => {{
    const u = new URL(link.href);
    if (u.pathname.endsWith("/" + e.data)) {{
      u.searchParams.set("v", Date.now());
      link.href = u;
    }}
  }});
}});
</script>"""


def _page(
    title: str,
    user: dict | None,
    content: Template,
    dev: bool = False,
    live: bool = False,
    nonce: str = "",
) -> Template:
    if user is not None:
        nav = t"""<nav>Signed in as {user["username"]}
            <form method="post" action="/logout">
                <button type="submit">Log out</button>
            </form></nav>"""
    else:
        nav = t"""<nav><a href="/login">Log in</a></nav>"""
    hot_swap = _DEV_SNIPPET if dev else t""
    # `live` pages open the push stream on load (Hyperlith-style): the
    # server re-renders <main id="app"> and fat-morphs it on every state
    # change. data-init lives on its own element, outside the morphed
    # region (and as a whole interpolated element — tdom templates must
    # keep their tags balanced within one literal).
    init = (
        t"""<div data-init="@get('/events')" hidden></div>""" if live else t""
    )
    # data-nonce carries this response's CSP nonce (set by the
    # security_headers middleware): Datastar's CSP mode reads it from
    # <html>, removes the attribute, and applies the nonce when
    # compiling expressions — which is what lets the CSP omit
    # 'unsafe-eval' entirely. See web.security_headers.
    return t"""<!doctype html>
<html lang="en" data-nonce="{nonce}">
<head><meta charset="utf-8"><title>{title}</title>
<link rel="stylesheet" href="/static/app.css">
<script type="module" src="/static/datastar.js"></script></head>
<body>{init}{nav}<main id="app">{content}</main>{hot_swap}</body>
</html>"""


def login_page(
    error: str | None = None, dev: bool = False, nonce: str = ""
) -> Template:
    err = t"""<p role="alert">{error}</p>""" if error else t""
    content = t"""<h1>Log in</h1>{err}
<form method="post" action="/login">
    <label>Username
        <input name="username" autocomplete="username" required>
    </label>
    <label>Password
        <input name="password" type="password"
               autocomplete="current-password" required>
    </label>
    <button type="submit">Log in</button>
</form>"""
    return _page("Log in", None, content, dev=dev, nonce=nonce)


def _note_item(note: dict) -> Template:
    # Each form works two ways: classic submit (POST + redirect), or —
    # with JS on — Datastar intercepts it (data-on:submit + @post) and the
    # server answers with SSE events that remove just this <li>.
    return t"""<li id="note-{note["id"]}"><strong>{note["title"]}</strong> {note["body"]}
    <form method="post" action="/notes/{note["id"]}/delete"
          data-on:submit="@post('/notes/{note["id"]}/delete')">
        <button type="submit">Delete</button>
    </form></li>"""


def notes_list(user_notes: list[dict]) -> Template:
    """The <ul> fragment the server re-patches after a Datastar add."""
    return t"""<ul id="notes">{[_note_item(n) for n in user_notes]}</ul>"""


def note_error(message: str | None) -> Template:
    return t"""<p id="note-error" role="alert">{message or ""}</p>"""


def home_content(user_notes: list[dict], error: str | None = None) -> Template:
    """Everything inside <main> on the home page — the unit of re-render
    for server push (see home_main). data-bind ties each input to a
    signal of the same name, so Datastar sends them as JSON on @post;
    without JS the same form posts classically."""
    return t"""<h1>Your notes</h1>
<form method="post" action="/notes" data-on:submit="@post('/notes')">
    <input name="title" data-bind="title" placeholder="Title" required>
    <input name="body" data-bind="body" placeholder="Body">
    <button type="submit">Add note</button>
</form>
{note_error(error)}
{notes_list(user_notes)}"""


def home_main(user_notes: list[dict], error: str | None = None) -> Template:
    """The whole <main id="app">, re-rendered and fat-morphed into the
    page on every push. Its root id is the patch target."""
    return t"""<main id="app">{home_content(user_notes, error)}</main>"""


def home_page(
    user: dict, user_notes: list[dict], dev: bool = False, nonce: str = ""
) -> Template:
    return _page(
        "Your notes",
        user,
        home_content(user_notes),
        dev=dev,
        live=True,
        nonce=nonce,
    )
