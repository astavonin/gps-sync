"""The Fujifilm-shaped MOV against the real exiftool: time dispatch, GPX path, write, brands.

The clip is a byte-derived stand-in for a Fujifilm X-T5 original (see ``tests.assets``). Its
position source is the GPX track, so a mis-resolved instant writes a wrong position into a file
that carried none, which the never-overwrite invariant cannot catch.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gps_sync.reader import read_records
from gps_sync.timeres import UTC_ATOM_BRANDS, TimeFailure, resolve_instant
from tests.assets import _BOX_HEADER, _children, _only
from tests.conftest import World, track_text
from tests.integration.helpers import tags
from tests.support import exiftool

CAPTURE = datetime(2026, 10, 26, 4, 5, 0, tzinfo=UTC)
FINALISATION = timedelta(seconds=25)
LOCAL_OFFSET = timedelta(hours=6)
TRACK_START = datetime(2026, 10, 26, 4, 0, 0, tzinfo=UTC)


def track_position(instant: datetime) -> tuple[float, float]:
    """The synthetic track's position at ``instant``, from its definition in ``track_text``."""
    return 42.0 + 0.00001 * (instant - TRACK_START).total_seconds(), 77.0


@pytest.fixture
def real_world(tmp_path) -> World:
    world = World(tmp_path)
    world.media.mkdir()
    world.tracks.mkdir()
    (world.tracks / "walk.gpx").write_text(track_text())
    world.home.mkdir()
    return world


def record_of(transport, path: Path, scratch: Path):
    return read_records(transport, [str(path)], scratch)[str(path)]


def mdat_digest(path: Path) -> str:
    data = path.read_bytes()
    _, at, size = _only(_children(data, 0, len(data)), b"mdat")
    return hashlib.sha256(data[at + _BOX_HEADER : at + size]).hexdigest()


def duration_s(path: Path) -> float:
    return float(tags(path, "-Duration")["Duration"])


class TestTheFujifilmDispatchAgainstTheRealBinary:
    def test_the_read_carries_the_local_exif_pair_unprefixed_and_the_later_atom_as_copy1(
        self, assets, real_transport
    ):
        raw = json.loads(
            exiftool(
                "-j",
                "-n",
                "-a",
                "-G4",
                "-CreateDate",
                "-DateTimeOriginal",
                str(assets.mov_fujifilm),
            ).stdout
        )[0]

        assert raw[":CreateDate"] == raw[":DateTimeOriginal"] == "2026:10:26 10:05:00"
        assert raw["Copy1:CreateDate"] == "2026:10:26 04:05:25"

    def test_the_record_is_local_exif_evidence_beside_a_second_createdate_instance(
        self, assets, real_transport, tmp_path
    ):
        record = record_of(real_transport, assets.mov_fujifilm, tmp_path)

        assert record.file_type == "MOV" and record.major_brand == "qt"
        assert record.date_time_original == "2026:10:26 10:05:00"
        assert record.offset_time_original == "+06:00"
        assert record.create_date == "2026:10:26 10:05:00"
        assert record.create_date_count == 2

    def test_the_resolved_instant_is_the_local_time_minus_its_offset_and_not_the_atom(
        self, assets, real_transport, tmp_path
    ):
        record = record_of(real_transport, assets.mov_fujifilm, tmp_path)

        resolved = resolve_instant(record, None, timedelta(0))

        assert resolved == CAPTURE
        assert resolved != CAPTURE + FINALISATION
        local_stamped_as_utc = datetime(2026, 10, 26, 10, 5, 0, tzinfo=UTC)
        assert local_stamped_as_utc - resolved == LOCAL_OFFSET

    def test_the_atom_trails_the_start_by_the_duration_plus_the_measured_finalisation_band(
        self, assets
    ):
        atom = datetime.strptime(
            tags(assets.mov_fujifilm, "-QuickTime:CreateDate")["CreateDate"], "%Y:%m:%d %H:%M:%S"
        ).replace(tzinfo=UTC)

        finalisation = (atom - CAPTURE).total_seconds() - duration_s(assets.mov_fujifilm)

        assert 23.95 <= finalisation <= 25.00

    def test_a_supplied_utc_offset_never_overrides_the_embedded_offset(
        self, assets, real_transport, tmp_path
    ):
        record = record_of(real_transport, assets.mov_fujifilm, tmp_path)

        with_option = resolve_instant(record, timedelta(hours=3), timedelta(0))

        assert with_option == CAPTURE

    def test_a_dry_run_places_the_clip_at_the_trackpoint_for_the_capture_instant(
        self, real_world, assets, real_transport
    ):
        clip = real_world.put("a.MOV", assets.mov_fujifilm)
        before = clip.read_bytes()

        result = real_world.run(
            real_world.options(json=True, video=True, tracks=real_world.tracks), real_transport
        )

        [planned] = json.loads(result.out)["files"]
        want_lat, want_lon = track_position(CAPTURE)
        atom_lat, _ = track_position(CAPTURE + FINALISATION)
        assert result.code == 0
        assert planned["variant"] == "Planned" and planned["via"] == "INTERPOLATED"
        assert planned["instant"] == CAPTURE.isoformat()
        assert planned["position"]["lat"] == pytest.approx(want_lat, abs=1e-9)
        assert planned["position"]["lon"] == pytest.approx(want_lon, abs=1e-9)
        assert abs(planned["position"]["lat"] - atom_lat) > 1e-4
        assert clip.read_bytes() == before

    def test_a_write_run_stores_that_position_and_leaves_the_media_payload_and_validity(
        self, real_world, assets, real_transport
    ):
        clip = real_world.put("a.MOV", assets.mov_fujifilm)
        payload = mdat_digest(clip)

        written = real_world.run(
            real_world.options(json=True, video=True, write=True, tracks=real_world.tracks),
            real_transport,
        )
        again = real_world.run(
            real_world.options(json=True, video=True, write=True, tracks=real_world.tracks),
            real_transport,
        )

        stored = tags(clip, "-Keys:GPSCoordinates")["GPSCoordinates"].split()
        want_lat, want_lon = track_position(CAPTURE)
        assert json.loads(written.out)["files"][0]["variant"] == "Geotagged"
        assert float(stored[0]) == pytest.approx(want_lat, abs=1e-6)
        assert float(stored[1]) == pytest.approx(want_lon, abs=1e-6)
        assert mdat_digest(clip) == payload
        assert tags(clip, "-DateTimeOriginal")["DateTimeOriginal"] == "2026:10:26 10:05:00"
        assert set(exiftool("-validate", "-Validate", "-s3", str(clip)).stdout.split()) == {"OK"}
        assert json.loads(again.out)["files"][0]["reason"] == "POSITION_PRESENT"
        assert not list(real_world.media.glob("*_original"))


class TestTheBrandBasedAdmitAndRefuseSplit:
    def test_the_fujifilm_clip_is_admitted_on_exif_evidence_although_its_brand_is_not_in_the_set(
        self, assets, real_transport, tmp_path
    ):
        record = record_of(real_transport, assets.mov_fujifilm, tmp_path)

        assert record.major_brand == "qt" and record.major_brand not in UTC_ATOM_BRANDS
        assert not isinstance(resolve_instant(record, None, timedelta(0)), TimeFailure)

    def test_a_generic_iso_brand_clip_with_a_lone_atom_is_admitted_on_the_brand_alone(
        self, assets, real_transport, tmp_path
    ):
        record = record_of(real_transport, assets.mp4_isom_without_position_or_telemetry, tmp_path)

        resolved = resolve_instant(record, None, timedelta(0))

        assert record.major_brand in UTC_ATOM_BRANDS
        assert record.date_time_original is None and record.creation_date is None
        assert record.create_date_count == 1
        assert resolved == CAPTURE + FINALISATION

    def test_a_quicktime_brand_clip_with_only_an_atom_is_refused_even_under_a_utc_offset(
        self, real_world, assets, real_transport
    ):
        clip = real_world.put("rewrap.MP4", assets.mp4_zoneless_rewrap)
        before = clip.read_bytes()

        result = real_world.run(
            real_world.options(
                json=True, video=True, utc_offset=timedelta(hours=6), tracks=real_world.tracks
            ),
            real_transport,
        )

        [refused] = json.loads(result.out)["files"]
        assert refused["variant"] == "Failed" and refused["reason"] == "TIME_SEMANTICS_UNVERIFIED"
        assert clip.read_bytes() == before

    def test_one_run_admits_the_exif_and_iso_clips_and_refuses_the_quicktime_atom_clip(
        self, real_world, assets, real_transport
    ):
        real_world.put("fuji.MOV", assets.mov_fujifilm)
        real_world.put("iso.MP4", assets.mp4_isom_without_position_or_telemetry)
        real_world.put("rewrap.MP4", assets.mp4_zoneless_rewrap)

        result = real_world.run(
            real_world.options(json=True, video=True, tracks=real_world.tracks), real_transport
        )

        by_name = {Path(f["path"]).name: f for f in json.loads(result.out)["files"]}
        assert by_name["fuji.MOV"]["variant"] == "Planned"
        assert by_name["fuji.MOV"]["instant"] == CAPTURE.isoformat()
        assert by_name["iso.MP4"]["variant"] == "Planned"
        assert by_name["iso.MP4"]["instant"] == (CAPTURE + FINALISATION).isoformat()
        assert by_name["rewrap.MP4"]["reason"] == "TIME_SEMANTICS_UNVERIFIED"
        assert result.code == 1
