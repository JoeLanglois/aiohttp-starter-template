"""Context tests: plain function calls against a real (temp) SQLite file.
No HTTP, no mocks — this is the payoff of the context boundary."""

import pytest

from myapp import accounts

PW = "correct horse battery staple"


def test_create_and_authenticate(db):
    user = accounts.create_user(db, "alice", PW)
    assert user["username"] == "alice"
    assert user["pw_hash"].startswith("$argon2id$")
    assert accounts.authenticate(db, "alice", PW)["id"] == user["id"]


def test_authenticate_wrong_password(db):
    accounts.create_user(db, "alice", PW)
    assert accounts.authenticate(db, "alice", "wrong wrong wrong") is None


def test_authenticate_unknown_user(db):
    # Exercises the dummy-hash path: must fail, and must not raise.
    assert accounts.authenticate(db, "nobody", PW) is None


def test_create_user_duplicate_and_short_password(db):
    accounts.create_user(db, "alice", PW)
    with pytest.raises(ValueError):
        accounts.create_user(db, "alice", PW)
    with pytest.raises(ValueError):
        accounts.create_user(db, "bob", "short")


def test_session_lifecycle(db):
    user = accounts.create_user(db, "alice", PW)
    token = accounts.start_session(db, user["id"])
    assert accounts.user_for_session(db, token)["id"] == user["id"]
    accounts.end_session(db, token)
    assert accounts.user_for_session(db, token) is None


def test_session_token_is_not_stored_in_clear(db):
    user = accounts.create_user(db, "alice", PW)
    token = accounts.start_session(db, user["id"])
    stored = [r["token_hash"] for r in db["sessions"].rows]
    assert token not in stored  # only the SHA-256 lands in the db


def test_expired_session_is_rejected(db):
    user = accounts.create_user(db, "alice", PW)
    token = accounts.start_session(db, user["id"], days=-1)  # already over
    assert accounts.user_for_session(db, token) is None


def test_change_password_revokes_sessions(db):
    user = accounts.create_user(db, "alice", PW)
    token = accounts.start_session(db, user["id"])
    accounts.change_password(db, user["id"], "a brand new passphrase")
    assert accounts.user_for_session(db, token) is None
    assert accounts.authenticate(db, "alice", PW) is None
    assert accounts.authenticate(db, "alice", "a brand new passphrase")
