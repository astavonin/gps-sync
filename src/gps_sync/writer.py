"""The batched writer: argfile construction and nominal, reconciled write attribution.

The writer never decides whether to write; its input type admits only ``Planned`` outcomes.
Every block carries a path marker on both streams, and every intended path leaves with exactly
one conclusion: applied, refused by the re-gate, failed, or unattributable. A path with no
conclusion is never resolved by inference in either direction.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from gps_sync.argfile import emit_argfile, path_line
from gps_sync.classifier import WRITE_ALLOWLIST
from gps_sync.errors import InternalConsistencyError
from gps_sync.model import (
    Failed,
    FailedReason,
    Geotagged,
    Planned,
    Protected,
    ProtectedReason,
)
from gps_sync.transport import MarkedBlock, Transport, TransportError, WriteBatchResult

APPLIED_MESSAGE = "1 image files updated"
REFUSED_MESSAGE = "1 files failed condition"
ERRORED_MESSAGE = "1 files weren't updated due to errors"

#: Re-evaluates the gate predicate per file inside the writing process. Written without ``-f``,
#: which would make the condition match every file.
REGATE_CONDITION = "not $Composite:GPSPosition"

WriteConclusion = Geotagged | Protected | Failed


@dataclass(frozen=True, slots=True)
class UnintendedConclusion:
    """A conclusion the backend reported for a path the run did not intend to write."""

    path: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The write phase's reconciliation result, the third verdict input of the exit code."""

    unintended: tuple[UnintendedConclusion, ...] = ()
    last_marker: str | None = None
    returncode: int | None = None
    process_failure: str | None = None


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Exactly one conclusion per intended path, in intent order, plus the reconciliation."""

    outcomes: tuple[WriteConclusion, ...]
    reconciliation: Reconciliation


def assert_targets_writable(planned: Sequence[Planned]) -> None:
    """Re-check every target against the guarantees discovery is supposed to provide.

    A violation is an internal-consistency failure (discovery cannot produce such a path), so the
    run aborts before the writer is spawned and before any byte changes.
    """
    seen: set[str] = set()
    for target in planned:
        if not isinstance(target, Planned):
            raise InternalConsistencyError(f"writer received a non-Planned outcome: {target!r}")
        path = target.path
        if not path.startswith("/"):
            raise InternalConsistencyError(f"write target is not absolute: {path!r}")
        if os.path.islink(path):
            raise InternalConsistencyError(f"write target is a symlink: {path}")
        file_type = target.context.file_type if target.context else None
        if file_type is None or file_type.upper() not in WRITE_ALLOWLIST:
            raise InternalConsistencyError(
                f"write target {path} has container identity {file_type!r}, outside the allowlist"
            )
        if path in seen:
            raise InternalConsistencyError(f"write target listed twice: {path}")
        seen.add(path)


def _block_lines(target: Planned, *, keep_original: bool) -> list[str]:
    position = target.position
    stamp = target.instant.astimezone(UTC)
    lines = [] if keep_original else ["-overwrite_original"]
    lines += [
        "-n",
        "-if",
        REGATE_CONDITION,
        f"-GPSLatitude={abs(position.lat):.9f}",
        f"-GPSLatitudeRef={'N' if position.lat >= 0 else 'S'}",
        f"-GPSLongitude={abs(position.lon):.9f}",
        f"-GPSLongitudeRef={'E' if position.lon >= 0 else 'W'}",
    ]
    if position.ele is not None:
        lines += [
            f"-GPSAltitude={abs(position.ele):.4f}",
            f"-GPSAltitudeRef={0 if position.ele >= 0 else 1}",
        ]
    path = path_line(target.path)
    lines += [
        f"-GPSDateStamp={stamp:%Y:%m:%d}",
        f"-GPSTimeStamp={stamp:%H:%M:%S}",
        "-echo3",
        path,
        "-echo4",
        path,
        path,
        "-execute",
    ]
    return lines


def build_write_lines(planned: Sequence[Planned], *, keep_original: bool) -> list[str]:
    """Build the write argfile: one self-contained block per target, one marker per stream."""
    lines: list[str] = []
    for target in planned:
        lines += _block_lines(target, keep_original=keep_original)
    return lines


def _signals(block: MarkedBlock) -> tuple[bool, bool, bool]:
    lines = [line.strip() for line in block.lines]
    return (
        APPLIED_MESSAGE in lines,
        REFUSED_MESSAGE in lines,
        ERRORED_MESSAGE in lines,
    )


def _error_lines(block: MarkedBlock) -> list[str]:
    return [line for line in block.lines if line.startswith("Error:")]


def _conclude(
    target: Planned,
    stdout_block: MarkedBlock | None,
    stderr_block: MarkedBlock | None,
    failure: str | None,
) -> WriteConclusion:
    def unattributed(detail: str) -> Failed:
        return _failed(target, FailedReason.WRITE_UNATTRIBUTED, detail)

    if stdout_block is None or stderr_block is None:
        reason = failure or "the batch never reached this path's marker"
        return unattributed(f"no conclusion attributable to this path: {reason}")
    applied, refused, errored = _signals(stdout_block)
    errors = _error_lines(stderr_block)
    if sum((applied, refused, errored)) != 1:
        return unattributed(
            f"ambiguous result lines in this path's block: {list(stdout_block.lines)}"
        )
    if applied and not errors:
        return Geotagged(
            target.path,
            target.instant,
            target.position,
            target.via,
            target.provenance,
            target.bracket_speed,
            target.context,
        )
    if refused and not errors:
        return Protected(
            target.path,
            ProtectedReason.POSITION_APPEARED,
            instant=target.instant,
            via=target.via,
            provenance=target.provenance,
            bracket_speed=target.bracket_speed,
            context=target.context,
        )
    if errored and errors:
        return _failed(target, FailedReason.WRITE_ERROR, " | ".join(errors))
    return unattributed(
        f"contradictory signals: stdout {list(stdout_block.lines)}, stderr errors {errors}"
    )


def _failed(target: Planned, reason: FailedReason, detail: str) -> Failed:
    return Failed(
        target.path,
        reason,
        detail,
        instant=target.instant,
        via=target.via,
        provenance=target.provenance,
        bracket_speed=target.bracket_speed,
        context=target.context,
    )


def _by_marker(blocks: Sequence[MarkedBlock]) -> tuple[dict[str, MarkedBlock], set[str]]:
    index: dict[str, MarkedBlock] = {}
    repeated: set[str] = set()
    for block in blocks:
        if block.marker in index:
            repeated.add(block.marker)
        index[block.marker] = block
    return index, repeated


def attribute(
    planned: Sequence[Planned],
    batch: WriteBatchResult | None,
    process_failure: str | None = None,
) -> WriteResult:
    """Map every intended path to exactly one conclusion and reconcile against the intent set.

    ``batch`` is None when the process produced nothing at all. A non-zero process status is a
    run-level fact, not a verdict on any path: the conclusions come from the blocks.
    """
    stdout_blocks = batch.stdout_blocks if batch else ()
    stderr_blocks = batch.stderr_blocks if batch else ()
    out_index, out_repeated = _by_marker(stdout_blocks)
    err_index, err_repeated = _by_marker(stderr_blocks)
    outcomes: list[WriteConclusion] = []
    for target in planned:
        if target.path in out_repeated or target.path in err_repeated:
            outcomes.append(
                _failed(
                    target,
                    FailedReason.WRITE_UNATTRIBUTED,
                    "this path's marker appeared more than once",
                )
            )
            continue
        outcomes.append(
            _conclude(
                target, out_index.get(target.path), err_index.get(target.path), process_failure
            )
        )
    intended = {t.path for t in planned}
    unintended: list[UnintendedConclusion] = []
    for marker in dict.fromkeys([b.marker for b in (*stdout_blocks, *stderr_blocks)]):
        if marker not in intended:
            lines = (
                *(out_index[marker].lines if marker in out_index else ()),
                *(err_index[marker].lines if marker in err_index else ()),
            )
            unintended.append(UnintendedConclusion(marker, lines))
    return WriteResult(
        tuple(outcomes),
        Reconciliation(
            tuple(unintended),
            stdout_blocks[-1].marker if stdout_blocks else None,
            batch.returncode if batch else None,
            process_failure,
        ),
    )


def run_write(
    transport: Transport,
    planned: Sequence[Planned],
    temp_dir: Path,
    *,
    keep_original: bool,
    trace: Callable[[str], None] = lambda _message: None,
) -> WriteResult:
    """Emit the write argfile, run the one batched write and attribute its result.

    A ``TransportError`` is the whole-process failure: whatever it emitted is still attributed,
    and every intended path without its own markers becomes ``WRITE_UNATTRIBUTED``.
    """
    argfile = temp_dir / "write.args"
    emit_argfile(argfile, build_write_lines(planned, keep_original=keep_original))
    trace(f"write argfile: {argfile} ({len(planned)} paths)")
    try:
        batch = transport.write(argfile)
    except TransportError as error:
        return attribute(planned, error.partial, f"write process failed: {error}")
    return attribute(planned, batch)


def conclusions_by_path(result: WriteResult) -> Mapping[str, WriteConclusion]:
    """Index a write result's outcomes by path."""
    return {outcome.path: outcome for outcome in result.outcomes}
