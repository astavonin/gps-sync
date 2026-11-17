"""Whole runs over a substituted transport: video candidates, telemetry, no tracks root, space."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gps_sync.model import RunLevelReason
from gps_sync.options import parse_options
from gps_sync.space import SLACK_BYTES, check_space
from tests.conftest import World
from tests.support import telemetry_json
from tests.unit.test_writer import planned

CLIP_RECORD = {
    ":FileType": "MP4",
    ":CreateDate": "2026:10:26 04:00:30",
    ":MajorBrand": "isom",
    ":Duration": 0.4004,
    ":HandlerType": "meta",
    "Copy1:HandlerType": "vide",
}
GOOD = {
    ":GPSLatitude": 42.0,
    "Copy1:GPSLatitude": 42.2,
    ":GPSLongitude": 77.0,
    "Copy1:GPSLongitude": 77.2,
    ":AbsoluteAltitude": 10.0,
    "Copy1:AbsoluteAltitude": 20.0,
    ":SampleTime": 0.0,
    "Copy1:SampleTime": 0.4,
}


def add_clip(world: World, name="a.MP4", record=None) -> str:
    return world.add(name, {**CLIP_RECORD, **(record or {})})


def run_json(world: World, transport, **options):
    result = world.run(world.options(json=True, **options), transport)
    return result.code, json.loads(result.out)


class TestAVideoRun:
    def test_a_bare_run_never_reads_a_clip(self, world):
        add_clip(world)
        transport = world.transport(telemetry=lambda p: GOOD)

        code, record = run_json(world, transport)

        assert code == 0
        assert transport.read_calls == [] or all(".MP4" not in p for p in transport.read_calls[0])
        assert transport.telemetry_calls == []
        assert record["summary"]["variant_counts"]["Ignored"] == 1

    def test_a_video_run_derives_a_clip_from_its_own_telemetry_in_one_pass(self, world):
        path = add_clip(world)
        transport = world.transport(telemetry=lambda p: GOOD)

        code, record = run_json(world, transport, video=True)

        [file] = record["files"]
        assert code == 0
        assert file["variant"] == "Planned"
        assert file["via"] == "CENTROID"
        assert file["provenance"]["kind"] == "Centroid"
        assert file["provenance"]["samples_used"] == 2
        assert file["position"]["lat"] == 42.1
        assert transport.telemetry_calls == [[path]]

    def test_only_the_paths_the_verdict_names_are_read_for_telemetry(self, world):
        add_clip(world, "a.MP4")
        add_clip(world, "b.MP4", {":HandlerType": "vide", "Copy1:HandlerType": "soun"})
        transport = world.transport(telemetry=lambda p: GOOD)

        run_json(world, transport, video=True)

        assert [[Path(p).name for p in call] for call in transport.telemetry_calls] == [["a.MP4"]]

    def test_a_clip_without_telemetry_is_matched_against_the_track(self, world):
        add_clip(world, "b.MP4", {":HandlerType": "vide"})

        code, record = run_json(world, world.transport(), video=True)

        [file] = record["files"]
        assert (code, file["via"]) == (0, "INTERPOLATED")

    def test_video_without_a_tracks_root_declares_an_empty_index_and_no_root(self, world):
        add_clip(world, "b.MP4", {":HandlerType": "vide"})
        add_clip(world, "a.MP4")

        code, record = run_json(
            world, world.transport(telemetry=lambda p: GOOD), video=True, tracks=None
        )

        assert code == 0
        assert record["tracks_root"] is None
        assert record["options"]["tracks"] is None
        assert record["options"]["video"] is True
        reasons = {
            f["path"].rsplit("/", 1)[1]: f["reason"] for f in record["files"] if "reason" in f
        }
        assert reasons == {"b.MP4": "OUTSIDE_COVERAGE"}
        assert record["run_level"] == {
            RunLevelReason.NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES.value: 1
        }

    def test_the_omitted_root_diagnostic_prints_before_the_per_file_sections(self, world):
        add_clip(world, "b.MP4", {":HandlerType": "vide"})

        result = world.run(world.options(video=True, tracks=None), world.transport())

        text = result.out
        assert "tracks:   (none given)" in text
        assert text.index("Run-level diagnostic: no tracks root") < text.index("Outcomes")

    def test_no_diagnostic_fires_when_nothing_reached_the_matcher(self, world):
        add_clip(world)

        _, record = run_json(
            world, world.transport(telemetry=lambda p: GOOD), video=True, tracks=None
        )

        assert record["run_level"] == {}

    def test_an_unrepresentable_elevation_fires_its_own_diagnostic(self, world):
        add_clip(world)
        huge = {**GOOD, ":AbsoluteAltitude": 1.7e308, "Copy1:AbsoluteAltitude": 1.7e308}

        _, record = run_json(world, world.transport(telemetry=lambda p: huge), video=True)

        assert record["run_level"][RunLevelReason.ELEVATION_NOT_REPRESENTABLE.value] == 1
        assert record["summary"]["altitude_unavailable"]["breakdown"]["NOT_REPRESENTABLE"] == 1

    def test_the_dispersion_figure_counts_the_centroid_outcomes(self, world):
        add_clip(world)

        _, record = run_json(world, world.transport(telemetry=lambda p: GOOD), video=True)

        dispersion = record["summary"]["centroid_dispersion"]
        assert dispersion["count"] == 1
        assert dispersion["maximum_m"] > 0

    def test_the_sensitivity_fold_excludes_a_centroid_under_its_own_key(self, world):
        add_clip(world)

        _, record = run_json(world, world.transport(telemetry=lambda p: GOOD), video=True)

        assert record["summary"]["sensitivity"]["excluded"] == {"centroid": 1}
        assert record["summary"]["coverage"]["by_via"] == {
            "INTERPOLATED": 0,
            "EXTENDED": 0,
            "CENTROID": 1,
        }

    def test_a_dry_run_reports_the_peak_space_demand_not_a_sum(self, world):
        add_clip(world, "a.MP4")
        add_clip(world, "b.MP4")

        result = world.run(world.options(video=True), world.transport(telemetry=lambda p: GOOD))

        assert "bytes at peak (the largest planned target plus slack)" in result.out


class TestTheSpaceForecastReachesEverySurface:
    def test_the_json_record_carries_the_forecast_the_report_prints(self, world):
        add_clip(world, "a.MP4")
        add_clip(world, "b.MP4")

        result = world.run(
            world.options(json=True, video=True), world.transport(telemetry=lambda p: GOOD)
        )

        [figure] = json.loads(result.out)["space_demand"]
        assert figure["peak_bytes"] == 5 + SLACK_BYTES
        assert figure["retained_bytes"] == 10
        assert isinstance(figure["volume"], int)

    def test_a_stills_only_run_reports_a_telemetry_read_that_does_not_depend_on_the_clock(
        self, world
    ):
        ticks = iter(
            datetime(2026, 11, 1, tzinfo=UTC) + timedelta(seconds=7 * i) for i in range(99)
        )

        record = json.loads(world.run(world.options(json=True), clock=lambda: next(ticks)).out)

        assert record["telemetry_read"] == {"clips": 0, "chunks": 0, "elapsed_s": 0.0}

    def test_a_telemetry_read_reports_the_elapsed_time_between_its_own_clock_readings(self, world):
        add_clip(world)
        ticks = iter(datetime(2026, 11, 1, tzinfo=UTC) + timedelta(seconds=i) for i in range(99))

        record = json.loads(
            world.run(
                world.options(json=True, video=True),
                world.transport(telemetry=lambda p: GOOD),
                clock=lambda: next(ticks),
            ).out
        )

        assert record["telemetry_read"]["clips"] == 1
        assert record["telemetry_read"]["elapsed_s"] > 0.0

    def test_a_run_with_nothing_planned_carries_an_empty_forecast(self, world):
        record = json.loads(world.run(world.options(json=True)).out)

        assert record["space_demand"] == []

    def test_a_write_runs_json_record_carries_the_same_forecast_as_the_dry_run(self, world):
        from tests.unit.test_run_contract import block_batch

        a = add_clip(world, "a.MP4")
        add_clip(world, "b.MP4")
        dry = json.loads(
            world.run(
                world.options(json=True, video=True), world.transport(telemetry=lambda p: GOOD)
            ).out
        )
        transport = world.transport(
            telemetry=lambda p: GOOD, on_write=lambda _f: block_batch([a], ["applied"])
        )

        written = json.loads(
            world.run(world.options(json=True, write=True, video=True), transport).out
        )

        assert dry["space_demand"] != []
        assert written["space_demand"] == dry["space_demand"]

    def test_a_write_runs_human_report_does_not_print_the_forecast(self, world):
        from tests.unit.test_run_contract import block_batch

        a = add_clip(world, "a.MP4")
        transport = world.transport(
            telemetry=lambda p: GOOD, on_write=lambda _f: block_batch([a], ["applied"])
        )

        result = world.run(world.options(write=True, video=True), transport)

        assert "Free space a write needs" not in result.out

    def test_the_audit_verdict_carries_the_forecast_of_a_write_run(self, world):
        from tests.unit.test_run_contract import block_batch, records_in

        a = add_clip(world, "a.MP4")
        transport = world.transport(
            telemetry=lambda p: GOOD, on_write=lambda _f: block_batch([a], ["applied"])
        )

        world.run(world.options(write=True, video=True), transport)

        [record] = records_in(world)
        [figure] = record["verdict"]["space_demand"]
        assert (figure["peak_bytes"], figure["retained_bytes"]) == (5 + SLACK_BYTES, 5)


class TestTheOptions:
    def test_tracks_is_required_without_video(self):
        import pytest

        from gps_sync.errors import ToolFailure

        with pytest.raises(ToolFailure, match="--tracks"):
            parse_options(["--media", "/m"])

    def test_tracks_is_optional_with_video(self):
        options = parse_options(["--media", "/m", "--video"])

        assert options.tracks is None and options.video

    def test_the_flags_default_to_the_001_behaviour(self):
        options = parse_options(["--media", "/m", "--tracks", "/t"])

        assert not options.video
        assert options.as_record()["video"] is False


class TestTheSpaceCheck:
    def targets(self, *names):
        return [planned(f"/v/{n}.MP4", container="MP4") for n in names]

    def check(self, targets, sizes, free, *, keep_original=False):
        return check_space(
            targets,
            keep_original=keep_original,
            size_of=lambda p: sizes[p],
            volume_of=lambda p: 1,
            free_of=lambda p: free,
        )

    def test_a_target_the_volume_can_hold_is_writable(self):
        targets = self.targets("a")

        writable, refused = self.check(targets, {targets[0].path: 100}, 100 + SLACK_BYTES)

        assert writable == targets and refused == {}

    def test_one_byte_short_of_size_plus_slack_is_refused_with_the_figures(self):
        targets = self.targets("a")

        writable, refused = self.check(targets, {targets[0].path: 100}, 100 + SLACK_BYTES - 1)

        [failure] = refused.values()
        assert writable == []
        assert failure.reason.value == "WRITE_INSUFFICIENT_SPACE"
        assert f"size 100 bytes, free {100 + SLACK_BYTES - 1} bytes on volume 1" in failure.detail
        assert failure.via is targets[0].via and failure.provenance is targets[0].provenance

    def test_an_oversized_target_does_not_deny_the_others(self):
        targets = self.targets("big", "small")
        sizes = {targets[0].path: 10 * SLACK_BYTES, targets[1].path: 10}

        writable, refused = self.check(targets, sizes, 2 * SLACK_BYTES)

        assert writable == [targets[1]]
        assert list(refused) == [targets[0].path]

    def test_keep_original_accumulates_retained_originals_in_intent_order(self):
        targets = self.targets("a", "b", "c")
        sizes = {t.path: 100 for t in targets}

        writable, refused = self.check(targets, sizes, SLACK_BYTES + 250, keep_original=True)

        assert [t.path for t in writable] == [targets[0].path, targets[1].path]
        assert list(refused) == [targets[2].path]
        assert (
            "retained originals already planned on it 200 bytes" in refused[targets[2].path].detail
        )

    def test_without_keep_original_demand_does_not_accumulate(self):
        targets = self.targets("a", "b", "c")
        sizes = {t.path: 100 for t in targets}

        writable, refused = self.check(targets, sizes, SLACK_BYTES + 150)

        assert writable == targets and refused == {}

    def test_the_peak_is_the_largest_target_plus_slack_and_the_sum_is_kept_apart(self):
        from gps_sync.space import demand

        targets = self.targets("a", "b")
        sizes = {targets[0].path: 100, targets[1].path: 300}

        [figure] = demand(targets, size_of=lambda p: sizes[p], volume_of=lambda p: 7)

        assert (figure.volume, figure.peak_bytes, figure.retained_bytes) == (
            7,
            300 + SLACK_BYTES,
            400,
        )


def test_the_telemetry_json_helper_round_trips_through_the_parser():
    from gps_sync.records import TelemetryRecord

    record = TelemetryRecord.from_json(telemetry_json("/m/a.MP4", lat=[1, 2], times=[0, 1]))

    assert record.latitudes == (1, 2) and record.sample_times == (0, 1)


def test_the_outside_coverage_count_cross_references_the_omitted_root_diagnostic(world):
    add_clip(world, "b.MP4", {":HandlerType": "vide"})

    text = world.run(world.options(video=True, tracks=None), world.transport()).out

    assert (
        "OUTSIDE_COVERAGE (instant outside the index's outer ends): 1 - see the run-level "
        "diagnostic above: no tracks root was given" in text
    )


def test_the_outside_coverage_count_is_unannotated_when_a_root_was_given(world):
    path = add_clip(world, "b.MP4", {":CreateDate": "2030:01:01 00:00:00", ":HandlerType": "vide"})

    text = world.run(world.options(video=True), world.transport()).out

    assert "OUTSIDE_COVERAGE (instant outside the index's outer ends): 1\n" in text
    assert path


class TestTheTelemetryReadIsInstrumentedOnEverySurface:
    def run_with(self, world, **options):
        times = iter(range(100, 200, 7))
        return world.run(
            world.options(**options),
            world.transport(telemetry=lambda p: GOOD),
            clock=lambda: (
                datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=next(times))
            ),
        )

    def test_the_json_record_carries_clips_chunks_and_elapsed_seconds(self, world):
        add_clip(world, "a.MP4")
        add_clip(world, "b.MP4")

        result = self.run_with(world, json=True, video=True)

        figures = json.loads(result.out)["telemetry_read"]
        assert (figures["clips"], figures["chunks"]) == (2, 1)
        assert figures["elapsed_s"] == 7.0

    def test_the_report_names_the_figures_when_a_clip_was_read(self, world):
        add_clip(world, "a.MP4")

        text = self.run_with(world, video=True).out

        assert "Telemetry read: 1 clip(s) in 1 chunk(s), 7.0 s" in text

    def test_a_run_that_read_no_telemetry_prints_no_line_and_a_zero_record(self, world):
        add_clip(world, "a.MP4")

        bare = self.run_with(world, json=True)
        text = world.run(world.options()).out

        assert json.loads(bare.out)["telemetry_read"]["clips"] == 0
        assert "Telemetry read:" not in text

    def test_the_audit_verdict_carries_the_same_figures(self, world):
        from tests.unit.test_run_contract import block_batch, records_in

        a = add_clip(world, "a.MP4")
        transport = world.transport(
            telemetry=lambda p: GOOD, on_write=lambda _f: block_batch([a], ["applied"])
        )

        world.run(world.options(write=True, video=True), transport)

        [record] = records_in(world)
        assert record["verdict"]["telemetry_read"] == {"clips": 1, "chunks": 1, "elapsed_s": 0.0}
