"""A ``--video`` run over a copied, reference-shaped tree, driven through the real CLI.

The tree holds the corpus's five stills that reach the matcher, a DNG that already carries a
position, and one DJI clip whose own telemetry is its position source.
"""

import io
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from gps_sync.options import Options
from gps_sync.orchestrator import run
from gps_sync.transport import ExiftoolTransport
from tests.e2e.conftest import PLANTED_IGNORED, Tree
from tests.e2e.test_full_tree import EXPECTED, MANIFEST, by_path, snapshot
from tests.integration.helpers import tags
from tests.support import EXIFTOOL, clone

CLIP = "drone/DJI_20261026094214_0024_D.MP4"
STILLS_THAT_MATCH = sorted(
    entry["rel"].removeprefix("media/")
    for entry in MANIFEST["files"]
    if entry["expect"] == "Matched"
)
# What a bare run's JSON record looks like to a consumer, flattened to key paths (a list element is
# "[]", an empty container "{}" or "[]", and the registry mirror and the per-reason counts collapse
# to "*" because their members are the registry's own rows, asserted by the registry tests).
# Frozen from a bare dry run of the shipped 001 tool over this tree; a path appearing in or leaving
# the record that is in neither constant fails the diff below, which is the point.
KEYPATHS_UNMOVED_SINCE_001 = frozenset(
    [
        "artifacts.audit_complete",
        "artifacts.audit_directory",
        "artifacts.audit_record",
        "artifacts.problems[]",
        "artifacts.pruned[]",
        "artifacts.review_complete",
        "artifacts.review_folder",
        "artifacts.review_root",
        "exiftool_version",
        "exit_code",
        "files[].bracket_speed",
        "files[].context.file_type",
        "files[].context.make",
        "files[].context.model",
        "files[].instant",
        "files[].miss_s",
        "files[].nearest_segment",
        "files[].path",
        "files[].position.ele",
        "files[].position.lat",
        "files[].position.lon",
        "files[].provenance.bracket_s",
        "files[].provenance.kind",
        "files[].provenance.segment",
        "files[].reason",
        "files[].variant",
        "files[].via",
        "incomplete_prior_records[]",
        "media_root",
        "mode",
        "options.extend_s",
        "options.json",
        "options.keep_original",
        "options.max_gap_s",
        "options.media",
        "options.review_dir",
        "options.time_shift_s",
        "options.tracks",
        "options.utc_offset_s",
        "options.verbose",
        "options.write",
        "overlaps[]",
        "reconciliation",
        "registry.*.exit_term",
        "registry.*.label",
        "registry.*.remedy",
        "registry.*.subfolder",
        "registry.*.variant",
        "skipped_track_symlinks[]",
        "summary.clamp_window.count",
        "summary.clamp_window.largest_s",
        "summary.clamp_window.median_s",
        "summary.clamp_window.smallest_s",
        "summary.coverage.applicable",
        "summary.coverage.by_via.EXTENDED",
        "summary.coverage.by_via.INTERPOLATED",
        "summary.coverage.matched",
        "summary.coverage.rate_percent",
        "summary.coverage.reached_matcher",
        "summary.reason_counts.*",
        "summary.remedies[].files",
        "summary.remedies[].parameter",
        "summary.remedies[].reason",
        "summary.remedies[].smallest_to_recover_all_s",
        "summary.remedies[].smallest_to_recover_any_s",
        "summary.sensitivity.applicable",
        "summary.sensitivity.contributors",
        "summary.sensitivity.displacement_60s_m",
        "summary.sensitivity.excluded.ignored",
        "summary.sensitivity.excluded.protected",
        "summary.sensitivity.excluded.unmatchable",
        "summary.sensitivity.excluded_total",
        "summary.sensitivity.median_speed_mps",
        "summary.success_by_via.Geotagged.EXTENDED",
        "summary.success_by_via.Geotagged.INTERPOLATED",
        "summary.success_by_via.Planned.EXTENDED",
        "summary.success_by_via.Planned.INTERPOLATED",
        "summary.variant_counts.Failed",
        "summary.variant_counts.Geotagged",
        "summary.variant_counts.Ignored",
        "summary.variant_counts.Planned",
        "summary.variant_counts.Protected",
        "summary.variant_counts.Unmatchable",
        "tool",
        "tracks[].dropped{}",
        "tracks[].duplicate_instants_collapsed",
        "tracks[].path",
        "tracks[].points_kept",
        "tracks_root",
    ]
)
# One row per renderer of the design's census that moves the bare dry-run record, with the key
# paths it adds. Each comment names the renderer and why it moves.
KEYPATHS_ADDED_BY_THE_CENSUS = frozenset(
    [
        # runrecord.build_run_record: the options mapping carries every flag, now including video.
        "options.video",
        # registry mirror in build_run_record: the per-file-lines column the report selects on.
        "registry.*.per_file_lines",
        # serialise.outcome_to_dict: the Protected row's detail.
        "files[].detail",
        # runrecord.build_run_record: the declared run-level object, empty when nothing fired.
        "run_level{}",
        # runrecord.space_demand_to_list: the dry run's per-volume free-space forecast.
        "space_demand[].peak_bytes",
        "space_demand[].retained_bytes",
        "space_demand[].volume",
        # runrecord.build_run_record: what the telemetry read cost, zeros when no clip was read.
        "telemetry_read.chunks",
        "telemetry_read.clips",
        "telemetry_read.elapsed_s",
        # runrecord.summary_to_dict: the altitude figure is a breakout over its declared terms.
        "summary.altitude_unavailable.total",
        "summary.altitude_unavailable.breakdown.GPX_PATH_NO_ELEVATION",
        "summary.altitude_unavailable.breakdown.NONE_USABLE",
        "summary.altitude_unavailable.breakdown.NOT_CO_INDEXED",
        "summary.altitude_unavailable.breakdown.NOT_REPRESENTABLE",
        # runrecord.summary_to_dict: the dispersion of centroid positions, null without a clip.
        "summary.centroid_dispersion.count",
        "summary.centroid_dispersion.maximum_m",
        "summary.centroid_dispersion.median_m",
        "summary.centroid_dispersion.minimum_m",
        # summary.Coverage.by_via and success_by_via: the third via at 0 in three breakouts.
        "summary.coverage.by_via.CENTROID",
        "summary.success_by_via.Geotagged.CENTROID",
        "summary.success_by_via.Planned.CENTROID",
    ]
)
# The one path 001 emitted that the record no longer has: the altitude figure was an integer.
KEYPATHS_REMOVED_BY_THE_CENSUS = frozenset(["summary.altitude_unavailable"])

# The bare dry-run report as a set of lines, volatile text replaced by placeholders (see
# normalised_lines). Lines 001 printed unchanged:
REPORT_LINES_UNMOVED_SINCE_001 = frozenset(
    [
        "    OUTSIDE_COVERAGE (instant outside the index's outer ends): 2",
        "    POSITION_PRESENT (position already present): 1",
        "  <tree>/media/day1/DSCF8326.RAF [FUJIFILM X-T5]: Unmatchable/OUTSIDE_COVERAGE - instant"
        " outside the index's outer ends: instant 2026-10-26T03:59:06+00:00, nearest segment"
        " activity_24855779045.gpx#trk0seg0, miss 3037s (~50.6 min)",
        "  <tree>/media/day1/DSCF8340.RAF [FUJIFILM X-T5]: Planned - position planned: 42.298849,"
        " 78.253343, 2446.8 m, instant 2026-10-26T06:21:51+00:00 via INTERPOLATED (segment"
        " activity_24855779045.gpx#trk0seg0, bracket 0s)",
        "  <tree>/media/day2/sub/DSCF8443.RAF [FUJIFILM X-T5]: Planned - position planned:"
        " 42.093574, 77.128271, 2555.0 m, instant 2026-10-27T08:29:30+00:00 via INTERPOLATED"
        " (segment activity_24867378667.gpx#trk0seg0, bracket 3s)",
        "  <tree>/media/day2/sub/DSCF8444.RAF [FUJIFILM X-T5]: Planned - position planned:"
        " 42.093595, 77.128269, 2555.4 m, instant 2026-10-27T08:29:32+00:00 via INTERPOLATED"
        " (segment activity_24867378667.gpx#trk0seg0, bracket 0s)",
        "  <tree>/media/day3/DSCF8526.RAF [FUJIFILM X-T5]: Planned - position planned: 42.339963,"
        " 78.468668, 2458.0 m, instant 2026-10-29T04:58:13+00:00 via INTERPOLATED (segment"
        " activity_24891302165.gpx#trk0seg0, bracket 6s)",
        "  <tree>/media/day4/DSCF8679.RAF [FUJIFILM X-T5]: Unmatchable/OUTSIDE_COVERAGE - instant"
        " outside the index's outer ends: instant 2026-10-31T06:05:26+00:00, nearest segment"
        " activity_24912825946.gpx#trk0seg0, miss 3751s (~1.04 h)",
        "  Failed: 0",
        "  Ignored: 3",
        "  OUTSIDE_COVERAGE (instant outside the index's outer ends), 2 file(s): --extend 3751s"
        " would recover all of them (smallest single miss 3037s)",
        "  OUTSIDE_COVERAGE: use --extend with at least the miss reported for the file, or add the"
        " covering track to the tracks root",
        "  POSITION_PRESENT: none needed; the file keeps its position and is linked here only so"
        " it can be browsed",
        "  Protected: 1",
        "  Unmatchable: 2",
        "  activity_24855779045.gpx: 3968 point(s) kept, dropped none, duplicate instants"
        " collapsed 0",
        "  activity_24864742550.gpx: 2643 point(s) kept, dropped none, duplicate instants"
        " collapsed 0",
        "  activity_24867378667.gpx: 1093 point(s) kept, dropped none, duplicate instants"
        " collapsed 0",
        "  activity_24891302165.gpx: 6070 point(s) kept, dropped none, duplicate instants"
        " collapsed 0",
        "  activity_24902171217.gpx: 3145 point(s) kept, dropped none, duplicate instants"
        " collapsed 0",
        "  activity_24902171736.gpx: 1260 point(s) kept, dropped none, duplicate instants"
        " collapsed 0",
        "  activity_24912825946.gpx: 2576 point(s) kept, dropped none, duplicate instants"
        " collapsed 0",
        "  audit record for this run: none (a dry run writes no audit record)",
        "  audit records: <home>/gps-sync/media-<hash>/runs",
        "  review folder: <home>/gps-sync/media-<hash>/_unprocessed",
        "A clean dry run does not guarantee a clean write run: the backend can still fail on a"
        " file, be interrupted, or find a position that appeared since the read.",
        "Clock sensitivity: median speed at the photo instants 1.00 m/s; a 60 s clock error would"
        " displace the median photo by 60.0 m (4 file(s) contributed; 6 excluded: ignored 3,"
        " protected 1, unmatchable 2; only interpolated Planned/Geotagged files with a defined"
        " bracket speed count)",
        "Outcomes",
        "Remedies",
        "Run artifacts",
        "Tracks",
        "exiftool: <version>",
        "gps-sync dry run (no media file is modified)",
        "media:    <tree>/media",
        "tracks:   <tree>/tracks",
    ]
)
# Lines the census adds in place of, or beside, 001's; each comment names the renderer.
REPORT_LINES_ADDED_BY_THE_CENSUS = frozenset(
    [
        # report._counts: NOT_A_STILL's label is restated ("not a still" was 001's).
        "    NOT_A_STILL (not a candidate file type for this run): 3",
        # report._outcomes: the third via at 0 on both success lines.
        "  Planned: 4 (INTERPOLATED 4, EXTENDED 0, CENTROID 0)",
        "  Geotagged: 0 (INTERPOLATED 0, EXTENDED 0, CENTROID 0)",
        # report._coverage_line: the same, on the coverage line.
        "Coverage: 4/6 = 66.7 % (INTERPOLATED 4, EXTENDED 0, CENTROID 0)",
        # report._space_lines: the free-space forecast, dry run only.
        "Free space a write needs on volume <volume>: <peak> bytes at peak (the largest planned "
        "target plus slack); with --keep-original <retained> bytes accumulate",
        # report._altitude_lines: the altitude figure as a four-term breakout.
        "Altitude not available for 0 planned/written file(s) (NONE_USABLE 0, NOT_CO_INDEXED 0, "
        "NOT_REPRESENTABLE 0, GPX_PATH_NO_ELEVATION 0)",
        # report._remedy_lines: NOT_A_STILL gains a remedy line.
        "  NOT_A_STILL: if the entry is a video container, pass --video to admit it; otherwise the "
        "file type is outside this tool's scope",
        # report._file_line: a Protected row carries its detail.
        "  <tree>/media/drone/DJI_20261026094631_0036_D.DNG [DJI FC9313]: "
        "Protected/POSITION_PRESENT - position already present: carries 42.337208, 78.241497, "
        "no instant: OffsetTimeOriginal is absent and --utc-offset was not given for this "
        "local-time still",
    ]
)

# What the same two assertions find on the other run shapes, as a delta from the bare dry run: a
# --video dry run, and a bare --write run (the two rows the census marks write-run-only). The write
# run's audit record path carries a timestamp and a pid, masked to <run> by normalised_lines.
KEYPATHS_ADDED_BY_A_VIDEO_RUN = frozenset(
    [
        "files[].provenance.altitude",
        "files[].provenance.dropped.OUT_OF_RANGE",
        "files[].provenance.dropped.UNPARSEABLE",
        "files[].provenance.dropped.ZERO_SENTINEL",
        "files[].provenance.rule",
        "files[].provenance.samples_read",
        "files[].provenance.samples_used",
        "files[].provenance.shortfall_s",
        "files[].provenance.span_s",
        "files[].provenance.spread_m",
        "summary.sensitivity.excluded.centroid",
    ]
)

KEYPATHS_REMOVED_BY_A_VIDEO_RUN = frozenset([])

REPORT_LINES_ADDED_BY_A_VIDEO_RUN = frozenset(
    [
        "    NOT_A_STILL (not a candidate file type for this run): 2",
        "  <tree>/media/drone/DJI_20261026094214_0024_D.MP4: Planned - position planned:"
        " 42.335033, 78.226356, 2188.5 m, instant 2026-10-26T03:42:14+00:00 via CENTROID"
        " (mean of 24 of 24 samples, 0 dropped (unparseable 0, out of range 0, zero sentinel"
        " 0), span 0.38 s, shortfall against the duration 0.02 s, farthest fix 0.5 m from"
        " the position, altitude ABSOLUTE_ALTITUDE)",
        "  Ignored: 2",
        "  Planned: 5 (INTERPOLATED 4, EXTENDED 0, CENTROID 1)",
        "Clock sensitivity: median speed at the photo instants 1.00 m/s; a 60 s clock error"
        " would displace the median photo by 60.0 m (4 file(s) contributed; 6 excluded:"
        " centroid 1, ignored 2, protected 1, unmatchable 2; only interpolated"
        " Planned/Geotagged files with a defined bracket speed count)",
        "Coverage: 5/7 = 71.4 % (INTERPOLATED 4, EXTENDED 0, CENTROID 1)",
        "Telemetry centroid dispersion: 1 file(s), farthest used fix from the written"
        " position - minimum 0.5 m, median 0.5 m, maximum 0.5 m",
        "Telemetry read: 1 clip(s) in 1 chunk(s), 0.1 s",
    ]
)

REPORT_LINES_REMOVED_BY_A_VIDEO_RUN = frozenset(
    [
        "    NOT_A_STILL (not a candidate file type for this run): 3",
        "  Ignored: 3",
        "  Planned: 4 (INTERPOLATED 4, EXTENDED 0, CENTROID 0)",
        "Clock sensitivity: median speed at the photo instants 1.00 m/s; a 60 s clock error"
        " would displace the median photo by 60.0 m (4 file(s) contributed; 6 excluded:"
        " ignored 3, protected 1, unmatchable 2; only interpolated Planned/Geotagged files"
        " with a defined bracket speed count)",
        "Coverage: 4/6 = 66.7 % (INTERPOLATED 4, EXTENDED 0, CENTROID 0)",
    ]
)

KEYPATHS_ADDED_BY_A_WRITE_RUN = frozenset(
    [
        "reconciliation.last_marker",
        "reconciliation.process_failure",
        "reconciliation.returncode",
        "reconciliation.unintended_conclusions[]",
        "reconciliation.unreconciled_blocks[]",
        "reconciliation.unreconciled_paths[]",
    ]
)

KEYPATHS_REMOVED_BY_A_WRITE_RUN = frozenset(
    [
        "reconciliation",
    ]
)

REPORT_LINES_ADDED_BY_A_WRITE_RUN = frozenset(
    [
        "  <tree>/media/day1/DSCF8340.RAF [FUJIFILM X-T5]: Geotagged - position written:"
        " 42.298849, 78.253343, 2446.8 m, instant 2026-10-26T06:21:51+00:00 via INTERPOLATED"
        " (segment activity_24855779045.gpx#trk0seg0, bracket 0s)",
        "  <tree>/media/day2/sub/DSCF8443.RAF [FUJIFILM X-T5]: Geotagged - position written:"
        " 42.093574, 77.128271, 2555.0 m, instant 2026-10-27T08:29:30+00:00 via INTERPOLATED"
        " (segment activity_24867378667.gpx#trk0seg0, bracket 3s)",
        "  <tree>/media/day2/sub/DSCF8444.RAF [FUJIFILM X-T5]: Geotagged - position written:"
        " 42.093595, 77.128269, 2555.4 m, instant 2026-10-27T08:29:32+00:00 via INTERPOLATED"
        " (segment activity_24867378667.gpx#trk0seg0, bracket 0s)",
        "  <tree>/media/day3/DSCF8526.RAF [FUJIFILM X-T5]: Geotagged - position written:"
        " 42.339963, 78.468668, 2458.0 m, instant 2026-10-29T04:58:13+00:00 via INTERPOLATED"
        " (segment activity_24891302165.gpx#trk0seg0, bracket 6s)",
        "  Geotagged: 4 (INTERPOLATED 4, EXTENDED 0, CENTROID 0)",
        "  Planned: 0 (INTERPOLATED 0, EXTENDED 0, CENTROID 0)",
        "  audit record for this run: <home>/gps-sync/media-<hash>/runs/<run>.json",
        "Write phase: exiftool exited 0; last marker present: <tree>/media/day3/DSCF8526.RAF",
        "gps-sync write run",
    ]
)

REPORT_LINES_REMOVED_BY_A_WRITE_RUN = frozenset(
    [
        "  <tree>/media/day1/DSCF8340.RAF [FUJIFILM X-T5]: Planned - position planned:"
        " 42.298849, 78.253343, 2446.8 m, instant 2026-10-26T06:21:51+00:00 via INTERPOLATED"
        " (segment activity_24855779045.gpx#trk0seg0, bracket 0s)",
        "  <tree>/media/day2/sub/DSCF8443.RAF [FUJIFILM X-T5]: Planned - position planned:"
        " 42.093574, 77.128271, 2555.0 m, instant 2026-10-27T08:29:30+00:00 via INTERPOLATED"
        " (segment activity_24867378667.gpx#trk0seg0, bracket 3s)",
        "  <tree>/media/day2/sub/DSCF8444.RAF [FUJIFILM X-T5]: Planned - position planned:"
        " 42.093595, 77.128269, 2555.4 m, instant 2026-10-27T08:29:32+00:00 via INTERPOLATED"
        " (segment activity_24867378667.gpx#trk0seg0, bracket 0s)",
        "  <tree>/media/day3/DSCF8526.RAF [FUJIFILM X-T5]: Planned - position planned:"
        " 42.339963, 78.468668, 2458.0 m, instant 2026-10-29T04:58:13+00:00 via INTERPOLATED"
        " (segment activity_24891302165.gpx#trk0seg0, bracket 6s)",
        "  Geotagged: 0 (INTERPOLATED 0, EXTENDED 0, CENTROID 0)",
        "  Planned: 4 (INTERPOLATED 4, EXTENDED 0, CENTROID 0)",
        "  audit record for this run: none (a dry run writes no audit record)",
        "A clean dry run does not guarantee a clean write run: the backend can still fail on"
        " a file, be interrupted, or find a position that appeared since the read.",
        "Free space a write needs on volume <volume>: <peak> bytes at peak (the largest"
        " planned target plus slack); with --keep-original <retained> bytes accumulate",
        "gps-sync dry run (no media file is modified)",
    ]
)

CLIP_KEYPATHS = frozenset(
    [
        "context.file_type",
        "instant",
        "path",
        "position.ele",
        "position.lat",
        "position.lon",
        "provenance.altitude",
        "provenance.dropped.OUT_OF_RANGE",
        "provenance.dropped.UNPARSEABLE",
        "provenance.dropped.ZERO_SENTINEL",
        "provenance.kind",
        "provenance.rule",
        "provenance.samples_read",
        "provenance.samples_used",
        "provenance.shortfall_s",
        "provenance.span_s",
        "provenance.spread_m",
        "variant",
        "via",
    ]
)


COLLAPSED_KEY_PARENTS = ("registry", "summary.reason_counts")


def key_paths(value: object, prefix: str = "") -> set[str]:
    """Flatten a JSON value to its key paths; see KEYPATHS_UNMOVED_SINCE_001 for the notation."""
    if isinstance(value, dict):
        found: set[str] = set() if value else {f"{prefix}{{}}"}
        for key, item in value.items():
            name = "*" if prefix in COLLAPSED_KEY_PARENTS else key
            found |= key_paths(item, f"{prefix}.{name}" if prefix else name)
        return found
    if isinstance(value, list):
        found = set() if value else {f"{prefix}[]"}
        for item in value:
            found |= key_paths(item, f"{prefix}[]")
        return found
    return {prefix}


def normalised_lines(text: str, tree: Tree) -> Counter[str]:
    """The report's non-blank lines, counted, with the run's own paths, version and bytes masked."""
    masked: Counter[str] = Counter()
    for line in text.splitlines():
        if not line.strip():
            continue
        line = line.replace(str(tree.home.resolve()), "<home>").replace(
            str(tree.root.resolve()), "<tree>"
        )
        line = re.sub(r"media-[0-9a-f]{8}", "media-<hash>", line)
        line = re.sub(r"^exiftool: \S+$", "exiftool: <version>", line)
        line = re.sub(r"runs/\d{8}T\d{6}Z-\d+\.json", "runs/<run>.json", line)
        line = re.sub(
            r"volume \d+: \d+ bytes at peak", "volume <volume>: <peak> bytes at peak", line
        )
        line = re.sub(r"keep-original \d+ bytes", "keep-original <retained> bytes", line)
        masked[line] += 1
    return masked


def unplant(tree: Tree) -> None:
    """Remove the planted hidden files: the premise of the equality is a tree with none."""
    for relative in PLANTED_IGNORED:
        (tree.root / relative).unlink()


def library_run(tree: Tree, *, space_check=None, injecting=None, **options) -> tuple[int, dict]:
    """Run in-process with the real exiftool, where the CLI cannot reach a seam."""
    transport = injecting or ExiftoolTransport(EXIFTOOL)
    out = io.StringIO()
    kwargs = {"space_check": space_check} if space_check else {}
    code = run(
        Options(media=tree.media, tracks=tree.tracks, json=True, **options),
        transport=transport,
        stdout=out,
        stderr=io.StringIO(),
        home=tree.home,
        **kwargs,
    )
    return code, json.loads(out.getvalue())


class Injecting(ExiftoolTransport):
    """The real transport, with one assignment of one target's block swapped for a refused value."""

    def __init__(self, target: Path, prefix: str, replacement: str) -> None:
        super().__init__(EXIFTOOL)
        self._target, self._prefix, self._replacement = str(target), prefix, replacement

    def write(self, argfile):
        lines = argfile.read_text().splitlines()
        start = 0
        for end, line in enumerate(lines):
            if line == "-execute":
                if lines[end - 1] == self._target:
                    hits = [i for i in range(start, end) if lines[i].startswith(self._prefix)]
                    assert len(hits) == 1, lines[start : end + 1]
                    lines[hits[0]] = self._replacement
                start = end + 1
        injected = argfile.with_name("injected.args")
        injected.write_text("\n".join(lines) + "\n")
        return super().write(injected)


class TestABareRunOverStillsAndVideo:
    def test_it_is_001s_run_and_only_the_census_rows_moved(self, tree: Tree):
        unplant(tree)
        before = snapshot(tree.media)

        result = tree.cli("--json")

        record = result.json()
        files = by_path(record)
        assert result.returncode == 0
        assert record["summary"]["variant_counts"] == {
            **EXPECTED,
            "Ignored": EXPECTED["Ignored"] - len(PLANTED_IGNORED),
        }
        assert {Path(p).name for p, f in files.items() if f["variant"] == "Planned"} == {
            Path(rel).name for rel in STILLS_THAT_MATCH
        }
        clip = files[str(tree.media / CLIP)]
        assert (clip["variant"], clip["reason"]) == ("Ignored", "NOT_A_STILL")
        assert snapshot(tree.media) == before

    def test_the_bare_record_moved_at_exactly_the_declared_key_paths(self, tree: Tree):
        unplant(tree)

        paths = key_paths(tree.cli("--json").json())

        assert not KEYPATHS_UNMOVED_SINCE_001 & KEYPATHS_ADDED_BY_THE_CENSUS
        assert not paths & KEYPATHS_REMOVED_BY_THE_CENSUS
        expected = KEYPATHS_UNMOVED_SINCE_001 | KEYPATHS_ADDED_BY_THE_CENSUS
        assert paths - expected == set(), "a key path no census row declares"
        assert expected - paths == set(), "a declared key path the record no longer has"

    def test_the_bare_report_moved_at_exactly_the_declared_lines(self, tree: Tree):
        unplant(tree)

        lines = normalised_lines(tree.cli().stdout, tree)

        assert not REPORT_LINES_UNMOVED_SINCE_001 & REPORT_LINES_ADDED_BY_THE_CENSUS
        assert lines == Counter(REPORT_LINES_UNMOVED_SINCE_001 | REPORT_LINES_ADDED_BY_THE_CENSUS)

    def test_two_identical_bare_runs_emit_the_same_record(self, tree: Tree):
        unplant(tree)

        first, second = tree.cli("--json").json(), tree.cli("--json").json()

        assert first == second

    def test_the_restated_not_a_still_row_is_published_in_the_registry_mirror(self, tree: Tree):
        unplant(tree)

        row = tree.cli("--json").json()["registry"]["NOT_A_STILL"]

        assert row == {
            "variant": "Ignored",
            "subfolder": None,
            "label": "not a candidate file type for this run",
            "remedy": "if the entry is a video container, pass --video to admit it; otherwise "
            "the file type is outside this tool's scope",
            "exit_term": 0,
            "per_file_lines": "suppressed",
        }

    def test_the_json_census_rows_each_move_as_declared(self, tree: Tree):
        unplant(tree)

        record = tree.cli("--json").json()

        summary = record["summary"]
        zero = {"INTERPOLATED": 4, "EXTENDED": 0, "CENTROID": 0}
        assert summary["success_by_via"] == {
            "Planned": zero,
            "Geotagged": dict.fromkeys(zero, 0),
        }
        assert summary["coverage"]["by_via"] == zero
        assert summary["altitude_unavailable"]["total"] == 0
        assert set(summary["altitude_unavailable"]["breakdown"]) == {
            "NONE_USABLE",
            "NOT_CO_INDEXED",
            "NOT_REPRESENTABLE",
            "GPX_PATH_NO_ELEVATION",
        }
        assert record["options"]["video"] is False
        assert record["registry"]["TELEMETRY_UNUSABLE"]["per_file_lines"] == "printed"
        assert record["registry"]["HIDDEN_ENTRY"]["per_file_lines"] == "suppressed"
        assert record["run_level"] == {}
        assert [d["peak_bytes"] for d in record["space_demand"]] == [
            max((tree.media / rel).stat().st_size + (1 << 20) for rel in STILLS_THAT_MATCH)
        ]

    def test_the_sensitivity_exclusions_do_not_move_and_name_no_centroid_key(self, tree: Tree):
        unplant(tree)

        bare = tree.cli("--json").json()["summary"]["sensitivity"]["excluded"]
        extended = tree.cli("--json", "--extend", "90m").json()["summary"]["sensitivity"]

        assert bare == {"ignored": 3, "protected": 1, "unmatchable": 2}
        assert extended["excluded"]["extended"] == 2
        assert "centroid" not in extended["excluded"]

    def test_the_report_census_rows_each_move_as_declared(self, tree: Tree):
        unplant(tree)

        text = tree.cli().stdout

        assert "excluded: ignored 3, protected 1, unmatchable 2;" in text
        missing = REPORT_LINES_ADDED_BY_THE_CENSUS - set(normalised_lines(text, tree))
        assert missing == set()

    def test_a_video_run_differs_only_in_what_the_clip_becomes(self, tree: Tree):
        unplant(tree)
        bare, video = (
            by_path(tree.cli("--json").json()),
            by_path(tree.cli("--json", "--video").json()),
        )

        clip = str(tree.media / CLIP)
        assert {p: f for p, f in video.items() if p != clip} == {
            p: f for p, f in bare.items() if p != clip
        }
        assert (video[clip]["variant"], video[clip]["via"]) == ("Planned", "CENTROID")

    def test_planted_hidden_entries_only_ever_remove_and_move_ignored_reasons(self, tree: Tree):
        clone(tree.media / "day1" / "DSCF8340.RAF", tree.media / "day1" / ".hidden.RAF")

        record = tree.cli("--json").json()

        files = by_path(record)
        hidden = {p for p, f in files.items() if f.get("reason") == "HIDDEN_ENTRY"}
        assert hidden == {str(tree.root / rel) for rel in PLANTED_IGNORED} | {
            str(tree.media / "day1" / ".hidden.RAF")
        }
        counts = record["summary"]["variant_counts"]
        assert counts == {**EXPECTED, "Ignored": EXPECTED["Ignored"] + 1}
        assert record["summary"]["reason_counts"]["NOT_A_STILL"] == 3
        assert record["summary"]["reason_counts"]["HIDDEN_ENTRY"] == 3
        assert record["exit_code"] == 0


BARE_KEYPATHS = KEYPATHS_UNMOVED_SINCE_001 | KEYPATHS_ADDED_BY_THE_CENSUS
BARE_LINES = REPORT_LINES_UNMOVED_SINCE_001 | REPORT_LINES_ADDED_BY_THE_CENSUS
RUN_SHAPES = {
    "video-dry-run": (
        ("--video",),
        (KEYPATHS_ADDED_BY_A_VIDEO_RUN, KEYPATHS_REMOVED_BY_A_VIDEO_RUN),
        (REPORT_LINES_ADDED_BY_A_VIDEO_RUN, REPORT_LINES_REMOVED_BY_A_VIDEO_RUN),
    ),
    "bare-write-run": (
        ("--write",),
        (KEYPATHS_ADDED_BY_A_WRITE_RUN, KEYPATHS_REMOVED_BY_A_WRITE_RUN),
        (REPORT_LINES_ADDED_BY_A_WRITE_RUN, REPORT_LINES_REMOVED_BY_A_WRITE_RUN),
    ),
}


def moved(base: frozenset[str], added: frozenset[str], removed: frozenset[str]) -> frozenset[str]:
    """Apply a declared delta, refusing a delta that adds what is there or removes what is not."""
    assert not base & added, "a declared addition the bare run already has"
    assert removed <= base, "a declared removal the bare run never had"
    return (base - removed) | added


@pytest.mark.parametrize("shape", RUN_SHAPES)
class TestTheCensusHoldsOnEveryRunShape:
    def test_the_record_moved_at_exactly_the_declared_key_paths(self, tree: Tree, shape: str):
        flags, (added, removed), _ = RUN_SHAPES[shape]
        unplant(tree)

        paths = key_paths(tree.cli("--json", *flags).json())

        expected = moved(BARE_KEYPATHS, added, removed)
        assert paths - expected == set(), "a key path no census row declares"
        assert expected - paths == set(), "a declared key path the record no longer has"

    def test_the_report_moved_at_exactly_the_declared_lines(self, tree: Tree, shape: str):
        flags, _, (added, removed) = RUN_SHAPES[shape]
        unplant(tree)

        lines = normalised_lines(tree.cli(*flags).stdout, tree)

        assert lines == Counter(moved(BARE_LINES, added, removed))


class TestTheClipsOwnRecord:
    def test_a_video_run_gives_the_clip_exactly_the_frozen_key_paths(self, tree: Tree):
        unplant(tree)

        clip = by_path(tree.cli("--json", "--video").json())[str(tree.media / CLIP)]

        assert key_paths(clip) == CLIP_KEYPATHS

    def test_a_write_run_does_not_change_a_still_records_key_paths(self, tree: Tree):
        unplant(tree)
        bare = by_path(tree.cli("--json").json())
        still = str(tree.media / STILLS_THAT_MATCH[0])

        written = by_path(tree.cli("--json", "--write").json())

        assert key_paths(written[still]) == key_paths(bare[still])


class TestOneRunForStillsAndVideo:
    def test_the_dry_run_is_exact_across_both_paths_and_both_write_sets(self, tree: Tree):
        dry = by_path(tree.cli("--json", "--video").json())
        planned = {p: f for p, f in dry.items() if f["variant"] == "Planned"}

        written = tree.cli("--json", "--video", "--write")

        files = by_path(written.json())
        geotagged = {p: f for p, f in files.items() if f["variant"] == "Geotagged"}
        assert written.returncode == 0
        assert set(geotagged) == set(planned) and len(planned) == 5
        for path, outcome in planned.items():
            for key in ("position", "via", "provenance", "instant"):
                assert geotagged[path][key] == outcome[key], (path, key)
        clip = tags(tree.media / CLIP, "-Keys:GPSCoordinates")["GPSCoordinates"].split()
        assert float(clip[0]) == pytest.approx(planned[str(tree.media / CLIP)]["position"]["lat"])

    def test_one_audit_record_one_report_one_review_folder_and_a_quiet_third_pass(
        self, tree: Tree, assets
    ):
        clone(assets.mp4_zoneless_rewrap, tree.media / "drone" / "rewrap.MP4")

        first = tree.cli("--video", "--write", "--json")
        second = tree.cli("--video", "--write", "--json")
        third_run = tree.cli("--video", "--write", "--json")

        runs = sorted((tree.review_root() / "runs").glob("*.json"))
        [intent_record, again, quiet] = (json.loads(r.read_text()) for r in runs)
        assert first.returncode == second.returncode == third_run.returncode == 1
        assert quiet["intended"] == [] and quiet["phase"] == "complete"
        assert intent_record["phase"] == again["phase"] == "complete"
        by_via = {e["via"]: e["provenance"]["kind"] for e in intent_record["intended"]}
        assert by_via == {"INTERPOLATED": "Interpolated", "CENTROID": "Centroid"}
        assert len(intent_record["intended"]) == 5
        assert {Path(c["path"]).name for c in intent_record["conclusions"]} == {
            Path(e["path"]).name for e in intent_record["intended"]
        }
        buckets = {
            p.relative_to(tree.review_root() / "_unprocessed").parts[0]
            for p in (tree.review_root() / "_unprocessed").rglob("*")
            if p.is_symlink()
        }
        assert {"no-track-coverage", "unverified-time-semantics"} <= buckets
        assert again["intended"] == []
        third = by_path(third_run.json())
        assert {third[str(tree.media / rel)]["reason"] for rel in (*STILLS_THAT_MATCH, CLIP)} == {
            "POSITION_PRESENT"
        }

    def test_the_via_breakout_is_total_on_a_tree_producing_all_three_values(self, tree: Tree):
        result = tree.cli("--json", "--video", "--extend", "90m")

        summary = result.json()["summary"]
        all_three = {"INTERPOLATED": 4, "EXTENDED": 2, "CENTROID": 1}
        assert summary["coverage"]["by_via"] == all_three
        assert summary["coverage"]["matched"] == sum(all_three.values())
        assert summary["success_by_via"]["Planned"] == all_three
        assert (
            sum(summary["success_by_via"]["Planned"].values())
            == (summary["variant_counts"]["Planned"])
        )
        assert summary["sensitivity"]["excluded"]["centroid"] == 1
        assert summary["sensitivity"]["excluded"]["extended"] == 2
        text = tree.cli("--video", "--extend", "90m").stdout
        assert "Coverage: 7/7 = 100.0 % (INTERPOLATED 4, EXTENDED 2, CENTROID 1)" in text

    def test_a_clip_has_no_independent_cross_check_by_construction(self, tree: Tree):
        # exiftool's own -geotag places a file from a GPX instant; the telemetry path derives a
        # position from the clip's own samples, which that machinery cannot produce. Asserting the
        # one fact that makes the asymmetry stated rather than silent: the clip is placed by
        # CENTROID and by no GPX bracket.
        record = by_path(tree.cli("--json", "--video").json())[str(tree.media / CLIP)]

        assert record["via"] == "CENTROID" and record["provenance"]["kind"] == "Centroid"


class TestExitStatusOverAMixedTree:
    def test_coverage_misses_and_hidden_entries_alone_exit_zero(self, tree: Tree):
        result = tree.cli("--video")

        assert result.returncode == 0

    def test_one_zone_less_rewrap_makes_the_same_tree_exit_one(self, tree: Tree, assets):
        clone(assets.mp4_zoneless_rewrap, tree.media / "drone" / "rewrap.MP4")

        result = tree.cli("--video", "--json")

        failed = [f for f in result.json()["files"] if f["variant"] == "Failed"]
        assert result.returncode == 1
        assert [f["reason"] for f in failed] == ["TIME_SEMANTICS_UNVERIFIED"]

    def test_a_target_whose_volume_cannot_hold_it_makes_the_same_tree_exit_one(self, tree: Tree):
        from gps_sync.space import check_space

        def refusing_the_clip(planned, *, keep_original):
            clip = str(tree.media / CLIP)
            writable, refused = check_space(
                [t for t in planned if t.path != clip],
                keep_original=keep_original,
            )
            refused[clip] = check_space(
                [t for t in planned if t.path == clip],
                keep_original=keep_original,
                free_of=lambda _path: 0,
            )[1][clip]
            return writable, refused

        code, record = library_run(tree, space_check=refusing_the_clip, write=True, video=True)

        files = by_path(record)
        assert code == 1
        assert files[str(tree.media / CLIP)]["reason"] == "WRITE_INSUFFICIENT_SPACE"
        assert record["summary"]["variant_counts"]["Geotagged"] == 4
        [audit] = (json.loads(p.read_text()) for p in (tree.review_root() / "runs").glob("*.json"))
        assert {c["path"] for c in audit["conclusions"]} == {e["path"] for e in audit["intended"]}

    def test_a_target_whose_position_was_refused_makes_the_same_tree_exit_one(self, tree: Tree):
        target = tree.media / "day3" / "DSCF8526.RAF"
        transport = Injecting(target, "-GPSLatitude=", "-GPSLatitude=notanumber")

        code, record = library_run(tree, injecting=transport, write=True, video=True)

        assert code == 1
        assert by_path(record)[str(target)]["reason"] == "WRITE_PARTIAL"
        assert "GPSLatitude" not in tags(target, "-GPS:GPSLatitude")

    def test_a_target_whose_altitude_alone_was_refused_exits_one_although_it_carries_its_position(
        self, tree: Tree
    ):
        target = tree.media / "day3" / "DSCF8526.RAF"
        transport = Injecting(target, "-GPSAltitude=", "-GPSAltitude=notanumber")

        code, record = library_run(tree, injecting=transport, write=True, video=True)

        stored = tags(target, "-GPS:GPSLatitude", "-GPS:GPSAltitude")
        assert code == 1
        assert by_path(record)[str(target)]["reason"] == "WRITE_INCOMPLETE"
        assert "GPSLatitude" in stored and "GPSAltitude" not in stored
