import sys

if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11+ required / Se necesita Python 3.11+ (Ubuntu 24.04, Debian 12/13)')

from .cli import main

raise SystemExit(main())
