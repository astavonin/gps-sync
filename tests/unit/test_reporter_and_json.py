import json

import pytest

from gps_sync.model import (
    Extended,
    Failed,
    FailedReason,
    FileContext,
    Geotagged,
    Ignored,
    IgnoredReason,
    Interpolated,
    Planned,
    Position,
    Protected,
    ProtectedReason,
    RunLevelReason,
    Unmatchable,
    UnmatchableReason,
    Via,
)
from gps_sync.registry import REGISTRY
from gps_sync.report import render_report
from gps_sync.runrecord import build_run_record, render_json
from gps_sync.summary import summarise
from tests.support import at, run_info

SEG = "t.gpx#trk0seg0"
POS = Position(42.0, 77.0, 100.0)
CTX = FileContext("FUJIFILM", "X-T5", "RAF")


def interp(
    speed: float | None = 1.0,
    ele: float | None = 100.0,
    via=Via.INTERPOLATED,
    cls=Planned,
    path="/m/p.RAF",
):
    provenance = (
        Interpolated(SEG, 5.0)
        if via is Via.INTERPOLATED
        else Extended(SEG, 600.0, UnmatchableReason.OUTSIDE_COVERAGE)
    )
    return cls(path, at(0), Position(42.0, 77.0, ele), via, provenance, speed, CTX)


def unmatchable(reason, miss, path="/m/u.RAF", bracket=None, nearest=SEG):
    return Unmatchable(path, at(0), reason, nearest, miss, bracket, None, CTX)


def matched_after_refusal(kind, path="/m/x.RAF", via=Via.INTERPOLATED):
    provenance = (
        Interpolated(SEG, 5.0)
        if via is Via.INTERPOLATED
        else Extended(SEG, 600.0, UnmatchableReason.OUTSIDE_COVERAGE)
    )
    if kind == "appeared":
        return Protected(
            path, ProtectedReason.POSITION_APPEARED, instant=at(0), via=via, provenance=provenance
        )
    return Failed(path, kind, "detail", instant=at(0), via=via, provenance=provenance)


def report(outcomes, **info):
    return render_report(outcomes, summarise(outcomes), run_info(**info))


class TestPerFileDetail:
    def test_an_unmatchable_line_carries_reason_nearest_segment_and_miss_magnitude(self):
        text = report([unmatchable(UnmatchableReason.OUTSIDE_COVERAGE, 3037.0)])

        line = next(line for line in text.splitlines() if "/m/u.RAF" in line)
        assert "OUTSIDE_COVERAGE" in line
        assert SEG in line
        assert "3037s" in line

    def test_the_two_gap_reasons_are_counted_separately_and_never_summed(self):
        outcomes = [
            unmatchable(UnmatchableReason.MAX_GAP_EXCEEDED, 40.0, "/m/1.RAF", bracket=200.0),
            unmatchable(UnmatchableReason.MAX_GAP_EXCEEDED, 41.0, "/m/2.RAF", bracket=210.0),
            unmatchable(UnmatchableReason.SEGMENT_BOUNDARY, 3000.0, "/m/3.RAF", bracket=9000.0),
        ]

        text = report(outcomes)

        assert "MAX_GAP_EXCEEDED (sampling dropout inside one activity): 2" in text
        assert "SEGMENT_BOUNDARY (camera used between two activities): 1" in text
        assert "outside track coverage" not in text.lower()
        assert "Unmatchable: 3" in text

    def test_an_empty_index_miss_says_so_rather_than_naming_a_segment(self):
        text = report([unmatchable(UnmatchableReason.OUTSIDE_COVERAGE, None, nearest=None)])

        assert "index was empty" in text

    def test_protected_appeared_says_the_position_was_never_read_beside_a_zero_position(self):
        outcomes = [
            matched_after_refusal("appeared", "/m/a.RAF"),
            Protected("/m/z.RAF", ProtectedReason.ZERO_POSITION, position=Position(0.0, 0.0)),
        ]

        text = report(outcomes)

        appeared = next(line for line in text.splitlines() if "/m/a.RAF" in line)
        zero = next(line for line in text.splitlines() if "/m/z.RAF" in line)
        assert "never read" in appeared and "0.000000, 0.000000" not in appeared
        assert "0.000000, 0.000000" in zero and "never read" not in zero

    def test_make_and_model_appear_on_the_per_file_line_as_context(self):
        text = report([interp()])

        assert "[FUJIFILM X-T5]" in text

    def test_altitude_unavailability_is_stated(self):
        text = report([interp(ele=None)])

        assert "altitude not available" in text
        assert "Altitude not available for 1" in text

    def test_every_registry_row_label_and_remedy_renders_for_a_sequence_containing_it(self):
        sequence = {
            ProtectedReason.POSITION_PRESENT: Protected(
                "/m/p", ProtectedReason.POSITION_PRESENT, position=Position(42.0, 77.0)
            ),
            IgnoredReason.SYMLINK: Ignored("/m/l", IgnoredReason.SYMLINK, "/m/real"),
            IgnoredReason.DUPLICATE_INODE: Ignored(
                "/m/d", IgnoredReason.DUPLICATE_INODE, "/m/kept"
            ),
            ProtectedReason.ZERO_POSITION: Protected(
                "/m/z", ProtectedReason.ZERO_POSITION, position=Position(0.0, 0.0)
            ),
            ProtectedReason.POSITION_APPEARED: matched_after_refusal("appeared"),
            UnmatchableReason.MAX_GAP_EXCEEDED: unmatchable(
                UnmatchableReason.MAX_GAP_EXCEEDED, 5.0, bracket=500.0
            ),
            UnmatchableReason.SEGMENT_BOUNDARY: unmatchable(
                UnmatchableReason.SEGMENT_BOUNDARY, 5.0, bracket=500.0
            ),
            UnmatchableReason.OUTSIDE_COVERAGE: unmatchable(
                UnmatchableReason.OUTSIDE_COVERAGE, 5.0
            ),
            UnmatchableReason.BRACKET_UNSOUND: unmatchable(UnmatchableReason.BRACKET_UNSOUND, 5.0),
            FailedReason.TIME_UNRESOLVABLE: Failed("/m/t", FailedReason.TIME_UNRESOLVABLE, "d"),
            FailedReason.METADATA_INDETERMINATE: Failed(
                "/m/i", FailedReason.METADATA_INDETERMINATE, "d"
            ),
            FailedReason.UNRECOGNISED_MINOR_WARNING: Failed(
                "/m/w", FailedReason.UNRECOGNISED_MINOR_WARNING, "[Minor] Not decoding"
            ),
            FailedReason.WRITE_UNSUPPORTED_CONTAINER: matched_after_refusal(
                FailedReason.WRITE_UNSUPPORTED_CONTAINER
            ),
            FailedReason.WRITE_ERROR: matched_after_refusal(FailedReason.WRITE_ERROR),
            FailedReason.WRITE_UNATTRIBUTED: matched_after_refusal(FailedReason.WRITE_UNATTRIBUTED),
        }

        remedy_bearing = {key for key, row in REGISTRY.items() if row.remedy}
        run_level = {key for key in remedy_bearing if isinstance(key, RunLevelReason)}
        assert set(sequence) == remedy_bearing - run_level

        for key, outcome in sequence.items():
            text = report([outcome])
            row = REGISTRY[key]
            assert row.label in text, key
            assert row.remedy, key
            assert row.remedy in text, key

    def test_a_sequence_with_an_intent_phase_prior_record_renders_the_notice_first(self):
        text = report([interp()], incomplete_prior_records=(("20261114T000000Z-7.json", "t", 3),))

        assert text.splitlines()[0].startswith(
            "WARNING: incomplete prior run 20261114T000000Z-7.json"
        )
        assert "3 path(s)" in text.splitlines()[0]

    def test_a_clean_dry_run_does_not_promise_a_clean_write_run(self):
        assert "does not guarantee a clean write run" in report([interp()])
        assert "does not guarantee" not in report([interp()], mode="write")


class TestRemedyFigures:
    def test_the_smallest_extend_that_recovers_all_misses_is_computed(self):
        outcomes = [
            unmatchable(UnmatchableReason.OUTSIDE_COVERAGE, 3000.0, "/m/1.RAF"),
            unmatchable(UnmatchableReason.OUTSIDE_COVERAGE, 3720.4, "/m/2.RAF"),
        ]

        summary = summarise(outcomes)
        text = report(outcomes)

        [figure] = summary.remedies
        assert (
            figure.parameter,
            figure.smallest_to_recover_any_s,
            figure.smallest_to_recover_all_s,
        ) == (
            "--extend",
            3000,
            3721,
        )
        assert "--extend 3721s would recover all of them" in text

    def test_width_refusals_name_the_smallest_max_gap_and_boundaries_say_max_gap_is_wrong(self):
        outcomes = [
            unmatchable(UnmatchableReason.MAX_GAP_EXCEEDED, 5.0, "/m/1.RAF", bracket=130.2),
            unmatchable(UnmatchableReason.MAX_GAP_EXCEEDED, 5.0, "/m/2.RAF", bracket=300.0),
            unmatchable(UnmatchableReason.SEGMENT_BOUNDARY, 3000.0, "/m/3.RAF", bracket=9000.0),
        ]

        text = report(outcomes)

        assert "--max-gap 300s would admit all of them" in text
        boundary = next(
            line
            for line in text.splitlines()
            if "SEGMENT_BOUNDARY (" in line and "--extend" in line
        )
        assert "--extend 3000s" in boundary and "--max-gap is not the remedy" in boundary

    def test_bracket_unsound_has_no_numeric_remedy(self):
        summary = summarise([unmatchable(UnmatchableReason.BRACKET_UNSOUND, 5.0, bracket=60.0)])

        assert summary.remedies == ()


class TestCoverageRate:
    def counts(self, n_matched, n_miss):
        return [interp(path=f"/m/m{i}.RAF") for i in range(n_matched)] + [
            unmatchable(UnmatchableReason.OUTSIDE_COVERAGE, 1.0, f"/m/u{i}.RAF")
            for i in range(n_miss)
        ]

    def test_323_matched_and_26_unmatchable_is_92_point_6_percent(self):
        coverage = summarise(self.counts(323, 26)).coverage

        assert (coverage.matched, coverage.reached_matcher) == (323, 349)
        assert coverage.rate == pytest.approx(92.55, abs=0.05)
        assert "Coverage: 323/349 = 92.6 %" in report(self.counts(323, 26))

    def test_ignored_protected_and_pre_matching_failures_are_outside_both_terms(self):
        base = summarise(self.counts(3, 1)).coverage
        extras = [
            Ignored("/m/i", IgnoredReason.NOT_A_STILL),
            Protected("/m/p1", ProtectedReason.POSITION_PRESENT, position=POS),
            Protected("/m/p2", ProtectedReason.ZERO_POSITION, position=Position(0.0, 0.0)),
            Failed("/m/f1", FailedReason.TIME_UNRESOLVABLE),
            Failed("/m/f2", FailedReason.METADATA_INDETERMINATE),
        ]

        assert summarise(self.counts(3, 1) + extras).coverage == base

    @pytest.mark.parametrize(
        "member",
        [
            matched_after_refusal(FailedReason.WRITE_UNSUPPORTED_CONTAINER, "/m/w1"),
            matched_after_refusal(FailedReason.WRITE_ERROR, "/m/w2"),
            matched_after_refusal(FailedReason.WRITE_UNATTRIBUTED, "/m/w3"),
            matched_after_refusal("appeared", "/m/w4"),
        ],
        ids=["unsupported", "write-error", "unattributed", "appeared"],
    )
    def test_a_matched_file_that_was_not_written_counts_in_numerator_and_denominator(self, member):
        base = summarise(self.counts(3, 1)).coverage
        with_member = summarise([*self.counts(3, 1), member]).coverage

        assert (with_member.matched, with_member.reached_matcher) == (
            base.matched + 1,
            base.reached_matcher + 1,
        )

    def test_protected_present_and_zero_alone_render_not_applicable_not_zero_percent(self):
        outcomes = [
            Protected("/m/p1", ProtectedReason.POSITION_PRESENT, position=POS),
            Protected("/m/p2", ProtectedReason.ZERO_POSITION, position=Position(0.0, 0.0)),
        ]

        summary = summarise(outcomes)
        text = report(outcomes)

        assert summary.coverage.rate is None
        assert "Coverage: not applicable" in text and "healthy" in text
        assert "0.0 %" not in text and "0 %" not in text

    def test_an_all_appeared_sequence_has_a_nonzero_denominator_and_renders_100_percent(self):
        outcomes = [matched_after_refusal("appeared", f"/m/a{i}") for i in range(3)]

        assert summarise(outcomes).coverage.rate == 100.0

    def test_the_numerator_splits_totally_by_via_including_matched_then_refused_members(self):
        outcomes = [
            interp(path="/m/1"),
            interp(via=Via.EXTENDED, path="/m/2"),
            matched_after_refusal(FailedReason.WRITE_UNSUPPORTED_CONTAINER, "/m/3", Via.EXTENDED),
            matched_after_refusal(FailedReason.WRITE_ERROR, "/m/4", Via.INTERPOLATED),
            matched_after_refusal(FailedReason.WRITE_UNATTRIBUTED, "/m/5", Via.EXTENDED),
            matched_after_refusal("appeared", "/m/6", Via.INTERPOLATED),
            unmatchable(UnmatchableReason.OUTSIDE_COVERAGE, 1.0),
        ]

        coverage = summarise(outcomes).coverage

        assert coverage.interpolated + coverage.extended == coverage.matched == 6
        assert (coverage.interpolated, coverage.extended) == (3, 3)


class TestClockSensitivity:
    def test_median_speed_and_the_sixty_second_displacement_follow_the_known_speed(self):
        outcomes = [interp(speed=1.5, path=f"/m/{i}") for i in range(5)]

        sensitivity = summarise(outcomes).sensitivity

        assert sensitivity.median_speed_mps == 1.5
        assert sensitivity.displacement_60s_m == pytest.approx(90.0)
        assert "1.50 m/s" in report(outcomes) and "90.0 m" in report(outcomes)

    def test_only_interpolated_planned_and_geotagged_files_with_a_present_speed_contribute(self):
        outcomes = [
            interp(speed=1.0, path="/m/ok1"),
            interp(speed=3.0, path="/m/ok2", cls=Geotagged),
            interp(speed=100.0, via=Via.EXTENDED, path="/m/ext"),
            interp(speed=None, path="/m/absent"),
            Ignored("/m/ign", IgnoredReason.NOT_A_STILL),
            unmatchable(UnmatchableReason.OUTSIDE_COVERAGE, 1.0),
            Protected("/m/pro", ProtectedReason.POSITION_PRESENT, position=POS),
            Failed(
                "/m/failed",
                FailedReason.WRITE_ERROR,
                "d",
                instant=at(0),
                via=Via.INTERPOLATED,
                provenance=Interpolated(SEG, 5.0),
                bracket_speed=500.0,
            ),
            matched_after_refusal("appeared", "/m/appeared"),
        ]

        sensitivity = summarise(outcomes).sensitivity

        assert sensitivity.contributors == 2
        assert sensitivity.median_speed_mps == 2.0
        assert sensitivity.excluded == {
            "extended": 1,
            "speed_absent": 1,
            "ignored": 1,
            "unmatchable": 1,
            "protected": 2,
            "failed": 1,
        }
        assert sensitivity.excluded_total == 7

    def test_a_file_that_failed_its_write_keeps_its_speed_but_stops_contributing(self):
        # The design's rule: only Planned and Geotagged contribute. A write-phase failure is one
        # of the outcomes only a write run can reach, so the dry-run population is an upper bound.
        dry = [interp(speed=1.0, path="/m/a"), interp(speed=2.0, path="/m/b")]
        written = [
            interp(speed=1.0, path="/m/a", cls=Geotagged),
            Failed(
                "/m/b",
                FailedReason.WRITE_ERROR,
                "d",
                instant=at(0),
                via=Via.INTERPOLATED,
                provenance=Interpolated(SEG, 5.0),
                bracket_speed=2.0,
            ),
        ]

        assert summarise(dry).sensitivity.contributors == 2
        assert summarise(written).sensitivity.contributors == 1

    def test_planned_and_geotagged_over_the_same_files_contribute_identically(self):
        planned = [interp(speed=s, path=f"/m/{s}") for s in (1.0, 2.0, 4.0)]
        geotagged = [interp(speed=s, path=f"/m/{s}", cls=Geotagged) for s in (1.0, 2.0, 4.0)]

        assert summarise(planned).sensitivity == summarise(geotagged).sensitivity

    def test_the_clamp_window_distribution_is_reported_separately_for_extended_files(self):
        outcomes = [interp(via=Via.EXTENDED, path=f"/m/{i}") for i in range(3)]

        window = summarise(outcomes).clamp_window
        text = report(outcomes)

        assert (window.count, window.smallest_s, window.median_s, window.largest_s) == (
            3,
            600,
            600,
            600,
        )
        assert "Extended clamp windows: 3 file(s)" in text

    def test_no_contributing_file_is_not_applicable_and_does_not_divide_by_zero(self):
        text = report([unmatchable(UnmatchableReason.OUTSIDE_COVERAGE, 1.0)])

        assert "Clock sensitivity: not applicable" in text

    def test_a_vehicle_speed_reports_proportionally_larger_displacement(self):
        walking = summarise([interp(speed=1.4)]).sensitivity.displacement_60s_m
        vehicle = summarise([interp(speed=16.7)]).sensitivity.displacement_60s_m

        assert vehicle / walking == pytest.approx(16.7 / 1.4)

    def test_a_zero_speed_is_a_real_value_and_never_confused_with_absent(self):
        zero = summarise([interp(speed=0.0)]).sensitivity
        absent = summarise([interp(speed=None)]).sensitivity

        assert zero.median_speed_mps == 0.0 and zero.contributors == 1
        assert absent.median_speed_mps is None and absent.contributors == 0

    def test_the_line_is_printed_in_a_dry_run_and_a_write_run(self):
        outcomes = [interp(speed=1.0)]

        assert "Clock sensitivity" in report(outcomes, mode="dry-run")
        assert "Clock sensitivity" in report(outcomes, mode="write")


class TestJsonRecord:
    def sequence(self):
        return [
            Ignored("/m/i.txt", IgnoredReason.NOT_A_STILL),
            Protected(
                "/m/p.RAF",
                ProtectedReason.POSITION_PRESENT,
                position=POS,
                instant=at(1),
                context=CTX,
            ),
            Protected(
                "/m/z.RAF",
                ProtectedReason.ZERO_POSITION,
                position=Position(0.0, 0.0),
                instant=at(2),
            ),
            matched_after_refusal("appeared", "/m/a.RAF"),
            Failed("/m/f.RAF", FailedReason.TIME_UNRESOLVABLE, "no tag"),
            unmatchable(UnmatchableReason.SEGMENT_BOUNDARY, 3000.0, bracket=9000.0),
            interp(speed=0.0, path="/m/zero.RAF"),
            interp(speed=None, ele=None, path="/m/none.RAF"),
            interp(path="/m/g.RAF", cls=Geotagged),
        ]

    def parsed(self, outcomes=None):
        outcomes = outcomes or self.sequence()
        return json.loads(render_json(outcomes, summarise(outcomes), run_info()))

    def test_every_variant_is_represented_with_the_registrys_stable_reason_strings(self):
        files = self.parsed()["files"]

        assert {f["variant"] for f in files} == {
            "Ignored",
            "Protected",
            "Failed",
            "Unmatchable",
            "Planned",
            "Geotagged",
        }
        names = {key.name for key in REGISTRY}
        assert {f["reason"] for f in files if "reason" in f} <= names

    def test_protected_appeared_omits_position_while_zero_position_renders_zero(self):
        files = {f["path"]: f for f in self.parsed()["files"]}

        assert "position" not in files["/m/a.RAF"]
        assert files["/m/z.RAF"]["position"] == {"lat": 0.0, "lon": 0.0}
        assert files["/m/a.RAF"]["via"] == "INTERPOLATED"
        assert files["/m/a.RAF"]["provenance"]["kind"] == "Interpolated"

    def test_an_absent_speed_and_elevation_are_omitted_and_a_zero_speed_is_rendered(self):
        files = {f["path"]: f for f in self.parsed()["files"]}

        assert files["/m/zero.RAF"]["bracket_speed"] == 0.0
        assert "bracket_speed" not in files["/m/none.RAF"]
        assert "ele" not in files["/m/none.RAF"]["position"]
        assert files["/m/g.RAF"]["position"]["ele"] == 100.0

    def test_match_payload_presence_follows_the_variant_rules_in_both_directions(self):
        files = {f["path"]: f for f in self.parsed()["files"]}

        assert "provenance" in files["/m/a.RAF"]
        for path in ("/m/p.RAF", "/m/z.RAF", "/m/f.RAF"):
            assert "provenance" not in files[path] and "via" not in files[path]

    def test_counts_derived_from_the_json_equal_the_report_counts(self):
        outcomes = self.sequence()
        record = self.parsed(outcomes)
        text = report(outcomes)

        for variant, count in record["summary"]["variant_counts"].items():
            assert f"{variant}: {count}" in text
        for reason, count in record["summary"]["reason_counts"].items():
            if count:
                assert f"{reason} (" in text and f"): {count}" in text

    def test_the_json_carries_the_run_level_figures_the_report_carries(self):
        record = self.parsed()

        assert {
            "coverage",
            "remedies",
            "sensitivity",
            "clamp_window",
            "variant_counts",
            "reason_counts",
        } <= set(record["summary"])
        assert record["summary"]["coverage"]["applicable"] is True
        assert {"review_folder", "audit_directory", "audit_complete", "pruned"} <= set(
            record["artifacts"]
        )
        assert record["registry"]["SEGMENT_BOUNDARY"]["subfolder"] == "between-segments"
        assert record["reconciliation"] is None

    def test_a_not_applicable_rate_is_explicit_in_json(self):
        record = self.parsed([Protected("/m/p", ProtectedReason.POSITION_PRESENT, position=POS)])

        assert record["summary"]["coverage"]["rate_percent"] is None
        assert record["summary"]["coverage"]["applicable"] is False
        assert record["summary"]["sensitivity"]["applicable"] is False

    def test_the_record_is_valid_json_with_no_human_report_text(self):
        text = render_json(self.sequence(), summarise(self.sequence()), run_info())

        json.loads(text)
        assert "gps-sync dry run" not in text

    def test_build_run_record_is_json_serialisable(self):
        outcomes = self.sequence()

        json.dumps(build_run_record(outcomes, summarise(outcomes), run_info(), 0))
