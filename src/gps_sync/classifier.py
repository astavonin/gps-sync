"""The classifier: the path verdict and the derivation of an outcome from it.

The two phases run the three-valued position gate, time resolution (including ``--time-shift``),
matching and the container-identity write check, and returns one ``Outcome``. It never sees the
write flag, which is what keeps classification identical in dry-run and write mode, and no gate,
branch or default may read ``Make`` or ``Model``.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from gps_sync.errors import InternalConsistencyError
from gps_sync.index import TrackIndex
from gps_sync.matcher import Match, NoCoverage, match
from gps_sync.model import (
    AltitudeOutcome,
    Centroid,
    Failed,
    FailedReason,
    FileContext,
    Outcome,
    Planned,
    Position,
    Protected,
    ProtectedReason,
    Unmatchable,
    Via,
)
from gps_sync.records import TagRecord, TelemetryRecord, parse_atom_coordinates, parse_gps_position
from gps_sync.telemetry import (
    AtomPresent,
    AtomUnreadable,
    TelemetryRefusal,
    TelemetryResult,
    derive_centroid,
)
from gps_sync.timeres import TimeFailure, resolve_instant
from gps_sync.writeset import (
    EXTENSION_CLASS,
    WriteSetClass,
    without_unrepresentable_elevation,
    write_set_class,
)

# exiftool capitalises the severity inconsistently ("[minor]" and "[Minor]" both occur on one file),
# so every pattern is matched case-insensitively.
# Each pattern is scoped to the container classes whose evidence admitted it; an unscoped entry
# would silently widen a safety gate for stills that nothing measured. ``None`` scopes to all.
_BENIGN_WARNINGS: tuple[tuple[re.Pattern[str], WriteSetClass | None], ...] = (
    (re.compile(r"^\[minor\] Possibly incorrect maker notes offsets", re.IGNORECASE), None),
    (
        re.compile(r"^\[minor\] The ExtractEmbedded option may find more tags", re.IGNORECASE),
        WriteSetClass.QUICKTIME_ATOM,
    ),
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
    write_class = write_set_class(record.file_type)
    return [
        w
        for w in record.warnings
        if not any(p.match(w) for p, scope in _BENIGN_WARNINGS if scope in (None, write_class))
    ]


def _gate(record: TagRecord) -> _Present | _Indeterminate | _Absent:
    """Decide present, indeterminate or provably absent from the file's own record alone.

    A parseable position wins over a diagnostic: the file demonstrably carries one and is not
    written either way. A diagnostic only matters when it stands between us and "absent".

    ``Composite:GPSPosition`` is derived, so an atom whose payload the backend cannot parse emits
    no such key. The group-qualified atom instances are therefore read as well: an instance proves
    an atom exists, and an unparseable or ambiguous one is indeterminate, never absent.
    """
    if record.atom_instances:
        values = record.atom_instances
        if len(values) > 1:
            return _Indeterminate(
                f"{len(values)} container position atoms were read and may disagree: "
                + "; ".join(repr(v) for v in values)
            )
        atom = parse_atom_coordinates(values[0])
        if atom is None:
            return _Indeterminate(
                f"a container position atom is present but unparseable: {values[0]!r}"
            )
        return _Present(atom)
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
    content_class = write_set_class(identity)
    if identity is None or content_class is None:
        return (
            f"container identity {identity or 'unknown'} (extension {extension or 'none'}): "
            "write safety is unverified"
        )
    if EXTENSION_CLASS.get(extension) is not content_class:
        return (
            f"extension {extension or 'none'} and container identity {identity} name different "
            "write-set classes"
        )
    if content_class is WriteSetClass.EXIF_STILL and extension != f".{identity.lower()}":
        # Stills keep 001's exact comparison: relaxing it to the class would make a mislabelled
        # still writable where 001 refused it, which a bare run must never gain.
        return (
            f"extension {extension} disagrees with container identity {identity}: "
            "write safety is unverified"
        )
    return None


@dataclass(frozen=True, slots=True)
class GpxPath:
    """The clip or still derives its position from a GPX match at its resolved instant."""

    instant: datetime


@dataclass(frozen=True, slots=True)
class TelemetryPath:
    """The clip carries telemetry and derives its position from it; the track is never used."""

    instant: datetime


PathVerdict = GpxPath | TelemetryPath

#: A ``meta`` handler track on any track of the container marks telemetry. It is a sound negative
#: (absent proves no telemetry) and only a candidate positive, which the derivation then tests.
_TELEMETRY_HANDLER = "meta"


def decide_path(record: TagRecord, config: ClassifierConfig) -> Outcome | PathVerdict:
    """Phase 1: the gate, the time-evidence dispatch and the path verdict, over the record alone.

    Returns a terminal outcome, or the verdict that says which source derives the position. It is
    a gate, not a preference order: a file carrying telemetry never falls back to the track.
    """
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
            detail=resolved.detail if isinstance(resolved, TimeFailure) else None,
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
        reason = (
            FailedReason.TIME_SEMANTICS_UNVERIFIED
            if resolved.unverified
            else FailedReason.TIME_UNRESOLVABLE
        )
        return Failed(record.path, reason, resolved.detail, context=context)
    if _TELEMETRY_HANDLER in record.handler_types:
        return TelemetryPath(resolved)
    return GpxPath(resolved)


def derive_outcome(
    record: TagRecord,
    verdict: PathVerdict,
    telemetry: TelemetryRecord | None,
    index: TrackIndex,
    config: ClassifierConfig,
) -> Outcome:
    """Phase 3: derive the position from the source the verdict names and classify the result."""
    context = FileContext(record.make, record.model, record.file_type)
    instant = verdict.instant
    if isinstance(verdict, TelemetryPath):
        if telemetry is None:
            raise InternalConsistencyError(f"{record.path} took the telemetry path with no read")
        return _from_telemetry(record, instant, derive_centroid(record, telemetry), context)
    result = match(index, instant, config.max_gap, config.extend)
    if isinstance(result, NoCoverage):
        return Unmatchable(
            record.path,
            instant,
            result.reason,
            nearest_segment=result.nearest_segment,
            miss_s=result.miss_s,
            bracket_s=result.bracket_s,
            detail=result.detail,
            context=context,
        )
    return _after_match(record, instant, result, context)


def _from_telemetry(
    record: TagRecord, instant: datetime, result: TelemetryResult, context: FileContext
) -> Outcome:
    if isinstance(result, AtomPresent):
        zero = result.position.lat == 0 and result.position.lon == 0
        return Protected(
            record.path,
            ProtectedReason.ZERO_POSITION if zero else ProtectedReason.POSITION_PRESENT,
            position=result.position,
            instant=instant,
            context=context,
        )
    if isinstance(result, AtomUnreadable):
        return Failed(
            record.path,
            FailedReason.METADATA_INDETERMINATE,
            result.detail,
            instant=instant,
            context=context,
        )
    if isinstance(result, TelemetryRefusal):
        return Failed(record.path, result.reason, result.detail, instant=instant, context=context)
    return _after_match(
        record, instant, Match(result.position, Via.CENTROID, result.provenance, None), context
    )


def _after_match(
    record: TagRecord, instant: datetime, result: Match, context: FileContext
) -> Outcome:
    refusal = _container_refusal(record)
    position, dropped = without_unrepresentable_elevation(result.position)
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
        position,
        result.via,
        result.provenance,
        result.bracket_speed,
        context,
        dropped or _centroid_dropped(result),
    )


def _centroid_dropped(result: Match) -> bool:
    return (
        isinstance(result.provenance, Centroid)
        and result.provenance.altitude is AltitudeOutcome.NOT_REPRESENTABLE
    )
