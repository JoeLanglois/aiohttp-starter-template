import pytest

from myapp import accounts, notes

PW = "correct horse battery staple"


@pytest.fixture
def alice(db):
    return accounts.create_user(db, "alice", PW)


@pytest.fixture
def bob(db):
    return accounts.create_user(db, "bob", PW)


def test_create_and_list(db, alice, bob):
    notes.create_note(db, alice["id"], "Alice note", "body")
    notes.create_note(db, bob["id"], "Bob note")
    mine = notes.list_notes(db, alice["id"])
    assert [n["title"] for n in mine] == ["Alice note"]


def test_create_note_requires_title(db, alice):
    with pytest.raises(ValueError):
        notes.create_note(db, alice["id"], "   ")


def test_delete_note_enforces_ownership(db, alice, bob):
    note = notes.create_note(db, alice["id"], "Mine")
    assert notes.delete_note(db, bob["id"], note["id"]) is False
    assert notes.list_notes(db, alice["id"])  # still there
    assert notes.delete_note(db, alice["id"], note["id"]) is True
    assert notes.list_notes(db, alice["id"]) == []
