"""The batched writer: argfile construction and nominal, reconciled write attribution.

The writer never decides whether to write; its input type admits only ``Planned`` outcomes.
Every block carries a path marker on both streams, and every intended path leaves with exactly
one conclusion: applied, refused by the re-gate, failed, or unattributable. A path with no
conclusion is never resolved by inference in either direction.
"""

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gps_sync.argfile import emit_argfile, path_line
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
from gps_sync.writeset import (
    Assignment,
    WriteSetClass,
    assignments,
    declared_tags,
    regate_condition,
    write_set_class,
)

APPLIED_MESSAGE = "1 image files updated"
REFUSED_MESSAGE = "1 files failed condition"
ERRORED_MESSAGE = "1 files weren't updated due to errors"
NOTHING_TO_DO = "Nothing to do."
ERROR_PREFIX = "Error:"

WriteConclusion = Geotagged | Protected | Failed

TRAILING_RUN = "trailing run of unterminated lines"
_NAMED_PATHS = 5

_CONVERSION_SUFFIX = re.compile(r"\((?:ValueConvInv|PrintConvInv)\)\s*$")


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
    unreconciled: tuple[str, ...] = ()
    unreconciled_paths: tuple[str, ...] = ()

    @property
    def unreconciled_figure(self) -> int:
        """The run-level count: every affected path, plus one for a trailing run naming none."""
        return len(self.unreconciled_paths) + (TRAILING_RUN in self.unreconciled)


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Exactly one conclusion per intended path, in intent order, plus the reconciliation."""

    outcomes: tuple[WriteConclusion, ...]
    reconciliation: Reconciliation


@dataclass(frozen=True, slots=True)
class WriteBlock:
    """One target's block: its declared class, the assignments it emits and its argfile lines."""

    write_class: WriteSetClass
    assignments: tuple[Assignment, ...]
    lines: tuple[str, ...]


def _write_class(target: Planned) -> WriteSetClass:
    file_type = target.context.file_type if target.context else None
    write_class = write_set_class(file_type)
    if write_class is None:
        raise InternalConsistencyError(
            f"write target {target.path} has container identity {file_type!r}, "
            "outside the allowlist"
        )
    return write_class


def block_assignments(target: Planned) -> tuple[WriteSetClass, tuple[Assignment, ...]]:
    """Return a target's declared class and the assignments its block emits."""
    write_class = _write_class(target)
    return write_class, assignments(write_class, target.position, target.instant)


def build_block(target: Planned, *, keep_original: bool) -> WriteBlock:
    """Build a target's block from its declared write set; a pure function of the target."""
    write_class, members = block_assignments(target)
    path = path_line(target.path)
    lines = [] if keep_original else ["-overwrite_original"]
    lines += ["-n", "-if", regate_condition(write_class)]
    lines += [member.line for member in members]
    lines += ["-echo3", path, "-echo4", path, path, "-execute"]
    return WriteBlock(write_class, members, tuple(lines))


#: Options a write block may carry alone, and those whose value is the following line.
_BLOCK_FLAGS = frozenset({"-overwrite_original", "-n", "-execute"})
_BLOCK_VALUED = frozenset({"-if", "-echo3", "-echo4"})


def _is_declared_assignment(line: str, declared: frozenset[str]) -> bool:
    option, separator, _ = line.partition("=")
    return bool(separator) and option.startswith("-") and option[1:] in declared


def assert_block_shape(block: WriteBlock) -> None:
    """Refuse a block line outside the four declared shapes, before anything is emitted.

    The shapes are an option alone, an option and its value on one line (split at the first ``=``,
    the part before it a member of the block's declared set), an option whose value is the next
    line, and an absolute path. The subject is the line *sequence*, because a value line is
    admitted only as the successor of an option that takes one.
    """
    declared = declared_tags(block.write_class)
    lines = iter(block.lines)
    for line in lines:
        if line in _BLOCK_VALUED:
            value = next(lines, None)
            if value is None:
                raise InternalConsistencyError("write block ends on an option that takes a value")
            if line != "-if":
                path_line(value)
        elif line.startswith("/"):
            path_line(line)
        elif line not in _BLOCK_FLAGS and not _is_declared_assignment(line, declared):
            raise InternalConsistencyError(f"write block carries {line!r}, outside its vocabulary")


def assert_targets_writable(planned: Sequence[Planned]) -> None:
    """Re-check every target against the guarantees discovery is supposed to provide.

    A violation is an internal-consistency failure (discovery cannot produce such a path, and
    nothing in user data can put an assignment outside a target's declared write set), so the run
    aborts before the intent record exists and before any byte changes.
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
        write_class, members = block_assignments(target)
        declared = declared_tags(write_class)
        outside = [m.tag for m in members if m.tag not in declared]
        if outside:
            raise InternalConsistencyError(
                f"write target {path} would receive {outside}, outside the {write_class} write set"
            )
        if path in seen:
            raise InternalConsistencyError(f"write target listed twice: {path}")
        seen.add(path)


def build_write_lines(planned: Sequence[Planned], *, keep_original: bool) -> list[str]:
    """Build the write argfile: one self-contained block per target, one marker per stream."""
    lines: list[str] = []
    for target in planned:
        block = build_block(target, keep_original=keep_original)
        assert_block_shape(block)
        lines += block.lines
    return lines


def _signals(block: MarkedBlock) -> tuple[bool, bool, bool]:
    lines = [line.strip() for line in block.lines]
    return (
        APPLIED_MESSAGE in lines,
        REFUSED_MESSAGE in lines,
        ERRORED_MESSAGE in lines,
    )


@dataclass(frozen=True, slots=True)
class _Refusal:
    """A class-R line and every emitted member it names, empty when it names none."""

    line: str
    members: tuple[Assignment, ...]

    @property
    def position_bearing(self) -> bool:
        return any(m.position_bearing for m in self.members)


@dataclass(frozen=True, slots=True)
class _Partition:
    """A stderr block's lines in the three declared classes, the refusals resolved by member."""

    errors: tuple[str, ...]
    refusals: tuple[_Refusal, ...]
    nothing_to_do: bool

    @property
    def refusal_lines(self) -> tuple[str, ...]:
        return tuple(r.line for r in self.refusals)

    @property
    def unknown(self) -> bool:
        return any(not r.members for r in self.refusals)

    @property
    def position_bearing(self) -> bool:
        return any(r.position_bearing for r in self.refusals)

    @property
    def other(self) -> bool:
        return any(r.members and not r.position_bearing for r in self.refusals)


_TOKEN_PUNCTUATION = "()[]\"',;"


def _resolve_members(line: str, emitted: Mapping[str, Assignment]) -> tuple[Assignment, ...]:
    """Return every emitted member a refusal line names, in line order.

    A token counts if, stripped of surrounding punctuation, it carries a colon and the part after
    its last colon is a member this block emitted. Tokens are not scanned for a single winner: a
    line naming two members is ambiguous about which was refused, and the ambiguity resolves to
    the position-bearing reading, because the opposite reading tells the operator the target
    carries a position it may not. Punctuation is stripped rather than a trailing parenthesis
    being discarded, so a member named inside the final parenthesis is still found.
    """
    found: dict[str, Assignment] = {}
    for token in line.split():
        bare = token.strip(_TOKEN_PUNCTUATION)
        if ":" not in bare:
            continue
        member = emitted.get(bare.rpartition(":")[2].casefold())
        if member is not None:
            found.setdefault(member.name, member)
    return tuple(found.values())


def _partition(block: MarkedBlock, members: Sequence[Assignment]) -> _Partition:
    emitted = {m.name.casefold(): m for m in members}
    lines = [raw.strip() for raw in block.lines]
    errors = tuple(line for line in lines if line.startswith(ERROR_PREFIX))
    refusals: list[_Refusal] = []
    for line in (line for line in lines if not line.startswith(ERROR_PREFIX)):
        named = _resolve_members(line, emitted)
        if named or _CONVERSION_SUFFIX.search(line) is not None:
            refusals.append(_Refusal(line, named))
    return _Partition(errors, tuple(refusals), NOTHING_TO_DO in lines)


def _conclude(
    target: Planned,
    stdout_block: MarkedBlock | None,
    stderr_block: MarkedBlock | None,
    failure: str | None,
) -> WriteConclusion:
    """Conclude one path from its two blocks by the first matching row of the declared table."""

    def unattributed(detail: str) -> Failed:
        return _failed(target, FailedReason.WRITE_UNATTRIBUTED, detail)

    if stdout_block is None or stderr_block is None:
        reason = failure or "the batch never reached this path's marker"
        return unattributed(f"no conclusion attributable to this path: {reason}")
    _, members = block_assignments(target)
    parts = _partition(stderr_block, members)
    applied, refused, errored = _signals(stdout_block)
    signals = sum((applied, refused, errored))
    refusals = " | ".join(parts.refusal_lines)
    if signals == 0 and parts.refusals and parts.nothing_to_do:
        return _failed(target, FailedReason.WRITE_ERROR, refusals)
    if signals != 1:
        return unattributed(
            f"ambiguous result lines in this path's block: {list(stdout_block.lines)}"
        )
    if parts.errors and errored:
        return _failed(target, FailedReason.WRITE_ERROR, " | ".join(parts.errors))
    if parts.errors or errored:
        return unattributed(
            f"contradictory signals: stdout {list(stdout_block.lines)}, "
            f"stderr errors {list(parts.errors)}"
        )
    if refused:
        if parts.refusals:
            return unattributed(
                f"the backend declined the re-gate condition and also refused a value: {refusals}"
            )
        return Protected(
            target.path,
            ProtectedReason.POSITION_APPEARED,
            instant=target.instant,
            via=target.via,
            provenance=target.provenance,
            bracket_speed=target.bracket_speed,
            context=target.context,
        )
    if parts.unknown:
        return unattributed(f"a refusal names a tag this block never assigned: {refusals}")
    if parts.position_bearing:
        return _failed(target, FailedReason.WRITE_PARTIAL, refusals)
    if parts.other:
        return _failed(target, FailedReason.WRITE_INCOMPLETE, refusals)
    return Geotagged(
        target.path,
        target.instant,
        target.position,
        target.via,
        target.provenance,
        target.bracket_speed,
        target.context,
        target.elevation_dropped,
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


def _named(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:_NAMED_PATHS])
    return shown + (f" and {len(paths) - _NAMED_PATHS} more" if len(paths) > _NAMED_PATHS else "")


def _block_anomalies(
    planned: Sequence[Planned],
    out_index: Mapping[str, MarkedBlock],
    err_index: Mapping[str, MarkedBlock],
    repeated: set[str],
    batch: WriteBatchResult | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Name every way the marker-terminated block counts fail to reconcile with the intent set.

    Returns the shapes, each naming the paths it affects, and the affected paths in intent order.
    """
    shapes: list[str] = []
    affected: dict[str, None] = {}
    missing = [t.path for t in planned if t.path not in out_index or t.path not in err_index]
    if missing:
        shapes.append(
            f"shortfall: {len(missing)} intended path(s) have no block on a stream: "
            f"{_named(missing)}"
        )
        affected.update(dict.fromkeys(missing))
    if repeated:
        again = [t.path for t in planned if t.path in repeated]
        shapes.append(f"repeated marker: {len(repeated)} path(s): {_named(again)}")
        affected.update(dict.fromkeys(again))
    if batch is not None and (batch.stdout_tail or batch.stderr_tail):
        shapes.append(TRAILING_RUN)
    return tuple(shapes), tuple(affected)


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
    unreconciled, unreconciled_paths = _block_anomalies(
        planned, out_index, err_index, out_repeated | err_repeated, batch
    )
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
            unreconciled,
            unreconciled_paths,
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


def merge_conclusions(
    planned: Sequence[Planned],
    result: WriteResult | None,
    refused: Mapping[str, Failed],
) -> WriteResult:
    """Return one conclusion per intended path, in intent order, over everything the run decided.

    The free-space stage sits between "planned" and "written": its refusals are conclusions the
    audit record must carry exactly as the write's own are, or an intended path with no conclusion
    would be indistinguishable from an interrupted write. ``result`` is None when nothing was
    spawned, which is the declared outcome for an empty write set.
    """
    concluded: dict[str, WriteConclusion] = {**refused}
    if result is not None:
        concluded.update(conclusions_by_path(result))
    absent = [t.path for t in planned if t.path not in concluded]
    if absent:
        raise InternalConsistencyError(f"intended paths with no conclusion: {absent[:3]}")
    return WriteResult(
        tuple(concluded[t.path] for t in planned),
        result.reconciliation if result is not None else Reconciliation(),
    )


def conclusions_by_path(result: WriteResult) -> Mapping[str, WriteConclusion]:
    """Index a write result's outcomes by path."""
    return {outcome.path: outcome for outcome in result.outcomes}
