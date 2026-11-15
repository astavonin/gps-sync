"""Parsing and merging of the real Garmin Connect tracks (read-only use of ``testdata/``)."""

import json

import pytest

from gps_sync.gpx import parse_gpx_file, parse_tracks
from gps_sync.index import build_index
from tests.support import TESTDATA

TRACKS = TESTDATA / "tracks"
MANIFEST = TESTDATA / "MANIFEST.json"

pytestmark = pytest.mark.skipif(not TRACKS.is_dir(), reason="testdata/tracks is not present")


def manifest() -> dict[str, dict]:
    return {t["file"]: t for t in json.loads(MANIFEST.read_text())["tracks"]}


def test_a_real_garmin_file_parses_with_the_manifest_point_count_and_utc_timestamps():
    expected = manifest()["activity_24855779045.gpx"]

    parsed = parse_gpx_file(TRACKS / "activity_24855779045.gpx", "activity_24855779045.gpx")

    assert parsed.kept == expected["n"] == 3968
    assert parsed.dropped == {}
    points = [p for s in parsed.segments for p in s.points]
    assert all(p.time.utcoffset().total_seconds() == 0 for p in points)
    assert points[0].time.isoformat() == expected["t0"]
    assert points[-1].time.isoformat() == expected["t1"]
    assert all(p.ele is not None for p in points)


def test_all_seven_tracks_merge_to_20755_points_and_every_point_still_resolves_to_its_file():
    parsed = parse_tracks(TRACKS)

    index = build_index(s for f in parsed.files for s in f.segments)

    assert len(parsed.files) == 7
    assert sum(f.kept for f in parsed.files) == 20_755
    assert len(index) == 20_755
    sources = {p.segment.split("#")[0] for p in index.points}
    assert sources == set(manifest())
    for point in index.points[::500]:
        entry = manifest()[point.segment.split("#")[0]]
        assert entry["t0"] <= point.time.isoformat() <= entry["t1"]
    assert list(index.times) == sorted(index.times)
    assert index.overlaps == ()
