"""Allow ``python -m gps_sync``."""

import sys

from gps_sync.cli import main

if __name__ == "__main__":
    sys.exit(main())
