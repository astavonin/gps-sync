"""Finite inputs do not make a finite derived elevation; the production paths contain it."""

import math
from datetime import timedelta

import pytest

from gps_sync.matcher import Match, match
from gps_sync.model import Position
from gps_sync.telemetry import _mean
from gps_sync.writeset import (
    ELEVATION_CEILING,
    is_representable_elevation,
    without_unrepresentable_elevation,
)
from tests.support import at, index_of, segment

EXTREMES = index_of(segment("t#0", [(0, 42.0, 77.0, -1.7e308), (60, 42.06, 77.0, 1.7e308)]))
GAP = timedelta(seconds=120)


def matched(index, seconds):
    result = match(index, at(seconds), GAP, timedelta(0))
    assert isinstance(result, Match)
    return result


class TestTheMatcherBetweenFiniteExtremes:
    def test_the_midpoint_of_a_bracket_of_extremes_is_infinite_not_finite(self):
        position = matched(EXTREMES, 30).position

        assert position.ele == math.inf
        assert position.lat == pytest.approx(42.03)

    def test_that_elevation_is_dropped_and_flagged_before_anything_is_formatted(self):
        position, dropped = without_unrepresentable_elevation(matched(EXTREMES, 30).position)

        assert dropped is True
        assert position == Position(pytest.approx(42.03), 77.0, None)

    def test_an_exact_trackpoint_hit_returns_the_trackpoints_own_elevation_and_never_nan(self):
        # The matcher answers an exact hit before interpolating, so a weight of zero (which would
        # turn an infinite span into nan) never reaches the interpolation.
        for seconds, expected in ((0, -1.7e308), (60, 1.7e308)):
            position = matched(EXTREMES, seconds).position

            assert position.ele == expected

    @pytest.mark.parametrize("seconds", [0.001, 59.999])
    def test_the_nearest_inexact_instants_are_strictly_inside_the_bracket_and_infinite_or_finite(
        self, seconds
    ):
        ele = matched(EXTREMES, seconds).position.ele

        assert ele is not None and not math.isnan(ele)


class TestTheCentroidMean:
    def test_a_mean_over_finite_in_domain_samples_is_infinite_rather_than_raising(self):
        assert _mean([1.7e308, 1.7e308, 1.7e308]) == math.inf

    def test_a_mean_of_ordinary_values_is_their_arithmetic_mean(self):
        assert _mean([1.0, 2.0, 6.0]) == 3.0


class TestTheRepresentabilityGuard:
    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, ELEVATION_CEILING * 2])
    def test_nan_and_infinity_and_the_oversized_are_not_representable(self, value):
        assert not is_representable_elevation(value)
        position, dropped = without_unrepresentable_elevation(Position(1.0, 2.0, value))
        assert dropped is True and position.ele is None

    @pytest.mark.parametrize("value", [0.0, -0.0, 2189.9, -1234.5678, ELEVATION_CEILING])
    def test_in_range_values_survive_untouched(self, value):
        position = Position(1.0, 2.0, value)

        assert without_unrepresentable_elevation(position) == (position, False)
