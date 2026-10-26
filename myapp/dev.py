"""Dev mode: auto-reload on .py changes.

    python -m myapp --dev

A tiny supervisor (watchfiles' run_process) runs the real server,
myapp.main.main, in a child process and restarts it whenever a Python
file under the project changes. The child is stopped with SIGINT — which
aiohttp handles as a graceful shutdown — escalating to SIGKILL after
1 s, so a restart is never held hostage by a stuck server. Combined with
the fast-shutdown settings in main.py, a save-to-restart cycle is
typically well under a second.

Dev-only: watchfiles lives in the dev dependency group and is imported
lazily here, so production never touches it.
"""

import os
from pathlib import Path


def _announce(changes) -> None:
    names = sorted({Path(path).name for _, path in changes})
    print(f"[dev] {', '.join(names)} changed — restarting", flush=True)


def run_dev() -> None:
    try:
        from watchfiles import PythonFilter, run_process
    except ImportError:
        raise SystemExit(
            "Dev mode needs watchfiles (dev dependency group): "
            "run `uv sync` or `pip install watchfiles`."
        )
    root = Path(__file__).resolve().parent.parent
    # The server child inherits our environment: this is what turns on its
    # dev extras (SSE CSS hot-swap endpoint + watcher, no-store static).
    os.environ["APP_DEV"] = "1"
    print(f"[dev] watching {root} for .py changes — Ctrl-C to stop")
    run_process(
        root,
        target="myapp.main.main",  # watchfiles wants a dotted path, not :
        target_type="function",
        watch_filter=PythonFilter(),  # .py only (also skips .venv, .git…)
        debounce=200,  # ms to batch a burst of saves (default is 1600!)
        step=25,  # ms between filesystem polls for the change queue
        sigint_timeout=1,  # then SIGKILL: restarts stay instant, always
        sigkill_timeout=1,
        callback=_announce,
    )
