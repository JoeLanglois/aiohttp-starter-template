"""Guard the fast-shutdown settings: aiohttp's defaults (60 s drain,
no handler cancellation) make Ctrl-C and dev restarts feel hung."""

from pathlib import Path

from myapp import main
from myapp.config import Config


def test_main_uses_fast_shutdown(monkeypatch):
    captured = {}

    def fake_run_app(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(main.web, "run_app", fake_run_app)
    monkeypatch.setattr(
        main.Config,
        "from_env",
        classmethod(lambda cls: Config(db_path=Path(":memory:"))),
    )
    main.main()
    assert captured["shutdown_timeout"] <= 0.5
    assert captured["handler_cancellation"] is True


def test_dev_module_imports_without_watchfiles_side_effects():
    # watchfiles is imported lazily inside run_dev(); importing the
    # module (and the --dev dispatch) must always work.
    from myapp import dev

    assert callable(dev.run_dev)
