"""Entry point for the `myapp` console script (and `python -m myapp`).

Dispatches to the dev supervisor when `--dev` is present, otherwise to
the real server. Kept as a callable so pyproject's [project.scripts]
can point at it.
"""

import sys


def main() -> None:
    if "--dev" in sys.argv[1:]:
        from .dev import run_dev

        run_dev()
    else:
        from .main import main as serve

        serve()


if __name__ == "__main__":
    main()
