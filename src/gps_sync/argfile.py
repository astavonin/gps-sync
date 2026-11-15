"""Argfile emission with the absolute-path invariant checked immediately before every write.

An argfile line beginning with ``-`` is read by exiftool as an option, so every path reaching
exiftool is absolute (begins with ``/``) and is checked here, at the boundary that depends on it.
"""

import os
from collections.abc import Sequence
from pathlib import Path

from gps_sync.errors import InternalConsistencyError, ToolFailure


def path_line(path: str) -> str:
    """Return ``path`` unchanged after proving it is safe to emit as a literal argfile line."""
    if not path.startswith("/"):
        raise InternalConsistencyError(f"refusing to emit non-absolute path {path!r}")
    if "\n" in path or "\r" in path:
        raise InternalConsistencyError(f"refusing to emit path containing a line break: {path!r}")
    return path


def is_representable(path: str) -> bool:
    """Return whether a path can travel as a literal argfile line at all."""
    return path.startswith("/") and "\n" not in path and "\r" not in path


def emit_argfile(destination: Path, lines: Sequence[str]) -> None:
    """Write argfile lines to ``destination`` (UTF-8, newline separated)."""
    try:
        destination.write_bytes(os.fsencode("\n".join(lines) + "\n"))
    except OSError as error:
        raise ToolFailure(f"cannot write argfile {destination}: {error}") from None
