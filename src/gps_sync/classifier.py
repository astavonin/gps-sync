"""The single ``classify`` entry point.

The classifier runs the three-valued position gate, time resolution (including ``--time-shift``),
matching and the container-identity write check, and returns one ``Outcome``. It never sees the
write flag, which is what keeps classification identical in dry-run and write mode, and no gate,
branch or default may read ``Make`` or ``Model``.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from gps_sync.index import TrackIndex
from gps_sync.matcher import Match, NoCoverage, match
from gps_sync.model import (
    Failed,
    FailedReason,
    FileContext,
    Outcome,
    Planned,
    Position,
    Protected,
    ProtectedReason,
    Unmatchable,
)
from gps_sync.records import TagRecord, parse_gps_position
from gps_sync.timeres import TimeFailure, resolve_instant

#: Container identities whose in-place write safety was verified byte-for-byte.
WRITE_ALLOWLIST = frozenset({"RAF", "DNG"})

# exiftool capitalises the severity inconsistently ("[minor]" and "[Minor]" both occur on one file),
# so every pattern is matched case-insensitively.
_BENIGN_WARNINGS = (
    re.compile(r"^\[minor\] Possibly incorrect maker notes offsets", re.IGNORECASE),
)
_MINOR_WARNING = re.compile(r"^\[minor\]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ClassifierConfig:
    """The options classification depends on; deliberately excludes the write flag."""

    max_gap: timedelta
    extend: timedelta
    time_shift: timedelta
    utc_offset: timedelta | None


@dataclass(frozen=True, slots=True)
class _Present:
    position: Position


@dataclass(frozen=True, slots=True)
class _Indeterminate:
    detail: str
    only_minor_warnings: bool = False


@dataclass(frozen=True, slots=True)
class _Absent:
    pass


def _unrecognised_warnings(record: TagRecord) -> list[str]:
    return [w for w in record.warnings if not any(p.match(w) for p in _BENIGN_WARNINGS)]


def _gate(record: TagRecord) -> _Present | _Indeterminate | _Absent:
    """Decide present, indeterminate or provably absent from the file's own record alone.

    A parseable position wins over a diagnostic: the file demonstrably carries one and is not
    written either way. A diagnostic only matters when it stands between us and "absent".
    """
    if record.has_gps_key:
        position = parse_gps_position(record.gps_position)
        if position is None:
            return _Indeterminate(
                f"Composite:GPSPosition is present but unparseable: {record.gps_position!r}"
            )
        return _Present(position)
    unrecognised = _unrecognised_warnings(record)
    problems = [*record.errors, *unrecognised]
    if problems:
        only_minor = not record.errors and all(_MINOR_WARNING.match(w) for w in unrecognised)
        return _Indeterminate("; ".join(problems), only_minor)
    return _Absent()


def _container_refusal(record: TagRecord) -> str | None:
    extension = PurePosixPath(record.path).suffix.lower()
    identity = record.file_type.upper() if record.file_type else None
    if identity not in WRITE_ALLOWLIST:
        return (
            f"container identity {identity or 'unknown'} (extension {extension or 'none'}): "
            "write safety is unverified"
        )
    if extension != f".{identity.lower()}":
        return (
            f"extension {extension or 'none'} disagrees with container identity {identity}: "
            "write safety is unverified"
        )
    return None


def classify(record: TagRecord, index: TrackIndex, config: ClassifierConfig) -> Outcome:
    """Classify one candidate from its record, the merged index and the options."""
    context = FileContext(record.make, record.model, record.file_type)
    resolved = resolve_instant(record, config.utc_offset, config.time_shift)
    instant: datetime | None = None if isinstance(resolved, TimeFailure) else resolved
    gate = _gate(record)
    if isinstance(gate, _Present):
        zero = gate.position.lat == 0 and gate.position.lon == 0
        return Protected(
            record.path,
            ProtectedReason.ZERO_POSITION if zero else ProtectedReason.POSITION_PRESENT,
            position=gate.position,
            instant=instant,
            context=context,
        )
    if isinstance(gate, _Indeterminate):
        return Failed(
            record.path,
            FailedReason.UNRECOGNISED_MINOR_WARNING
            if gate.only_minor_warnings
            else FailedReason.METADATA_INDETERMINATE,
            gate.detail,
            instant=instant,
            context=context,
        )
    if isinstance(resolved, TimeFailure):
        return Failed(record.path, FailedReason.TIME_UNRESOLVABLE, resolved.detail, context=context)
    result = match(index, resolved, config.max_gap, config.extend)
    if isinstance(result, NoCoverage):
        return Unmatchable(
            record.path,
            resolved,
            result.reason,
            nearest_segment=result.nearest_segment,
            miss_s=result.miss_s,
            bracket_s=result.bracket_s,
            detail=result.detail,
            context=context,
        )
    return _after_match(record, resolved, result, context)


def _after_match(
    record: TagRecord, instant: datetime, result: Match, context: FileContext
) -> Outcome:
    refusal = _container_refusal(record)
    if refusal is not None:
        return Failed(
            record.path,
            FailedReason.WRITE_UNSUPPORTED_CONTAINER,
            refusal,
            instant=instant,
            via=result.via,
            provenance=result.provenance,
            bracket_speed=result.bracket_speed,
            context=context,
        )
    return Planned(
        record.path,
        instant,
        result.position,
        result.via,
        result.provenance,
        result.bracket_speed,
        context,
    )
