import sys

if "--dev" in sys.argv[1:]:
    from .dev import run_dev

    run_dev()
else:
    from .main import main

    main()
