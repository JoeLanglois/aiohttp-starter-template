"""Runtime configuration, from environment variables.

One frozen dataclass, constructed in exactly one place (main.make_app or
the CLI). Contexts never read the environment themselves.
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    db_path: Path = Path("app.db")
    host: str = "127.0.0.1"
    port: int = 8080
    # Set APP_SECURE_COOKIES=1 in production, behind HTTPS. Secure cookies
    # are never sent over plain HTTP, so login breaks on http://localhost
    # if this is on during development — hence the explicit switch.
    secure_cookies: bool = False
    session_days: int = 30

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=Path(os.environ.get("APP_DB", "app.db")),
            host=os.environ.get("APP_HOST", "127.0.0.1"),
            port=int(os.environ.get("APP_PORT", "8080")),
            secure_cookies=os.environ.get("APP_SECURE_COOKIES", "0") == "1",
            session_days=int(os.environ.get("APP_SESSION_DAYS", "30")),
        )
