"""Argfile emission with the absolute-path invariant checked immediately before every write.

An argfile line beginning with ``-`` is read by exiftool as an option, so every path reaching
exiftool is absolute (begins with ``/``) and is checked here, at the boundary that depends on it.
"""

import os
from collections.abc import Sequence
from pathlib import Path

from gps_sync.errors import InternalConsistencyError, ToolFailure


def _is_one_line(path: str) -> bool:
    """Return whether ``path`` contains no character ``str.splitlines`` treats as a line break.

    That is the set the marker partition splits on, which is wider than newline and carriage
    return: a path holding a vertical tab, a form feed, a record separator, NEL or a Unicode line
    separator would be written correctly and then have its echoed marker split into two and
    mis-attributed.
    """
    return path.splitlines() == [path]


def path_line(path: str) -> str:
    """Return ``path`` unchanged after proving it is safe to emit as a literal argfile line."""
    if not path.startswith("/"):
        raise InternalConsistencyError(f"refusing to emit non-absolute path {path!r}")
    if not _is_one_line(path):
        raise InternalConsistencyError(f"refusing to emit path containing a line break: {path!r}")
    return path


def is_representable(path: str) -> bool:
    """Return whether a path can travel as a literal argfile line at all."""
    return path.startswith("/") and _is_one_line(path)


def emit_argfile(destination: Path, lines: Sequence[str]) -> None:
    """Write argfile lines to ``destination`` (UTF-8, newline separated)."""
    try:
        destination.write_bytes(os.fsencode("\n".join(lines) + "\n"))
    except OSError as error:
        raise ToolFailure(f"cannot write argfile {destination}: {error}") from None
