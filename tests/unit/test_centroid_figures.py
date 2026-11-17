"""The centroid's declaring tables and the figures rendered from them."""

import pytest

from gps_sync.model import (
    AGGREGATION_SPECS,
    ALTITUDE_SPECS,
    GPX_PATH_NO_ELEVATION,
    REJECTION_SPECS,
    AggregationRule,
    AltitudeOutcome,
    Centroid,
    Geotagged,
    Interpolated,
    Planned,
    Position,
    SampleRejection,
    Via,
    _assert_total,
)
from gps_sync.report import file_line, render_report
from gps_sync.runrecord import summary_to_dict
from gps_sync.summary import summarise
from tests.support import at, run_info

SEG = "t.gpx#trk0seg0"


def centroid(altitude, *, dropped=(0, 0, 0), spread=0.5, shortfall=0.0167, span=0.3837):
    return Centroid(
        samples_read=24 + sum(dropped),
        samples_used=24,
        dropped=tuple(zip(SampleRejection, dropped, strict=True)),
        span_s=span,
        shortfall_s=shortfall,
        rule=AggregationRule.ARITHMETIC_MEAN,
        spread_m=spread,
        altitude=altitude,
    )


def clip(name, altitude, *, ele=None, kind=Planned, **figures):
    return kind(
        f"/m/{name}.MP4",
        at(0),
        Position(42.0, 77.0, ele),
        Via.CENTROID,
        centroid(altitude, **figures),
    )


def gpx_still(name, ele):
    return Planned(
        f"/m/{name}.RAF", at(0), Position(42.0, 77.0, ele), Via.INTERPOLATED, Interpolated(SEG, 5.0)
    )


class TestTheDeclaringTables:
    @pytest.mark.parametrize(
        ("members", "table"),
        [
            (AggregationRule, AGGREGATION_SPECS),
            (AltitudeOutcome, ALTITUDE_SPECS),
            (SampleRejection, REJECTION_SPECS),
        ],
        ids=["aggregation", "altitude", "rejection"],
    )
    def test_every_member_has_exactly_one_row(self, members, table):
        assert set(table) == set(members)

    @pytest.mark.parametrize(
        ("name", "members", "table"),
        [
            ("aggregation", AggregationRule, AGGREGATION_SPECS),
            ("altitude", AltitudeOutcome, ALTITUDE_SPECS),
            ("rejection", SampleRejection, REJECTION_SPECS),
        ],
    )
    def test_a_member_without_a_row_refuses_the_table(self, name, members, table):
        first = next(iter(table))
        without = {k: v for k, v in table.items() if k is not first}

        with pytest.raises(RuntimeError, match=f"the {name} table has no row for: .*{first.name}"):
            _assert_total(name, members, without)

    def test_only_a_file_that_carries_an_elevation_is_marked_available(self):
        assert {m for m, row in ALTITUDE_SPECS.items() if not row.unavailable} == {
            AltitudeOutcome.ABSOLUTE_ALTITUDE
        }

    def test_the_rows_name_each_value_for_a_reader(self):
        assert AGGREGATION_SPECS[AggregationRule.ARITHMETIC_MEAN].phrase == "mean"
        assert {r: row.phrase for r, row in REJECTION_SPECS.items()} == {
            SampleRejection.UNPARSEABLE: "unparseable",
            SampleRejection.OUT_OF_RANGE: "out of range",
            SampleRejection.ZERO_SENTINEL: "zero sentinel",
        }


class TestTheUnavailableAltitudeBreakout:
    def outcomes(self):
        return [
            clip("with", AltitudeOutcome.ABSOLUTE_ALTITUDE, ele=10.0),
            clip("none", AltitudeOutcome.NONE_USABLE),
            clip("skew", AltitudeOutcome.NOT_CO_INDEXED),
            clip("huge", AltitudeOutcome.NOT_REPRESENTABLE),
            clip("written", AltitudeOutcome.NONE_USABLE, kind=Geotagged),
            gpx_still("bare", None),
            gpx_still("tall", 100.0),
        ]

    def test_the_terms_sum_to_the_figure_they_break_out(self):
        summary = summarise(self.outcomes())

        assert summary.altitude_unavailable == 5
        assert sum(summary.altitude_breakdown.values()) == summary.altitude_unavailable

    def test_the_available_member_is_outside_the_breakout_and_the_gpx_term_is_counted(self):
        breakdown = summarise(self.outcomes()).altitude_breakdown

        assert breakdown == {
            "NONE_USABLE": 2,
            "NOT_CO_INDEXED": 1,
            "NOT_REPRESENTABLE": 1,
            GPX_PATH_NO_ELEVATION: 1,
        }

    def test_the_json_and_the_report_render_the_same_terms(self):
        outcomes = self.outcomes()
        summary = summarise(outcomes)

        text = render_report(outcomes, summary, run_info())

        assert summary_to_dict(summary)["altitude_unavailable"] == {
            "total": 5,
            "breakdown": summary.altitude_breakdown,
        }
        assert (
            "Altitude not available for 5 planned/written file(s) "
            "(NONE_USABLE 2, NOT_CO_INDEXED 1, NOT_REPRESENTABLE 1, GPX_PATH_NO_ELEVATION 1)"
        ) in text
        assert "ABSOLUTE_ALTITUDE" not in text.split("Altitude not available")[1].splitlines()[0]


class TestTheCentroidIsRenderedWithEveryFigureItCarries:
    def test_the_line_names_the_shortfall_and_each_drop_reason(self):
        outcome = clip(
            "a", AltitudeOutcome.ABSOLUTE_ALTITUDE, ele=10.0, dropped=(1, 2, 3), shortfall=0.0167
        )

        assert file_line(outcome).endswith(
            "via CENTROID (mean of 24 of 30 samples, 6 dropped (unparseable 1, out of range 2, "
            "zero sentinel 3), span 0.38 s, shortfall against the duration 0.02 s, "
            "farthest fix 0.5 m from the position, altitude ABSOLUTE_ALTITUDE)"
        )

    def test_the_dispersion_line_is_exact(self):
        outcomes = [
            clip("a", AltitudeOutcome.NONE_USABLE, spread=0.25),
            clip("b", AltitudeOutcome.NONE_USABLE, spread=0.75),
            clip("c", AltitudeOutcome.NONE_USABLE, spread=1.5),
        ]

        text = render_report(outcomes, summarise(outcomes), run_info())

        assert (
            "Telemetry centroid dispersion: 3 file(s), farthest used fix from the written "
            "position - minimum 0.2 m, median 0.8 m, maximum 1.5 m"
        ) in text

    def test_no_dispersion_line_is_printed_when_no_clip_was_centroided(self):
        outcomes = [gpx_still("a", 1.0)]

        assert "dispersion" not in render_report(outcomes, summarise(outcomes), run_info())

    def test_the_json_carries_the_dispersion_figures_unrounded(self):
        outcomes = [
            clip("a", AltitudeOutcome.NONE_USABLE, spread=0.25),
            clip("b", AltitudeOutcome.NONE_USABLE, spread=0.75),
        ]

        assert summary_to_dict(summarise(outcomes))["centroid_dispersion"] == {
            "count": 2,
            "minimum_m": 0.25,
            "median_m": 0.5,
            "maximum_m": 0.75,
        }
