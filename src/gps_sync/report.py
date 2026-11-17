"""The human-readable report.

Matches the variants and the reason enumerations exhaustively, and prints the registry's label
and remedy text for every reason present. The two gap reasons are never summed.
"""

from collections.abc import Sequence

from gps_sync.model import (
    AGGREGATION_SPECS,
    REJECTION_SPECS,
    Centroid,
    Extended,
    Failed,
    Geotagged,
    Ignored,
    IgnoredReason,
    Interpolated,
    Outcome,
    Planned,
    Position,
    Protected,
    ProtectedReason,
    RunLevelReason,
    Unmatchable,
    UnmatchableReason,
    assert_never,
    reason_key,
)
from gps_sync.registry import PRINTED, REGISTRY, spec
from gps_sync.serialise import instant_text
from gps_sync.summary import RunInfo, RunSummary

_BY_NAME = {key.value: row for key, row in REGISTRY.items()}
_REASON_ORDER = [
    s for s in REGISTRY.values() if s.variant not in ("Planned", "Geotagged", "run-level")
]


def _duration(seconds: float) -> str:
    whole = round(seconds)
    if whole >= 3600:
        return f"{whole}s (~{whole / 3600:.2f} h)"
    if whole >= 60:
        return f"{whole}s (~{whole / 60:.1f} min)"
    return f"{whole}s"


def _coordinates(position: Position) -> str:
    text = f"{position.lat:.6f}, {position.lon:.6f}"
    return text if position.ele is None else f"{text}, {position.ele:.1f} m"


def _context(outcome: Outcome) -> str:
    match outcome:
        case Ignored():
            return ""
        case Protected() | Failed() | Unmatchable() | Planned() | Geotagged():
            ctx = outcome.context
            parts = [p for p in ((ctx.make, ctx.model) if ctx else ()) if p]
            return f" [{' '.join(parts)}]" if parts else ""
        case _:
            assert_never(outcome)


def _provenance(outcome: Planned | Geotagged | Protected | Failed) -> str:
    provenance = outcome.provenance
    if provenance is None:
        return ""
    match provenance:
        case Interpolated():
            return (
                f" via INTERPOLATED (segment {provenance.segment}, "
                f"bracket {_duration(provenance.bracket_s)})"
            )
        case Extended():
            return (
                f" via EXTENDED (segment {provenance.segment}, "
                f"clamp {_duration(provenance.clamp_s)}, "
                f"overrode {provenance.overridden_reason.value})"
            )
        case Centroid():
            dropped = sum(count for _, count in provenance.dropped)
            reasons = ", ".join(
                f"{REJECTION_SPECS[reason].phrase} {count}" for reason, count in provenance.dropped
            )
            return (
                f" via CENTROID ({AGGREGATION_SPECS[provenance.rule].phrase} of "
                f"{provenance.samples_used} of {provenance.samples_read} samples, "
                f"{dropped} dropped ({reasons}), span {provenance.span_s:.2f} s, "
                f"shortfall against the duration {provenance.shortfall_s:.2f} s, "
                f"farthest fix {provenance.spread_m:.1f} m from the position, "
                f"altitude {provenance.altitude.value})"
            )
        case _:
            assert_never(provenance)


def file_line(outcome: Outcome) -> str:
    """Render one per-file line; the refusal detail is on the line, not only in the counts."""
    label = spec(reason_key(outcome)).label
    head = f"{outcome.path}{_context(outcome)}: "
    match outcome:
        case Ignored():
            extra = f" ({outcome.detail})" if outcome.detail else ""
            return f"{head}Ignored/{outcome.reason.value} - {label}{extra}"
        case Protected():
            if outcome.reason is ProtectedReason.POSITION_APPEARED:
                state = (
                    "the file now carries a position whose value this run never read"
                    + _provenance(outcome)
                )
            else:
                assert outcome.position is not None
                state = f"carries {_coordinates(outcome.position)}"
            if outcome.instant:
                when = f", instant {instant_text(outcome.instant)}"
            else:
                when = f", no instant: {outcome.detail}" if outcome.detail else ""
            return f"{head}Protected/{outcome.reason.value} - {label}: {state}{when}"
        case Failed():
            detail = f": {outcome.detail}" if outcome.detail else ""
            return f"{head}Failed/{outcome.reason.value} - {label}{detail}{_provenance(outcome)}"
        case Unmatchable():
            return (
                f"{head}Unmatchable/{outcome.reason.value} - {label}{_unmatchable_detail(outcome)}"
            )
        case Planned() | Geotagged():
            kind = "Planned" if isinstance(outcome, Planned) else "Geotagged"
            altitude = "" if outcome.position.ele is not None else " (altitude not available)"
            return (
                f"{head}{kind} - {label}: {_coordinates(outcome.position)}{altitude}, "
                f"instant {instant_text(outcome.instant)}{_provenance(outcome)}"
            )
        case _:
            assert_never(outcome)


def _unmatchable_detail(outcome: Unmatchable) -> str:
    parts = [f"instant {instant_text(outcome.instant)}"]
    if outcome.nearest_segment is None or outcome.miss_s is None:
        parts.append("the track index was empty, so no segment is named")
    else:
        parts.append(f"nearest segment {outcome.nearest_segment}")
        parts.append(f"miss {_duration(outcome.miss_s)}")
    if outcome.bracket_s is not None:
        parts.append(f"bracket width {_duration(outcome.bracket_s)}")
    if outcome.detail:
        parts.append(outcome.detail)
    return ": " + ", ".join(parts)


def _counts_section(summary: RunSummary, run_level: dict[str, int]) -> list[str]:
    lines = ["Outcomes"]
    for variant, total in summary.variant_counts.items():
        if variant in ("Planned", "Geotagged"):
            by_via = summary.success_by_via[variant]
            detail = ", ".join(f"{via} {n}" for via, n in by_via.items())
            lines.append(f"  {variant}: {total} ({detail})")
            continue
        lines.append(f"  {variant}: {total}")
        for row in _REASON_ORDER:
            if row.variant == variant and summary.reason_counts.get(row.json, 0):
                note = (
                    " - see the run-level diagnostic above: no tracks root was given"
                    if row.json == UnmatchableReason.OUTSIDE_COVERAGE.value
                    and RunLevelReason.NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES.value in run_level
                    else ""
                )
                lines.append(
                    f"    {row.json} ({row.label}): {summary.reason_counts[row.json]}{note}"
                )
    return lines


def _run_level_lines(info: RunInfo) -> list[str]:
    """Print each run-level diagnostic that fired, ahead of the per-file sections it qualifies."""
    lines = [
        f"Run-level diagnostic: {row.label}: {info.run_level[row.json]} - {row.remedy}"
        for row in REGISTRY.values()
        if row.variant == "run-level" and row.json in info.run_level
    ]
    return [*lines, ""] if lines else []


def _space_lines(info: RunInfo) -> list[str]:
    """Print the demand the write run's own per-file check will evaluate, per volume.

    Only a dry run prints it: a write run's report shows the check's outcome, and its figures
    reach the JSON record and the audit verdict instead.
    """
    if info.mode != "dry-run":
        return []
    return [
        f"Free space a write needs on volume {d.volume}: {d.peak_bytes} bytes at peak (the largest "
        f"planned target plus slack); with --keep-original {d.retained_bytes} bytes accumulate"
        for d in info.space_demand
    ]


def _telemetry_read_lines(info: RunInfo) -> list[str]:
    """Print what the telemetry read cost, only when a clip was read for telemetry."""
    read = info.telemetry_read
    if read.clips == 0:
        return []
    return [
        f"Telemetry read: {read.clips} clip(s) in {read.chunks} chunk(s), {read.elapsed_s:.1f} s"
    ]


def _altitude_lines(summary: RunSummary) -> list[str]:
    parts = ", ".join(f"{name} {count}" for name, count in summary.altitude_breakdown.items())
    return [
        f"Altitude not available for {summary.altitude_unavailable} planned/written file(s) "
        f"({parts})"
    ]


def _dispersion_lines(summary: RunSummary) -> list[str]:
    dispersion = summary.dispersion
    if dispersion.count == 0 or dispersion.median_m is None:
        return []
    return [
        f"Telemetry centroid dispersion: {dispersion.count} file(s), farthest used fix from the "
        f"written position - minimum {dispersion.minimum_m:.1f} m, "
        f"median {dispersion.median_m:.1f} m, maximum {dispersion.maximum_m:.1f} m"
    ]


def _coverage_line(summary: RunSummary) -> str:
    coverage = summary.coverage
    if coverage.rate is None:
        return (
            "Coverage: not applicable - nothing reached the matcher, which is the healthy reading "
            "for an idempotent re-run and not a regression"
        )
    split = ", ".join(f"{via} {count}" for via, count in coverage.by_via.items())
    return (
        f"Coverage: {coverage.matched}/{coverage.reached_matcher} = {coverage.rate:.1f} % ({split})"
    )


def _remedy_lines(summary: RunSummary) -> list[str]:
    lines: list[str] = []
    for figure in summary.remedies:
        row = _BY_NAME[figure.reason]
        if figure.parameter == "--max-gap":
            lines.append(
                f"  {figure.reason} ({row.label}), {figure.files} file(s): --max-gap "
                f"{figure.smallest_to_recover_all_s}s would admit all of them "
                f"(smallest single bracket {figure.smallest_to_recover_any_s}s)"
            )
        else:
            note = (
                "; --max-gap is not the remedy for a segment boundary"
                if figure.reason == "SEGMENT_BOUNDARY"
                else ""
            )
            lines.append(
                f"  {figure.reason} ({row.label}), {figure.files} file(s): --extend "
                f"{figure.smallest_to_recover_all_s}s would recover all of them "
                f"(smallest single miss {figure.smallest_to_recover_any_s}s){note}"
            )
    return lines


def _remedy_text_lines(summary: RunSummary) -> list[str]:
    lines: list[str] = []
    for row in _REASON_ORDER:
        if summary.reason_counts.get(row.json, 0) and row.remedy:
            lines.append(f"  {row.json}: {row.remedy}")
    return lines


def _sensitivity_lines(summary: RunSummary) -> list[str]:
    sensitivity = summary.sensitivity
    excluded = ", ".join(f"{k} {v}" for k, v in sorted(sensitivity.excluded.items())) or "none"
    if sensitivity.median_speed_mps is None:
        lines = [
            "Clock sensitivity: not applicable - no interpolated Planned or Geotagged file with a "
            f"defined bracket speed contributed ({excluded} excluded)"
        ]
    else:
        lines = [
            f"Clock sensitivity: median speed at the photo instants "
            f"{sensitivity.median_speed_mps:.2f} m/s; a 60 s clock error would displace the median "
            f"photo by {sensitivity.displacement_60s_m:.1f} m "
            f"({sensitivity.contributors} file(s) contributed; {sensitivity.excluded_total} "
            f"excluded: {excluded}; only interpolated Planned/Geotagged files with a defined "
            "bracket speed count)"
        ]
    window = summary.clamp_window
    if window.count:
        assert window.smallest_s is not None
        assert window.median_s is not None
        assert window.largest_s is not None
        lines.append(
            f"Extended clamp windows: {window.count} file(s), "
            f"smallest {_duration(window.smallest_s)}, "
            f"median {_duration(window.median_s)}, largest {_duration(window.largest_s)}"
        )
    return lines


def render_report(outcomes: Sequence[Outcome], summary: RunSummary, info: RunInfo) -> str:
    """Render the full human-readable report."""
    lines: list[str] = []
    for name, created, count in info.incomplete_prior_records:
        lines.append(
            f"WARNING: incomplete prior run {name} (created {created}) is still in its intent "
            f"phase and lists {count} path(s); some of them may now carry new positions. "
            "Inspect them against that record."
        )
    lines.append(
        "gps-sync dry run (no media file is modified)"
        if info.mode == "dry-run"
        else "gps-sync write run"
    )
    lines += [
        f"media:    {info.media_root}",
        f"tracks:   {info.tracks_root if info.tracks_root is not None else '(none given)'}",
        f"exiftool: {info.exiftool_version}",
        "",
        "Run artifacts",
        f"  review folder: {info.unprocessed}",
        f"  audit records: {info.runs}",
        "  audit record for this run: "
        + (info.audit_record or "none (a dry run writes no audit record)"),
    ]
    lines += [f"  pruned from discovery: {path}" for path in info.pruned]
    lines.append("")
    lines.append("Tracks")
    for gpx in info.gpx_files:
        drops = ", ".join(f"{k.value} {v}" for k, v in sorted(gpx.dropped.items())) or "none"
        duplicates = info.duplicate_instants.get(gpx.relative_path, 0)
        lines.append(
            f"  {gpx.relative_path}: {gpx.kept} point(s) kept, dropped {drops}, "
            f"duplicate instants collapsed {duplicates}"
        )
    lines += [f"  skipped symlink in tracks root: {p}" for p in info.skipped_track_symlinks]
    lines += [
        f"  overlap: {o.segment_a} and {o.segment_b} from {instant_text(o.start)} to "
        f"{instant_text(o.end)}; brackets inside it are refused as SEGMENT_BOUNDARY"
        for o in info.overlaps
    ]
    lines.append("")
    lines += _run_level_lines(info)
    lines += _counts_section(summary, info.run_level)
    lines.append(_coverage_line(summary))
    lines += _space_lines(info)
    lines += _telemetry_read_lines(info)
    lines += _altitude_lines(summary)
    lines += _dispersion_lines(summary)
    figures = _remedy_lines(summary)
    texts = _remedy_text_lines(summary)
    if figures or texts:
        lines += ["", "Remedies", *figures, *texts]
    lines += ["", *_sensitivity_lines(summary), ""]
    listed = [o for o in outcomes if spec(reason_key(o)).per_file_lines == PRINTED]
    lines += [f"  {file_line(o)}" for o in listed if isinstance(o, Ignored)]
    lines += [f"  {file_line(o)}" for o in listed if not isinstance(o, Ignored)]
    if any(isinstance(o, Ignored) and o.reason is IgnoredReason.DUPLICATE_INODE for o in outcomes):
        lines += [
            "",
            "Note: a second hard link to a file already processed was skipped. After a write the "
            "other links keep pointing at the pre-write content, because the write replaces the "
            "file it names.",
        ]
    if info.reconciliation is not None:
        reconciliation = info.reconciliation
        lines.append("")
        exit_text = (
            "exiftool did not run"
            if reconciliation.returncode is None
            else f"exiftool exited {reconciliation.returncode}"
        )
        lines.append(
            f"Write phase: {exit_text}; last marker present: {reconciliation.last_marker or 'none'}"
        )
        if reconciliation.process_failure:
            lines.append(f"  process failure: {reconciliation.process_failure}")
        for shape in reconciliation.unreconciled:
            row = _BY_NAME["BLOCK_COUNT_UNRECONCILED"]
            lines.append(f"  BLOCK_COUNT_UNRECONCILED ({row.label}): {shape}")
        for unintended in reconciliation.unintended:
            row = _BY_NAME["WRITE_UNINTENDED_PATH"]
            lines.append(
                f"  WRITE_UNINTENDED_PATH ({row.label}): {unintended.path} "
                f"{list(unintended.lines)} - "
                f"{row.remedy}"
            )
    if info.artifacts.problems:
        lines += ["", "Run artifacts incomplete"]
        lines += [f"  {problem}" for problem in info.artifacts.problems]
    if info.mode == "dry-run":
        lines += [
            "",
            "A clean dry run does not guarantee a clean write run: the backend can still fail on a "
            "file, be interrupted, or find a position that appeared since the read.",
        ]
    return "\n".join(lines) + "\n"
