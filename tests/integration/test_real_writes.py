"""Container write safety, attributes and backup modes against the real binary."""

import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from gps_sync.model import FileContext, Geotagged, Interpolated, Planned, Position, Via
from gps_sync.transport import ExiftoolTransport
from gps_sync.writer import run_write
from tests.conftest import World, track_text
from tests.integration.conftest import spawn_counts
from tests.integration.helpers import (
    common_suffix,
    preview_digest,
    stat_identity,
    tag_names,
    tags,
    walk_files,
)
from tests.support import EXIFTOOL, at, clone, exiftool, gpx_text

GPS_TAGS = {
    "GPS:GPSLatitude",
    "GPS:GPSLatitudeRef",
    "GPS:GPSLongitude",
    "GPS:GPSLongitudeRef",
    "GPS:GPSAltitude",
    "GPS:GPSAltitudeRef",
    "GPS:GPSDateStamp",
    "GPS:GPSTimeStamp",
}


# exiftool adds GPSVersionID by itself when it creates a GPS block; it is not in our write set.
IMPLICIT_GPS_TAGS = {"GPS:GPSVersionID"}


def target(path: Path, container: str, lat=42.5, lon=78.25, ele: float | None = 1500.5) -> Planned:
    return Planned(
        str(path),
        at(300),
        Position(lat, lon, ele),
        Via.INTERPOLATED,
        Interpolated("t.gpx#trk0seg0", 10.0),
        0.5,
        FileContext("X", "Y", container),
    )


@pytest.fixture
def real_world(tmp_path) -> World:
    w = World(tmp_path)
    w.media.mkdir()
    w.tracks.mkdir()
    (w.tracks / "walk.gpx").write_text(track_text())
    w.home.mkdir()
    return w


class TestRafWrite:
    def test_gps_tags_land_and_the_payload_and_tag_set_survive(
        self, tmp_path, assets, real_transport
    ):
        original = clone(assets.raf_original, tmp_path / "before.RAF")
        copy = clone(assets.raf_original, tmp_path / "a.RAF")
        before_names = tag_names(original)

        result = run_write(real_transport, [target(copy, "RAF")], tmp_path, keep_original=False)

        assert isinstance(result.outcomes[0], Geotagged)
        values = tags(copy, "-GPSLatitude", "-GPSLongitude", "-GPSAltitude", "-GPSAltitudeRef")
        assert values["GPSLatitude"] == pytest.approx(42.5)
        assert values["GPSLongitude"] == pytest.approx(78.25)
        assert values["GPSAltitude"] == pytest.approx(1500.5)
        assert values["GPSAltitudeRef"] == 0
        after_names = tag_names(copy)
        assert before_names <= after_names, "no tag may be lost"
        assert {
            n for n in after_names - before_names if n.startswith("GPS:")
        } <= GPS_TAGS | IMPLICIT_GPS_TAGS
        # The size reduction is confined to the header and preview region; the RAW payload that
        # makes up the bulk of the file is byte-identical at the end.
        assert copy.stat().st_size < original.stat().st_size
        assert common_suffix(original, copy) > 0.7 * copy.stat().st_size
        assert preview_digest(original) == preview_digest(copy)

    def test_no_xmp_gps_properties_are_written_and_only_exif_gps_tags_are_added(
        self, tmp_path, assets, real_transport
    ):
        original = clone(assets.raf, tmp_path / "before.RAF")
        copy = clone(assets.raf, tmp_path / "a.RAF")

        run_write(real_transport, [target(copy, "RAF")], tmp_path, keep_original=False)

        added = tag_names(copy) - tag_names(original)
        assert not [n for n in tag_names(copy) if n.startswith("XMP") and "GPS" in n]
        assert {n for n in added if n.startswith("GPS:")} == GPS_TAGS | IMPLICIT_GPS_TAGS
        assert {n for n in added if not n.startswith(("GPS:", "Composite:"))} == set()

    def test_a_track_without_elevation_writes_six_tags_and_no_altitude(
        self, tmp_path, assets, real_transport
    ):
        copy = clone(assets.raf, tmp_path / "a.RAF")

        result = run_write(
            real_transport, [target(copy, "RAF", ele=None)], tmp_path, keep_original=False
        )

        assert isinstance(result.outcomes[0], Geotagged)
        present = {n for n in tag_names(copy) if n.startswith("GPS:")}
        assert present == (GPS_TAGS | IMPLICIT_GPS_TAGS) - {"GPS:GPSAltitude", "GPS:GPSAltitudeRef"}


class TestInstanceSelectionDecidesTheWrittenCoordinate:
    # The synthetic track runs north from 42.0 at 04:00:00Z at 0.00001 degrees per second, so
    # 04:05:00Z (the EXIF time, 10:05:00 at +06:00) is 300 s in: 42.0 + 300 * 0.00001 = 42.003.
    # The competing XMP time, 10:09:00, is 540 s in and would write 42.0054.
    EXIF_LATITUDE = 42.003
    XMP_LATITUDE = 42.0054

    def test_the_written_latitude_is_the_exif_instants_and_not_the_xmp_ones(self, tmp_path, assets):
        world = World(tmp_path)
        world.media.mkdir()
        world.tracks.mkdir()
        (world.tracks / "walk.gpx").write_text(track_text())
        world.home.mkdir()
        copy = world.put("drone/competing.DNG", assets.dng_xmp_competing_time)

        result = world.run(
            world.options(
                json=True, write=True, utc_offset=timedelta(hours=6), review_dir=tmp_path / "rv"
            ),
            ExiftoolTransport(EXIFTOOL),
        )

        assert result.code == 0
        assert json.loads(result.out)["files"][0]["variant"] == "Geotagged"
        written = tags(copy, "-GPSLatitude")["GPSLatitude"]
        assert written == pytest.approx(self.EXIF_LATITUDE, abs=1e-6)
        assert written != pytest.approx(self.XMP_LATITUDE, abs=1e-4)


class TestDngWrite:
    def test_header_region_changes_but_trailing_bytes_tags_and_preview_survive(
        self, tmp_path, assets, real_transport
    ):
        original = clone(assets.dng_stripped, tmp_path / "before.DNG")
        copy = clone(assets.dng_stripped, tmp_path / "a.DNG")
        before_names = tag_names(original)

        result = run_write(real_transport, [target(copy, "DNG")], tmp_path, keep_original=False)

        assert isinstance(result.outcomes[0], Geotagged)
        assert tags(copy, "-GPSLatitude")["GPSLatitude"] == pytest.approx(42.5)
        assert before_names <= tag_names(copy)
        assert common_suffix(original, copy) > 0.9 * copy.stat().st_size
        assert abs(copy.stat().st_size - original.stat().st_size) < 100_000
        assert preview_digest(original) == preview_digest(copy)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="xattr(1) and st_birthtime are the macOS measurements"
)
class TestFilesystemAttributes:
    """Documents what each backup mode preserves; the README discloses these measurements."""

    @pytest.fixture(params=["RAF", "DNG"])
    def source(self, request, assets):
        return assets.raf_original if request.param == "RAF" else assets.dng_stripped

    def prepare(self, source: Path, tmp_path: Path) -> Path:
        copy = clone(source, tmp_path / f"a{source.suffix}")
        exiftool("-q", "-q", "-overwrite_original", "-Comment=x", str(copy))  # normalise first
        os.chmod(copy, 0o640)
        subprocess_xattr(copy, "user.gpssync.test", "hello")
        subprocess_xattr(copy, "com.apple.metadata:_kMDItemUserTags", "red")
        return copy

    def test_default_mode_preserves_mode_and_ownership_but_not_xattrs_or_the_inode(
        self, source, tmp_path, real_transport
    ):
        copy = self.prepare(source, tmp_path)
        before = (stat_identity(copy), os.stat(copy).st_ino, os.stat(copy).st_birthtime)

        run_write(
            real_transport, [target(copy, source.suffix[1:].upper())], tmp_path, keep_original=False
        )

        assert stat_identity(copy) == before[0]
        assert os.stat(copy).st_ino != before[1]
        assert os.stat(copy).st_birthtime > before[2]
        assert "user.gpssync.test" not in xattr_names(copy)
        assert "com.apple.metadata:_kMDItemUserTags" not in xattr_names(copy)
        assert not Path(f"{copy}_original").exists()

    def test_keep_original_leaves_the_prior_bytes_and_attributes_on_the_retained_copy(
        self, source, tmp_path, real_transport
    ):
        copy = self.prepare(source, tmp_path)
        pre_bytes = copy.read_bytes()
        pre_inode = os.stat(copy).st_ino
        pre_identity = stat_identity(copy)
        pre_birthtime = os.stat(copy).st_birthtime

        run_write(
            real_transport, [target(copy, source.suffix[1:].upper())], tmp_path, keep_original=True
        )

        retained = Path(f"{copy}_original")
        assert retained.read_bytes() == pre_bytes
        assert os.stat(retained).st_ino == pre_inode
        assert os.stat(retained).st_birthtime == pre_birthtime
        assert stat_identity(retained) == pre_identity
        assert xattr_value(retained, "user.gpssync.test") == "hello"
        assert "com.apple.metadata:_kMDItemUserTags" in xattr_names(retained)
        assert stat_identity(copy) == pre_identity


def subprocess_xattr(path: Path, name: str, value: str) -> None:
    subprocess.run(["xattr", "-w", name, value, str(path)], check=True)


def xattr_names(path: Path) -> set[str]:
    out = subprocess.run(["xattr", str(path)], capture_output=True, text=True, check=True).stdout
    return set(out.split())


def xattr_value(path: Path, name: str) -> str:
    return subprocess.run(
        ["xattr", "-p", name, str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()


class TestKeepOriginal:
    def test_with_the_flag_a_byte_identical_sibling_exists_and_a_second_pass_never_admits_it(
        self, real_world, assets
    ):
        copy = real_world.put("a.RAF", assets.raf)
        pre = copy.read_bytes()

        first = real_world.run(
            real_world.options(write=True, keep_original=True, json=True),
            ExiftoolTransport(EXIFTOOL),
        )
        sibling = Path(f"{copy}_original")
        assert first.code == 0 and sibling.read_bytes() == pre
        sibling_state = sibling.read_bytes()

        second = real_world.run(
            real_world.options(write=True, keep_original=True, json=True),
            ExiftoolTransport(EXIFTOOL),
        )

        files = {os.path.basename(f["path"]): f for f in json.loads(second.out)["files"]}
        assert files["a.RAF_original"]["variant"] == "Ignored"
        assert files["a.RAF_original"]["reason"] == "NOT_A_STILL"
        assert files["a.RAF"]["reason"] == "POSITION_PRESENT"
        assert sibling.read_bytes() == sibling_state == pre

    def test_without_the_flag_no_original_is_produced_anywhere_in_the_tree(
        self, real_world, assets
    ):
        real_world.put("a.RAF", assets.raf)
        real_world.put("sub/b.RAF", assets.raf)

        result = real_world.run(real_world.options(write=True), ExiftoolTransport(EXIFTOOL))

        assert result.code == 0
        assert list(real_world.media.rglob("*_original")) == []


class TestContainerIdentityWriteGate:
    def make_tree(self, real_world, assets):
        real_world.put("positionless.JPG", assets.jpeg)
        real_world.put("raf_named_jpg.JPG", assets.raf)
        shutil.copyfile(assets.jpeg, real_world.media / "jpeg_named_raf.RAF")
        return real_world

    @pytest.mark.parametrize("write", [False, True])
    def test_unsupported_and_mismatched_containers_are_refused_in_both_modes_and_never_touched(
        self, real_world, assets, write
    ):
        self.make_tree(real_world, assets)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in walk_files(real_world.media)}

        result = real_world.run(
            real_world.options(write=write, json=True), ExiftoolTransport(EXIFTOOL)
        )

        files = {os.path.basename(f["path"]): f for f in json.loads(result.out)["files"]}
        assert result.code == 1
        for name in ("positionless.JPG", "raf_named_jpg.JPG", "jpeg_named_raf.RAF"):
            assert files[name]["reason"] == "WRITE_UNSUPPORTED_CONTAINER", name
        assert "JPEG" in files["positionless.JPG"]["detail"]
        assert (
            ".jpg" in files["raf_named_jpg.JPG"]["detail"]
            and "RAF" in files["raf_named_jpg.JPG"]["detail"]
        )
        assert (
            ".raf" in files["jpeg_named_raf.RAF"]["detail"]
            and "JPEG" in files["jpeg_named_raf.RAF"]["detail"]
        )
        assert {
            p: (p.read_bytes(), p.stat().st_mtime_ns) for p in walk_files(real_world.media)
        } == before

    def test_a_positioned_jpeg_is_protected_and_the_run_exits_zero(self, real_world, assets):
        real_world.put("positioned.JPG", assets.jpeg_gps)

        result = real_world.run(real_world.options(json=True), ExiftoolTransport(EXIFTOOL))

        assert result.code == 0
        assert json.loads(result.out)["files"][0]["reason"] == "POSITION_PRESENT"


class TestProcessCounts:
    def test_one_read_and_one_write_process_serve_ten_files_with_their_own_coordinates(
        self, real_world, assets, counting_exiftool
    ):
        from tests.integration.conftest import set_local_times

        script, log = counting_exiftool
        paths = {}
        for i in range(10):
            paths[real_world.put(f"f{i}.RAF", assets.raf)] = f"2026:10:26 10:0{i}:30"
        set_local_times(paths)

        result = real_world.run(
            real_world.options(write=True, json=True), ExiftoolTransport(str(script))
        )

        assert result.code == 0
        counts = spawn_counts(log)
        assert (counts["read"], counts["write"]) == (1, 1)
        latitudes = []
        for path in paths:
            latitudes.append(tags(path, "-GPSLatitude")["GPSLatitude"])
        assert len(set(latitudes)) == 10
        assert latitudes == sorted(latitudes)
        for i, path in enumerate(paths):
            expected = 42.0 + 0.00001 * (i * 60 + 30 - 0) + 0.00001 * 0
            # instant 04:0i:30Z is i*60+30 s after the track start.
            assert tags(path, "-GPSLatitude")["GPSLatitude"] == pytest.approx(
                42.0 + 0.00001 * (i * 60 + 30), abs=2e-6
            ), expected


def test_gpx_without_elevation_gives_a_six_tag_write_end_to_end(real_world, assets):
    (real_world.tracks / "walk.gpx").write_text(
        gpx_text(
            [
                ("2026-10-26T04:00:00Z", "42.0", "77.0", None),
                ("2026-10-26T04:10:00Z", "42.006", "77.0", None),
            ],
        ).replace("04:10:00Z", "04:01:00Z")
    )
    copy = real_world.put("a.RAF", assets.raf)
    # 04:05:00Z is outside this short track; extend recovers it as a clamp with no elevation.
    result = real_world.run(
        real_world.options(write=True, extend=timedelta(minutes=10)), ExiftoolTransport(EXIFTOOL)
    )

    assert result.code == 0
    assert not {n for n in tag_names(copy) if n.startswith("GPS:")} & {
        "GPS:GPSAltitude",
        "GPS:GPSAltitudeRef",
    }
    assert "GPS:GPSLatitude" in tag_names(copy)


class TestTheFilenameCharsetGovernsEveryBlock:
    @pytest.fixture
    def latin_filenames(self, tmp_path) -> Path:
        """The real exiftool, invoked as if the filename charset were not UTF-8.

        The shipped invocation asks for UTF-8; swapping the word for ``latin`` turns the request
        into one the non-ASCII paths below cannot satisfy, which makes whether it governs a given
        block observable on a platform whose default already happens to be UTF-8.
        """
        script = tmp_path / "exiftool-latin"
        script.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            f"argv = [a.replace('filename=utf8', 'filename=latin') for a in sys.argv[1:]]\n"
            f"os.execv('{EXIFTOOL}', ['{EXIFTOOL}', *argv])\n"
        )
        script.chmod(0o755)
        return script

    def test_a_non_ascii_path_in_a_later_block_is_governed_by_the_same_request(
        self, tmp_path, assets, latin_filenames
    ):
        first = clone(assets.raf_original, tmp_path / "é1.RAF")
        second = clone(assets.raf_original, tmp_path / "é2.RAF")

        result = run_write(
            ExiftoolTransport(str(latin_filenames)),
            [target(first, "RAF"), target(second, "RAF")],
            tmp_path,
            keep_original=False,
        )

        assert not any(isinstance(o, Geotagged) for o in result.outcomes)
        assert "GPSLatitude" not in tags(first, "-GPSLatitude")
        assert "GPSLatitude" not in tags(second, "-GPSLatitude")

    def test_the_shipped_request_writes_non_ascii_paths_in_every_block(
        self, tmp_path, assets, real_transport
    ):
        first = clone(assets.raf_original, tmp_path / "é1.RAF")
        second = clone(assets.raf_original, tmp_path / "é2.RAF")

        result = run_write(
            real_transport,
            [target(first, "RAF"), target(second, "RAF")],
            tmp_path,
            keep_original=False,
        )

        assert all(isinstance(o, Geotagged) for o in result.outcomes)
        assert tags(second, "-GPSLatitude")["GPSLatitude"] == pytest.approx(42.5)
