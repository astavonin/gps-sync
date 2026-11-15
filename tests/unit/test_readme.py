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
    ],
)
def test_readme_carries_the_mandated_disclosure(phrase):
    assert phrase in README


def test_readme_does_not_claim_the_corpus_is_uncommitted():
    assert "Real containers are not committed" not in README
