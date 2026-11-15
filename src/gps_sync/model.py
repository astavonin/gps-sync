"""The outcome union, its payload shapes and the reason enumerations.

Every file a run discovers terminates as exactly one of six variants. The presence rules the
design states for the optional payload fields are enforced at construction, so an outcome that
violates them cannot exist.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Never


class Via(StrEnum):
    """How a position was derived. Open enumeration: issue 002 adds a third value."""

    INTERPOLATED = "INTERPOLATED"
    EXTENDED = "EXTENDED"


class IgnoredReason(StrEnum):
    """Why an entry never became a candidate."""

    NOT_A_STILL = "NOT_A_STILL"
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


class SuccessState(StrEnum):
    """The two success variants, which carry no reason."""

    PLANNED = "PLANNED"
    GEOTAGGED = "GEOTAGGED"


class RunLevelReason(StrEnum):
    """Reasons that attach to the run rather than to a candidate."""

    WRITE_UNINTENDED_PATH = "WRITE_UNINTENDED_PATH"


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


Provenance = Interpolated | Extended


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
        expected = Interpolated if via is Via.INTERPOLATED else Extended
        if not isinstance(provenance, expected):
            raise ValueError(f"provenance {type(provenance).__name__} does not match via {via}")
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
    """A file carrying a position, or one that gained a position mid-run; never written."""

    path: str
    reason: ProtectedReason
    position: Position | None = None
    instant: datetime | None = None
    via: Via | None = None
    provenance: Provenance | None = None
    bracket_speed: float | None = None
    context: FileContext | None = None

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
