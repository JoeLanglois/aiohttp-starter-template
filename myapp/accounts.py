"""Accounts context: users and sessions — i.e. all of auth.

This module's public functions are the ONLY way other code (web handlers,
the CLI, other contexts) touches users and sessions. It knows nothing
about aiohttp or HTTP: functions take plain values (db first, always) and
return plain dicts.

Security recipe implemented here:
- Passwords hashed with Argon2id via pwdlib (never a fast hash).
- Sessions are random 256-bit tokens; only their SHA-256 is stored, so a
  leaked database contains neither usable passwords nor live sessions.
- Sessions expire absolutely (default 30 days) and die server-side at
  logout / password change — real revocation, not just cookie deletion.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash

_ph = PasswordHash.recommended()  # Argon2id with maintained defaults

MIN_PASSWORD_LEN = 12

# A real Argon2id hash of a random throwaway password. authenticate()
# verifies against it when the username doesn't exist, so "no such user"
# costs the same time as "wrong password" and leaks nothing through
# timing. (The error message is identical too — see web.py.)
DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$k3XJ+nX76L1nrZclsjBKGg"
    "$vBPqP8uEEQSbgOoKZ1GuDaD7pG2TJ5WMOcGwL/UyQns"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _one(db, table: str, where: str, params: list) -> dict | None:
    rows = list(db[table].rows_where(where, params))
    return rows[0] if rows else None


# -- Passwords ------------------------------------------------------------


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LEN} characters."
        )
    return _ph.hash(password)


def check_password(password: str, pw_hash: str) -> bool:
    """Pure and CPU-heavy (~100-250 ms of Argon2). The web layer runs this
    in a thread so it never blocks the event loop; sync callers (CLI,
    tests) can just call it directly."""
    return _ph.verify(password, pw_hash)


# -- Users ----------------------------------------------------------------


def create_user(db, username: str, password: str) -> dict:
    username = username.strip()
    if not username:
        raise ValueError("Username is required.")
    if user_by_username(db, username) is not None:
        raise ValueError("Username already taken.")
    db["users"].insert(
        {
            "username": username,
            "pw_hash": hash_password(password),
            "created_at": _now().isoformat(),
        }
    )
    return user_by_username(db, username)


def user_by_username(db, username: str) -> dict | None:
    return _one(db, "users", "username = ?", [username.strip()])


def user_by_id(db, user_id: int) -> dict | None:
    return _one(db, "users", "id = ?", [user_id])


def authenticate(db, username: str, password: str) -> dict | None:
    """Return the user dict on success, None on any failure.

    Sync convenience wrapper (CLI, tests). The web layer deliberately
    does NOT call this: it composes user_by_username + check_password so
    the Argon2 verify can run in a worker thread.
    """
    user = user_by_username(db, username)
    ok = check_password(password, user["pw_hash"] if user else DUMMY_HASH)
    return user if (user and ok) else None


def change_password(db, user_id: int, new_password: str) -> None:
    db["users"].update(user_id, {"pw_hash": hash_password(new_password)})
    end_all_sessions(db, user_id)  # a password change logs out everywhere


# -- Sessions -------------------------------------------------------------


def _token_hash(token: str) -> str:
    # A fast hash is correct here: unlike a human password, the token is
    # 256 bits of randomness, so there is nothing to brute-force.
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db, user_id: int, days: int = 30) -> str:
    """Create a session and return the raw token (goes in the cookie).
    Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    now = _now()
    db["sessions"].insert(
        {
            "token_hash": _token_hash(token),
            "user_id": user_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=days)).isoformat(),
        }
    )
    return token


def user_for_session(db, token: str | None) -> dict | None:
    if not token:
        return None
    session = _one(db, "sessions", "token_hash = ?", [_token_hash(token)])
    if session is None:
        return None
    if session["expires_at"] <= _now().isoformat():
        end_session(db, token)
        return None
    return user_by_id(db, session["user_id"])


def end_session(db, token: str | None) -> None:
    if token:
        db["sessions"].delete_where("token_hash = ?", [_token_hash(token)])


def end_all_sessions(db, user_id: int) -> None:
    db["sessions"].delete_where("user_id = ?", [user_id])
