"""The coverage regression over the real photo archive: skipped unless explicitly enabled.

It reads ``/Volumes/Data SSD 2TB`` and ``~/Downloads/tracks``, both strictly read-only (a dry
run never writes the media tree), and only when ``GPS_SYNC_REFERENCE_ARCHIVE=1`` is set and the
volume is mounted.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import REPO

ARCHIVE = Path("/Volumes/Data SSD 2TB/PhotoArchive/2026/Issyk Kul")
TRACKS = Path.home() / "Downloads" / "tracks"

pytestmark = pytest.mark.skipif(
    os.environ.get("GPS_SYNC_REFERENCE_ARCHIVE") != "1"
    or not ARCHIVE.is_dir()
    or not TRACKS.is_dir(),
    reason="set GPS_SYNC_REFERENCE_ARCHIVE=1 with /Volumes/Data SSD 2TB mounted to run",
)


def dry_run(tmp_path: Path, *extra: str) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gps_sync",
            "--media",
            str(ARCHIVE),
            "--tracks",
            str(TRACKS),
            "--review-dir",
            str(tmp_path / "review"),
            "--json",
            *extra,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "src"), "HOME": str(tmp_path)},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_default_coverage_is_323_of_349_and_extend_90m_recovers_all_26(tmp_path):
    plain = dry_run(tmp_path)
    coverage = plain["summary"]["coverage"]
    assert (coverage["matched"], coverage["reached_matcher"]) == (323, 349)
    assert coverage["rate_percent"] == pytest.approx(92.6, abs=0.05)

    extended = dry_run(tmp_path, "--extend", "90m")
    files = [f for f in extended["files"] if f.get("via") == "EXTENDED"]
    assert len(files) == 26
    assert extended["summary"]["coverage"]["by_via"]["EXTENDED"] == 26
    assert all("overridden_reason" in f["provenance"] for f in files)
