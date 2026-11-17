"""Whole-tool behaviour over a copied, reference-shaped tree, driven through the real CLI."""

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    PLANTED_IGNORED,
    Tree,
    missing_ignored_fixtures,
    plant_ignored_fixtures,
)
from tests.integration.helpers import tags
from tests.support import EXIFTOOL, TESTDATA, clone, exiftool

MANIFEST = json.loads((TESTDATA / "MANIFEST.json").read_text())


def expected_variant_counts() -> dict[str, int]:
    """The e2e totals, read from the manifest's per-file expectations rather than restated.

    The manifest derives them from track time-window containment, independently of the matcher.
    ``Matched`` is the dry-run's ``Planned``, and the planted ``.DS_Store`` files are Ignored too
    but live in no committed tree, so they are added here.
    """
    counted = Counter({"Planned": 0, "Protected": 0, "Unmatchable": 0, "Ignored": 0})
    counted.update(
        "Planned" if e["expect"] == "Matched" else e["expect"] for e in MANIFEST["files"]
    )
    counted["Ignored"] += len(PLANTED_IGNORED)
    return {**counted, "Failed": 0, "Geotagged": 0}


EXPECTED = expected_variant_counts()


def test_the_manifest_describes_exactly_the_committed_media_tree_and_the_expected_totals():
    on_disk = {
        path.relative_to(TESTDATA).as_posix()
        for path in (TESTDATA / "media").rglob("*")
        if path.is_file() and path.name != ".DS_Store"
    }

    assert {entry["rel"] for entry in MANIFEST["files"]} == on_disk
    assert EXPECTED == {
        "Planned": 4,
        "Protected": 1,
        "Unmatchable": 2,
        "Ignored": 5,
        "Failed": 0,
        "Geotagged": 0,
    }


def test_the_ignore_path_fixtures_are_planted_in_every_tree_and_reported_as_ignored(tree: Tree):
    assert missing_ignored_fixtures(tree.root) == []

    result = tree.cli("--json")

    ignored = {f["path"] for f in result.json()["files"] if f["variant"] == "Ignored"}
    assert {str(tree.root / relative) for relative in PLANTED_IGNORED} <= ignored
    assert len(ignored) == EXPECTED["Ignored"]


def test_planting_replaces_whatever_finder_left_and_a_missing_file_is_named(tmp_path: Path):
    (tmp_path / "media" / "day1").mkdir(parents=True)
    (tmp_path / "media" / "stray").mkdir()
    (tmp_path / "media" / "stray" / ".DS_Store").write_bytes(b"x")
    assert missing_ignored_fixtures(tmp_path) == list(PLANTED_IGNORED)

    plant_ignored_fixtures(tmp_path)

    assert missing_ignored_fixtures(tmp_path) == []
    assert not (tmp_path / "media" / "stray" / ".DS_Store").exists()
    (tmp_path / PLANTED_IGNORED[1]).unlink()
    assert missing_ignored_fixtures(tmp_path) == [PLANTED_IGNORED[1]]


def snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def by_path(result_json: dict) -> dict[str, dict]:
    return {f["path"]: f for f in result_json["files"]}


def link_tree(review_root: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    base = review_root / "_unprocessed"
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            bucket = path.relative_to(base).parts[0]
            out.setdefault(bucket, []).append(path.relative_to(base / bucket).as_posix())
    return out


def test_a_full_tree_dry_run_matches_the_fixtures_known_composition_and_mutates_nothing(tree: Tree):
    (tree.media / "other").mkdir()
    clone(tree.media / "day1" / "DSCF8326.RAF", tree.media / "other" / "DSCF8326.RAF")
    before = snapshot(tree.media)
    names_before = sorted(str(p) for p in tree.media.rglob("*"))

    result = tree.cli("--json")

    record = result.json()
    counts = record["summary"]["variant_counts"]
    assert result.returncode == 0 and result.stderr == ""
    assert counts == {**EXPECTED, "Unmatchable": 3}
    assert record["summary"]["coverage"]["matched"] == 4
    assert snapshot(tree.media) == before
    assert sorted(str(p) for p in tree.media.rglob("*")) == names_before
    links = link_tree(tree.review_root())
    assert set(links) == {"already-tagged", "no-track-coverage"}
    assert links["already-tagged"] == [
        "drone/DJI_20261026094631_0036_D.DNG",
    ]
    assert links["no-track-coverage"] == [
        "day1/DSCF8326.RAF",
        "day4/DSCF8679.RAF",
        "other/DSCF8326.RAF",
    ]


def test_dry_run_write_run_and_rerun_form_a_complete_account_of_what_the_tool_did(tree: Tree):
    dry = tree.cli("--json")
    dry_files = by_path(dry.json())
    planned = {p: f["position"] for p, f in dry_files.items() if f["variant"] == "Planned"}
    before_media = snapshot(tree.media)
    assert len(planned) == 4
    assert list((tree.review_root() / "runs").iterdir()) == []

    written = tree.cli("--write", "--json")
    rerun = tree.cli("--write", "--json")

    assert dry.returncode == written.returncode == rerun.returncode == 0
    written_files = by_path(written.json())
    geotagged = {p: f["position"] for p, f in written_files.items() if f["variant"] == "Geotagged"}
    assert geotagged == planned, "the dry run is exact, not predictive"
    changed = {p for p, state in snapshot(tree.media).items() if before_media.get(p) != state}
    assert changed == set(planned)
    for path in planned:
        values = tags(Path(path), "-GPSLatitude", "-GPSLongitude")
        assert values["GPSLatitude"] == pytest.approx(planned[path]["lat"], abs=1e-6)
        assert values["GPSLongitude"] == pytest.approx(planned[path]["lon"], abs=1e-6)

    rerun_files = by_path(rerun.json())
    for path in planned:
        assert rerun_files[path]["reason"] == "POSITION_PRESENT"
    assert rerun.json()["summary"]["variant_counts"]["Protected"] == 1 + 4
    after_rerun = snapshot(tree.media)
    third = tree.cli("--write")
    assert snapshot(tree.media) == after_rerun and third.returncode == 0

    records = sorted((tree.review_root() / "runs").glob("*.json"))
    documents = [json.loads(r.read_text()) for r in records]
    assert len(documents) == 3
    assert [d["phase"] for d in documents] == ["complete"] * 3
    assert [e["path"] for e in documents[0]["intended"]] == sorted(planned)
    assert {e["path"]: e["position"] for e in documents[0]["intended"]} == planned
    assert {c["path"]: c["position"] for c in documents[0]["conclusions"]} == planned
    assert documents[1]["intended"] == [] and documents[2]["intended"] == []


def test_extend_recovers_the_near_misses_as_extended_and_never_as_interpolated(tree: Tree):
    result = tree.cli("--json", "--extend", "90m")

    record = result.json()
    assert result.returncode == 0
    summary = record["summary"]
    assert summary["variant_counts"]["Unmatchable"] == 0
    assert summary["coverage"]["by_via"] == {"INTERPOLATED": 4, "EXTENDED": 2, "CENTROID": 0}
    extended = [f for f in record["files"] if f.get("via") == "EXTENDED"]
    assert len(extended) == 2
    assert all(f["provenance"]["overridden_reason"] == "OUTSIDE_COVERAGE" for f in extended)


def test_the_report_names_the_computed_extend_remedy(tree: Tree):
    result = tree.cli()

    assert result.returncode == 0
    assert "--extend 3751s would recover all of them" in result.stdout


def add_covered_jpeg(tree: Tree, assets) -> Path:
    """A still whose instant lies inside track coverage but whose container is not writable."""
    path = clone(assets.jpeg, tree.media / "extracted-preview.JPG")
    exiftool("-q", "-overwrite_original", "-DateTimeOriginal=2026:10:26 12:00:00", str(path))
    return path


class TestExitStatusEndToEnd:
    def test_matched_files_plus_deliberate_coverage_misses_exit_zero(self, tree: Tree):
        assert tree.cli().returncode == 0

    def test_one_unsupported_container_makes_the_same_tree_exit_one(self, tree: Tree, assets):
        add_covered_jpeg(tree, assets)

        assert tree.cli().returncode == 1
        assert tree.cli("--write").returncode == 1

    def test_an_absent_exiftool_exits_two(self, tree: Tree):
        result = tree.cli(env={"GPS_SYNC_EXIFTOOL": "/nonexistent/exiftool"})

        assert result.returncode == 2 and "cannot run exiftool" in result.stderr
        assert result.stdout == ""

    def test_a_malformed_gpx_exits_two_without_reporting_a_tally(self, tree: Tree):
        (tree.tracks / "activity_24855779045.gpx").write_text("<gpx")

        result = tree.cli()

        assert result.returncode == 2 and result.stdout == ""

    def test_a_foreign_review_dir_exits_two_and_removes_nothing(self, tree: Tree, tmp_path):
        foreign = tmp_path / "review"
        (foreign / "_unprocessed").mkdir(parents=True)
        (foreign / "_unprocessed" / "precious.txt").write_text("keep me")

        result = tree.cli("--review-dir", str(foreign))

        assert result.returncode == 2
        assert (foreign / "_unprocessed" / "precious.txt").read_text() == "keep me"

    def test_a_bad_flag_exits_two(self, tree: Tree):
        assert tree.cli("--max-gap", "1h30m").returncode == 2

    def test_a_dry_run_that_exits_one_predicts_the_write_run_will_too(self, tree: Tree, assets):
        add_covered_jpeg(tree, assets)
        dry = tree.cli("--json")
        written = tree.cli("--json", "--write")

        failed_dry = {f["path"] for f in dry.json()["files"] if f["variant"] == "Failed"}
        failed_write = {f["path"] for f in written.json()["files"] if f["variant"] == "Failed"}
        assert dry.returncode == written.returncode == 1
        assert failed_dry <= failed_write


def test_independent_cross_check_against_exiftools_own_geotag(tree: Tree, tmp_path):
    """The sole place exiftool's geotagging machinery runs: a standing guard on Shape B."""
    oracle = tmp_path / "oracle"
    subprocess.run(["cp", "-c", "-R", str(tree.media), str(oracle)], check=True)
    gpx_args = [arg for g in sorted(tree.tracks.glob("*.gpx")) for arg in ("-geotag", str(g))]
    exiftool("-q", "-q", "-overwrite_original", *gpx_args, "-r", "-ext", "RAF", str(oracle))

    ours = by_path(tree.cli("--json").json())
    compared = refused = 0
    for path, outcome in ours.items():
        if not path.endswith(".RAF"):
            continue
        theirs = tags(oracle / Path(path).relative_to(tree.media), "-GPSLatitude", "-GPSLongitude")
        if outcome["variant"] == "Planned":
            assert theirs["GPSLatitude"] == pytest.approx(outcome["position"]["lat"], abs=1e-6)
            assert theirs["GPSLongitude"] == pytest.approx(outcome["position"]["lon"], abs=1e-6)
            compared += 1
        else:
            assert "GPSLatitude" not in theirs, f"exiftool matched a file we refused: {path}"
            refused += 1
    assert (compared, refused) == (4, 2)
    assert Path(EXIFTOOL).exists()
