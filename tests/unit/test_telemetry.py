"""The telemetry derivation: the atom question, co-indexing, value domain, coverage and mean."""

import math

import pytest

from gps_sync.model import (
    AggregationRule,
    AltitudeOutcome,
    FailedReason,
    Position,
    SampleRejection,
)
from gps_sync.records import TelemetryRecord
from gps_sync.telemetry import (
    AtomPresent,
    AtomUnreadable,
    TelemetryCentroid,
    TelemetryRefusal,
    derive_centroid,
)
from tests.support import tag_record, telemetry_json

DURATION = 0.4004


def clip(duration: float | None = DURATION):
    extra = {} if duration is None else {":Duration": duration}
    return tag_record("/media/a.MP4", file_type="MP4", extra=extra)


def telemetry(**kwargs) -> TelemetryRecord:
    return TelemetryRecord.from_json(telemetry_json(**kwargs))


def samples(n: int, step: float = 0.0167):
    return [round(i * step, 4) for i in range(n)]


def derive(duration: float | None = DURATION, **kwargs):
    return derive_centroid(clip(duration), telemetry(**kwargs))


def centroid_of(result) -> TelemetryCentroid:
    assert isinstance(result, TelemetryCentroid), result
    return result


def refusal_of(result, reason: FailedReason) -> TelemetryRefusal:
    assert isinstance(result, TelemetryRefusal), result
    assert result.reason is reason
    return result


class TestTheCentroid:
    def test_the_position_is_the_arithmetic_mean_of_every_fix_and_the_mean_altitude(self):
        result = centroid_of(
            derive(
                lat=[42.0, 42.2, 42.4],
                lon=[77.0, 77.1, 77.5],
                alt=[1000.0, 1100.0, 1300.0],
                times=[0.0, 0.2, 0.4],
            )
        )

        assert result.position.lat == pytest.approx(42.2)
        assert result.position.lon == pytest.approx(77.2)
        assert result.position.ele == pytest.approx(1133.3333333)

    def test_the_provenance_counts_read_used_and_dropped_and_names_the_rule_and_altitude(self):
        result = centroid_of(
            derive(lat=[42.0, 42.2], lon=[77.0, 77.2], alt=[10.0, 20.0], times=[0.0, 0.4])
        )

        provenance = result.provenance
        assert (provenance.samples_read, provenance.samples_used) == (2, 2)
        assert provenance.dropped == tuple((r, 0) for r in SampleRejection)
        assert provenance.rule is AggregationRule.ARITHMETIC_MEAN
        assert provenance.altitude is AltitudeOutcome.ABSOLUTE_ALTITUDE

    def test_the_span_is_the_read_lists_and_the_shortfall_is_against_the_duration(self):
        result = centroid_of(derive(lat=[42.0, 42.0], lon=[77.0, 77.0], times=[0.0, 0.3837]))

        assert result.provenance.span_s == pytest.approx(0.3837)
        assert result.provenance.shortfall_s == pytest.approx(0.0167)

    def test_the_spread_is_the_greatest_distance_from_a_used_fix_to_the_position(self):
        result = centroid_of(derive(lat=[42.0, 42.002], lon=[77.0, 77.0], times=[0.0, 0.4]))

        # Two fixes 0.002 degrees of latitude apart sit 111.2 m apart, so each is half that away.
        assert result.provenance.spread_m == pytest.approx(111.2, abs=0.5)

    def test_a_single_sample_clip_yields_that_fix_with_zero_span_and_zero_spread(self):
        result = centroid_of(derive(duration=0.0167, lat=[42.5], lon=[77.5], times=[0.0]))

        assert result.position == Position(42.5, 77.5, None)
        assert (result.provenance.span_s, result.provenance.spread_m) == (0.0, 0.0)

    def test_identical_fixes_give_that_position_with_zero_spread(self):
        result = centroid_of(derive(lat=[42.5] * 3, lon=[77.5] * 3, times=[0.0, 0.2, 0.4]))

        assert result.position == Position(42.5, 77.5, None)
        assert result.provenance.spread_m == pytest.approx(0.0, abs=1e-6)

    def test_numeric_strings_are_read_as_numbers(self):
        result = centroid_of(derive(lat=["42.0", "42.2"], lon=["77.0", "77.2"], times=[0, 0.4]))

        assert result.position.lat == pytest.approx(42.1)


class TestTheAtomQuestion:
    def test_one_readable_atom_is_present_and_nothing_is_derived(self):
        result = derive(atoms=["42.5 77.5 100.0"], lat=[1.0], lon=[1.0], times=[0.0])

        assert result == AtomPresent(Position(42.5, 77.5, 100.0))

    def test_a_two_number_atom_is_present_with_no_elevation(self):
        assert derive(atoms=["42.5 77.5"]) == AtomPresent(Position(42.5, 77.5, None))

    def test_a_zero_atom_is_still_present_because_a_probe_applies_no_domain(self):
        assert derive(atoms=["0 0"]) == AtomPresent(Position(0.0, 0.0, None))

    @pytest.mark.parametrize(
        "value", ["+42,0721+077,1292+2189,900/", "", "1", "1 2 3 4", "a b", "nan nan", "inf 1"]
    )
    def test_one_unreadable_atom_is_indeterminate_quoting_its_value(self, value):
        result = derive(atoms=[value])

        assert isinstance(result, AtomUnreadable)
        assert repr(value) in result.detail

    def test_several_atoms_are_indeterminate_naming_the_count_and_every_value(self):
        result = derive(atoms=["1 2", "3 4"])

        assert isinstance(result, AtomUnreadable)
        assert "2 container position atoms" in result.detail
        assert "'1 2'" in result.detail and "'3 4'" in result.detail

    def test_an_atom_outranks_a_sample_list_that_would_otherwise_be_refused(self):
        assert isinstance(derive(atoms=["1 2"], lat=[1.0], lon=[]), AtomPresent)


class TestCoIndexing:
    def test_disagreeing_coordinate_counts_are_inconsistent_naming_both(self):
        result = derive(lat=[42.0, 42.1], lon=[77.0], times=[0.0, 0.4])

        assert (
            "2 GPSLatitude samples but 1 GPSLongitude"
            in refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail
        )

    @pytest.mark.parametrize("times", [[], [0.0], [0.0, 0.2, 0.4]])
    def test_a_sample_time_list_of_another_length_is_inconsistent_naming_both_counts(self, times):
        result = derive(lat=[42.0, 42.1], lon=[77.0, 77.1], times=times)

        detail = refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail
        assert f"{len(times)} SampleTime samples for 2 fixes" in detail

    def test_sample_times_with_no_fixes_at_all_are_inconsistent_not_unusable(self):
        result = derive(lat=[], lon=[], times=[0.0, 0.4])

        assert (
            refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail
            == "2 SampleTime samples for 0 fixes"
        )

    def test_no_samples_at_all_is_unusable_rather_than_inconsistent(self):
        result = derive()

        assert "no samples" in refusal_of(result, FailedReason.TELEMETRY_UNUSABLE).detail

    def test_an_altitude_list_of_another_length_derives_no_elevation_but_keeps_the_position(self):
        result = centroid_of(
            derive(lat=[42.0, 42.2], lon=[77.0, 77.2], alt=[1000.0], times=[0.0, 0.4])
        )

        assert result.position.ele is None
        assert result.provenance.altitude is AltitudeOutcome.NOT_CO_INDEXED
        assert result.position.lat == pytest.approx(42.1)

    def test_an_empty_altitude_list_is_none_usable(self):
        result = centroid_of(derive(lat=[42.0, 42.2], lon=[77.0, 77.2], times=[0.0, 0.4]))

        assert result.position.ele is None
        assert result.provenance.altitude is AltitudeOutcome.NONE_USABLE

    def test_an_altitude_list_with_no_parseable_entry_is_none_usable(self):
        result = centroid_of(
            derive(lat=[42.0, 42.2], lon=[77.0, 77.2], alt=["x", "y"], times=[0.0, 0.4])
        )

        assert result.provenance.altitude is AltitudeOutcome.NONE_USABLE

    def test_the_elevation_is_the_mean_over_the_fixes_whose_altitude_parses(self):
        result = centroid_of(
            derive(lat=[42.0, 42.2], lon=[77.0, 77.2], alt=["x", 40.0], times=[0.0, 0.4])
        )

        assert result.position.ele == 40.0

    def test_an_altitude_belonging_to_a_dropped_fix_does_not_enter_the_mean(self):
        result = centroid_of(
            derive(lat=[0.0, 42.2], lon=[0.0, 77.2], alt=[9999.0, 40.0], times=[0.0, 0.4])
        )

        assert result.position.ele == 40.0


class TestTheValueDomain:
    @pytest.mark.parametrize(
        ("lat", "lon", "reason"),
        [
            ("x", 77.0, SampleRejection.UNPARSEABLE),
            (42.0, None, SampleRejection.UNPARSEABLE),
            (math.nan, 77.0, SampleRejection.UNPARSEABLE),
            (42.0, math.inf, SampleRejection.UNPARSEABLE),
            (True, 77.0, SampleRejection.UNPARSEABLE),
            (90.1, 77.0, SampleRejection.OUT_OF_RANGE),
            (-90.1, 77.0, SampleRejection.OUT_OF_RANGE),
            (42.0, 180.1, SampleRejection.OUT_OF_RANGE),
            (42.0, -180.1, SampleRejection.OUT_OF_RANGE),
            (0.0, 0.0, SampleRejection.ZERO_SENTINEL),
        ],
    )
    def test_each_unusable_sample_is_dropped_under_its_own_counted_reason(self, lat, lon, reason):
        result = centroid_of(derive(lat=[42.0, lat], lon=[77.0, lon], times=[0.0, 0.4]))

        assert result.provenance.samples_read == 2
        assert result.provenance.samples_used == 1
        assert dict(result.provenance.dropped)[reason] == 1
        assert sum(count for _, count in result.provenance.dropped) == 1

    @pytest.mark.parametrize(
        ("lat", "lon"), [(90.0, 180.0), (-90.0, -180.0), (0.0, 1.0), (1.0, 0.0)]
    )
    def test_the_domain_boundaries_and_a_half_zero_pair_are_usable(self, lat, lon):
        result = centroid_of(derive(lat=[lat], lon=[lon], times=[0.0], duration=0.0167))

        assert result.provenance.samples_used == 1

    def test_a_far_but_in_domain_fix_is_never_discarded(self):
        result = centroid_of(
            derive(lat=[42.0, 42.0, 60.0], lon=[77.0, 77.0, 100.0], times=[0.0, 0.2, 0.4])
        )

        assert result.provenance.samples_used == 3
        assert result.position.lat == pytest.approx(48.0)

    def test_a_clip_with_no_usable_fix_is_unusable_and_the_detail_carries_the_drop_counts(self):
        result = derive(lat=[0.0, "x"], lon=[0.0, 1.0], times=[0.0, 0.4])

        detail = refusal_of(result, FailedReason.TELEMETRY_UNUSABLE).detail
        assert "none of 2 samples" in detail
        assert "UNPARSEABLE 1" in detail and "ZERO_SENTINEL 1" in detail

    def test_a_longitude_span_over_half_the_globe_is_refused_not_averaged(self):
        result = derive(lat=[10.0, 10.0], lon=[-100.0, 100.0], times=[0.0, 0.4])

        assert "200.0 degrees" in refusal_of(result, FailedReason.TELEMETRY_UNUSABLE).detail

    def test_a_longitude_span_of_exactly_half_the_globe_is_averaged(self):
        result = centroid_of(derive(lat=[10.0, 10.0], lon=[-90.0, 90.0], times=[0.0, 0.4]))

        assert result.position.lon == pytest.approx(0.0)


class TestTheCoverageCheck:
    @pytest.mark.parametrize("last", [0.3837, 0.4004])
    def test_a_shortfall_between_zero_inclusive_and_the_ceiling_is_admitted(self, last):
        result = centroid_of(derive(lat=[42.0, 42.0], lon=[77.0, 77.0], times=[0.0, last]))

        assert result.provenance.shortfall_s == pytest.approx(0.4004 - last)

    def test_a_shortfall_just_over_the_ceiling_is_inconsistent(self):
        result = derive(duration=10.0, lat=[42.0, 42.0], lon=[77.0, 77.0], times=[0.0, 8.99])

        assert (
            "shortfall 1.0100 s" in refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail
        )

    def test_a_shortfall_of_exactly_the_ceiling_is_admitted(self):
        result = derive(duration=10.0, lat=[42.0, 42.0], lon=[77.0, 77.0], times=[0.0, 9.0])

        assert isinstance(result, TelemetryCentroid)

    def test_a_span_longer_than_the_duration_is_inconsistent(self):
        result = derive(duration=0.4, lat=[42.0, 42.0], lon=[77.0, 77.0], times=[0.0, 0.5])

        detail = refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail
        assert "shortfall -0.1000 s" in detail

    def test_a_missing_duration_is_inconsistent(self):
        result = derive(duration=None, lat=[42.0], lon=[77.0], times=[0.0])

        assert (
            "no readable Duration" in refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail
        )

    def test_an_unreadable_sample_time_is_inconsistent(self):
        result = derive(lat=[42.0, 42.0], lon=[77.0, 77.0], times=[0.0, "later"])

        assert "SampleTime" in refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail

    def test_a_single_sample_read_of_a_long_clip_is_refused_as_a_short_scan(self):
        result = derive(duration=30.0, lat=[42.0], lon=[77.0], times=[0.0])

        refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT)

    def test_a_dropped_leading_sample_still_counts_toward_the_span(self):
        # A receiver without lock emits (0,0) for its first second; the clip is not a short scan.
        result = centroid_of(
            derive(
                duration=2.0,
                lat=[0.0, 42.0, 42.0],
                lon=[0.0, 77.0, 77.0],
                times=[0.0, 1.0, 1.99],
            )
        )

        assert result.provenance.span_s == pytest.approx(1.99)
        assert result.provenance.samples_used == 2


class TestTheAggregationArithmetic:
    def test_a_non_finite_mean_altitude_drops_the_elevation_and_keeps_the_position(self):
        result = centroid_of(
            derive(
                lat=[42.0] * 3,
                lon=[77.0] * 3,
                alt=[1.7e308] * 3,
                times=[0.0, 0.2, 0.4],
            )
        )

        assert result.position.ele is None
        assert result.provenance.altitude is AltitudeOutcome.NOT_REPRESENTABLE
        assert result.position.lat == pytest.approx(42.0)

    def test_a_mean_altitude_above_the_backend_ceiling_is_not_representable(self):
        result = centroid_of(
            derive(lat=[42.0] * 2, lon=[77.0] * 2, alt=[4.3e9, 4.3e9], times=[0.0, 0.4])
        )

        assert result.provenance.altitude is AltitudeOutcome.NOT_REPRESENTABLE

    def test_a_mean_altitude_at_the_ceiling_is_kept(self):
        result = centroid_of(
            derive(lat=[42.0], lon=[77.0], alt=[4_294_967_295.0], times=[0.0], duration=0.0167)
        )

        assert result.position.ele == 4_294_967_295.0
        assert result.provenance.altitude is AltitudeOutcome.ABSOLUTE_ALTITUDE

    def test_a_negative_altitude_is_kept_and_its_sign_preserved(self):
        result = centroid_of(
            derive(lat=[42.0], lon=[77.0], alt=[-12.5], times=[0.0], duration=0.0167)
        )

        assert result.position.ele == -12.5


class TestTheDiagnosticsAreExact:
    def test_the_empty_read_detail_names_the_condition(self):
        assert refusal_of(derive(), FailedReason.TELEMETRY_UNUSABLE).detail == (
            "the telemetry read returned no samples"
        )

    def test_the_unusable_detail_lists_every_reason_with_its_count_in_enumeration_order(self):
        result = derive(
            lat=[0.0, "x", 99.0, "y"], lon=[0.0, 1.0, 1.0, 2.0], times=[0.0, 0.1, 0.2, 0.4]
        )

        assert refusal_of(result, FailedReason.TELEMETRY_UNUSABLE).detail == (
            "none of 4 samples is a usable fix (UNPARSEABLE 2, OUT_OF_RANGE 1, ZERO_SENTINEL 1)"
        )

    def test_repeated_rejections_of_one_reason_accumulate(self):
        result = centroid_of(
            derive(lat=[42.0, "x", "y", "z"], lon=[77.0] * 4, times=[0.0, 0.1, 0.2, 0.4])
        )

        assert dict(result.provenance.dropped)[SampleRejection.UNPARSEABLE] == 3

    def test_the_unreadable_sample_time_detail_names_the_tag(self):
        result = derive(lat=[42.0, 42.0], lon=[77.0, 77.0], times=[0.0, None])

        assert refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail == (
            "a SampleTime value is not a finite number"
        )

    def test_the_missing_duration_detail_names_the_tag(self):
        result = derive(duration=None, lat=[42.0], lon=[77.0], times=[0.0])

        assert refusal_of(result, FailedReason.TELEMETRY_INCONSISTENT).detail == (
            "the container record carries no readable Duration"
        )

    def test_the_span_is_the_difference_of_the_extremes_not_their_sum(self):
        result = refusal_of(
            derive(duration=10.5, lat=[42.0, 42.0], lon=[77.0, 77.0], times=[5.0, 10.0]),
            FailedReason.TELEMETRY_INCONSISTENT,
        )

        assert "sample span 5.0000 s" in result.detail
        assert "shortfall 5.5000 s" in result.detail

    def test_several_atoms_are_separated_in_the_detail(self):
        result = derive(atoms=["1 2", "3 4", "5 6"])

        assert result.detail == (
            "3 container position atoms were read and may disagree: '1 2'; '3 4'; '5 6'"
        )

    def test_a_late_window_has_its_own_span_not_the_sum_of_its_times(self):
        result = centroid_of(
            derive(duration=5.5, lat=[42.0, 42.0], lon=[77.0, 77.0], times=[5.0, 10.0])
        )

        assert result.provenance.span_s == 5.0
        assert result.provenance.shortfall_s == 0.5
