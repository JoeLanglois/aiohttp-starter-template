"""Notes context: a toy feature that shows the context pattern.

One feature = one context module. Business rules AND their SQL live here
(Phoenix contexts talk to the database directly — there is deliberately
no repository layer underneath). Handlers never write SQL.
"""

from datetime import datetime, timezone


def create_note(db, user_id: int, title: str, body: str = "") -> dict:
    title = title.strip()
    if not title:
        raise ValueError("Title is required.")
    db["notes"].insert(
        {
            "user_id": user_id,
            "title": title,
            "body": body.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    # sqlite-utils' insert() returns the table, not the new pk.
    note_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return list(db["notes"].rows_where("id = ?", [note_id]))[0]


def list_notes(db, user_id: int) -> list[dict]:
    return list(
        db["notes"].rows_where("user_id = ?", [user_id], order_by="id desc")
    )


def delete_note(db, user_id: int, note_id: int) -> bool:
    """Delete one of the user's own notes. Ownership is enforced here, in
    the context, so a handler can never forget the check. Returns True if
    a note was actually deleted."""
    cursor = db.execute(
        "DELETE FROM notes WHERE id = ? AND user_id = ?", [note_id, user_id]
    )
    return cursor.rowcount > 0
