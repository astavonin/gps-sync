"""A whole video run against the real exiftool: telemetry centroid, write, idempotence."""

import json
import statistics

import pytest

from tests.conftest import World, track_text
from tests.integration.helpers import tags
from tests.support import exiftool


@pytest.fixture
def real_world(tmp_path) -> World:
    world = World(tmp_path)
    world.media.mkdir()
    world.tracks.mkdir()
    (world.tracks / "walk.gpx").write_text(track_text())
    world.home.mkdir()
    return world


def own_samples(path) -> tuple[float, float]:
    """The clip's mean fix, read independently of the tool through a plain ``-ee`` read."""
    raw = json.loads(
        exiftool("-ee", "-j", "-n", "-a", "-G4", "-GPSLatitude", "-GPSLongitude", str(path)).stdout
    )[0]
    lats = [v for k, v in raw.items() if k.endswith("GPSLatitude")]
    lons = [v for k, v in raw.items() if k.endswith("GPSLongitude")]
    return statistics.fmean(lats), statistics.fmean(lons)


class TestARealVideoRun:
    def test_a_bare_run_ignores_the_clip_and_touches_nothing(
        self, real_world, assets, real_transport
    ):
        clip = real_world.put("a.MP4", assets.mp4)
        before = clip.read_bytes()

        result = real_world.run(real_world.options(json=True, write=True), real_transport)

        record = json.loads(result.out)
        assert result.code == 0
        assert record["files"][0]["reason"] == "NOT_A_STILL"
        assert clip.read_bytes() == before

    def test_a_dry_run_plans_the_centroid_of_the_clips_own_samples_and_writes_nothing(
        self, real_world, assets, real_transport
    ):
        clip = real_world.put("a.MP4", assets.mp4)
        before = clip.read_bytes()

        result = real_world.run(
            real_world.options(json=True, video=True, tracks=None), real_transport
        )

        [planned] = json.loads(result.out)["files"]
        mean_lat, mean_lon = own_samples(clip)
        assert result.code == 0
        assert planned["variant"] == "Planned" and planned["via"] == "CENTROID"
        assert planned["position"]["lat"] == pytest.approx(mean_lat, abs=1e-9)
        assert planned["position"]["lon"] == pytest.approx(mean_lon, abs=1e-9)
        assert planned["provenance"]["samples_read"] == planned["provenance"]["samples_used"] == 24
        assert clip.read_bytes() == before

    def test_a_write_run_stores_the_centroid_and_the_next_run_leaves_the_clip_alone(
        self, real_world, assets, real_transport
    ):
        clip = real_world.put("a.MP4", assets.mp4)
        mean_lat, mean_lon = own_samples(clip)

        first = real_world.run(
            real_world.options(json=True, video=True, write=True, tracks=None), real_transport
        )
        stored = tags(clip, "-Keys:GPSCoordinates")["GPSCoordinates"].split()
        second = real_world.run(
            real_world.options(json=True, video=True, write=True, tracks=None), real_transport
        )

        assert first.code == 0
        assert json.loads(first.out)["files"][0]["variant"] == "Geotagged"
        assert float(stored[0]) == pytest.approx(mean_lat, abs=1e-8)
        assert float(stored[1]) == pytest.approx(mean_lon, abs=1e-8)
        [again] = json.loads(second.out)["files"]
        assert again["variant"] == "Protected" and again["reason"] == "POSITION_PRESENT"
        assert not list(real_world.media.glob("*_original"))

    def test_a_clip_whose_atom_this_backend_cannot_read_is_refused_and_left_byte_identical(
        self, real_world, assets, real_transport
    ):
        clip = real_world.put("a.MP4", assets.mp4_atom_unreadable)
        before = clip.read_bytes()

        result = real_world.run(
            real_world.options(json=True, video=True, write=True, tracks=None), real_transport
        )

        [record] = json.loads(result.out)["files"]
        assert result.code == 1
        assert record["reason"] == "METADATA_INDETERMINATE"
        assert clip.read_bytes() == before

    def test_a_hidden_clip_is_skipped_under_its_own_reason_even_with_video_on(
        self, real_world, assets, real_transport
    ):
        real_world.put(".hidden.MP4", assets.mp4)

        result = real_world.run(
            real_world.options(json=True, video=True, tracks=None), real_transport
        )

        [record] = json.loads(result.out)["files"]
        assert record["reason"] == "HIDDEN_ENTRY"

    def test_the_telemetry_read_is_the_only_invocation_carrying_ee(
        self, real_world, assets, counting_exiftool
    ):
        from gps_sync.transport import ExiftoolTransport

        script, log = counting_exiftool
        real_world.put("a.MP4", assets.mp4)

        real_world.run(
            real_world.options(video=True, write=True, tracks=None), ExiftoolTransport(str(script))
        )

        invocations = log.read_text().splitlines()
        with_ee = [line for line in invocations if " -ee " in f" {line} "]
        assert len(with_ee) == 1
        assert " -j " in f" {with_ee[0]} " and " -n " in f" {with_ee[0]} "
        assert all("geotag" not in line.lower() for line in invocations)
