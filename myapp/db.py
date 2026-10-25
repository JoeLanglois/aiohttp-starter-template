"""Database setup: opening the SQLite file and running migrations.

Migrations use sqlite-utils' built-in Migrations (forward-only: never edit
an applied migration, add a new one). Contexts receive an open Database
from here and never open files themselves.
"""

from pathlib import Path

from sqlite_utils import Database, Migrations

migrations = Migrations("myapp")


@migrations()
def create_users_and_sessions(db):
    db["users"].create(
        {"id": int, "username": str, "pw_hash": str, "created_at": str},
        pk="id",
    )
    db["users"].create_index(["username"], unique=True)
    db["sessions"].create(
        {
            "token_hash": str,  # SHA-256 of the session token, never the token
            "user_id": int,
            "created_at": str,
            "expires_at": str,
        },
        pk="token_hash",
        foreign_keys=[("user_id", "users", "id")],
    )


@migrations()
def create_notes(db):
    db["notes"].create(
        {
            "id": int,
            "user_id": int,
            "title": str,
            "body": str,
            "created_at": str,
        },
        pk="id",
        foreign_keys=[("user_id", "users", "id")],
    )


def open_db(path: str | Path) -> Database:
    if str(path) == ":memory:":
        db = Database(memory=True)
    else:
        db = Database(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    migrations.apply(db)
    return db
