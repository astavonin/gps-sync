"""The outcome union, its payload shapes and the reason enumerations.

Every file a run discovers terminates as exactly one of six variants. The presence rules the
design states for the optional payload fields are enforced at construction, so an outcome that
violates them cannot exist.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Never


class Via(StrEnum):
    """How a position was derived; every member needs a row in ``VIA_SPECS``."""

    INTERPOLATED = "INTERPOLATED"
    EXTENDED = "EXTENDED"
    CENTROID = "CENTROID"


class IgnoredReason(StrEnum):
    """Why an entry never became a candidate."""

    NOT_A_STILL = "NOT_A_STILL"
    HIDDEN_ENTRY = "HIDDEN_ENTRY"
    SYMLINK = "SYMLINK"
    DUPLICATE_INODE = "DUPLICATE_INODE"


class ProtectedReason(StrEnum):
    """Why a file carrying a position was left alone."""

    POSITION_PRESENT = "POSITION_PRESENT"
    ZERO_POSITION = "ZERO_POSITION"
    POSITION_APPEARED = "POSITION_APPEARED"


class UnmatchableReason(StrEnum):
    """The four independent refusals of the matcher; they stay distinct everywhere."""

    MAX_GAP_EXCEEDED = "MAX_GAP_EXCEEDED"
    SEGMENT_BOUNDARY = "SEGMENT_BOUNDARY"
    OUTSIDE_COVERAGE = "OUTSIDE_COVERAGE"
    BRACKET_UNSOUND = "BRACKET_UNSOUND"


class FailedReason(StrEnum):
    """Why the tool intended to act on a file and could not."""

    TIME_UNRESOLVABLE = "TIME_UNRESOLVABLE"
    METADATA_INDETERMINATE = "METADATA_INDETERMINATE"
    UNRECOGNISED_MINOR_WARNING = "UNRECOGNISED_MINOR_WARNING"
    WRITE_UNSUPPORTED_CONTAINER = "WRITE_UNSUPPORTED_CONTAINER"
    WRITE_ERROR = "WRITE_ERROR"
    WRITE_UNATTRIBUTED = "WRITE_UNATTRIBUTED"
    WRITE_PARTIAL = "WRITE_PARTIAL"
    WRITE_INCOMPLETE = "WRITE_INCOMPLETE"
    TIME_SEMANTICS_UNVERIFIED = "TIME_SEMANTICS_UNVERIFIED"
    TELEMETRY_UNUSABLE = "TELEMETRY_UNUSABLE"
    TELEMETRY_INCONSISTENT = "TELEMETRY_INCONSISTENT"
    WRITE_INSUFFICIENT_SPACE = "WRITE_INSUFFICIENT_SPACE"
    WRITE_TARGET_UNEXAMINABLE = "WRITE_TARGET_UNEXAMINABLE"


class SuccessState(StrEnum):
    """The two success variants, which carry no reason."""

    PLANNED = "PLANNED"
    GEOTAGGED = "GEOTAGGED"


class RunLevelReason(StrEnum):
    """Reasons that attach to the run rather than to a candidate."""

    WRITE_UNINTENDED_PATH = "WRITE_UNINTENDED_PATH"
    NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES = "NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES"
    ELEVATION_NOT_REPRESENTABLE = "ELEVATION_NOT_REPRESENTABLE"
    BLOCK_COUNT_UNRECONCILED = "BLOCK_COUNT_UNRECONCILED"


ReasonKey = (
    IgnoredReason
    | ProtectedReason
    | UnmatchableReason
    | FailedReason
    | SuccessState
    | RunLevelReason
)

#: Failed reasons that follow a match and therefore keep the match payload.
FAILED_AFTER_MATCH = frozenset(
    {
        FailedReason.WRITE_UNSUPPORTED_CONTAINER,
        FailedReason.WRITE_ERROR,
        FailedReason.WRITE_UNATTRIBUTED,
        FailedReason.WRITE_PARTIAL,
        FailedReason.WRITE_INCOMPLETE,
        FailedReason.WRITE_INSUFFICIENT_SPACE,
        FailedReason.WRITE_TARGET_UNEXAMINABLE,
    }
)


def assert_never(value: Never) -> Never:
    """Fail loudly when a match over a closed union misses a member."""
    raise AssertionError(f"unhandled member: {value!r}")


@dataclass(frozen=True, slots=True)
class Position:
    """A coordinate; ``ele`` is absent when elevation is not derivable."""

    lat: float
    lon: float
    ele: float | None = None


@dataclass(frozen=True, slots=True)
class Interpolated:
    """Provenance of a position derived between two bracketing points of one segment."""

    segment: str
    bracket_s: float


@dataclass(frozen=True, slots=True)
class Extended:
    """Provenance of a position clamped to a segment endpoint."""

    segment: str
    clamp_s: float
    overridden_reason: UnmatchableReason


class AggregationRule(StrEnum):
    """The rule a centroid was aggregated by, declared so a later variant stays distinguishable."""

    ARITHMETIC_MEAN = "ARITHMETIC_MEAN"


class AltitudeOutcome(StrEnum):
    """The altitude tag a centroid's elevation was taken over, or why none was derived."""

    ABSOLUTE_ALTITUDE = "ABSOLUTE_ALTITUDE"
    NONE_USABLE = "NONE_USABLE"
    NOT_CO_INDEXED = "NOT_CO_INDEXED"
    NOT_REPRESENTABLE = "NOT_REPRESENTABLE"


class SampleRejection(StrEnum):
    """Why one telemetry sample was not used as a fix."""

    UNPARSEABLE = "UNPARSEABLE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    ZERO_SENTINEL = "ZERO_SENTINEL"


@dataclass(frozen=True, slots=True)
class AggregationSpec:
    """One row of the aggregation table: how a rule is named where a reader sees it."""

    phrase: str


@dataclass(frozen=True, slots=True)
class AltitudeSpec:
    """One row of the altitude table: whether a file with this outcome lacks an elevation."""

    unavailable: bool


@dataclass(frozen=True, slots=True)
class RejectionSpec:
    """One row of the rejection table: how a dropped sample's reason is named for a reader."""

    phrase: str


#: The GPX path's own term of the unavailable-altitude breakdown. It is not a member of
#: ``AltitudeOutcome`` because that enumeration lives on ``Centroid`` alone.
GPX_PATH_NO_ELEVATION = "GPX_PATH_NO_ELEVATION"

AGGREGATION_SPECS: dict[AggregationRule, AggregationSpec] = {
    AggregationRule.ARITHMETIC_MEAN: AggregationSpec("mean"),
}

ALTITUDE_SPECS: dict[AltitudeOutcome, AltitudeSpec] = {
    AltitudeOutcome.ABSOLUTE_ALTITUDE: AltitudeSpec(unavailable=False),
    AltitudeOutcome.NONE_USABLE: AltitudeSpec(unavailable=True),
    AltitudeOutcome.NOT_CO_INDEXED: AltitudeSpec(unavailable=True),
    AltitudeOutcome.NOT_REPRESENTABLE: AltitudeSpec(unavailable=True),
}

REJECTION_SPECS: dict[SampleRejection, RejectionSpec] = {
    SampleRejection.UNPARSEABLE: RejectionSpec("unparseable"),
    SampleRejection.OUT_OF_RANGE: RejectionSpec("out of range"),
    SampleRejection.ZERO_SENTINEL: RejectionSpec("zero sentinel"),
}


def _assert_total(table_name: str, members: Iterable[StrEnum], table: Mapping[Any, object]) -> None:
    """Refuse import when a declared enumeration has a member its table has no row for."""
    missing = [member for member in members if member not in table]
    if missing:
        raise RuntimeError(f"the {table_name} table has no row for: {missing}")


_assert_total("aggregation", AggregationRule, AGGREGATION_SPECS)
_assert_total("altitude", AltitudeOutcome, ALTITUDE_SPECS)
_assert_total("rejection", SampleRejection, REJECTION_SPECS)


@dataclass(frozen=True, slots=True)
class Centroid:
    """Provenance of a position averaged over a clip's own telemetry fixes.

    ``dropped`` holds one count per rejection reason in enumeration order, so ``samples_used``
    plus its sum is always ``samples_read``. ``span_s`` is the span of every sample the read
    returned and ``shortfall_s`` its shortfall against the clip's own duration.
    """

    samples_read: int
    samples_used: int
    dropped: tuple[tuple[SampleRejection, int], ...]
    span_s: float
    shortfall_s: float
    rule: AggregationRule
    spread_m: float
    altitude: AltitudeOutcome


Provenance = Interpolated | Extended | Centroid


@dataclass(frozen=True, slots=True)
class ViaSpec:
    """One row of the ``via`` table: the provenance shape and the figures it may enter."""

    provenance: type[Provenance]
    bracket_speed_admitted: bool
    fold_admitted: bool
    exclusion_key: str | None


#: The single declaring mapping for ``via``; ``_assert_via_table`` refuses import on a missing row.
VIA_SPECS: dict[Via, ViaSpec] = {
    Via.INTERPOLATED: ViaSpec(Interpolated, True, True, None),
    Via.EXTENDED: ViaSpec(Extended, True, False, "extended"),
    Via.CENTROID: ViaSpec(Centroid, False, False, "centroid"),
}


def _assert_via_table() -> None:
    missing = [via for via in Via if via not in VIA_SPECS]
    if missing:
        raise RuntimeError(f"the via table has no row for: {missing}")
    if any(spec.fold_admitted == (spec.exclusion_key is not None) for spec in VIA_SPECS.values()):
        raise RuntimeError("a via row must be fold-admitted or name its exclusion key, never both")


_assert_via_table()


@dataclass(frozen=True, slots=True)
class FileContext:
    """Context fields that the report and JSON show and no gate may ever read."""

    make: str | None = None
    model: str | None = None
    file_type: str | None = None


def _check_match_payload(
    via: Via | None,
    provenance: Provenance | None,
    bracket_speed: float | None,
    *,
    present: bool,
) -> None:
    if present:
        if via is None or provenance is None:
            raise ValueError("match payload (via and provenance) is required here")
        spec = VIA_SPECS[via]
        if not isinstance(provenance, spec.provenance):
            raise ValueError(f"provenance {type(provenance).__name__} does not match via {via}")
        if bracket_speed is not None and not spec.bracket_speed_admitted:
            raise ValueError(f"a {via} outcome carries no bracket speed")
    elif via is not None or provenance is not None or bracket_speed is not None:
        raise ValueError("match payload must be absent here")


@dataclass(frozen=True, slots=True)
class Ignored:
    """An entry that never became a candidate."""

    path: str
    reason: IgnoredReason
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Protected:
    """A file carrying a position, or one that gained a position mid-run; never written.

    ``detail`` says why no instant is carried when the time evidence could not be resolved: a file
    that is protected needs no instant, but the reason it has none is still a finding about it.
    """

    path: str
    reason: ProtectedReason
    position: Position | None = None
    instant: datetime | None = None
    via: Via | None = None
    provenance: Provenance | None = None
    bracket_speed: float | None = None
    context: FileContext | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        appeared = self.reason is ProtectedReason.POSITION_APPEARED
        if appeared == (self.position is not None):
            raise ValueError("position is present exactly on POSITION_PRESENT and ZERO_POSITION")
        _check_match_payload(self.via, self.provenance, self.bracket_speed, present=appeared)


@dataclass(frozen=True, slots=True)
class Failed:
    """The tool intended to act on a file and could not."""

    path: str
    reason: FailedReason
    detail: str | None = None
    instant: datetime | None = None
    via: Via | None = None
    provenance: Provenance | None = None
    bracket_speed: float | None = None
    context: FileContext | None = None

    def __post_init__(self) -> None:
        _check_match_payload(
            self.via,
            self.provenance,
            self.bracket_speed,
            present=self.reason in FAILED_AFTER_MATCH,
        )


@dataclass(frozen=True, slots=True)
class Unmatchable:
    """A file the track cannot place; the data's answer, not a failure of the tool.

    ``nearest_segment`` and ``miss_s`` are absent together only against an empty index.
    ``bracket_s`` and ``detail`` are present whenever a bracketing pair exists, and exist so the
    reporter can compute the ``--max-gap`` remedy and name the trackpoints of an unsound bracket.
    """

    path: str
    instant: datetime
    reason: UnmatchableReason
    nearest_segment: str | None = None
    miss_s: float | None = None
    bracket_s: float | None = None
    detail: str | None = None
    context: FileContext | None = None

    def __post_init__(self) -> None:
        if (self.nearest_segment is None) != (self.miss_s is None):
            raise ValueError("nearest_segment and miss_s are absent together or not at all")


@dataclass(frozen=True, slots=True)
class Planned:
    """A matched, write-eligible file: terminal in a dry run, the writer's only input."""

    path: str
    instant: datetime
    position: Position
    via: Via
    provenance: Provenance
    bracket_speed: float | None = None
    context: FileContext | None = None
    elevation_dropped: bool = False

    def __post_init__(self) -> None:
        _check_match_payload(self.via, self.provenance, self.bracket_speed, present=True)


@dataclass(frozen=True, slots=True)
class Geotagged:
    """A file the writer applied a position to; only the writer can produce it."""

    path: str
    instant: datetime
    position: Position
    via: Via
    provenance: Provenance
    bracket_speed: float | None = None
    context: FileContext | None = None
    elevation_dropped: bool = False

    def __post_init__(self) -> None:
        _check_match_payload(self.via, self.provenance, self.bracket_speed, present=True)


Outcome = Ignored | Protected | Failed | Unmatchable | Planned | Geotagged

VARIANT_NAMES: tuple[str, ...] = (
    "Ignored",
    "Protected",
    "Failed",
    "Unmatchable",
    "Planned",
    "Geotagged",
)


def variant_name(outcome: Outcome) -> str:
    """Return the variant's name; exhaustive over the union."""
    match outcome:
        case Ignored():
            return "Ignored"
        case Protected():
            return "Protected"
        case Failed():
            return "Failed"
        case Unmatchable():
            return "Unmatchable"
        case Planned():
            return "Planned"
        case Geotagged():
            return "Geotagged"
        case _:
            assert_never(outcome)


def reason_key(outcome: Outcome) -> ReasonKey:
    """Return the registry key an outcome renders under; exhaustive over the union."""
    match outcome:
        case Ignored() | Protected() | Failed() | Unmatchable():
            return outcome.reason
        case Planned():
            return SuccessState.PLANNED
        case Geotagged():
            return SuccessState.GEOTAGGED
        case _:
            assert_never(outcome)


def matched_via(outcome: Outcome) -> Via | None:
    """Return the ``via`` an outcome was matched under, if it reached the matcher."""
    match outcome:
        case Protected() | Failed() | Planned() | Geotagged():
            return outcome.via
        case Ignored() | Unmatchable():
            return None
        case _:
            assert_never(outcome)
