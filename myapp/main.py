"""Composition root: the only module that knows about every layer.

Reads config, opens the database (which runs migrations), builds the
aiohttp app. Everything else receives what it needs — the db handle lives
on app["db"], config on app["config"].
"""

from aiohttp import web

from .config import Config
from .db import open_db
from .web import auth_middleware, origin_check, register_routes


def make_app(config: Config | None = None) -> web.Application:
    config = config or Config.from_env()
    app = web.Application(middlewares=[origin_check, auth_middleware])
    app["config"] = config
    app["db"] = open_db(config.db_path)

    async def _close_db(app_: web.Application) -> None:
        app_["db"].conn.close()

    app.on_cleanup.append(_close_db)
    register_routes(app)
    return app


def main() -> None:
    cfg = Config.from_env()
    web.run_app(
        make_app(cfg),
        host=cfg.host,
        port=cfg.port,
        # Fast shutdown, on purpose. run_app's defaults park for up to
        # shutdown_timeout=60 s waiting for connections/handlers to drain
        # — with a browser keep-alive connection, and especially with a
        # long-lived handler (SSE stream, websocket), Ctrl-C and dev-mode
        # restarts appear to hang. handler_cancellation cancels in-flight
        # handlers when shutdown starts instead of waiting on them.
        # Measured on this stack: total shutdown time is roughly
        # 2 × shutdown_timeout, so 0.1 s here means ~0.3 s worst case even
        # with a never-ending handler in flight — and 0 would be a trap
        # (aiohttp treats it as "no timeout" and hangs). 100 ms of grace
        # is plenty for an app whose writes are local SQLite.
        shutdown_timeout=0.1,
        handler_cancellation=True,
    )


if __name__ == "__main__":
    main()
