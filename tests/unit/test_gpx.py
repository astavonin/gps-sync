from datetime import timedelta
from pathlib import Path

import pytest

from gps_sync.errors import ToolFailure, TrackIdentityCollision
from gps_sync.gpx import DropReason, parse_gpx_file, parse_tracks, segment_id
from gps_sync.index import build_index
from gps_sync.matcher import Match, NoCoverage, match
from tests.support import T0, at, gpx_text, segment

GOOD = [
    ("2026-10-26T04:00:00Z", "42.0", "77.0", "100"),
    ("2026-10-26T04:00:10Z", "42.001", "77.0", "110"),
]


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("defect", "point", "reason"),
    [
        ("no time", (None, "42.0", "77.0", "1"), DropReason.TIME_ABSENT),
        ("bad time", ("yesterday", "42.0", "77.0", "1"), DropReason.TIME_UNPARSEABLE),
        ("naive time", ("2026-10-26T04:00:05", "42.0", "77.0", "1"), DropReason.TIME_UNPARSEABLE),
        (
            "non-numeric lat",
            ("2026-10-26T04:00:05Z", "north", "77.0", "1"),
            DropReason.COORDINATE_INVALID,
        ),
        ("NaN lon", ("2026-10-26T04:00:05Z", "42.0", "NaN", "1"), DropReason.COORDINATE_INVALID),
        (
            "infinite lat",
            ("2026-10-26T04:00:05Z", "inf", "77.0", "1"),
            DropReason.COORDINATE_INVALID,
        ),
        ("lat 91", ("2026-10-26T04:00:05Z", "91", "77.0", "1"), DropReason.COORDINATE_OUT_OF_RANGE),
        (
            "lon 181",
            ("2026-10-26T04:00:05Z", "42.0", "181", "1"),
            DropReason.COORDINATE_OUT_OF_RANGE,
        ),
        (
            "overflowing lat",
            ("2026-10-26T04:00:05Z", "1e999", "77.0", "1"),
            DropReason.COORDINATE_INVALID,
        ),
        ("null island", ("2026-10-26T04:00:05Z", "0", "0", "1"), DropReason.ZERO_SENTINEL),
    ],
)
def test_each_defective_point_is_dropped_and_counted_while_the_rest_survive(
    tmp_path, defect, point, reason
):
    path = write(tmp_path, "t.gpx", gpx_text([GOOD[0], point, GOOD[1]]))

    parsed = parse_gpx_file(path, "t.gpx")

    assert parsed.dropped == {reason: 1}, defect
    assert parsed.kept == 2


def test_a_point_on_one_zero_coordinate_alone_is_not_the_sentinel(tmp_path):
    path = write(tmp_path, "t.gpx", gpx_text([GOOD[0], ("2026-10-26T04:00:05Z", "0", "77.0", "1")]))

    assert parse_gpx_file(path, "t.gpx").kept == 2


def test_a_dropped_point_widens_the_bracket_which_max_gap_then_judges_on_its_merits(tmp_path):
    points = [
        ("2026-10-26T04:00:00Z", "42.0", "77.0", "1"),
        ("2026-10-26T04:01:00Z", "91", "77.0", "1"),
        ("2026-10-26T04:02:30Z", "42.002", "77.0", "1"),
    ]
    parsed = parse_gpx_file(write(tmp_path, "t.gpx", gpx_text(points)), "t.gpx")
    index = build_index(parsed.segments)
    refused = match(index, T0 + timedelta(seconds=60), timedelta(seconds=120), timedelta(0))
    accepted = match(index, T0 + timedelta(seconds=60), timedelta(seconds=150), timedelta(0))

    assert isinstance(refused, NoCoverage)
    assert isinstance(accepted, Match)


def test_a_point_without_elevation_is_kept_and_its_bracket_has_no_elevation(tmp_path):
    points = [GOOD[0], ("2026-10-26T04:00:10Z", "42.001", "77.0", None)]
    parsed = parse_gpx_file(write(tmp_path, "t.gpx", gpx_text(points)), "t.gpx")

    result = match(build_index(parsed.segments), at(5), timedelta(seconds=120), timedelta(0))

    assert parsed.kept == 2
    assert isinstance(result, Match)
    assert result.position.ele is None


@pytest.mark.parametrize(
    "points",
    [
        [(None, "42.0", "77.0", "1")],
        [("2026-10-26T04:00:00Z", "91", "77.0", "1")],
        [],
    ],
    ids=["no timed points", "every point dropped", "no points at all"],
)
def test_a_file_without_a_usable_point_is_a_tool_failure_naming_the_file(tmp_path, points):
    path = write(tmp_path, "broken.gpx", gpx_text(points))

    with pytest.raises(ToolFailure, match=r"broken\.gpx"):
        parse_gpx_file(path, "broken.gpx")


def test_segment_identities_distinguish_trk_and_trkseg_elements(tmp_path):
    text = (
        '<gpx xmlns="http://www.topografix.com/GPX/1/1">'
        '<trk><trkseg><trkpt lat="42" lon="77"><time>2026-10-26T04:00:00Z</time></trkpt></trkseg>'
        '<trkseg><trkpt lat="42" lon="77"><time>2026-10-26T04:01:00Z</time></trkpt></trkseg></trk>'
        '<trk><trkseg><trkpt lat="42" lon="77"><time>2026-10-26T04:02:00Z</time></trkpt></trkseg>'
        "</trk></gpx>"
    )

    parsed = parse_gpx_file(write(tmp_path, "d/f.gpx", text), "d/f.gpx")

    assert [s.id for s in parsed.segments] == [
        "d/f.gpx#trk0seg0",
        "d/f.gpx#trk0seg1",
        "d/f.gpx#trk1seg0",
    ]
    assert parsed.segments[0].id == segment_id("d/f.gpx", 0, 0)


def test_same_basename_in_different_subdirectories_gets_distinct_identities(tmp_path):
    for sub in ("2026-10", "2026-11"):
        write(tmp_path, f"{sub}/activity.gpx", gpx_text(GOOD))

    parsed = parse_tracks(tmp_path)

    ids = [s.id for f in parsed.files for s in f.segments]
    assert ids == ["2026-10/activity.gpx#trk0seg0", "2026-11/activity.gpx#trk0seg0"]
    assert len(set(ids)) == 2


def test_a_constructed_identity_collision_is_a_tool_failure_naming_both_sources():
    first = segment("same#trk0seg0", [(0, 42.0, 77.0, None)], source="one.gpx")
    second = segment("same#trk0seg0", [(5, 42.0, 77.0, None)], source="two.gpx")

    with pytest.raises(TrackIdentityCollision, match=r"one\.gpx.*two\.gpx"):
        build_index([first, second])


@pytest.mark.parametrize(
    "prologue",
    [
        '<!DOCTYPE gpx [<!ENTITY x "boom">]>',
        '<!DOCTYPE gpx SYSTEM "http://example.com/x.dtd">',
        '<!DOCTYPE gpx [<!ENTITY % p "q">]>',
    ],
)
def test_doctype_and_entity_declarations_are_rejected_rather_than_expanded(tmp_path, prologue):
    text = gpx_text(GOOD).replace("?>\n", f"?>\n{prologue}\n", 1)

    with pytest.raises(ToolFailure, match="DOCTYPE"):
        parse_gpx_file(write(tmp_path, "evil.gpx", text), "evil.gpx")


def test_malformed_xml_is_a_tool_failure_naming_the_file(tmp_path):
    with pytest.raises(ToolFailure, match=r"bad\.gpx"):
        parse_gpx_file(write(tmp_path, "bad.gpx", "<gpx><trk>"), "bad.gpx")


def test_symlinks_in_the_tracks_root_are_skipped_and_counted(tmp_path):
    write(tmp_path, "real.gpx", gpx_text(GOOD))
    (tmp_path / "link.gpx").symlink_to(tmp_path / "real.gpx")

    parsed = parse_tracks(tmp_path)

    assert [f.relative_path for f in parsed.files] == ["real.gpx"]
    assert parsed.skipped_symlinks == (str(tmp_path / "link.gpx"),)


def test_a_tracks_root_with_no_gpx_is_a_tool_failure(tmp_path):
    with pytest.raises(ToolFailure, match="no GPX"):
        parse_tracks(tmp_path)


def test_a_missing_tracks_root_is_a_tool_failure(tmp_path):
    with pytest.raises(ToolFailure):
        parse_tracks(tmp_path / "absent")


def test_namespaced_extensions_are_ignored_without_error(tmp_path):
    extension = (
        '<extensions xmlns:ns3="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        "<ns3:TrackPointExtension><ns3:hr>120</ns3:hr></ns3:TrackPointExtension></extensions>"
    )
    text = (
        '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
        f'<trkpt lat="42" lon="77"><ele>5</ele><time>2026-10-26T04:00:00Z</time>{extension}</trkpt>'
        "</trkseg></trk></gpx>"
    )

    assert parse_gpx_file(write(tmp_path, "g.gpx", text), "g.gpx").kept == 1
