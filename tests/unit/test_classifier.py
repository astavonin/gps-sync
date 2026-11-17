import dataclasses
from datetime import timedelta
from typing import ClassVar

import pytest

from gps_sync.geo import haversine_m
from gps_sync.model import (
    Failed,
    FailedReason,
    FileContext,
    Planned,
    Protected,
    ProtectedReason,
    Unmatchable,
    UnmatchableReason,
    Via,
)
from tests.support import T0, at, classify, config, index_of, segment, tag_record

SEG = "t.gpx#trk0seg0"
# The default record's local time 10:00:30 at +06:00 is 04:00:30 UTC, i.e. 30 s after T0.
TRACK = index_of(segment(SEG, [(0, 42.0, 77.0, 100.0), (60, 42.001, 77.0, 100.0)]))
EMPTY = index_of()
# The two warnings a positionless DJI DNG carries as it left the camera, measured on exiftool 13.55.
CAMERA_ORIGINAL_DNG_WARNINGS = (
    "[minor] Possibly incorrect maker notes offsets (fix by -81484?)",
    "[Minor] Not decoding some large array(s). Ignore minor errors to decode [x2]",
)


def classify_default(record, index=TRACK, **overrides):
    return classify(record, index, config(**overrides))


class TestPositionGate:
    def test_a_present_position_is_protected_and_carries_its_value(self):
        outcome = classify_default(tag_record(gps="42.5 78.25"))

        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.POSITION_PRESENT
        assert (outcome.position.lat, outcome.position.lon) == (42.5, 78.25)

    def test_a_zero_position_is_protected_under_its_own_reason(self):
        outcome = classify_default(tag_record(gps="0 0"))

        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.ZERO_POSITION
        assert outcome.position is not None

    def test_a_position_present_only_in_xmp_reads_the_same_as_any_other(self):
        # Composite:GPSPosition is what the reader requests, and it covers XMP-only coordinates.
        outcome = classify_default(tag_record(gps="42.337 78.241", file_type="DNG", offset=None))

        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.POSITION_PRESENT

    def test_an_absent_tag_in_a_clean_record_continues_to_matching(self):
        assert isinstance(classify_default(tag_record()), Planned)

    def test_a_partial_block_with_no_composite_and_no_diagnostic_is_not_protected(self):
        # GPSLatitudeRef alone surviving yields no composite; the record simply lacks the key.
        outcome = classify_default(tag_record(extra={":GPSLatitudeRef": "N"}))

        assert isinstance(outcome, Planned)

    def test_an_error_makes_the_record_indeterminate_and_quotes_the_diagnostic(self):
        outcome = classify_default(tag_record(errors=("File format error",)))

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE
        assert "File format error" in outcome.detail

    def test_a_warning_outside_the_benign_allowlist_is_indeterminate(self):
        outcome = classify_default(tag_record(warnings=("Processing JPEG-like data",)))

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE

    def test_only_the_benign_maker_notes_warning_is_classified_on_its_tags(self):
        warning = "[minor] Possibly incorrect maker notes offsets (fix by -81484?)"

        outcome = classify_default(tag_record(warnings=(warning,)))

        assert isinstance(outcome, Planned)

    def test_the_benign_warning_does_not_excuse_a_second_unrecognised_one(self):
        warning = "[minor] Possibly incorrect maker notes offsets (fix by -81484?)"

        outcome = classify_default(tag_record(warnings=(warning, "Truncated data")))

        assert isinstance(outcome, Failed)

    def test_the_benign_warning_matches_whatever_the_case_of_its_severity_tag(self):
        warning = "[Minor] Possibly incorrect maker notes offsets (fix by -81484?)"

        assert isinstance(classify_default(tag_record(warnings=(warning,))), Planned)

    def test_an_unrecognised_minor_warning_alone_is_reported_as_such(self):
        outcome = classify_default(
            tag_record(warnings=("[Minor] Not decoding some large array(s). Ignore minor errors",))
        )

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.UNRECOGNISED_MINOR_WARNING
        assert "Not decoding" in outcome.detail

    def test_a_camera_original_dji_warning_pair_is_refused_with_no_rewrite_in_its_history(self):
        outcome = classify_default(tag_record(warnings=CAMERA_ORIGINAL_DNG_WARNINGS))

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.UNRECOGNISED_MINOR_WARNING

    def test_the_detail_quotes_only_the_warning_that_decided(self):
        outcome = classify_default(tag_record(warnings=CAMERA_ORIGINAL_DNG_WARNINGS))

        assert isinstance(outcome, Failed)
        assert outcome.detail == CAMERA_ORIGINAL_DNG_WARNINGS[1]
        assert "maker notes" not in outcome.detail

    @pytest.mark.parametrize(
        "warnings",
        [
            ("[minor] Possibly incorrect maker notes offsets", "Bad GPS directory"),
            ("[Minor] Not decoding some large array(s)", "Bad GPS directory"),
        ],
    )
    def test_a_serious_warning_beside_minor_ones_keeps_the_indeterminate_reason(self, warnings):
        outcome = classify_default(tag_record(warnings=warnings))

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE

    def test_an_error_beside_a_minor_warning_keeps_the_indeterminate_reason(self):
        outcome = classify_default(
            tag_record(errors=("File format error",), warnings=("[Minor] Not decoding x",))
        )

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE

    @pytest.mark.parametrize("raw", ["", "   ", "not coordinates", "42.5", "1 2 3", "nan nan"])
    def test_a_present_but_empty_or_unparseable_composite_is_indeterminate_not_absent(self, raw):
        outcome = classify_default(tag_record(gps=raw))

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE

    def test_a_non_string_composite_value_is_indeterminate(self):
        outcome = classify_default(tag_record(gps=["42.5 78.25"]))

        assert isinstance(outcome, Failed)

    def test_a_parseable_position_wins_over_a_diagnostic_because_the_file_is_never_written(self):
        outcome = classify_default(tag_record(gps="42.5 78.25", warnings=("Truncated data",)))

        assert isinstance(outcome, Protected)


class TestInstantRidesOnEveryVariant:
    def test_a_protected_file_carries_its_resolved_instant(self):
        outcome = classify_default(tag_record(gps="42.5 78.25"))

        assert outcome.instant == at(30)

    def test_a_protected_file_whose_instant_cannot_be_resolved_stays_protected_without_one(self):
        outcome = classify_default(tag_record(gps="42.5 78.25", offset=None))

        assert isinstance(outcome, Protected)
        assert outcome.instant is None

    def test_an_unresolvable_instant_on_a_file_that_passed_the_gate_is_a_failure(self):
        outcome = classify_default(tag_record(offset=None))

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.TIME_UNRESOLVABLE
        assert outcome.instant is None and outcome.via is None

    def test_a_row_two_still_is_classified_once_the_flag_supplies_the_offset(self):
        outcome = classify_default(tag_record(offset=None), utc_offset=timedelta(hours=6))

        assert isinstance(outcome, Planned)


class TestContainerIdentityWriteGate:
    def test_a_container_outside_the_allowlist_is_refused_after_the_match_and_keeps_its_payload(
        self,
    ):
        outcome = classify_default(tag_record(path="/m/a.HEIC", file_type="HEIC"))

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.WRITE_UNSUPPORTED_CONTAINER
        assert "HEIC" in outcome.detail and "unverified" in outcome.detail
        assert outcome.via is Via.INTERPOLATED and outcome.provenance is not None
        assert outcome.instant == at(30)

    @pytest.mark.parametrize(
        ("path", "file_type"),
        [("/m/a.JPG", "RAF"), ("/m/a.RAF", "JPEG"), ("/m/a.DNG", "RAF"), ("/m/a.RAF", None)],
    )
    def test_an_extension_that_disagrees_with_the_identity_is_refused_naming_both(
        self, path, file_type
    ):
        outcome = classify_default(tag_record(path=path, file_type=file_type))

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.WRITE_UNSUPPORTED_CONTAINER

    def test_extension_case_is_irrelevant_when_the_identity_agrees(self):
        assert isinstance(classify_default(tag_record(path="/m/a.raf")), Planned)

    def test_a_protected_unsupported_container_is_protected_not_failed(self):
        outcome = classify_default(tag_record(path="/m/a.HEIC", file_type="HEIC", gps="1 2"))

        assert isinstance(outcome, Protected)


class TestEvidenceReplay:
    """The measured DNG tags, replayed: a local timestamp matches only when read as local."""

    # DJI_20261026094631_0036_D.DNG carries DateTimeOriginal 09:46:31 with no OffsetTimeOriginal,
    # shot at +06:00, so the exposure was 03:46:31Z.
    DNG: ClassVar[dict[str, object]] = {
        "date_time_original": "2026:10:26 09:46:31",
        "offset": None,
        "file_type": "DNG",
    }
    TRACK_AT_EXPOSURE = index_of(
        segment("drone.gpx#trk0seg0", [(-840, 42.0, 77.0, None), (-780, 42.001, 77.0, None)])
    )

    def record(self):
        return tag_record("/media/drone/DJI.DNG", **self.DNG)

    def test_the_local_stamp_matches_when_read_at_the_shooting_offset(self):
        outcome = classify_default(
            self.record(), self.TRACK_AT_EXPOSURE, utc_offset=timedelta(hours=6)
        )

        assert isinstance(outcome, Planned)
        assert outcome.instant == at(-809)
        assert outcome.position.lat == pytest.approx(42.0 + 0.001 * 31 / 60, abs=1e-9)

    def test_the_same_stamp_read_as_utc_does_not_match(self):
        outcome = classify_default(self.record(), self.TRACK_AT_EXPOSURE, utc_offset=timedelta(0))

        assert isinstance(outcome, Unmatchable)
        assert outcome.reason is UnmatchableReason.OUTSIDE_COVERAGE
        assert outcome.instant == at(-809 + 6 * 3600)

    def test_without_any_offset_the_stamp_is_unresolvable_rather_than_assumed_utc(self):
        outcome = classify_default(self.record(), self.TRACK_AT_EXPOSURE)

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.TIME_UNRESOLVABLE


class TestUnmatchable:
    def test_a_coverage_miss_is_unmatchable_with_its_instant_and_magnitude(self):
        outcome = classify_default(tag_record(date_time_original="2026:10:26 10:10:00"))

        assert isinstance(outcome, Unmatchable)
        assert outcome.reason is UnmatchableReason.OUTSIDE_COVERAGE
        assert outcome.instant == at(600)
        assert outcome.miss_s == 540
        assert outcome.nearest_segment == SEG

    def test_extend_recovers_the_miss_as_extended(self):
        outcome = classify_default(tag_record(date_time_original="2026:10:26 10:10:00"), extend=600)

        assert isinstance(outcome, Planned)
        assert outcome.via is Via.EXTENDED

    def test_an_empty_index_is_outside_coverage_with_no_segment(self):
        outcome = classify_default(tag_record(), index=EMPTY)

        assert isinstance(outcome, Unmatchable)
        assert outcome.nearest_segment is None and outcome.miss_s is None


class TestMakeAndModelAreContextOnly:
    @pytest.mark.parametrize(
        "scenario",
        [
            {},
            {"gps": "42.5 78.25"},
            {"offset": None},
            {"date_time_original": "2026:10:26 10:10:00"},
            {"errors": ("boom",)},
            {"path": "/m/a.HEIC", "file_type": "HEIC"},
        ],
        ids=["planned", "protected", "time-failure", "unmatchable", "indeterminate", "container"],
    )
    @pytest.mark.parametrize(
        ("make", "model"),
        [(None, None), ("DJI", "FC9313"), ("Apple", "iPhone 15"), ("", "X-T5"), ("FUJIFILM", None)],
    )
    def test_altering_swapping_or_removing_them_changes_nothing_but_the_context(
        self, scenario, make, model
    ):
        record = tag_record(**scenario)
        baseline = classify_default(record)
        altered = classify_default(tag_record(**scenario, make=make, model=model))

        strip = {"context": FileContext(file_type=record.file_type)}
        if hasattr(baseline, "context"):
            assert dataclasses.replace(baseline, **strip) == dataclasses.replace(altered, **strip)
        assert type(baseline) is type(altered)


class TestTimeShift:
    @staticmethod
    def moving_north(speed_mps: float):
        degrees_per_second = speed_mps / 111_194.9
        return index_of(
            segment(
                SEG,
                [(t, 42.0 + degrees_per_second * t, 77.0, None) for t in range(0, 600, 10)],
            )
        )

    @staticmethod
    def record_at(seconds: int):
        stamp = (T0 + timedelta(seconds=seconds, hours=6)).strftime("%Y:%m:%d %H:%M:%S")
        return tag_record(date_time_original=stamp)

    def test_the_default_shift_is_an_identity(self):
        index = self.moving_north(1.4)
        record = self.record_at(200)

        assert classify(record, index, config()) == classify(record, index, config(time_shift=0))

    @pytest.mark.parametrize("speed", [1.4, 16.7], ids=["walking", "vehicle"])
    def test_a_sixty_second_shift_moves_the_position_by_speed_times_sixty(self, speed):
        index = self.moving_north(speed)
        record = self.record_at(200)
        base = classify(record, index, config())
        shifted = classify(record, index, config(time_shift=60))

        moved = haversine_m(
            base.position.lat, base.position.lon, shifted.position.lat, shifted.position.lon
        )

        assert moved == pytest.approx(speed * 60, rel=0.01)

    def test_the_sign_is_documented_one_plus_sixty_matches_the_position_held_sixty_seconds_later(
        self,
    ):
        index = self.moving_north(1.4)
        record = self.record_at(200)

        later = classify(self.record_at(260), index, config())
        earlier = classify(self.record_at(140), index, config())
        plus = classify(record, index, config(time_shift=60))
        minus = classify(record, index, config(time_shift=-60))

        assert plus.position.lat == pytest.approx(later.position.lat, abs=1e-9)
        assert minus.position.lat == pytest.approx(earlier.position.lat, abs=1e-9)
        assert plus.position.lat > minus.position.lat

    def test_the_shift_lands_on_the_same_position_for_local_plus_offset_and_utc_native_files(self):
        index = self.moving_north(1.4)
        local = tag_record(date_time_original="2026:10:26 10:03:20", offset="+06:00")
        utc = tag_record(
            path="/m/a.RAF",
            date_time_original=None,
            offset=None,
            create_date="2026:10:26 04:03:20",
            major_brand="isom",
            file_type="MP4",
        )

        a = classify(local, index, config(time_shift=60))
        b = classify(utc, index, config(time_shift=60))

        assert isinstance(a, Planned)
        # The MP4 is refused at the write gate but was matched at the same shifted instant.
        assert isinstance(b, Failed) and b.instant == a.instant

    def test_a_shift_can_carry_an_in_coverage_instant_out_of_coverage(self):
        index = self.moving_north(1.4)

        outcome = classify(self.record_at(500), index, config(time_shift=300))

        assert isinstance(outcome, Unmatchable)
        assert outcome.reason is UnmatchableReason.OUTSIDE_COVERAGE

    def test_a_shift_can_carry_an_out_of_coverage_instant_into_coverage(self):
        index = self.moving_north(1.4)

        assert isinstance(classify(self.record_at(-100), index, config()), Unmatchable)
        assert isinstance(classify(self.record_at(-100), index, config(time_shift=150)), Planned)

    def test_nothing_infers_a_shift_a_systematic_error_is_matched_at_its_uncorrected_instants(self):
        index = self.moving_north(1.4)

        outcome = classify(self.record_at(500 + 300), index, config())

        assert isinstance(outcome, Unmatchable)


class TestAProtectedFileWithNoResolvableInstantKeepsTheReason:
    def zoneless_positioned(self):
        return tag_record(
            gps="42.5 78.25",
            date_time_original=None,
            offset=None,
            file_type="MOV",
            create_date="2026:10:26 04:00:30",
            major_brand="qt",
        )

    def test_the_time_failure_detail_is_carried_beside_the_absent_instant(self):
        outcome = classify(self.zoneless_positioned(), TRACK, config())

        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.POSITION_PRESENT
        assert outcome.instant is None
        assert outcome.detail is not None
        assert "no zone this project can verify" in outcome.detail

    def test_it_reaches_the_json_record_and_the_report_line(self):
        from gps_sync.report import file_line
        from gps_sync.serialise import outcome_to_dict

        outcome = classify(self.zoneless_positioned(), TRACK, config())

        assert "no zone this project can verify" in outcome_to_dict(outcome)["detail"]
        assert ", no instant: the container's creation time has no zone" in file_line(outcome)

    def test_a_protected_file_whose_instant_resolved_carries_no_detail(self):
        outcome = classify(tag_record(gps="42.5 78.25"), TRACK, config())

        assert isinstance(outcome, Protected) and outcome.instant is not None
        assert outcome.detail is None
