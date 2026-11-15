"""Link hazards, awkward paths and discovery against ``exiftool -r`` on real containers."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from gps_sync.discovery import discover
from gps_sync.transport import ExiftoolTransport
from tests.conftest import World, track_text
from tests.integration.helpers import file_state, tags
from tests.support import EXIFTOOL


@pytest.fixture
def real_world(tmp_path) -> World:
    w = World(tmp_path)
    w.media.mkdir()
    w.tracks.mkdir()
    (w.tracks / "walk.gpx").write_text(track_text())
    w.home.mkdir()
    return w


def run_json(world: World, **options):
    result = world.run(world.options(json=True, **options), ExiftoolTransport(EXIFTOOL))
    return result, {os.path.basename(f["path"]): f for f in json.loads(result.out)["files"]}


def test_a_symlink_is_skipped_counted_never_written_through_and_stays_a_symlink(real_world, assets):
    real = real_world.put("real.RAF", assets.raf)
    link = real_world.media / "link.RAF"
    link.symlink_to(real)
    options = {"write": True, "verbose": True}

    result, files = run_json(real_world, **options)

    assert files["link.RAF"]["reason"] == "SYMLINK"
    assert files["link.RAF"]["detail"] == str(real.resolve())
    assert files["real.RAF"]["variant"] == "Geotagged"
    assert link.is_symlink() and os.readlink(link) == str(real)
    assert tags(link, "-GPSLatitude")["GPSLatitude"] == tags(real, "-GPSLatitude")["GPSLatitude"]
    trace = [line for line in result.err.splitlines() if "write argfile:" in line]
    argfile = Path(trace[0].split("write argfile: ")[1].split(" (")[0])
    assert str(link) not in argfile.read_text()
    assert str(real.resolve()) in argfile.read_text()


def test_two_names_for_one_inode_are_one_candidate_one_duplicate_and_one_written_file(
    real_world, assets
):
    first = real_world.put("a.RAF", assets.raf)
    os.link(first, real_world.media / "b.RAF")

    result, files = run_json(real_world, write=True)

    assert files["a.RAF"]["variant"] == "Geotagged"
    assert files["b.RAF"]["reason"] == "DUPLICATE_INODE"
    assert files["b.RAF"]["detail"] == str(first.resolve())
    assert json.loads(result.out)["summary"]["variant_counts"]["Geotagged"] == 1


def test_a_path_with_spaces_in_directory_and_basename_is_written_and_reads_back(real_world, assets):
    path = real_world.put("sp dir/sub dir/a b.RAF", assets.raf)

    _, files = run_json(real_world, write=True)

    assert files["a b.RAF"]["variant"] == "Geotagged"
    assert tags(path, "-GPSLatitude")["GPSLatitude"] == pytest.approx(
        files["a b.RAF"]["position"]["lat"], abs=1e-6
    )


def test_a_non_ascii_path_round_trips(real_world, assets):
    path = real_world.put("dé ü 日本/é.RAF", assets.raf)

    _, files = run_json(real_world, write=True)

    assert files["é.RAF"]["variant"] == "Geotagged"
    assert "GPSLatitude" in tags(path, "-GPSLatitude")


def test_a_dash_named_file_is_read_and_written_because_every_argfile_path_is_absolute(
    real_world, assets
):
    path = real_world.put("-fast2.RAF", assets.raf)
    real_world.put("neighbour.RAF", assets.raf_gps)

    result, files = run_json(real_world, write=True, verbose=True)

    assert files["-fast2.RAF"]["variant"] == "Geotagged"
    # The option-injection failure direction is "return nothing", which would also have made the
    # already-geotagged neighbour look absent; it must still be protected.
    assert files["neighbour.RAF"]["reason"] == "POSITION_PRESENT"
    assert "GPSLatitude" in tags(path, "-GPSLatitude")
    for line in result.err.splitlines():
        if "argfile:" in line:
            argfile = Path(line.split("argfile: ")[1].split(" (")[0])
            assert all(
                entry.startswith(("/", "-")) or entry == "not $Composite:GPSPosition"
                for entry in argfile.read_text().splitlines()
            )


def test_discovery_emits_14_candidates_where_exiftool_recursive_counts_16(real_world, assets):
    for i in range(14):
        real_world.put(f"day{i % 3}/DSCF{i:04d}.RAF", assets.raf_gps)
    review = real_world.media / "_unprocessed" / "already-tagged"
    review.mkdir(parents=True)
    (review / "one.RAF").symlink_to(real_world.media / "day0" / "DSCF0000.RAF")
    (review / "two.RAF").symlink_to(real_world.media / "day1" / "DSCF0001.RAF")

    found = discover(real_world.media.resolve(), frozenset())
    again = discover(real_world.media.resolve(), frozenset())
    listed = subprocess.run(
        [EXIFTOOL, "-r", "-ext", "RAF", "-p", "$FileName", str(real_world.media)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    assert len(found.candidates) == 14
    assert found.candidates == again.candidates
    assert len(listed) == 16
    assert sum(i.reason.value == "SYMLINK" for i in found.ignored) == 2


@pytest.mark.parametrize("inside", [False, True])
def test_the_review_folder_is_never_rediscovered_on_a_second_pass(real_world, assets, inside):
    for i in range(3):
        real_world.put(f"f{i}.RAF", assets.raf_gps)
    real_world.put("late.RAF", assets.raf)
    (real_world.tracks / "walk.gpx").write_text(
        track_text().replace("04:", "09:")
    )  # nothing covers the photos: they land in the review folder
    review_dir = real_world.media / "review" if inside else None

    first, first_files = run_json(real_world, review_dir=review_dir)
    second, second_files = run_json(real_world, review_dir=review_dir)

    assert first.code == second.code == 0
    assert (
        json.loads(first.out)["summary"]["variant_counts"]
        == json.loads(second.out)["summary"]["variant_counts"]
    )
    assert set(first_files) == set(second_files)
    review_root = Path(json.loads(second.out)["artifacts"]["review_root"])
    links = [p for p in (review_root / "_unprocessed").rglob("*") if p.is_symlink()]
    assert len(links) == 4
    assert all(
        f["variant"] != "Ignored" or f["reason"] == "NOT_A_STILL" for f in second_files.values()
    )


def test_a_dry_run_changes_no_byte_or_mtime_and_adds_nothing_inside_the_tree(real_world, assets):
    paths = [real_world.put(f"d{i}/f{i}.RAF", assets.raf) for i in range(4)]
    paths.append(real_world.put("gps.RAF", assets.raf_gps))
    before = {p: file_state(p) for p in paths}
    names = sorted(str(p) for p in real_world.media.rglob("*"))
    dir_mtimes = {d: d.stat().st_mtime_ns for d in real_world.media.rglob("*") if d.is_dir()}

    for as_json in (False, True):
        result = real_world.run(real_world.options(json=as_json), ExiftoolTransport(EXIFTOOL))
        assert result.code == 0

    assert {p: file_state(p) for p in paths} == before
    assert sorted(str(p) for p in real_world.media.rglob("*")) == names
    assert {
        d: d.stat().st_mtime_ns for d in real_world.media.rglob("*") if d.is_dir()
    } == dir_mtimes
    root = real_world.review_root()
    assert list((root / "runs").iterdir()) == []
    assert (root / "_unprocessed").exists()


def test_a_dry_run_leaves_no_argfile_behind_unless_verbose_retains_them(
    real_world, assets, tmp_path, monkeypatch
):
    temp_root = tmp_path / "tmp-root"
    temp_root.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp_root))
    real_world.put("a.RAF", assets.raf)

    real_world.run(real_world.options(), ExiftoolTransport(EXIFTOOL))
    assert list(temp_root.iterdir()) == []

    result = real_world.run(real_world.options(verbose=True), ExiftoolTransport(EXIFTOOL))
    [kept] = list(temp_root.iterdir())
    assert (kept / "read-000.args").read_text().strip() == str(
        (real_world.media / "a.RAF").resolve()
    )
    assert str(kept / "read-000.args") in result.err


class TestExiftoolPreflight:
    def test_the_version_is_parsed_from_the_real_binary(self):
        from gps_sync.transport import check_version

        assert check_version(ExiftoolTransport(EXIFTOOL).version()) >= (13, 55)

    def test_a_stub_reporting_an_older_version_is_rejected_before_any_file_is_touched(
        self, real_world, assets, tmp_path
    ):
        stub = tmp_path / "old-exiftool"
        stub.write_text("#!/bin/sh\necho 12.0\n")
        stub.chmod(0o755)
        path = real_world.put("a.RAF", assets.raf)
        before = file_state(path)

        result = real_world.run(real_world.options(write=True), ExiftoolTransport(str(stub)))

        assert result.code == 2 and "13.55" in result.err
        assert file_state(path) == before

    def test_an_absent_binary_is_rejected_before_any_file_is_touched(self, real_world, assets):
        path = real_world.put("a.RAF", assets.raf)
        before = file_state(path)

        result = real_world.run(
            real_world.options(write=True), ExiftoolTransport("/nonexistent/exiftool")
        )

        assert result.code == 2 and "cannot run exiftool" in result.err
        assert file_state(path) == before
        assert (
            not list((real_world.review_root() / "runs").glob("*.json"))
            if (real_world.home / "gps-sync").exists()
            else True
        )
