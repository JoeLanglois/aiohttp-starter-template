"""Management CLI: a second driving adapter over the same contexts.

There is deliberately NO public signup endpoint. Users are created here:

    python -m myapp.cli create-user alice
    python -m myapp.cli reset-password alice      # also revokes sessions
    python -m myapp.cli logout-everywhere alice
"""

import argparse
import getpass
import sys

from . import accounts
from .config import Config
from .db import open_db


def _ask_new_password() -> str | None:
    pw = getpass.getpass("Password (12+ chars): ")
    if pw != getpass.getpass("Repeat: "):
        print("Passwords don't match.")
        return None
    return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="myapp.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-user").add_argument("username")
    sub.add_parser("reset-password").add_argument("username")
    sub.add_parser("logout-everywhere").add_argument("username")
    args = parser.parse_args(argv)

    db = open_db(Config.from_env().db_path)

    if args.cmd == "create-user":
        pw = _ask_new_password()
        if pw is None:
            return 1
        try:
            user = accounts.create_user(db, args.username, pw)
        except ValueError as exc:
            print(exc)
            return 1
        print(f"Created user #{user['id']} ({user['username']})")
        return 0

    user = accounts.user_by_username(db, args.username)
    if user is None:
        print("No such user.")
        return 1

    if args.cmd == "reset-password":
        pw = _ask_new_password()
        if pw is None:
            return 1
        try:
            accounts.change_password(db, user["id"], pw)
        except ValueError as exc:
            print(exc)
            return 1
        print("Password changed; all sessions revoked.")
    elif args.cmd == "logout-everywhere":
        accounts.end_all_sessions(db, user["id"])
        print("All sessions revoked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
