"""The ``gps-sync`` console entry point."""

import sys
from collections.abc import Sequence

from gps_sync.errors import ToolFailure
from gps_sync.options import parse_options
from gps_sync.orchestrator import run


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run, and return the process exit code."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        options = parse_options(arguments)
    except ToolFailure as failure:
        print(f"gps-sync: error: {failure}", file=sys.stderr)
        return 2
    except SystemExit as exit_request:
        code = exit_request.code
        return code if isinstance(code, int) else 0
    return run(options)
