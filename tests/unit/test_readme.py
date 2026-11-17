"""The README is a deliverable: every disclosure the design mandates must be present."""

import pytest

from tests.support import REPO

README = (REPO / "README.md").read_text()


@pytest.mark.parametrize(
    "phrase",
    [
        # the three parameter definitions that are gps-sync's own, and the sign convention
        "`--max-gap` is the width of the bracketing interval",
        "`--extend D` clamps to the nearer endpoint",
        "`--time-shift` is added to the file's resolved UTC instant",
        "`--time-shift +60s`",
        "GeoMaxIntSecs",
        "-geosync",
        # byte-level changes and the filesystem-attribute consequence of -overwrite_original
        "52,968",
        "-2,674",
        "`-overwrite_original`",
        "extended attributes (including Finder tags), the creation date, and the inode",
        "`--keep-original`",
        # a DJI DNG is refused whether or not exiftool rewrote it, and the consequence
        "A DJI DNG is refused whether or not exiftool has rewritten it",
        "no rewrite is needed to reach it",
        "no DJI DNG from this drone can be written at all",
        "A positionless DNG from a DJI drone is refused in practice",
        "`UNRECOGNISED_MINOR_WARNING`",
        "the allowlist grows only on measured evidence",
        # a read-only file is not protected
        "A read-only file is not protected",
        "only the *directory's* writability matters",
        # hard links, exclusive access
        "every other hard link continues to point at the pre-write content",
        "gps-sync assumes nothing else modifies the media tree during a run",
        # the corpus is committed and pinned, not absent from the repository
        "The fixture corpus is committed and pinned",
        "353 MB across 19 files",
        "The suite skips only when `testdata/` has been removed locally",
        "restored from the repository, not skipped past",
        "pinned by `testdata/.baseline.md5`",
        "`GPS_SYNC_CORPUS_BASELINE=ignore`",
        # NFR7: position quality
        "`Protected` makes no claim about a position's *quality*",
        "median fix age of 329 s",
        "80 of them share one coordinate across 4.3 hours",
        # issue 002: video
        "A video write rewrites the whole file.",
        "a full-size temporary file is created in the target's own directory",
        "plus 1 MiB, on the clip's own volume",
        "it would add 78.80 GB",
        "A written altitude on a drone clip is the aircraft's altitude",
        "The position is written to `Keys:GPSCoordinates` as an ISO 6709 value",
        "A container whose time semantics this project has not verified is refused "
        "rather than guessed at",
        "A hidden entry is skipped on every run, including a bare one.",
        "so a deliberately hidden photograph is no longer geotagged",
        "A clip whose telemetry scan covers less than its own duration is refused",
        "An elevation the file format cannot hold is dropped, "
        "and the position is written without it.",
        "A video already carrying a container position this tool cannot read is refused, "
        "not written.",
        "When the backend refuses one assignment of a multi-assignment write, "
        "the target has still been rewritten.",
        "which the next run reports as already positioned and never retries",
        "There is deliberately no target that rewrites the baseline in place.",
        "`N` is the operating system's device number for that filesystem",
        "`space_demand` list of the JSON record, which a write run carries too",
        "`telemetry_read` object (`clips`, `chunks`, `elapsed_s`",
        "`GPX_PATH_NO_ELEVATION`; the JSON record's `summary.altitude_unavailable` is an object",
        "`summary.centroid_dispersion`",
        "The JSON record's `run_level` object carries one count per condition that fired",
    ],
)
def test_readme_carries_the_mandated_disclosure(phrase):
    assert phrase in README


def test_readme_does_not_claim_the_corpus_is_uncommitted():
    assert "Real containers are not committed" not in README
