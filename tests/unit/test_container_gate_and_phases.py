"""The atom-aware gate, the path verdict, the three classification phases and the write gate."""

from datetime import UTC, datetime

import pytest

from gps_sync.classifier import GpxPath, TelemetryPath, decide_path, derive_outcome
from gps_sync.errors import InternalConsistencyError
from gps_sync.model import (
    AltitudeOutcome,
    Centroid,
    Failed,
    FailedReason,
    FileContext,
    Planned,
    Position,
    Protected,
    ProtectedReason,
    Via,
)
from gps_sync.records import TelemetryRecord
from tests.support import classify, config, index_of, segment, tag_record, telemetry_json

EXTRACT = "[minor] The ExtractEmbedded option may find more tags in the media data"
ATOM_TIME = "2026:10:26 04:00:30"
EMPTY = index_of(segment("t#0", [(0, 42.0, 77.0, 1000.0), (60, 42.06, 77.0, 1060.0)]))


def clip(path="/m/a.MP4", file_type="MP4", **kwargs):
    defaults = {
        "file_type": file_type,
        "date_time_original": None,
        "offset": None,
        "create_date": ATOM_TIME,
        "major_brand": "isom",
        "extra": {":Duration": 0.4004},
    }
    defaults.update(kwargs)
    return tag_record(path, **defaults)


def with_extra(record_kwargs, **extra):
    return {**record_kwargs, "extra": {**record_kwargs.get("extra", {}), **extra}}


class TestTheAtomAwareGate:
    def test_a_readable_atom_is_protected_with_its_position_and_elevation(self):
        outcome = classify(clip(extra={":GPSCoordinates": "42.5 77.5 100.0"}), EMPTY, config())

        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.POSITION_PRESENT
        assert outcome.position == Position(42.5, 77.5, 100.0)

    def test_a_zero_atom_is_protected_as_a_zero_position(self):
        outcome = classify(clip(extra={":GPSCoordinates": "0 0"}), EMPTY, config())

        assert outcome.reason is ProtectedReason.ZERO_POSITION

    def test_an_unreadable_atom_is_indeterminate_when_composite_emitted_no_key(self):
        value = "+42,0721+077,1292+2189,900/"

        outcome = classify(clip(extra={":GPSCoordinates": value}), EMPTY, config())

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE
        assert repr(value) in outcome.detail

    def test_an_unreadable_atom_is_indeterminate_whatever_composite_says(self):
        outcome = classify(clip(gps="1 2", extra={":GPSCoordinates": "garbage"}), EMPTY, config())

        assert outcome.reason is FailedReason.METADATA_INDETERMINATE

    def test_two_atoms_are_indeterminate_naming_the_count_and_both_values(self):
        extra = {":GPSCoordinates": "1 2", "Copy1:GPSCoordinates": "3 4"}

        outcome = classify(clip(extra=extra), EMPTY, config())

        assert outcome.reason is FailedReason.METADATA_INDETERMINATE
        assert "2 container position atoms" in outcome.detail
        assert "'1 2'" in outcome.detail and "'3 4'" in outcome.detail

    def test_a_parseable_atom_wins_over_a_parseable_composite(self):
        outcome = classify(
            clip(gps="1 2", extra={":GPSCoordinates": "42.5 77.5 100.0"}), EMPTY, config()
        )

        assert outcome.position == Position(42.5, 77.5, 100.0)

    def test_no_atom_and_a_composite_position_still_protects_a_file_as_before(self):
        outcome = classify(tag_record(gps="42.5 78.25"), EMPTY, config())

        assert outcome.reason is ProtectedReason.POSITION_PRESENT

    def test_a_positionless_still_and_a_positionless_clip_both_continue_to_derivation(self):
        assert isinstance(
            classify(tag_record(date_time_original="2026:10:26 10:00:30"), EMPTY, config()), Planned
        )
        assert isinstance(classify(clip(), EMPTY, config()), Planned)


class TestTheScopedWarningAllowlist:
    def test_the_extract_embedded_advisory_is_benign_for_a_video_container(self):
        assert isinstance(classify(clip(warnings=(EXTRACT,)), EMPTY, config()), Planned)

    def test_the_same_advisory_on_a_still_keeps_001s_verdict(self):
        outcome = classify(tag_record(warnings=(EXTRACT,)), EMPTY, config())

        assert outcome.reason is FailedReason.UNRECOGNISED_MINOR_WARNING

    def test_the_maker_notes_advisory_stays_benign_for_every_class(self):
        warning = "[minor] Possibly incorrect maker notes offsets (fix by -F)"

        assert isinstance(classify(tag_record(warnings=(warning,)), EMPTY, config()), Planned)
        assert isinstance(classify(clip(warnings=(warning,)), EMPTY, config()), Planned)

    def test_an_advisory_beside_a_serious_warning_does_not_hide_it_on_a_video(self):
        outcome = classify(clip(warnings=(EXTRACT, "Bad GPS directory")), EMPTY, config())

        assert outcome.reason is FailedReason.METADATA_INDETERMINATE


class TestTheWriteGateComparesClasses:
    @pytest.mark.parametrize(
        ("path", "identity"),
        [("/m/a.mov", "MP4"), ("/m/a.mp4", "MOV"), ("/m/a.MOV", "MOV"), ("/m/a.mp4", "MP4")],
    )
    def test_both_quicktime_spellings_are_admitted_together(self, path, identity):
        assert isinstance(classify(clip(path, identity), EMPTY, config()), Planned)

    @pytest.mark.parametrize(
        ("path", "identity", "text"),
        [
            ("/m/a.raf", "MP4", "different write-set classes"),
            ("/m/a.mp4", "RAF", "different write-set classes"),
            ("/m/a.dng", "RAF", "disagrees with container identity RAF"),
            ("/m/a.m4v", "MP4", "different write-set classes"),
            ("/m/a.mp4", "M4V", "write safety is unverified"),
        ],
    )
    def test_a_disagreement_is_refused_with_a_detail_that_is_true(self, path, identity, text):
        record = (
            clip(path, identity)
            if identity in {"MP4", "M4V"}
            else tag_record(path, file_type=identity)
        )

        outcome = classify(record, EMPTY, config())

        assert outcome.reason is FailedReason.WRITE_UNSUPPORTED_CONTAINER
        assert text in outcome.detail

    def test_a_refused_container_keeps_the_match_payload(self):
        outcome = classify(clip("/m/a.raf", "MP4"), EMPTY, config())

        assert outcome.via is Via.INTERPOLATED and outcome.provenance is not None


class TestThePathVerdict:
    def test_a_container_without_a_meta_track_takes_the_gpx_path(self):
        verdict = decide_path(
            clip(extra={":HandlerType": "vide", "Copy1:HandlerType": "soun"}), config()
        )

        assert isinstance(verdict, GpxPath)

    def test_a_meta_track_on_any_instance_takes_the_telemetry_path(self):
        extra = {":HandlerType": "mdta", "Copy1:HandlerType": "vide", "Copy3:HandlerType": "meta"}

        assert isinstance(decide_path(clip(extra=extra), config()), TelemetryPath)

    def test_an_atom_terminates_before_the_verdict_so_no_telemetry_read_is_needed(self):
        extra = {":HandlerType": "meta", ":GPSCoordinates": "1 2"}

        assert isinstance(decide_path(clip(extra=extra), config()), Protected)

    def test_an_unverified_time_terminates_before_the_verdict(self):
        outcome = decide_path(clip(major_brand="qt", extra={":HandlerType": "meta"}), config())

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.TIME_SEMANTICS_UNVERIFIED

    def test_the_verdict_carries_the_resolved_instant(self):
        verdict = decide_path(clip(), config(time_shift=60))

        assert verdict.instant.isoformat() == "2026-10-26T04:01:30+00:00"


def telemetry(**kwargs) -> TelemetryRecord:
    return TelemetryRecord.from_json(telemetry_json("/m/a.MP4", **kwargs))


GOOD = {"lat": [42.0, 42.2], "lon": [77.0, 77.2], "alt": [10.0, 20.0], "times": [0.0, 0.4]}
META = {":HandlerType": "meta"}


class TestTheTelemetryPhase:
    def test_a_clip_with_telemetry_is_planned_from_its_centroid_and_never_the_track(self):
        outcome = classify(
            clip(extra={**META, ":Duration": 0.4004}), EMPTY, config(), telemetry(**GOOD)
        )

        assert isinstance(outcome, Planned)
        assert outcome.via is Via.CENTROID
        assert isinstance(outcome.provenance, Centroid)
        assert outcome.bracket_speed is None
        assert outcome.position.lat == pytest.approx(42.1)
        assert outcome.position.ele == pytest.approx(15.0)

    def test_the_track_is_not_a_fallback_for_an_unusable_clip(self):
        bad = telemetry(lat=[0.0], lon=[0.0], times=[0.0])

        outcome = classify(clip(extra={**META, ":Duration": 0.0167}), EMPTY, config(), bad)

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.TELEMETRY_UNUSABLE
        assert outcome.via is None and outcome.provenance is None
        assert outcome.instant is not None

    def test_an_inconsistent_record_is_a_failure_with_its_own_reason(self):
        bad = telemetry(lat=[42.0, 42.1], lon=[77.0], times=[0.0, 0.4])

        outcome = classify(clip(extra={**META, ":Duration": 0.4004}), EMPTY, config(), bad)

        assert outcome.reason is FailedReason.TELEMETRY_INCONSISTENT

    def test_an_atom_that_appeared_between_the_reads_is_protected_with_the_instant(self):
        appeared = telemetry(atoms=["42.5 77.5 100.0"])

        outcome = classify(clip(extra=META), EMPTY, config(), appeared)

        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.POSITION_PRESENT
        assert outcome.position == Position(42.5, 77.5, 100.0)
        assert outcome.instant is not None

    def test_a_zero_atom_that_appeared_is_a_zero_position(self):
        outcome = classify(clip(extra=META), EMPTY, config(), telemetry(atoms=["0 0"]))

        assert outcome.reason is ProtectedReason.ZERO_POSITION

    def test_an_unreadable_atom_that_appeared_is_indeterminate(self):
        outcome = classify(clip(extra=META), EMPTY, config(), telemetry(atoms=["x"]))

        assert outcome.reason is FailedReason.METADATA_INDETERMINATE

    def test_a_telemetry_verdict_with_no_read_is_an_internal_consistency_failure(self):
        verdict = decide_path(clip(extra=META), config())

        with pytest.raises(InternalConsistencyError, match="telemetry path with no read"):
            derive_outcome(clip(extra=META), verdict, None, EMPTY, config())

    def test_a_clip_whose_container_is_unverified_is_failed_after_a_centroid_match(self):
        outcome = classify(
            clip("/m/a.m4v", "M4V", extra={**META, ":Duration": 0.4004}),
            EMPTY,
            config(),
            telemetry(**GOOD),
        )

        assert outcome.reason is FailedReason.WRITE_UNSUPPORTED_CONTAINER
        assert outcome.via is Via.CENTROID


class TestTheElevationGuardInClassification:
    def test_a_non_finite_gpx_elevation_is_dropped_and_flagged(self):
        index = index_of(segment("t#0", [(0, 42.0, 77.0, -1.7e308), (60, 42.06, 77.0, 1.7e308)]))

        outcome = classify(tag_record(date_time_original="2026:10:26 10:00:30"), index, config())

        assert isinstance(outcome, Planned)
        assert outcome.position.ele is None
        assert outcome.elevation_dropped

    def test_a_representable_elevation_is_kept_and_not_flagged(self):
        outcome = classify(tag_record(date_time_original="2026:10:26 10:00:30"), EMPTY, config())

        assert outcome.position.ele is not None
        assert not outcome.elevation_dropped

    def test_a_gpx_point_without_an_elevation_is_not_a_drop(self):
        index = index_of(segment("t#0", [(0, 42.0, 77.0, None), (60, 42.06, 77.0, None)]))

        outcome = classify(tag_record(date_time_original="2026:10:26 10:00:30"), index, config())

        assert outcome.position.ele is None
        assert not outcome.elevation_dropped

    def test_a_centroid_whose_altitude_is_not_representable_is_flagged(self):
        huge = telemetry(lat=[42.0] * 3, lon=[77.0] * 3, alt=[1.7e308] * 3, times=[0.0, 0.2, 0.4])

        outcome = classify(clip(extra={**META, ":Duration": 0.4004}), EMPTY, config(), huge)

        assert outcome.position.ele is None
        assert outcome.elevation_dropped
        assert outcome.provenance.altitude is AltitudeOutcome.NOT_REPRESENTABLE


INSTANT = datetime(2026, 10, 26, 4, 0, 30, tzinfo=UTC)
CONTEXT = FileContext("FUJIFILM", "X-T5", "MP4")
STILL_CONTEXT = FileContext("FUJIFILM", "X-T5", "RAF")


class TestEveryOutcomeFieldIsAsserted:
    def test_an_atom_protected_clip_carries_its_context_and_instant(self):
        outcome = classify(clip(extra={":GPSCoordinates": "42.5 77.5 100.0"}), EMPTY, config())

        assert outcome == Protected(
            "/m/a.MP4",
            ProtectedReason.POSITION_PRESENT,
            position=Position(42.5, 77.5, 100.0),
            instant=INSTANT,
            context=CONTEXT,
        )

    def test_a_half_zero_atom_is_not_a_zero_position(self):
        outcome = classify(clip(extra={":GPSCoordinates": "0 5"}), EMPTY, config())

        assert outcome.reason is ProtectedReason.POSITION_PRESENT

    def test_an_indeterminate_atom_carries_its_context_instant_and_exact_detail(self):
        outcome = classify(clip(extra={":GPSCoordinates": "x"}), EMPTY, config())

        assert outcome == Failed(
            "/m/a.MP4",
            FailedReason.METADATA_INDETERMINATE,
            "a container position atom is present but unparseable: 'x'",
            instant=INSTANT,
            context=CONTEXT,
        )

    def test_several_atoms_name_each_value_separated_in_the_detail(self):
        extra = {
            ":GPSCoordinates": "1 2",
            "Copy1:GPSCoordinates": "3 4",
            "Copy2:GPSCoordinates": "5 6",
        }

        outcome = classify(clip(extra=extra), EMPTY, config())

        assert outcome.detail == (
            "3 container position atoms were read and may disagree: '1 2'; '3 4'; '5 6'"
        )

    def test_several_diagnostics_are_joined_with_a_separator(self):
        outcome = classify(tag_record(errors=("first problem", "second problem")), EMPTY, config())

        assert outcome.detail == "first problem; second problem"

    def test_a_time_refusal_carries_the_context_and_the_exact_detail(self):
        outcome = classify(clip(major_brand="qt"), EMPTY, config())

        assert outcome == Failed(
            "/m/a.MP4",
            FailedReason.TIME_SEMANTICS_UNVERIFIED,
            "the container's creation time has no zone this project can verify "
            "(CreateDate instances 1, CreationDate None, MajorBrand 'qt')",
            context=CONTEXT,
        )

    def test_an_unmatchable_clip_carries_the_whole_matcher_payload(self):
        far = index_of(segment("t#0", [(5000, 42.0, 77.0, 1.0), (5060, 42.1, 77.0, 1.0)]))

        outcome = classify(clip(), far, config())

        assert outcome.reason.value == "OUTSIDE_COVERAGE"
        assert outcome.instant == INSTANT
        assert outcome.context == CONTEXT
        assert (outcome.nearest_segment, outcome.miss_s) == ("t#0", 4970.0)

    def test_a_bracket_refusal_carries_the_bracket_and_the_detail(self):
        wide = index_of(segment("t#0", [(-10, 42.0, 77.0, 1.0), (500, 42.1, 77.0, 1.0)]))

        outcome = classify(clip(), wide, config())

        assert outcome.reason.value == "MAX_GAP_EXCEEDED"
        assert outcome.bracket_s == 510.0

    def test_a_planned_clip_equals_the_expected_outcome_field_for_field(self):
        index = index_of(segment("t#0", [(0, 42.0, 77.0, 1000.0), (60, 42.06, 77.0, 1060.0)]))

        outcome = classify(clip(), index, config())

        assert outcome.path == "/m/a.MP4"
        assert outcome.instant == INSTANT
        assert outcome.context == CONTEXT
        assert outcome.via is Via.INTERPOLATED
        assert outcome.bracket_speed == pytest.approx(111.2, abs=0.5)
        assert outcome.position.lat == pytest.approx(42.03)

    def test_a_refused_container_equals_the_expected_failure_field_for_field(self):
        index = index_of(segment("t#0", [(0, 42.0, 77.0, 1000.0), (60, 42.06, 77.0, 1060.0)]))

        outcome = classify(clip("/m/a.raf", "MP4"), index, config())

        assert outcome.path == "/m/a.raf"
        assert outcome.instant == INSTANT
        assert outcome.context == FileContext("FUJIFILM", "X-T5", "MP4")
        assert outcome.detail == (
            "extension .raf and container identity MP4 name different write-set classes"
        )
        assert outcome.bracket_speed == pytest.approx(111.2, abs=0.5)
        assert outcome.provenance.segment == "t#0"

    @pytest.mark.parametrize(
        ("path", "identity", "detail"),
        [
            (
                "/m/noext",
                "HEIC",
                "container identity HEIC (extension none): write safety is unverified",
            ),
            (
                "/m/a.heic",
                "HEIC",
                "container identity HEIC (extension .heic): write safety is unverified",
            ),
            (
                "/m/a.heic",
                None,
                "container identity unknown (extension .heic): write safety is unverified",
            ),
            (
                "/m/noext",
                "RAF",
                "extension none and container identity RAF name different write-set classes",
            ),
            (
                "/m/a.dng",
                "RAF",
                "extension .dng disagrees with container identity RAF: write safety is unverified",
            ),
        ],
    )
    def test_the_refusal_details_are_exact(self, path, identity, detail):
        outcome = classify(
            tag_record(path, file_type=identity, date_time_original="2026:10:26 10:00:30"),
            EMPTY,
            config(),
        )

        assert outcome.detail == detail

    def test_a_telemetry_refusal_carries_the_context_and_instant(self):
        outcome = classify(clip(extra=META), EMPTY, config(), telemetry(atoms=["x"]))

        assert outcome.context == CONTEXT
        assert outcome.instant == INSTANT

    def test_a_telemetry_refusal_failed_outcome_equals_the_expected_field_for_field(self):
        outcome = classify(
            clip(extra=META), EMPTY, config(), telemetry(lat=[0.0], lon=[0.0], times=[0.0])
        )

        assert outcome == Failed(
            "/m/a.MP4",
            FailedReason.TELEMETRY_UNUSABLE,
            "none of 1 samples is a usable fix (UNPARSEABLE 0, OUT_OF_RANGE 0, ZERO_SENTINEL 1)",
            instant=INSTANT,
            context=CONTEXT,
        )

    def test_a_zero_atom_that_appeared_carries_context_and_instant(self):
        outcome = classify(clip(extra=META), EMPTY, config(), telemetry(atoms=["0 0"]))

        assert outcome == Protected(
            "/m/a.MP4",
            ProtectedReason.ZERO_POSITION,
            position=Position(0.0, 0.0, None),
            instant=INSTANT,
            context=CONTEXT,
        )

    def test_a_half_zero_atom_that_appeared_is_not_a_zero_position(self):
        outcome = classify(clip(extra=META), EMPTY, config(), telemetry(atoms=["0 5"]))

        assert outcome.reason is ProtectedReason.POSITION_PRESENT

    def test_a_centroid_plan_carries_the_context_and_instant(self):
        outcome = classify(
            clip(extra={**META, ":Duration": 0.4004}), EMPTY, config(), telemetry(**GOOD)
        )

        assert outcome.instant == INSTANT
        assert outcome.context == CONTEXT
        assert outcome.path == "/m/a.MP4"


class TestRemainingOutcomeDetails:
    def test_an_unparseable_composite_position_is_quoted_in_the_detail(self):
        outcome = classify(tag_record(gps="garbage"), EMPTY, config())

        assert outcome.detail == "Composite:GPSPosition is present but unparseable: 'garbage'"

    def test_a_telemetry_refusal_names_its_path_and_detail(self):
        outcome = classify(
            clip(extra=META), EMPTY, config(), telemetry(lat=[42.0, 42.1], lon=[77.0], times=[0, 1])
        )

        assert outcome.path == "/m/a.MP4"
        assert outcome.detail == "2 GPSLatitude samples but 1 GPSLongitude samples"

    def test_an_antimeridian_bracket_refusal_carries_the_matcher_detail(self):
        wide = index_of(
            segment("t#0", [(0, 42.0, 179.0, 1.0), (60, 42.0, -179.0, 1.0)]),
        )

        outcome = classify(clip(), wide, config())

        assert outcome.reason.value == "BRACKET_UNSOUND"
        assert outcome.detail


class TestAnAtomThatAppearedBetweenTheReads:
    def test_an_unreadable_one_equals_the_expected_failure_field_for_field(self):
        outcome = classify(clip(extra=META), EMPTY, config(), telemetry(atoms=["x"]))

        assert outcome == Failed(
            "/m/a.MP4",
            FailedReason.METADATA_INDETERMINATE,
            "a container position atom is present but unparseable: 'x'",
            instant=INSTANT,
            context=CONTEXT,
        )
