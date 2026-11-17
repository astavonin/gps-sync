"""The reason registry: the single declaring mapping every rendering derives from.

Each row declares a reason's stable JSON string, review subfolder, human label, remedy text and
exit-code term. The reporter, review-folder writer, JSON record and exit-code fold read only this
mapping, and the module refuses to import if any reason enumeration member lacks a row.
"""

from dataclasses import dataclass

from gps_sync.model import (
    FailedReason,
    IgnoredReason,
    ProtectedReason,
    ReasonKey,
    RunLevelReason,
    SuccessState,
    UnmatchableReason,
)

#: Values of ``ReasonSpec.per_file_lines``: whether a reason's files get a line of their own.
PRINTED = "printed"
SUPPRESSED = "suppressed"
NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True, slots=True)
class ReasonSpec:
    """One row of the registry."""

    variant: str
    json: str
    subfolder: str | None
    label: str
    remedy: str | None
    exit_term: int
    per_file_lines: str = PRINTED


def _row(
    key: ReasonKey,
    variant: str,
    subfolder: str | None,
    label: str,
    remedy: str | None,
    exit_term: int,
    per_file_lines: str | None = None,
) -> tuple[ReasonKey, ReasonSpec]:
    """Declare one row; a run-level row names no file unless it declares otherwise."""
    lines = per_file_lines or (NOT_APPLICABLE if variant == "run-level" else PRINTED)
    return key, ReasonSpec(variant, key.value, subfolder, label, remedy, exit_term, lines)


REGISTRY: dict[ReasonKey, ReasonSpec] = dict(
    [
        _row(
            IgnoredReason.NOT_A_STILL,
            "Ignored",
            None,
            "not a candidate file type for this run",
            "if the entry is a video container, pass --video to admit it; otherwise the file "
            "type is outside this tool's scope",
            0,
            SUPPRESSED,
        ),
        _row(
            IgnoredReason.HIDDEN_ENTRY,
            "Ignored",
            None,
            "hidden entry skipped, with its subtree when it is a directory",
            "rename the entry without its leading dot if it should be processed; for a "
            "directory that admits its whole subtree",
            0,
            SUPPRESSED,
        ),
        _row(
            IgnoredReason.SYMLINK,
            "Ignored",
            None,
            "symlink skipped",
            "the real target is named when it lies inside the media root, so it is visible that "
            "the file was discovered on its own merit",
            0,
        ),
        _row(
            IgnoredReason.DUPLICATE_INODE,
            "Ignored",
            None,
            "second link to a file already processed",
            "the retained path is named",
            0,
        ),
        _row(
            ProtectedReason.POSITION_PRESENT,
            "Protected",
            "already-tagged",
            "position already present",
            "none needed; the file keeps its position and is linked here only so it can be browsed",
            0,
        ),
        _row(
            ProtectedReason.ZERO_POSITION,
            "Protected",
            "zero-position",
            "position present but zero",
            "clear the coordinates by hand if the position is wrong; the tool will not decide "
            "that for you",
            0,
        ),
        _row(
            ProtectedReason.POSITION_APPEARED,
            "Protected",
            "appeared-mid-run",
            "position appeared between the read and the write",
            "re-run; check for a concurrent writer",
            0,
        ),
        _row(
            UnmatchableReason.MAX_GAP_EXCEEDED,
            "Unmatchable",
            "sampling-dropout",
            "sampling dropout inside one activity",
            "raise --max-gap to at least the bracket width reported for the file",
            0,
        ),
        _row(
            UnmatchableReason.SEGMENT_BOUNDARY,
            "Unmatchable",
            "between-segments",
            "camera used between two activities",
            "use --extend with at least the miss reported for the file; --max-gap is not the "
            "remedy for a segment boundary",
            0,
        ),
        _row(
            UnmatchableReason.OUTSIDE_COVERAGE,
            "Unmatchable",
            "no-track-coverage",
            "instant outside the index's outer ends",
            "use --extend with at least the miss reported for the file, or add the covering "
            "track to the tracks root",
            0,
        ),
        _row(
            UnmatchableReason.BRACKET_UNSOUND,
            "Unmatchable",
            "unsound-bracket",
            "bracket crosses the antimeridian",
            "none; the two trackpoints are named so the track can be inspected",
            0,
        ),
        _row(
            FailedReason.TIME_UNRESOLVABLE,
            "Failed",
            "no-timestamp",
            "no resolvable UTC instant",
            "pass --utc-offset when the file is local-without-offset; otherwise repair the time "
            "tag",
            1,
        ),
        _row(
            FailedReason.METADATA_INDETERMINATE,
            "Failed",
            "unreadable-metadata",
            "metadata could not be read cleanly",
            "inspect the file; the backend's own diagnostic is quoted",
            1,
        ),
        _row(
            FailedReason.UNRECOGNISED_MINOR_WARNING,
            "Failed",
            "unrecognised-warning",
            "the file read, but with a minor warning the tool does not recognise",
            "none; the warning is quoted and the file is not written. A DJI DNG shows this "
            "warning whether or not exiftool has rewritten it, and the allowlist grows only with "
            "evidence",
            1,
        ),
        _row(
            FailedReason.TIME_SEMANTICS_UNVERIFIED,
            "Failed",
            "unverified-time-semantics",
            "the container states no time zone this tool can verify",
            "none; --utc-offset is not admissible for a video container, because a wrong zone "
            "here writes a wrong position into a file that has none",
            1,
        ),
        _row(
            FailedReason.WRITE_UNSUPPORTED_CONTAINER,
            "Failed",
            "unsupported-container",
            "write safety unverified for this container",
            "move the file out of the scanned tree, or take the container through the "
            "verification gate",
            1,
        ),
        _row(
            FailedReason.WRITE_ERROR,
            "Failed",
            "write-failed",
            "the backend refused the write",
            "the backend's own diagnostic is quoted; where that diagnostic is a value refusal, "
            "the refused value is one this tool formatted from data its own declared domain "
            "admitted, so this is a defect in this tool rather than in the file: the target is "
            "unchanged, a re-run reproduces it, and the quoted diagnostic is what to report",
            1,
        ),
        _row(
            FailedReason.WRITE_UNATTRIBUTED,
            "Failed",
            "write-unattributed",
            "the tool cannot establish whether this file changed",
            "inspect by hand against the audit record named in the report; a re-run does not "
            "settle it",
            1,
        ),
        _row(
            FailedReason.WRITE_PARTIAL,
            "Failed",
            "partial-write-no-position",
            "the position itself was refused and the rest of the write set was applied",
            "the target was rewritten and does not carry the position this run planned, and "
            "nothing in this tool reverses that. The refused value is one this tool formatted "
            "from data its own declared domain admitted, so this is a defect in this tool rather "
            "than in the file: report the quoted diagnostic. What a re-run does depends on which "
            "member the detail names, and the two halves are opposite. A refused coordinate "
            "magnitude leaves no readable position, so the re-gate still finds none and a re-run "
            "retries, reproducing this state until the defect is fixed. A refused hemisphere "
            "reference leaves the magnitude on disk with its sign read as positive, so the next "
            "run's gate reports the file as already positioned and never retries: that file "
            "carries a position which may be its own mirror image, and it has to be corrected or "
            "removed by hand",
            1,
        ),
        _row(
            FailedReason.WRITE_INCOMPLETE,
            "Failed",
            "incomplete-write",
            "the position was applied and another declared assignment was refused",
            "the target was rewritten and carries the position this run planned, with the named "
            "tags absent. The refused value is one this tool formatted from data its own "
            "declared domain admitted, so this is a defect in this tool rather than in the file: "
            "report the quoted diagnostic. A re-run will not add the missing tags, because the "
            "file now reports as already positioned and is not written again, so supply them "
            "separately if they are needed",
            1,
        ),
        _row(
            FailedReason.TELEMETRY_UNUSABLE,
            "Failed",
            "unusable-telemetry",
            "the clip's telemetry did not yield a position this tool will write",
            "none; the detail says which case it was - no usable fix among the samples read, or "
            "a fix list spanning more than half the globe - and the GPX track is deliberately "
            "not used as a substitute",
            1,
        ),
        _row(
            FailedReason.TELEMETRY_INCONSISTENT,
            "Failed",
            "inconsistent-telemetry",
            "the clip's telemetry does not agree with itself or with its container record, so no "
            "fix list this tool will average could be formed",
            "the detail names the case and its figures, and a re-run helps only where the "
            "disagreement was in what the backend returned. A count that differs between the "
            "latitude, longitude and sample-time lists, or a sample time that is not a number, is "
            "a defect in the read: re-run once and report the detail if it repeats. A container "
            "record with no readable Duration, or a telemetry scan that ends more than a second "
            "short of the clip's own duration, means the file is truncated or its telemetry "
            "track cannot be read in full: a re-run will not change it and the clip needs "
            "inspecting by hand",
            1,
        ),
        _row(
            FailedReason.WRITE_INSUFFICIENT_SPACE,
            "Failed",
            "insufficient-space",
            "not enough free space for a whole-file rewrite",
            "free at least the file's own size on the volume holding it, or move the file to a "
            "volume with room; a write rewrites the whole file, a still as well as a clip. Under "
            "--keep-original the requirement is larger and accumulates: every retained _original "
            "already planned on that volume stays, so the space needed is this file's size plus "
            "the sum of those - the detail names both figures",
            1,
        ),
        _row(
            FailedReason.WRITE_TARGET_UNEXAMINABLE,
            "Failed",
            "target-unexaminable",
            "the target could not be examined when the write was about to start",
            "the file vanished, moved or became unreadable between discovery and the write, and "
            "nothing was written to it; the detail carries the operating system's own words. "
            "Check that the file is still where the report says and re-run",
            1,
        ),
        _row(SuccessState.PLANNED, "Planned", None, "position planned", None, 0),
        _row(SuccessState.GEOTAGGED, "Geotagged", None, "position written", None, 0),
        _row(
            RunLevelReason.WRITE_UNINTENDED_PATH,
            "run-level",
            None,
            "the backend concluded about a path the run did not intend to write",
            "inspect the completion record, which names the path verbatim, and check for a "
            "concurrent writer",
            1,
        ),
        _row(
            RunLevelReason.NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES,
            "run-level",
            None,
            "no tracks root was given and candidates reached the matcher",
            "those files are Unmatchable (OUTSIDE_COVERAGE), and that reason's own remedy names "
            "a tracks root this run does not have: pass --tracks to match them, or ignore the "
            "reason for this run. The field carries the count",
            0,
        ),
        _row(
            RunLevelReason.ELEVATION_NOT_REPRESENTABLE,
            "run-level",
            None,
            "a derived elevation was dropped as unrepresentable",
            "the affected files carry exactly the position the run planned and no elevation; on "
            "the telemetry path the provenance names the member, on the GPX path it does not. "
            "The field carries the count",
            0,
        ),
        _row(
            RunLevelReason.BLOCK_COUNT_UNRECONCILED,
            "run-level",
            None,
            "the marker-terminated block counts do not reconcile against the intended path set",
            "every path the shortfall prevented isolating is reported WRITE_UNATTRIBUTED and "
            "must be reconciled by hand against the audit record; the field carries the number "
            "of affected paths, plus one for a trailing run of unterminated lines, which names "
            "none. The write-phase section of the report names each shape - a shortfall, a "
            "repeated marker or the trailing run - with the paths it affects, and the audit "
            "record lists every affected path",
            1,
        ),
    ]
)


def _assert_complete() -> None:
    members: list[ReasonKey] = [
        *IgnoredReason,
        *ProtectedReason,
        *UnmatchableReason,
        *FailedReason,
        *SuccessState,
        *RunLevelReason,
    ]
    missing = [m for m in members if m not in REGISTRY]
    if missing:
        raise RuntimeError(f"reason registry is missing rows for: {missing}")


_assert_complete()


def spec(key: ReasonKey) -> ReasonSpec:
    """Return the registry row for a reason."""
    return REGISTRY[key]


def review_subfolders() -> frozenset[str]:
    """Return every declared review subfolder name."""
    return frozenset(s.subfolder for s in REGISTRY.values() if s.subfolder is not None)
