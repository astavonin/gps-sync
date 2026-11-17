"""Run-level figures, computed once so the report and the JSON record cannot disagree."""

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from gps_sync.exitcode import ArtifactStatus
from gps_sync.gpx import GpxFile
from gps_sync.index import Overlap
from gps_sync.model import (
    ALTITUDE_SPECS,
    FAILED_AFTER_MATCH,
    GPX_PATH_NO_ELEVATION,
    VARIANT_NAMES,
    VIA_SPECS,
    AltitudeOutcome,
    Centroid,
    Extended,
    Failed,
    Geotagged,
    Ignored,
    Outcome,
    Planned,
    Protected,
    ProtectedReason,
    RunLevelReason,
    Unmatchable,
    UnmatchableReason,
    Via,
    matched_via,
    reason_key,
    variant_name,
)
from gps_sync.registry import REGISTRY, spec
from gps_sync.space import VolumeDemand
from gps_sync.writer import Reconciliation


@dataclass(frozen=True, slots=True)
class RemedyFigure:
    """The parameter value that would change the outcome for one refusal reason."""

    reason: str
    parameter: str
    files: int
    smallest_to_recover_any_s: int
    smallest_to_recover_all_s: int


@dataclass(frozen=True, slots=True)
class Coverage:
    """The coverage rate over candidates that reached the matcher."""

    matched: int
    reached_matcher: int
    by_via: dict[str, int]

    @property
    def rate(self) -> float | None:
        """The rate in percent, or None when nothing reached the matcher."""
        if self.reached_matcher == 0:
            return None
        return 100.0 * self.matched / self.reached_matcher


@dataclass(frozen=True, slots=True)
class Sensitivity:
    """The clock-sensitivity figures folded from interpolated Planned/Geotagged outcomes."""

    contributors: int
    excluded: dict[str, int]
    median_speed_mps: float | None

    @property
    def displacement_60s_m(self) -> float | None:
        """Displacement a 60 s clock error causes at the median speed."""
        return None if self.median_speed_mps is None else self.median_speed_mps * 60.0

    @property
    def excluded_total(self) -> int:
        """Total number of files that did not contribute."""
        return sum(self.excluded.values())


@dataclass(frozen=True, slots=True)
class ClampWindow:
    """The clamp-window distribution of extended Planned/Geotagged outcomes."""

    count: int
    smallest_s: float | None
    median_s: float | None
    largest_s: float | None


@dataclass(frozen=True, slots=True)
class Dispersion:
    """The telemetry path's position-quality figure: the farthest used fix from each centroid."""

    count: int
    minimum_m: float | None
    median_m: float | None
    maximum_m: float | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Every figure the report and the JSON record carry beyond the per-file records."""

    variant_counts: dict[str, int]
    reason_counts: dict[str, int]
    success_by_via: dict[str, dict[str, int]]
    coverage: Coverage
    remedies: tuple[RemedyFigure, ...]
    sensitivity: Sensitivity
    clamp_window: ClampWindow
    altitude_unavailable: int
    altitude_breakdown: dict[str, int]
    dispersion: Dispersion


@dataclass(frozen=True, slots=True)
class TelemetryRead:
    """What the telemetry read cost: the phase that dominates a ``--video`` run's wall time."""

    clips: int = 0
    chunks: int = 0
    elapsed_s: float = 0.0

    def as_record(self) -> dict[str, float | int]:
        """Render the figures for the JSON record and the audit verdict."""
        return {"clips": self.clips, "chunks": self.chunks, "elapsed_s": self.elapsed_s}


@dataclass(slots=True)
class RunInfo:
    """Run facts that are not derived from the outcome sequence."""

    mode: str
    media_root: str
    tracks_root: str | None
    exiftool_version: str
    options: dict[str, Any]
    review_root: str
    unprocessed: str
    runs: str
    audit_record: str | None
    pruned: tuple[str, ...]
    gpx_files: tuple[GpxFile, ...]
    duplicate_instants: dict[str, int]
    skipped_track_symlinks: tuple[str, ...]
    overlaps: tuple[Overlap, ...]
    incomplete_prior_records: tuple[tuple[str, str, int], ...]
    artifacts: ArtifactStatus
    reconciliation: Reconciliation | None
    run_level: dict[str, int] = field(default_factory=dict)
    space_demand: tuple[VolumeDemand, ...] = ()
    telemetry_read: TelemetryRead = field(default_factory=TelemetryRead)


def _is_matched(outcome: Outcome) -> bool:
    match outcome:
        case Planned() | Geotagged():
            return True
        case Failed():
            return outcome.reason in FAILED_AFTER_MATCH
        case Protected():
            return outcome.reason is ProtectedReason.POSITION_APPEARED
        case Ignored() | Unmatchable():
            return False
    return False


def _coverage(outcomes: Sequence[Outcome]) -> Coverage:
    unmatchable = sum(isinstance(o, Unmatchable) for o in outcomes)
    matched_outcomes = [o for o in outcomes if _is_matched(o)]
    vias = Counter(matched_via(o) for o in matched_outcomes)
    by_via = {via.value: vias[via] for via in Via}
    if sum(by_via.values()) != len(matched_outcomes):
        raise AssertionError("a matched outcome carries no via the coverage split enumerates")
    return Coverage(
        matched=len(matched_outcomes),
        reached_matcher=len(matched_outcomes) + unmatchable,
        by_via=by_via,
    )


def _remedies(outcomes: Sequence[Outcome]) -> tuple[RemedyFigure, ...]:
    figures: list[RemedyFigure] = []
    for reason, parameter in (
        (UnmatchableReason.MAX_GAP_EXCEEDED, "--max-gap"),
        (UnmatchableReason.SEGMENT_BOUNDARY, "--extend"),
        (UnmatchableReason.OUTSIDE_COVERAGE, "--extend"),
    ):
        values = [
            (o.bracket_s if reason is UnmatchableReason.MAX_GAP_EXCEEDED else o.miss_s)
            for o in outcomes
            if isinstance(o, Unmatchable) and o.reason is reason
        ]
        present = [v for v in values if v is not None]
        if present:
            figures.append(
                RemedyFigure(
                    reason.value,
                    parameter,
                    len(values),
                    math.ceil(min(present)),
                    math.ceil(max(present)),
                )
            )
    return tuple(figures)


def _sensitivity(outcomes: Sequence[Outcome]) -> Sensitivity:
    speeds: list[float] = []
    excluded: Counter[str] = Counter()
    for outcome in outcomes:
        match outcome:
            case Planned() | Geotagged():
                if not VIA_SPECS[outcome.via].fold_admitted:
                    excluded[VIA_SPECS[outcome.via].exclusion_key or "speed_absent"] += 1
                elif outcome.bracket_speed is None:
                    excluded["speed_absent"] += 1
                else:
                    speeds.append(outcome.bracket_speed)
            case Ignored():
                excluded["ignored"] += 1
            case Unmatchable():
                excluded["unmatchable"] += 1
            case Failed():
                excluded["failed"] += 1
            case Protected():
                excluded["protected"] += 1
    return Sensitivity(len(speeds), dict(excluded), statistics.median(speeds) if speeds else None)


def _clamp_window(outcomes: Sequence[Outcome]) -> ClampWindow:
    clamps = [
        o.provenance.clamp_s
        for o in outcomes
        if isinstance(o, Planned | Geotagged) and isinstance(o.provenance, Extended)
    ]
    if not clamps:
        return ClampWindow(0, None, None, None)
    return ClampWindow(len(clamps), min(clamps), statistics.median(clamps), max(clamps))


def summarise(outcomes: Sequence[Outcome]) -> RunSummary:
    """Compute every run-level figure from the complete outcome sequence."""
    variants = Counter(variant_name(o) for o in outcomes)
    reasons = Counter(
        spec(reason_key(o)).json for o in outcomes if not isinstance(o, Planned | Geotagged)
    )
    candidate_reasons = [
        s.json for s in REGISTRY.values() if s.variant not in ("Planned", "Geotagged", "run-level")
    ]
    success: dict[str, dict[str, int]] = {}
    for name, kind in (("Planned", Planned), ("Geotagged", Geotagged)):
        counted = Counter(o.via for o in outcomes if isinstance(o, kind))
        success[name] = {via.value: counted[via] for via in Via}
    planned_or_written = [o for o in outcomes if isinstance(o, Planned | Geotagged)]
    return RunSummary(
        variant_counts={name: variants[name] for name in VARIANT_NAMES},
        reason_counts={name: reasons[name] for name in candidate_reasons},
        success_by_via=success,
        coverage=_coverage(outcomes),
        remedies=_remedies(outcomes),
        sensitivity=_sensitivity(outcomes),
        clamp_window=_clamp_window(outcomes),
        altitude_unavailable=sum(o.position.ele is None for o in planned_or_written),
        altitude_breakdown=_altitude_breakdown(planned_or_written),
        dispersion=_dispersion(planned_or_written),
    )


def _altitude_breakdown(outcomes: Sequence[Planned | Geotagged]) -> dict[str, int]:
    """Break the unavailable-altitude figure out over its declared terms.

    The terms are the altitude outcomes the table marks unavailable plus the GPX-path term, so
    they sum to the figure. An outcome the table does not mark unavailable names files that carry
    an elevation and is outside the breakout by that declaration.
    """
    counts = {m.value: 0 for m in AltitudeOutcome if ALTITUDE_SPECS[m].unavailable}
    gpx = 0
    for outcome in outcomes:
        if isinstance(outcome.provenance, Centroid):
            if ALTITUDE_SPECS[outcome.provenance.altitude].unavailable:
                counts[outcome.provenance.altitude.value] += 1
        elif outcome.position.ele is None:
            gpx += 1
    counts[GPX_PATH_NO_ELEVATION] = gpx
    return counts


def _dispersion(outcomes: Sequence[Planned | Geotagged]) -> Dispersion:
    spreads = [o.provenance.spread_m for o in outcomes if isinstance(o.provenance, Centroid)]
    if not spreads:
        return Dispersion(0, None, None, None)
    return Dispersion(len(spreads), min(spreads), statistics.median(spreads), max(spreads))


def run_level_figures(
    outcomes: Sequence[Outcome],
    *,
    tracks_given: bool,
    reconciliation: Reconciliation | None,
) -> dict[str, int]:
    """Count each run-level condition that fired, keyed on its registry row's JSON string.

    A field is present exactly when its condition fired, so absence means it did not.
    """
    figures: dict[str, int] = {}
    unmatched = sum(isinstance(o, Unmatchable) for o in outcomes)
    if not tracks_given and unmatched:
        figures[RunLevelReason.NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES.value] = unmatched
    dropped = sum(isinstance(o, Planned | Geotagged) and o.elevation_dropped for o in outcomes)
    if dropped:
        figures[RunLevelReason.ELEVATION_NOT_REPRESENTABLE.value] = dropped
    if reconciliation is not None and reconciliation.unintended:
        figures[RunLevelReason.WRITE_UNINTENDED_PATH.value] = len(reconciliation.unintended)
    if reconciliation is not None and reconciliation.unreconciled:
        figures[RunLevelReason.BLOCK_COUNT_UNRECONCILED.value] = reconciliation.unreconciled_figure
    return figures
