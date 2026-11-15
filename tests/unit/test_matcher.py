import math
import statistics
from datetime import timedelta

import pytest

from gps_sync.geo import haversine_m
from gps_sync.matcher import Match, NoCoverage, match
from gps_sync.model import Extended, Interpolated, UnmatchableReason, Via
from tests.support import at, index_of, segment

GAP = timedelta(seconds=120)
NO_EXTEND = timedelta(0)
HOUR = timedelta(hours=1)

SEG_A = "a.gpx#trk0seg0"
SEG_B = "b.gpx#trk0seg0"


def two_segments_50km_apart():
    """Two segments 60 s apart in time and about 50 km apart in space."""
    return index_of(
        segment(SEG_A, [(-60, 42.0, 77.0, 100.0), (0, 42.0, 77.0, 100.0)]),
        segment(SEG_B, [(60, 42.45, 77.0, 200.0), (120, 42.45, 77.0, 200.0)]),
    )


def one_segment(*points):
    return index_of(segment(SEG_A, points))


def refused(result) -> NoCoverage:
    assert isinstance(result, NoCoverage), result
    return result


def matched(result) -> Match:
    assert isinstance(result, Match), result
    return result


class TestSegmentGuard:
    def test_dead_centre_of_a_60s_inter_segment_gap_is_refused_as_a_boundary(self):
        # A max-gap-only matcher returns a position 25 km from both ends here.
        result = match(two_segments_50km_apart(), at(30), GAP, NO_EXTEND)

        assert refused(result).reason is UnmatchableReason.SEGMENT_BOUNDARY

    def test_boundary_refusal_is_independent_of_how_large_max_gap_is(self):
        result = match(two_segments_50km_apart(), at(30), timedelta(days=365), NO_EXTEND)

        assert refused(result).reason is UnmatchableReason.SEGMENT_BOUNDARY

    def test_two_points_of_one_segment_within_max_gap_are_interpolated(self):
        index = one_segment((0, 42.0, 77.0, None), (60, 42.001, 77.0, None))

        result = matched(match(index, at(30), GAP, NO_EXTEND))

        assert result.via is Via.INTERPOLATED
        assert result.position.lat == pytest.approx(42.0005)

    def test_a_watch_pause_between_two_trkseg_elements_is_not_interpolated_across(self):
        index = index_of(
            segment("f.gpx#trk0seg0", [(0, 42.0, 77.0, None), (10, 42.0001, 77.0, None)]),
            segment("f.gpx#trk0seg1", [(20, 42.0002, 77.0, None), (30, 42.0003, 77.0, None)]),
        )

        result = match(index, at(15), GAP, NO_EXTEND)

        assert refused(result).reason is UnmatchableReason.SEGMENT_BOUNDARY

    def test_boundary_dominates_width_when_both_refusals_apply(self):
        index = index_of(
            segment(SEG_A, [(0, 42.0, 77.0, None), (10, 42.0, 77.0, None)]),
            segment(SEG_B, [(5000, 42.0, 77.1, None), (5010, 42.0, 77.1, None)]),
        )

        result = match(index, at(2500), GAP, NO_EXTEND)

        assert refused(result).reason is UnmatchableReason.SEGMENT_BOUNDARY

    def test_a_wide_bracket_inside_one_segment_is_a_max_gap_refusal_and_not_a_boundary(self):
        index = one_segment((0, 42.0, 77.0, None), (500, 42.001, 77.0, None))

        result = match(index, at(250), GAP, NO_EXTEND)

        assert refused(result).reason is UnmatchableReason.MAX_GAP_EXCEEDED

    def test_a_narrow_gap_across_segments_is_a_boundary_and_not_a_max_gap_refusal(self):
        result = match(two_segments_50km_apart(), at(10), GAP, NO_EXTEND)

        assert refused(result).reason is UnmatchableReason.SEGMENT_BOUNDARY

    def test_extend_may_clamp_across_a_boundary_and_never_blends_the_two_segments(self):
        result = matched(match(two_segments_50km_apart(), at(20), GAP, HOUR))

        assert result.via is Via.EXTENDED
        assert isinstance(result.provenance, Extended)
        assert result.provenance.overridden_reason is UnmatchableReason.SEGMENT_BOUNDARY
        assert result.position.lat == 42.0
        assert result.provenance.segment == SEG_A
        assert result.provenance.clamp_s == 20

    def test_an_exact_hit_spanning_two_segments_clamps_to_the_earlier_segments_own_point(self):
        index = index_of(
            segment(SEG_A, [(-60, 41.0, 77.0, None), (0, 42.0, 77.0, None)]),
            segment(SEG_B, [(0, 43.0, 77.0, None), (60, 43.0, 77.0, None)]),
        )

        result = matched(match(index, at(0), GAP, HOUR))

        assert isinstance(result.provenance, Extended)
        assert result.provenance.segment == SEG_A
        assert result.provenance.clamp_s == 0
        assert result.provenance.overridden_reason is UnmatchableReason.SEGMENT_BOUNDARY
        assert result.position.lat == 42.0

    def test_an_exact_hit_spanning_two_segments_at_the_first_index_slot_stays_local(self):
        # Pins the shape where a bracket reached from the wrong side would index -1, the last
        # point of the whole track, an unrelated activity. The position cannot differ here (the
        # tie always picks the hit itself); the earlier-slot case above is what kills the mutant.
        index = index_of(
            segment(SEG_A, [(0, 42.0, 77.0, None), (10, 42.001, 77.0, None)]),
            segment(SEG_B, [(0, 43.0, 77.0, None)]),
        )
        assert index.points[1].segment == SEG_B

        result = matched(match(index, at(0), GAP, HOUR))

        assert isinstance(result.provenance, Extended)
        assert result.provenance.segment == SEG_A
        assert result.position.lat == 42.0
        assert result.bracket_speed == pytest.approx(
            haversine_m(42.0, 77.0, 42.001, 77.0) / 10, rel=1e-9
        )


class TestRefusalReasons:
    def test_exact_hit_returns_that_point_with_zero_bracket(self):
        index = one_segment((0, 42.0, 77.0, 10.0), (60, 42.001, 77.0, 20.0))

        result = matched(match(index, at(60), GAP, NO_EXTEND))

        assert result.position.lat == 42.001
        assert result.via is Via.INTERPOLATED
        assert result.provenance == Interpolated(SEG_A, 0.0)

    def test_midpoint_of_a_two_point_bracket_is_the_arithmetic_midpoint_of_all_three(self):
        index = one_segment((0, 42.0, 77.0, 10.0), (60, 42.002, 77.004, 30.0))

        position = matched(match(index, at(30), GAP, NO_EXTEND)).position

        assert (position.lat, position.lon, position.ele) == pytest.approx((42.001, 77.002, 20.0))

    def test_bracket_exactly_equal_to_max_gap_is_accepted(self):
        index = one_segment((0, 42.0, 77.0, None), (120, 42.001, 77.0, None))

        assert isinstance(match(index, at(60), GAP, NO_EXTEND), Match)

    def test_bracket_one_second_over_max_gap_is_refused_with_the_miss_magnitude(self):
        index = one_segment((0, 42.0, 77.0, None), (121, 42.001, 77.0, None))

        result = refused(match(index, at(40), GAP, NO_EXTEND))

        assert result.reason is UnmatchableReason.MAX_GAP_EXCEEDED
        assert result.miss_s == 40
        assert result.nearest_segment == SEG_A
        assert result.bracket_s == 121

    def test_instant_before_the_first_point_is_outside_coverage(self):
        index = one_segment((100, 42.0, 77.0, None), (160, 42.001, 77.0, None))

        result = refused(match(index, at(40), GAP, NO_EXTEND))

        assert result.reason is UnmatchableReason.OUTSIDE_COVERAGE
        assert result.nearest_segment == SEG_A
        assert result.miss_s == 60

    def test_instant_after_the_last_point_is_outside_coverage(self):
        index = one_segment((100, 42.0, 77.0, None), (160, 42.001, 77.0, None))

        result = refused(match(index, at(700), GAP, NO_EXTEND))

        assert result.reason is UnmatchableReason.OUTSIDE_COVERAGE
        assert result.miss_s == 540

    def test_instant_before_the_first_point_within_extend_clamps_to_it(self):
        index = one_segment((100, 42.0, 77.0, None), (160, 42.001, 77.0, None))

        result = matched(match(index, at(40), GAP, timedelta(seconds=90)))

        assert result.via is Via.EXTENDED
        assert result.position.lat == 42.0
        assert isinstance(result.provenance, Extended)
        assert result.provenance.overridden_reason is UnmatchableReason.OUTSIDE_COVERAGE
        assert result.provenance.clamp_s == 60

    def test_instant_beyond_the_extend_window_keeps_its_reason(self):
        index = one_segment((100, 42.0, 77.0, None), (160, 42.001, 77.0, None))

        result = refused(match(index, at(40), GAP, timedelta(seconds=59)))

        assert result.reason is UnmatchableReason.OUTSIDE_COVERAGE

    def test_extend_of_zero_disables_clamping_even_at_zero_distance(self):
        index = two_segments_50km_apart()

        assert isinstance(match(index, at(0), GAP, NO_EXTEND), Match)
        shared = index_of(
            segment(SEG_A, [(0, 42.0, 77.0, None)]),
            segment(SEG_B, [(0, 43.0, 77.0, None)]),
        )
        assert refused(match(shared, at(0), GAP, NO_EXTEND)).reason is (
            UnmatchableReason.SEGMENT_BOUNDARY
        )

    def test_interior_gap_wider_than_max_gap_within_extend_clamps_to_the_nearer_endpoint(self):
        index = one_segment(
            (0, 42.0, 77.0, None), (10, 42.0001, 77.0, None), (1000, 42.1, 77.0, None)
        )

        result = matched(match(index, at(900), GAP, timedelta(seconds=200)))

        assert isinstance(result.provenance, Extended)
        assert result.provenance.overridden_reason is UnmatchableReason.MAX_GAP_EXCEEDED
        assert result.position.lat == 42.1
        assert result.provenance.clamp_s == 100

    def test_an_exact_tie_between_the_endpoints_clamps_to_the_earlier_one(self):
        index = one_segment((0, 42.0, 77.0, None), (1000, 42.1, 77.0, None))

        result = matched(match(index, at(500), GAP, timedelta(seconds=600)))

        assert result.position.lat == 42.0

    def test_a_bracket_across_the_antimeridian_is_unsound_and_extend_does_not_rescue_it(self):
        index = one_segment((0, 10.0, 179.9995, None), (60, 10.0, -179.9995, None))

        result = refused(match(index, at(30), GAP, timedelta(days=1)))

        assert result.reason is UnmatchableReason.BRACKET_UNSOUND
        assert result.nearest_segment == SEG_A
        assert result.detail is not None and "179.9995" in result.detail

    def test_a_bracket_between_180_and_181_degrees_wide_is_unsound(self):
        index = one_segment((0, 10.0, -90.5, None), (60, 10.0, 90.25, None))

        result = refused(match(index, at(30), GAP, timedelta(days=1)))

        assert result.reason is UnmatchableReason.BRACKET_UNSOUND

    def test_a_bracket_exactly_180_degrees_wide_is_interpolated(self):
        # The refusal is strictly wider than half the globe: 180 degrees is still unambiguous
        # about which way round it goes only because the rule is ">" and not ">=".
        index = one_segment((0, 10.0, -90.0, None), (60, 10.0, 90.0, None))

        result = matched(match(index, at(30), GAP, NO_EXTEND))

        assert result.via is Via.INTERPOLATED
        assert result.position.lon == pytest.approx(0.0)

    def test_single_point_index_matches_only_an_exact_hit_or_a_clamp_with_no_speed(self):
        index = one_segment((100, 42.0, 77.0, None))

        exact = matched(match(index, at(100), GAP, NO_EXTEND))
        clamp = matched(match(index, at(130), GAP, timedelta(seconds=60)))

        assert exact.bracket_speed is None
        assert clamp.bracket_speed is None
        assert refused(match(index, at(130), GAP, NO_EXTEND)).reason is (
            UnmatchableReason.OUTSIDE_COVERAGE
        )

    @pytest.mark.parametrize("extend", [NO_EXTEND, HOUR])
    def test_empty_index_is_outside_coverage_for_every_instant_and_names_no_segment(self, extend):
        result = refused(match(index_of(), at(5), GAP, extend))

        assert result.reason is UnmatchableReason.OUTSIDE_COVERAGE
        assert result.nearest_segment is None
        assert result.miss_s is None


class TestBracketSpeed:
    def test_two_point_bracket_speed_is_distance_over_time(self):
        index = one_segment((0, 42.0, 77.0, None), (100, 42.001, 77.0, None))
        expected = haversine_m(42.0, 77.0, 42.001, 77.0) / 100

        assert matched(match(index, at(50), GAP, NO_EXTEND)).bracket_speed == pytest.approx(
            expected
        )

    def test_exact_hit_uses_the_later_side_within_the_segment(self):
        index = one_segment(
            (0, 42.0, 77.0, None), (10, 42.0001, 77.0, None), (30, 42.0005, 77.0, None)
        )
        expected = haversine_m(42.0001, 77.0, 42.0005, 77.0) / 20

        assert matched(match(index, at(10), GAP, NO_EXTEND)).bracket_speed == pytest.approx(
            expected
        )

    def test_exact_hit_on_the_last_point_uses_the_earlier_side(self):
        index = one_segment((0, 42.0, 77.0, None), (10, 42.0001, 77.0, None))
        expected = haversine_m(42.0, 77.0, 42.0001, 77.0) / 10

        assert matched(match(index, at(10), GAP, NO_EXTEND)).bracket_speed == pytest.approx(
            expected
        )

    def test_exact_hit_on_the_only_point_of_its_segment_is_absent_and_not_zero(self):
        index = index_of(
            segment(SEG_A, [(0, 42.0, 77.0, None), (10, 42.0001, 77.0, None)]),
            segment(SEG_B, [(500, 42.5, 77.0, None)]),
        )

        assert matched(match(index, at(500), GAP, NO_EXTEND)).bracket_speed is None

    def test_a_stationary_bracket_has_a_defined_zero_speed(self):
        index = one_segment((0, 42.0, 77.0, None), (60, 42.0, 77.0, None))

        assert matched(match(index, at(30), GAP, NO_EXTEND)).bracket_speed == 0.0

    def test_extend_clamp_uses_the_interval_on_the_side_away_from_the_gap(self):
        index = one_segment(
            (0, 42.0, 77.0, None),
            (10, 42.0001, 77.0, None),
            (20, 42.0002, 77.0, None),
            (2000, 42.5, 77.0, None),
            (2010, 42.5001, 77.0, None),
        )
        away = haversine_m(42.0001, 77.0, 42.0002, 77.0) / 10

        result = matched(match(index, at(500), GAP, timedelta(seconds=600)))

        assert result.position.lat == 42.0002
        assert result.bracket_speed == pytest.approx(away)

    def test_extend_clamp_onto_a_segment_with_only_the_gap_as_neighbour_is_absent(self):
        index = one_segment((0, 42.0, 77.0, None), (1000, 42.1, 77.0, None))

        assert matched(match(index, at(100), GAP, timedelta(seconds=200))).bracket_speed is None


class TestDuplicateInstants:
    def test_duplicates_within_a_segment_collapse_to_the_first_in_file_order(self):
        index = one_segment(
            (0, 42.0, 77.0, None),
            (10, 42.1, 77.0, None),
            (10, 43.0, 77.0, None),
            (20, 42.2, 77.0, None),
        )

        assert [p.lat for p in index.points] == [42.0, 42.1, 42.2]
        assert list(index.times) == sorted(index.times)
        assert index.duplicates_dropped == {SEG_A.split("#")[0]: 1}

    def test_duplicates_across_segments_are_kept_and_an_exact_hit_there_is_a_boundary(self):
        index = index_of(
            segment(SEG_A, [(0, 42.0, 77.0, None), (10, 42.0, 77.0, None)]),
            segment(SEG_B, [(10, 43.0, 77.0, None), (20, 43.0, 77.0, None)]),
        )

        assert len(index) == 4
        result = refused(match(index, at(10), GAP, NO_EXTEND))
        assert result.reason is UnmatchableReason.SEGMENT_BOUNDARY

    def test_overlapping_segments_refuse_every_bracket_in_the_overlap(self):
        index = index_of(
            segment(SEG_A, [(0, 42.0, 77.0, None), (20, 42.0, 77.0, None), (40, 42.0, 77.0, None)]),
            segment(
                SEG_B, [(10, 42.2, 77.0, None), (30, 42.2, 77.0, None), (50, 42.2, 77.0, None)]
            ),
        )

        reasons = {refused(match(index, at(t), GAP, NO_EXTEND)).reason for t in (15, 25, 35, 45)}

        assert reasons == {UnmatchableReason.SEGMENT_BOUNDARY}
        assert len(index.overlaps) == 1
        assert {index.overlaps[0].segment_a, index.overlaps[0].segment_b} == {SEG_A, SEG_B}
        assert index.overlaps[0].start == at(10)
        assert index.overlaps[0].end == at(40)


def test_leave_one_out_error_on_a_curved_walking_track_is_sub_metre():
    # A transposed latitude/longitude or an inverted weight would give errors of kilometres.
    points = [
        (
            float(t),
            42.0 + 0.001 * math.sin(t / 400),
            77.0 + 0.001 * math.cos(t / 400),
            None,
        )
        for t in range(0, 600, 5)
    ]
    errors = []
    for i in range(1, len(points) - 1):
        kept = [p for j, p in enumerate(points) if j != i]
        result = matched(match(one_segment(*kept), at(points[i][0]), GAP, NO_EXTEND))
        errors.append(
            haversine_m(result.position.lat, result.position.lon, points[i][1], points[i][2])
        )

    assert statistics.median(errors) < 1.0
    assert max(errors) < 5.0
