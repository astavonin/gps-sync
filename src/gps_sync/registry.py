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


@dataclass(frozen=True, slots=True)
class ReasonSpec:
    """One row of the registry."""

    variant: str
    json: str
    subfolder: str | None
    label: str
    remedy: str | None
    exit_term: int


def _row(
    key: ReasonKey,
    variant: str,
    subfolder: str | None,
    label: str,
    remedy: str | None,
    exit_term: int,
) -> tuple[ReasonKey, ReasonSpec]:
    return key, ReasonSpec(variant, key.value, subfolder, label, remedy, exit_term)


REGISTRY: dict[ReasonKey, ReasonSpec] = dict(
    [
        _row(IgnoredReason.NOT_A_STILL, "Ignored", None, "not a still", None, 0),
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
            "the backend's own diagnostic is quoted",
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
