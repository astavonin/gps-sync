"""The single named boundary to the exiftool subprocess.

Three operations with declared result shapes: ``version``, batched ``read`` and batched
``write``. exiftool is a batched tag transport only; no geotagging capability of its own is
ever used, and this module never constructs an invocation that reads a GPX or NMEA file.

The write operation's declared shape is the batch's two output streams partitioned into
per-block runs by the path markers, because the backend supplies no named per-path result.
The marker (``-echo3`` on stdout, ``-echo4`` on stderr) is the target's own absolute path and
arrives at the *end* of its block, so a block is the run of lines a marker terminates.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gps_sync.errors import ToolFailure

MINIMUM_VERSION = (13, 55)
DEFAULT_EXIFTOOL = "/opt/homebrew/bin/exiftool"
EXIFTOOL_ENV = "GPS_SYNC_EXIFTOOL"

#: Tags the batched read requests; every one has a declared consumer.
READ_TAGS: tuple[str, ...] = (
    "-Composite:GPSPosition",
    "-DateTimeOriginal",
    "-OffsetTimeOriginal",
    "-CreateDate",
    "-FileType",
    "-Make",
    "-Model",
    "-Warning",
    "-Error",
)

_VERSION = re.compile(r"^\d+(?:\.\d+)*$")


class TransportError(Exception):
    """The write process failed as a whole; ``partial`` holds whatever it emitted first."""

    def __init__(self, message: str, partial: WriteBatchResult | None = None) -> None:
        super().__init__(message)
        self.partial = partial


@dataclass(frozen=True, slots=True)
class MarkedBlock:
    """The run of lines a path marker terminates."""

    marker: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WriteBatchResult:
    """Both output streams partitioned into per-block runs, plus the process status.

    ``*_tail`` holds lines after the last marker, which belong to a block that never finished.
    """

    stdout_blocks: tuple[MarkedBlock, ...]
    stderr_blocks: tuple[MarkedBlock, ...]
    returncode: int
    stdout_tail: tuple[str, ...] = ()
    stderr_tail: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadResult:
    """Per-path tag records keyed by ``SourceFile``, plus the backend's own stderr text."""

    records: Mapping[str, Mapping[str, object]]
    stderr: str = ""


class Transport(Protocol):
    """The substitutable exiftool boundary."""

    def version(self) -> str:
        """Return the backend's version string."""

    def read(self, argfile: Path) -> ReadResult:
        """Run one batched read over the paths in ``argfile``."""

    def write(self, argfile: Path) -> WriteBatchResult:
        """Run one batched write described by ``argfile``."""


def parse_version(text: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple."""
    stripped = text.strip()
    if _VERSION.fullmatch(stripped) is None:
        raise ToolFailure(f"cannot parse exiftool version from {text!r}")
    return tuple(int(part) for part in stripped.split("."))


def check_version(text: str) -> tuple[int, ...]:
    """Reject a version below the minimum tested one with an actionable message."""
    parsed = parse_version(text)
    if parsed < MINIMUM_VERSION:
        wanted = ".".join(map(str, MINIMUM_VERSION))
        raise ToolFailure(
            f"exiftool {text.strip()} is older than the minimum tested version {wanted}; "
            "upgrade exiftool (for example with `brew upgrade exiftool`)"
        )
    return parsed


def locate_exiftool(environ: Mapping[str, str] | None = None) -> str:
    """Return the exiftool path: ``GPS_SYNC_EXIFTOOL``, else ``PATH``, else the default."""
    env = os.environ if environ is None else environ
    configured = env.get(EXIFTOOL_ENV)
    if configured:
        return configured
    return shutil.which("exiftool", path=env.get("PATH")) or DEFAULT_EXIFTOOL


def read_command(executable: str, argfile: Path) -> list[str]:
    """Build the batched read invocation: ``-j -n`` with diagnostics requested as tags.

    ``-a`` keeps duplicate tags and ``-G4`` makes ``-j`` emit them under distinct keys
    (``:Warning``, ``Copy1:Warning``, ...). Without both, exiftool reports only the first warning
    generated while parsing, so a benign warning would hide a serious one from the gate.
    """
    return [
        executable,
        "-charset",
        "filename=utf8",
        "-j",
        "-n",
        "-a",
        "-G4",
        *READ_TAGS,
        "-@",
        str(argfile),
    ]


def write_command(executable: str, argfile: Path) -> list[str]:
    """Build the batched write invocation; every per-block option lives in the argfile."""
    return [executable, "-charset", "filename=utf8", "-@", str(argfile)]


def partition_stream(text: str) -> tuple[tuple[MarkedBlock, ...], tuple[str, ...]]:
    """Partition one output stream into marker-terminated blocks.

    A marker is a line beginning with ``/`` (the target's absolute path and nothing else). Lines
    are buffered until the next marker rather than keyed on a prefix, because a block's result
    lines come *before* its marker and their count varies from one block to the next.
    """
    blocks: list[MarkedBlock] = []
    buffer: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if raw.startswith("/"):
            blocks.append(MarkedBlock(raw, tuple(buffer)))
            buffer = []
        else:
            buffer.append(line)
    return tuple(blocks), tuple(buffer)


class ExiftoolTransport:
    """The real transport: runs the exiftool binary with an argv list and no shell."""

    def __init__(
        self, executable: str, trace: Callable[[str], None] = lambda _message: None
    ) -> None:
        self._executable = executable
        self._trace = trace

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        self._trace("exiftool invocation: " + shlex.join(command))
        try:
            return subprocess.run(command, capture_output=True, check=False)
        except OSError as error:
            raise ToolFailure(f"cannot run exiftool ({self._executable}): {error}") from None

    def version(self) -> str:
        """Return ``exiftool -ver`` output."""
        completed = self._run([self._executable, "-ver"])
        if completed.returncode != 0:
            raise ToolFailure(f"`{self._executable} -ver` exited {completed.returncode}")
        return completed.stdout.decode("utf-8", "replace").strip()

    def read(self, argfile: Path) -> ReadResult:
        """Run the batched read and return its records keyed by ``SourceFile``."""
        completed = self._run(read_command(self._executable, argfile))
        stderr = os.fsdecode(completed.stderr)
        records: dict[str, Mapping[str, object]] = {}
        if completed.stdout.strip():
            try:
                parsed = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise ToolFailure(f"exiftool returned unparseable JSON: {error}") from None
            for item in parsed:
                records[str(item["SourceFile"])] = item
        return ReadResult(records, stderr)

    def write(self, argfile: Path) -> WriteBatchResult:
        """Run the batched write and partition both streams by path marker."""
        command = write_command(self._executable, argfile)
        self._trace("exiftool invocation: " + shlex.join(command))
        try:
            completed = subprocess.run(command, capture_output=True, check=False)
        except OSError as error:
            raise TransportError(f"cannot run exiftool ({self._executable}): {error}") from None
        out_blocks, out_tail = partition_stream(os.fsdecode(completed.stdout))
        err_blocks, err_tail = partition_stream(os.fsdecode(completed.stderr))
        return WriteBatchResult(out_blocks, err_blocks, completed.returncode, out_tail, err_tail)
