"""The write set per container class, the signed ISO 6709 formatter and the elevation guard."""

import math

import pytest

from gps_sync.errors import InternalConsistencyError
from gps_sync.model import Position
from gps_sync.writer import assert_targets_writable, build_block, build_write_lines
from gps_sync.writeset import (
    CONTAINER_CLASS,
    ELEVATION_CEILING,
    QUICKTIME_REGATE,
    STILL_REGATE,
    WriteSetClass,
    assignments,
    declared_tags,
    is_representable_elevation,
    iso6709,
    regate_condition,
    without_unrepresentable_elevation,
    write_set_class,
)
from tests.support import at
from tests.unit.test_writer import planned


class TestTheIso6709Formatter:
    @pytest.mark.parametrize(
        ("lat", "lon", "expected"),
        [
            (33.868812345, 151.209387654, "33.868812345 151.209387654"),
            (33.868812345, -151.209387654, "33.868812345 -151.209387654"),
            (-33.868812345, 151.209387654, "-33.868812345 151.209387654"),
            (-33.868812345, -151.209387654, "-33.868812345 -151.209387654"),
        ],
        ids=["north-east", "north-west", "south-east", "south-west"],
    )
    def test_each_quadrant_keeps_both_signs(self, lat, lon, expected):
        assert iso6709(Position(lat, lon)) == expected

    def test_a_negative_altitude_keeps_its_sign_and_four_decimals(self):
        assert iso6709(Position(-33.868812345, -151.209387654, -1234.5678)) == (
            "-33.868812345 -151.209387654 -1234.5678"
        )

    def test_a_positive_altitude_is_the_third_component(self):
        assert iso6709(Position(42.0, 77.0, 2189.9)) == "42.000000000 77.000000000 2189.9000"

    def test_no_elevation_means_a_two_component_value(self):
        assert len(iso6709(Position(42.0, 77.0)).split()) == 2

    def test_coordinates_are_nine_decimals_and_rounded(self):
        assert iso6709(Position(42.1234567896, 77.0)).split()[0] == "42.123456790"

    def test_a_negative_zero_carries_no_sign(self):
        assert iso6709(Position(-0.0, -0.0, -0.0)) == "0.000000000 0.000000000 0.0000"


class TestTheDeclaredSets:
    def test_a_still_with_an_elevation_emits_eight_members_four_of_them_position_bearing(self):
        members = assignments(WriteSetClass.EXIF_STILL, Position(-1.5, 2.5, -3.0), at(30))

        assert [m.tag for m in members] == [
            "GPSLatitude",
            "GPSLatitudeRef",
            "GPSLongitude",
            "GPSLongitudeRef",
            "GPSAltitude",
            "GPSAltitudeRef",
            "GPSDateStamp",
            "GPSTimeStamp",
        ]
        assert [m.tag for m in members if m.position_bearing] == [
            "GPSLatitude",
            "GPSLatitudeRef",
            "GPSLongitude",
            "GPSLongitudeRef",
        ]

    def test_a_still_encodes_signs_as_magnitudes_and_references(self):
        values = {
            m.tag: m.value
            for m in assignments(WriteSetClass.EXIF_STILL, Position(-1.5, -2.5, -3.0), at(30))
        }

        assert values["GPSLatitude"] == "1.500000000"
        assert values["GPSLatitudeRef"] == "S"
        assert values["GPSLongitudeRef"] == "W"
        assert values["GPSAltitude"] == "3.0000"
        assert values["GPSAltitudeRef"] == "1"

    def test_a_northern_eastern_above_sea_level_still_uses_the_other_literals(self):
        values = {
            m.tag: m.value
            for m in assignments(WriteSetClass.EXIF_STILL, Position(1.5, 2.5, 3.0), at(30))
        }

        assert (values["GPSLatitudeRef"], values["GPSLongitudeRef"]) == ("N", "E")
        assert values["GPSAltitudeRef"] == "0"

    def test_a_still_without_an_elevation_emits_six_members(self):
        tags = [m.tag for m in assignments(WriteSetClass.EXIF_STILL, Position(1.5, 2.5), at(30))]

        assert len(tags) == 6
        assert "GPSAltitude" not in tags and "GPSAltitudeRef" not in tags

    def test_the_date_and_time_stamps_are_the_utc_instant(self):
        values = {
            m.tag: m.value
            for m in assignments(WriteSetClass.EXIF_STILL, Position(1.5, 2.5), at(3661))
        }

        assert values["GPSDateStamp"] == "2026:10:26"
        assert values["GPSTimeStamp"] == "05:01:01"

    def test_a_quicktime_container_receives_exactly_one_position_bearing_assignment(self):
        members = assignments(WriteSetClass.QUICKTIME_ATOM, Position(1.5, 2.5, 3.0), at(30))

        assert len(members) == 1
        assert members[0].tag == "Keys:GPSCoordinates"
        assert members[0].name == "GPSCoordinates"
        assert members[0].position_bearing
        assert members[0].line == "-Keys:GPSCoordinates=1.500000000 2.500000000 3.0000"

    def test_declared_tags_match_each_classs_members(self):
        assert declared_tags(WriteSetClass.QUICKTIME_ATOM) == {"Keys:GPSCoordinates"}
        assert len(declared_tags(WriteSetClass.EXIF_STILL)) == 8

    @pytest.mark.parametrize(
        ("identity", "expected"),
        [
            ("RAF", WriteSetClass.EXIF_STILL),
            ("dng", WriteSetClass.EXIF_STILL),
            ("MP4", WriteSetClass.QUICKTIME_ATOM),
            ("mov", WriteSetClass.QUICKTIME_ATOM),
            ("JPEG", None),
            ("M4V", None),
            ("", None),
            (None, None),
        ],
    )
    def test_only_verified_identities_have_a_class(self, identity, expected):
        assert write_set_class(identity) is expected

    def test_the_allowlist_is_exactly_the_four_verified_identities(self):
        assert set(CONTAINER_CLASS) == {"RAF", "DNG", "MP4", "MOV"}


class TestTheRegateConditions:
    def test_a_still_names_composite_alone(self):
        assert (
            regate_condition(WriteSetClass.EXIF_STILL)
            == STILL_REGATE
            == ("not $Composite:GPSPosition")
        )

    def test_a_quicktime_container_names_the_three_atom_tags_beside_composite(self):
        condition = regate_condition(WriteSetClass.QUICKTIME_ATOM)

        assert condition == QUICKTIME_REGATE
        for tag in ("Composite:GPSPosition", "Keys:GPSCoordinates", "UserData:GPSCoordinates"):
            assert f"not ${tag}" in condition
        assert "not $ItemList:GPSCoordinates" in condition


class TestTheBlockIsBuiltFromTheDeclaredSet:
    def test_a_clip_block_carries_the_atom_assignment_and_the_quicktime_condition(self):
        block = build_block(planned("/m/a.MP4", container="MP4"), keep_original=False)

        assert block.write_class is WriteSetClass.QUICKTIME_ATOM
        assert block.lines[block.lines.index("-if") + 1] == QUICKTIME_REGATE
        assert [line for line in block.lines if line.startswith("-Keys:")] == [
            "-Keys:GPSCoordinates=42.500000000 78.250000000 1500.5000"
        ]
        assert not any(line.startswith(("-GPS", "-XMP")) for line in block.lines)

    def test_a_clip_block_never_carries_the_ee_flag_or_a_geotag_option(self):
        block = build_block(planned("/m/a.MOV", container="MOV"), keep_original=True)

        assert "-ee" not in block.lines
        assert not any("geo" in line.lower() for line in block.lines)

    def test_a_still_block_has_the_declared_option_prefix_and_marker_suffix(self):
        lines = build_block(planned("/m/a.RAF"), keep_original=False).lines

        assert lines[:4] == ("-overwrite_original", "-n", "-if", "not $Composite:GPSPosition")
        assert lines[-6:] == ("-echo3", "/m/a.RAF", "-echo4", "/m/a.RAF", "/m/a.RAF", "-execute")

    def test_keep_original_drops_the_overwrite_option(self):
        assert "-overwrite_original" in build_block(planned("/m/a.RAF"), keep_original=False).lines
        assert (
            "-overwrite_original" not in build_block(planned("/m/a.RAF"), keep_original=True).lines
        )

    def test_a_target_outside_the_allowlist_cannot_build_a_block(self):
        with pytest.raises(InternalConsistencyError) as raised:
            build_block(planned("/m/a.HEIC", container="HEIC"), keep_original=False)

        assert str(raised.value) == (
            "write target /m/a.HEIC has container identity 'HEIC', outside the allowlist"
        )

    def test_a_target_with_no_context_cannot_build_a_block(self):
        target = planned("/m/a.RAF")
        object.__setattr__(target, "context", None)

        with pytest.raises(InternalConsistencyError, match="outside the allowlist"):
            build_write_lines([target], keep_original=False)

    def test_the_assertion_accepts_both_classes(self):
        assert_targets_writable([planned("/m/a.RAF"), planned("/m/b.MP4", container="MP4")])

    def test_the_assertion_rejects_a_clip_whose_block_would_leave_its_set(self, monkeypatch):
        from gps_sync import writer
        from gps_sync.writeset import Assignment

        monkeypatch.setattr(
            writer,
            "assignments",
            lambda *_: (Assignment("XMP-exif:GPSLatitude", "1", True),),
        )

        with pytest.raises(InternalConsistencyError, match="outside the quicktime-atom write set"):
            assert_targets_writable([planned("/m/a.MP4", container="MP4")])


class TestTheElevationGuard:
    @pytest.mark.parametrize(
        "ele",
        [0.0, -0.0, 1500.5, -1234.5678, ELEVATION_CEILING, -ELEVATION_CEILING, 1e-300],
    )
    def test_a_finite_elevation_within_the_ceiling_is_representable(self, ele):
        assert is_representable_elevation(ele)

    @pytest.mark.parametrize(
        "ele",
        [
            math.nan,
            math.inf,
            -math.inf,
            ELEVATION_CEILING + 1,
            -ELEVATION_CEILING - 1,
            4294967296.0,
            1e12,
            1.7e308,
            -1.7e308,
        ],
    )
    def test_a_non_finite_or_over_ceiling_elevation_is_not_representable(self, ele):
        assert not is_representable_elevation(ele)

    def test_the_ceiling_is_the_unsigned_rational_numerator_limit(self):
        assert ELEVATION_CEILING == 2**32 - 1

    def test_an_unrepresentable_elevation_is_dropped_and_the_position_kept(self):
        position, dropped = without_unrepresentable_elevation(Position(42.0, 77.0, math.nan))

        assert (position, dropped) == (Position(42.0, 77.0, None), True)

    @pytest.mark.parametrize("ele", [None, 100.0, -5.0])
    def test_an_acceptable_or_absent_elevation_passes_through_untouched(self, ele):
        original = Position(42.0, 77.0, ele)

        assert without_unrepresentable_elevation(original) == (original, False)


class TestTheBlockShapeAssertion:
    def block(self, lines, container="RAF"):
        from gps_sync.writer import WriteBlock

        real = build_block(planned("/m/a.RAF", container=container), keep_original=False)
        return WriteBlock(real.write_class, real.assignments, tuple(lines))

    def test_every_block_the_writer_builds_satisfies_its_own_vocabulary(self):
        from gps_sync.writer import assert_block_shape

        for container, path in (("RAF", "/m/a.RAF"), ("MP4", "/m/a.MP4")):
            for keep in (False, True):
                assert_block_shape(
                    build_block(planned(path, container=container), keep_original=keep)
                )

    @pytest.mark.parametrize(
        "line",
        [
            "-geotag",
            "-geotag=/t",
            "-ee",
            "-api",
            "-XMP-exif:GPSLatitude=1",
            "-GPSLatitude",
            "plain",
            "-r",
        ],
    )
    def test_a_line_outside_the_four_shapes_is_refused_before_emission(self, line):
        from gps_sync.writer import assert_block_shape

        lines = [*build_block(planned("/m/a.RAF"), keep_original=False).lines]
        lines.insert(3, line)

        with pytest.raises(InternalConsistencyError, match="outside its vocabulary"):
            assert_block_shape(self.block(lines))

    def test_a_clip_block_may_not_carry_a_still_assignment(self):
        from gps_sync.writer import assert_block_shape

        lines = [*build_block(planned("/m/a.MP4", container="MP4"), keep_original=False).lines]
        lines.insert(4, "-GPSLatitude=1.000000000")

        with pytest.raises(InternalConsistencyError, match="outside its vocabulary"):
            assert_block_shape(self.block(lines, "MP4"))

    def test_a_value_line_with_no_option_before_it_is_refused(self):
        from gps_sync.writer import assert_block_shape

        with pytest.raises(InternalConsistencyError, match="outside its vocabulary"):
            assert_block_shape(self.block(["-n", "not $Composite:GPSPosition"]))

    def test_a_block_ending_on_an_option_that_takes_a_value_is_refused(self):
        from gps_sync.writer import assert_block_shape

        with pytest.raises(InternalConsistencyError) as raised:
            assert_block_shape(self.block(["-n", "-if"]))

        assert str(raised.value) == "write block ends on an option that takes a value"

    def test_a_value_containing_an_equals_sign_is_still_one_declared_assignment(self):
        from gps_sync.writer import assert_block_shape

        assert_block_shape(self.block(["-n", "-GPSLatitude=a=b", "-execute"]))

    def test_an_invalid_first_line_is_refused(self):
        from gps_sync.writer import assert_block_shape

        with pytest.raises(InternalConsistencyError, match="outside its vocabulary"):
            assert_block_shape(self.block(["-geotag", "-n"]))

    def test_an_echo_option_must_be_followed_by_an_absolute_path(self):
        from gps_sync.writer import assert_block_shape

        with pytest.raises(InternalConsistencyError, match="non-absolute"):
            assert_block_shape(self.block(["-echo3", "relative.RAF"]))

    def test_a_relative_path_line_is_refused(self):
        from gps_sync.writer import assert_block_shape

        with pytest.raises(InternalConsistencyError, match="outside its vocabulary"):
            assert_block_shape(self.block(["-n", "relative/a.RAF"]))


class TestPathRepresentability:
    @pytest.mark.parametrize(
        "separator",
        [
            "\n",
            "\r",
            "\v",
            "\f",
            "\x1c",
            "\x1d",
            "\x1e",
            chr(0x85),
            chr(0x2028),
            chr(0x2029),
            "\r\n",
        ],
        ids=["lf", "cr", "vt", "ff", "fs", "gs", "rs", "nel", "ls", "ps", "crlf"],
    )
    def test_every_character_the_marker_partition_splits_on_is_refused(self, separator):
        from gps_sync.argfile import is_representable, path_line

        path = f"/m/a{separator}b.RAF"

        assert not is_representable(path)
        with pytest.raises(InternalConsistencyError, match="line break"):
            path_line(path)

    @pytest.mark.parametrize("path", ["/m/a b#c.RAF", "/m/ø/a.MP4", "/m/tab\tname.RAF"])
    def test_spaces_hashes_non_ascii_and_a_tab_are_representable(self, path):
        from gps_sync.argfile import is_representable, path_line

        assert is_representable(path)
        assert path_line(path) == path

    def test_a_trailing_break_is_refused_too(self):
        from gps_sync.argfile import is_representable

        assert not is_representable("/m/a.RAF\n")

    def test_a_relative_path_is_not_representable(self):
        from gps_sync.argfile import is_representable

        assert not is_representable("m/a.RAF")
