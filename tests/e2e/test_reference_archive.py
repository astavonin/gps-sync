"""The coverage regression over the real photo archive: skipped unless explicitly enabled.

It reads ``/Volumes/Data SSD 2TB`` and ``~/Downloads/tracks``, both strictly read-only (a dry
run never writes the media tree), and only when ``GPS_SYNC_REFERENCE_ARCHIVE=1`` is set and the
volume is mounted.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gps_sync.space import SLACK_BYTES
from tests.support import EXIFTOOL, REPO

ARCHIVE = Path("/Volumes/Data SSD 2TB/PhotoArchive/2026/Issyk Kul")
#: The whole library: the movie files the design's partition is stated over live across all of it.
LIBRARY = ARCHIVE.parents[1]
TRACKS = Path.home() / "Downloads" / "tracks"

pytestmark = pytest.mark.skipif(
    os.environ.get("GPS_SYNC_REFERENCE_ARCHIVE") != "1"
    or not ARCHIVE.is_dir()
    or not TRACKS.is_dir(),
    reason="set GPS_SYNC_REFERENCE_ARCHIVE=1 with /Volumes/Data SSD 2TB mounted to run",
)


def dry_run(
    tmp_path: Path, *extra: str, media: Path = ARCHIVE, failures_exit: bool = False
) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gps_sync",
            "--media",
            str(media),
            "--tracks",
            str(TRACKS),
            "--review-dir",
            str(tmp_path / "review"),
            "--json",
            *extra,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "src"), "HOME": str(tmp_path)},
        check=False,
    )
    record = json.loads(completed.stdout)
    expected = 1 if failures_exit and record["summary"]["variant_counts"]["Failed"] else 0
    assert completed.returncode == expected, completed.stderr
    return record


EXTEND_S = 90 * 60
CLAMPABLE = {"MAX_GAP_EXCEEDED", "SEGMENT_BOUNDARY", "OUTSIDE_COVERAGE"}


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> tuple[dict, dict]:
    base = tmp_path_factory.mktemp("reference")
    return dry_run(base), dry_run(base, "--extend", "90m")


def by_path(record: dict) -> dict[str, dict]:
    return {f["path"]: f for f in record["files"]}


class TestTheInvariantsTheArchiveStateCannotChange:
    """What holds however many files the archive has already had written.

    The archive is the real photo library and is written to for real by the tool, so the counts of
    matched and protected files move with every run: only relations between the figures hold.
    """

    def test_the_variant_counts_partition_every_discovered_file(self, runs):
        for record in runs:
            counts = record["summary"]["variant_counts"]
            assert sum(counts.values()) == len(record["files"])
            assert {f["variant"] for f in record["files"]} <= set(counts)

    def test_the_coverage_figure_is_self_consistent_and_total_over_the_via_members(self, runs):
        for record in runs:
            coverage = record["summary"]["coverage"]
            unmatchable = record["summary"]["variant_counts"]["Unmatchable"]
            assert coverage["reached_matcher"] == coverage["matched"] + unmatchable
            assert sum(coverage["by_via"].values()) == coverage["matched"]
            if coverage["reached_matcher"]:
                assert coverage["rate_percent"] == pytest.approx(
                    100 * coverage["matched"] / coverage["reached_matcher"]
                )

    def test_extend_recovers_exactly_the_clampable_misses_within_its_window(self, runs):
        plain, extended = (by_path(r) for r in runs)

        expected = {
            path
            for path, f in plain.items()
            if f["variant"] == "Unmatchable"
            and f["reason"] in CLAMPABLE
            and f["miss_s"] <= EXTEND_S
        }
        recovered = {p for p, f in extended.items() if f.get("via") == "EXTENDED"}

        assert recovered == expected
        assert runs[1]["summary"]["coverage"]["by_via"]["EXTENDED"] == len(expected)
        assert all("overridden_reason" in extended[p]["provenance"] for p in recovered)

    def test_extend_only_ever_adds_matches_and_moves_no_planned_position(self, runs):
        plain, extended = (by_path(r) for r in runs)

        for path, outcome in plain.items():
            if outcome["variant"] == "Planned":
                assert extended[path]["variant"] == "Planned"
                assert extended[path]["position"] == outcome["position"]
                assert extended[path]["via"] == outcome["via"]

    def test_a_file_carrying_a_position_stays_protected_with_that_position_under_extend(self, runs):
        plain, extended = (by_path(r) for r in runs)

        protected = {p: f for p, f in plain.items() if f["variant"] == "Protected"}

        for path, outcome in protected.items():
            assert extended[path]["variant"] == "Protected"
            assert extended[path]["position"] == outcome["position"]
        assert not any(f["variant"] == "Geotagged" for f in plain.values())


MOVIE_SUFFIXES = {".mp4", ".mov"}
#: The three places this archive stores a container position, as exiftool group-qualified tags.
POSITION_TAGS = ("Keys:GPSCoordinates", "UserData:GPSCoordinates", "GPS:GPSLatitude")
COVERAGE_CEILING_S = 1.0


def movie_files(root: Path) -> list[Path]:
    """Every movie file under ``root``, hidden ones included, found without the tool."""
    return sorted(
        Path(directory) / name
        for directory, _dirs, names in os.walk(root)
        for name in names
        if Path(name).suffix.lower() in MOVIE_SUFFIXES
    )


def is_hidden(path: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def movie_state(paths: list[Path]) -> dict[Path, tuple[int, int]]:
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}


def read_tags(paths: list[Path]) -> dict[str, dict]:
    """Read position and handler tags with the backend directly, as the partition's oracle."""
    argfile = ["-j", "-n", "-a", "-G1", *(f"-{t}" for t in POSITION_TAGS), "-HandlerType"]
    done = subprocess.run(
        [EXIFTOOL, *argfile, "-@", "-"],
        input="\n".join(str(p) for p in paths),
        capture_output=True,
        text=True,
        check=False,
    )
    return {r["SourceFile"]: r for r in json.loads(done.stdout)}


@pytest.fixture(scope="module")
def library_run(tmp_path_factory) -> dict:
    movies = movie_files(LIBRARY)
    before = movie_state(movies)
    record = dry_run(
        tmp_path_factory.mktemp("library"), "--video", media=LIBRARY, failures_exit=True
    )
    assert movie_state(movies) == before, "a dry run changed a movie file"
    return record


@pytest.fixture(scope="module")
def library_movies(library_run) -> dict[str, dict]:
    return {
        f["path"]: f
        for f in library_run["files"]
        if Path(f["path"]).suffix.lower() in MOVIE_SUFFIXES
    }


class TestTheVideoPartitionOverTheWholeLibrary:
    """Counts that move with the archive or the track set are derived at test time; the counts that
    are properties of the files alone (hidden 1, Protected 19 as 5-8-6, re-wraps 37 as 34-3, the one
    undated clip) are literals."""

    def test_every_movie_file_the_walk_finds_is_reported_once_and_none_is_invented(
        self, library_run, library_movies
    ):
        walked = {str(p) for p in movie_files(LIBRARY)}

        assert set(library_movies) == walked
        assert len([f for f in library_run["files"] if f["path"] in walked]) == len(walked)

    def test_the_movie_outcomes_partition_the_total_the_test_itself_counted(self, library_movies):
        counts = {}
        for outcome in library_movies.values():
            counts[outcome["variant"]] = counts.get(outcome["variant"], 0) + 1

        assert sum(counts.values()) == len(movie_files(LIBRARY))
        vias = [f.get("via") for f in library_movies.values() if f["variant"] == "Planned"]
        assert counts["Planned"] == vias.count("CENTROID") + vias.count("INTERPOLATED")

    def test_a_hidden_movie_is_ignored_as_hidden_and_nothing_else_is(self, library_movies):
        hidden = {str(p) for p in movie_files(LIBRARY) if is_hidden(p, LIBRARY)}

        reported = {p for p, f in library_movies.items() if f.get("reason") == "HIDDEN_ENTRY"}

        assert reported == hidden
        assert len(hidden) == 1, "the one truncated dot-file the design names"

    def test_protected_movies_are_exactly_those_carrying_a_position_in_any_storage_place(
        self, library_movies
    ):
        visible = [Path(p) for p in library_movies if not is_hidden(Path(p), LIBRARY)]
        tags = read_tags(visible)
        positioned = {
            p for p, record in tags.items() if any(tag in record for tag in POSITION_TAGS)
        }
        by_place = {tag: {p for p, r in tags.items() if tag in r} for tag in POSITION_TAGS}

        protected = {p for p, f in library_movies.items() if f["variant"] == "Protected"}

        assert protected == positioned
        assert all(library_movies[p]["reason"] == "POSITION_PRESENT" for p in protected)
        assert set().union(*by_place.values()) == positioned
        assert {tag: len(paths) for tag, paths in by_place.items()} == {
            "Keys:GPSCoordinates": 5,
            "UserData:GPSCoordinates": 8,
            "GPS:GPSLatitude": 6,
        }
        assert len(protected) == 19

    def test_every_movie_outcome_is_one_the_design_declares_for_a_movie(self, library_movies):
        allowed_failures = {"TIME_SEMANTICS_UNVERIFIED", "TIME_UNRESOLVABLE"}
        for path, outcome in library_movies.items():
            variant = outcome["variant"]
            assert variant in {"Ignored", "Protected", "Failed", "Planned", "Unmatchable"}, path
            if variant == "Failed":
                assert outcome["reason"] in allowed_failures, (path, outcome["reason"])
            if variant == "Planned":
                assert outcome["via"] in {"CENTROID", "INTERPOLATED"}, path
            if variant == "Unmatchable":
                assert outcome["reason"] in CLAMPABLE | {"BRACKET_UNSOUND"}, path

    def test_every_centroid_clip_passes_the_declared_coverage_band(self, library_movies):
        centroid = [f for f in library_movies.values() if f.get("via") == "CENTROID"]

        assert centroid, "no clip was positioned from its own telemetry"
        for outcome in centroid:
            assert 0 <= outcome["provenance"]["shortfall_s"] <= COVERAGE_CEILING_S, outcome["path"]
        assert not [
            f for f in library_movies.values() if f.get("reason", "").startswith("TELEMETRY_")
        ]

    def test_a_clip_is_positioned_from_telemetry_only_when_it_carries_a_meta_track(
        self, library_movies
    ):
        centroid = [Path(p) for p, f in library_movies.items() if f.get("via") == "CENTROID"]

        tags = read_tags(centroid)

        for path in centroid:
            handlers = {v for k, v in tags[str(path)].items() if k.endswith(":HandlerType")}
            assert "meta" in handlers, path

    def test_the_apple_rewraps_are_refused_for_time_semantics_and_never_positioned(
        self, library_movies
    ):
        unverified = [
            Path(p)
            for p, f in library_movies.items()
            if f.get("reason") == "TIME_SEMANTICS_UNVERIFIED"
        ]

        refused = {str(p): library_movies[str(p)] for p in unverified}
        assert all(f["variant"] == "Failed" for f in refused.values())
        no_creation_date = [p for p, f in refused.items() if "CreationDate None," in f["detail"]]
        assert len(refused) == 37
        assert (len(refused) - len(no_creation_date), len(no_creation_date)) == (34, 3)

    def test_exactly_one_undated_clip_is_unresolvable_and_is_the_android_zero_date(
        self, library_movies
    ):
        unresolvable = [
            f for f in library_movies.values() if f.get("reason") == "TIME_UNRESOLVABLE"
        ]

        assert len(unresolvable) == 1
        assert "0000:00:00 00:00:00" in unresolvable[0]["detail"]
        assert unresolvable[0]["variant"] == "Failed"

    def test_the_forecast_is_derived_from_the_planned_targets_per_volume(self, library_run):
        planned = [Path(f["path"]) for f in library_run["files"] if f["variant"] == "Planned"]
        sizes = [p.stat().st_size for p in planned]

        [figure] = library_run["space_demand"]

        assert figure["peak_bytes"] == max(sizes) + SLACK_BYTES
        assert figure["retained_bytes"] == sum(sizes)

    def test_the_run_summary_is_consistent_with_the_movie_outcomes(
        self, library_run, library_movies
    ):
        centroid = sum(1 for f in library_movies.values() if f.get("via") == "CENTROID")

        assert library_run["summary"]["coverage"]["by_via"]["CENTROID"] == centroid
        assert library_run["telemetry_read"]["clips"] == centroid
        failed = library_run["summary"]["variant_counts"]["Failed"]
        assert library_run["exit_code"] == (1 if failed else 0)
