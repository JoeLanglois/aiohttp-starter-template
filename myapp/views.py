"""Views: pure functions from plain data to tdom templates.

Every function returns an unrendered Template. Compose by interpolating
Templates into Templates (values are auto-escaped; Templates are not) and
let web.py call tdom.html() exactly once, at the boundary. Nothing here
touches the db, aiohttp, or a request object.
"""

from string.templatelib import Template


def _page(title: str, user: dict | None, content: Template) -> Template:
    if user is not None:
        nav = t"""<nav>Signed in as {user["username"]}
            <form method="post" action="/logout">
                <button type="submit">Log out</button>
            </form></nav>"""
    else:
        nav = t"""<nav><a href="/login">Log in</a></nav>"""
    return t"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>{nav}<main>{content}</main></body>
</html>"""


def login_page(error: str | None = None) -> Template:
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
    return _page("Log in", None, content)


def _note_item(note: dict) -> Template:
    return t"""<li><strong>{note["title"]}</strong> {note["body"]}
    <form method="post" action="/notes/{note['id']}/delete">
        <button type="submit">Delete</button>
    </form></li>"""


def home_page(user: dict, user_notes: list[dict]) -> Template:
    content = t"""<h1>Your notes</h1>
<form method="post" action="/notes">
    <input name="title" placeholder="Title" required>
    <input name="body" placeholder="Body">
    <button type="submit">Add note</button>
</form>
<ul>{[_note_item(n) for n in user_notes]}</ul>"""
    return _page("Your notes", user, content)
