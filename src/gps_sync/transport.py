"""The single named boundary to the exiftool subprocess.

Four operations with declared result shapes: ``version``, batched ``read``, batched
``read_telemetry`` and batched ``write``, each with its own option vocabulary. exiftool is a
batched tag transport only; no geotagging capability of its own is ever used, and this module never
constructs an invocation that reads a GPX or NMEA file.

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gps_sync.errors import InternalConsistencyError, ToolFailure

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
    "-CreationDate",
    "-MajorBrand",
    "-Duration",
    "-HandlerType",
    # Requested by group so that a bare ``:GPSCoordinates`` key proves a container atom exists,
    # which ``Composite:GPSPosition`` (a derived tag) cannot when the payload is unparseable.
    "-Keys:GPSCoordinates",
    "-UserData:GPSCoordinates",
    "-ItemList:GPSCoordinates",
)

#: The per-sample tags and the group-qualified atom requests of the telemetry read.
TELEMETRY_TAGS: tuple[str, ...] = (
    "-GPSLatitude",
    "-GPSLongitude",
    "-AbsoluteAltitude",
    "-SampleTime",
    "-Keys:GPSCoordinates",
    "-UserData:GPSCoordinates",
    "-ItemList:GPSCoordinates",
)

#: Options (and the value of the charset option) the container read may carry.
READ_OPTIONS: tuple[str, ...] = ("-charset", "filename=utf8", "-j", "-n", "-a", "-G4", "-@")
#: ``-ee`` is a member of the telemetry read's vocabulary and of no other operation's: in a read it
#: makes ``Composite:GPSPosition`` the first sample, and in a write block it would make the re-gate
#: condition false for every clip with telemetry.
TELEMETRY_OPTIONS: tuple[str, ...] = (*READ_OPTIONS, "-ee")
VERSION_OPTIONS: tuple[str, ...] = ("-ver",)
WRITE_OPTIONS: tuple[str, ...] = ("-@", "-common_args", "-charset", "filename=utf8")

#: The telemetry read's chunk, derived: the byte budget over the measured maximum record size.
TELEMETRY_BUDGET_BYTES = 256_000_000
TELEMETRY_MAX_RECORD_BYTES = 8_894_679
TELEMETRY_CHUNK_SIZE = TELEMETRY_BUDGET_BYTES // TELEMETRY_MAX_RECORD_BYTES

_VERSION = re.compile(r"^\d+(?:\.\d+)*$")


def _assert_vocabulary(
    operation: str, command: Sequence[str], vocabulary: Sequence[str], tags: Sequence[str]
) -> list[str]:
    """Refuse a constructed option outside the operation's vocabulary, before anything runs.

    The executable is the first element and the argfile path is the one following ``-@``; every
    other element must be a declared option, a declared tag request or the option value that
    follows ``-charset``.
    """
    for position, element in enumerate(command[1:], start=1):
        if command[position - 1] == "-@":
            continue
        if element not in vocabulary and element not in tags:
            raise InternalConsistencyError(
                f"{operation} invocation carries {element!r}, outside its declared options"
            )
    return list(command)


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

    def read_telemetry(self, argfile: Path) -> ReadResult:
        """Run one batched ``-ee`` read of per-sample telemetry over the paths in ``argfile``."""

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
    command = [
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
    return _assert_vocabulary("container read", command, READ_OPTIONS, READ_TAGS)


def telemetry_command(executable: str, argfile: Path) -> list[str]:
    """Build the batched telemetry read: the container read's options plus ``-ee``.

    The numeric flag is load-bearing, not inherited: without it every coordinate prints in
    degrees-minutes-seconds and every sample would fail the numeric value domain.
    """
    command = [
        executable,
        "-charset",
        "filename=utf8",
        "-j",
        "-n",
        "-a",
        "-G4",
        "-ee",
        *TELEMETRY_TAGS,
        "-@",
        str(argfile),
    ]
    return _assert_vocabulary("telemetry read", command, TELEMETRY_OPTIONS, TELEMETRY_TAGS)


def write_command(executable: str, argfile: Path) -> list[str]:
    """Build the batched write invocation; every per-block option lives in the argfile.

    The filename charset follows ``-common_args`` because exiftool resets its options at every
    ``-execute`` except those, so given before ``-@`` it would govern the first block alone and a
    non-ASCII path in any later block would not be found. The argfile has to come first: put after
    ``-common_args`` it is re-read for every command.
    """
    command = [executable, "-@", str(argfile), "-common_args", "-charset", "filename=utf8"]
    return _assert_vocabulary("write", command, WRITE_OPTIONS, ())


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
        completed = self._run([self._executable, *VERSION_OPTIONS])
        if completed.returncode != 0:
            raise ToolFailure(f"`{self._executable} -ver` exited {completed.returncode}")
        return completed.stdout.decode("utf-8", "replace").strip()

    def read(self, argfile: Path) -> ReadResult:
        """Run the batched read and return its records keyed by ``SourceFile``."""
        return self._records(read_command(self._executable, argfile))

    def read_telemetry(self, argfile: Path) -> ReadResult:
        """Run the batched ``-ee`` telemetry read and return its records by ``SourceFile``."""
        return self._records(telemetry_command(self._executable, argfile))

    def _records(self, command: list[str]) -> ReadResult:
        completed = self._run(command)
        stderr = os.fsdecode(completed.stderr)
        records: dict[str, Mapping[str, object]] = {}
        if completed.stdout.strip():
            try:
                parsed = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise ToolFailure(f"exiftool returned unparseable JSON: {error}") from None
            if not isinstance(parsed, list) or not all(
                isinstance(item, dict) and "SourceFile" in item for item in parsed
            ):
                raise ToolFailure(
                    "exiftool returned JSON that is not a list of records naming a SourceFile"
                )
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
