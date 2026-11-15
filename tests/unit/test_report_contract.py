"""Remaining reporter, review-writer and discovery-level contract points."""

import json
import os
from pathlib import Path

from gps_sync.model import (
    Failed,
    FailedReason,
    Geotagged,
    Ignored,
    IgnoredReason,
    Interpolated,
    Planned,
    Position,
    Protected,
    ProtectedReason,
    Unmatchable,
    UnmatchableReason,
    Via,
)
from gps_sync.registry import REGISTRY
from gps_sync.report import render_report
from gps_sync.review import rebuild
from gps_sync.summary import summarise
from tests.conftest import World, track_text
from tests.support import at, raf_json, run_info

SEG = "t.gpx#trk0seg0"
PAYLOAD = {"via": Via.INTERPOLATED, "provenance": Interpolated(SEG, 5.0)}


def one_of_every_candidate_reason(media: Path):
    def p(name: str) -> str:
        return str(media / name)

    return [
        Protected(p("present.RAF"), ProtectedReason.POSITION_PRESENT, position=Position(1, 2)),
        Protected(p("zero.RAF"), ProtectedReason.ZERO_POSITION, position=Position(0.0, 0.0)),
        Protected(p("appeared.RAF"), ProtectedReason.POSITION_APPEARED, **PAYLOAD),
        *(
            Unmatchable(p(f"{reason.name}.RAF"), at(0), reason, SEG, 1.0)
            for reason in UnmatchableReason
        ),
        Failed(p("time.RAF"), FailedReason.TIME_UNRESOLVABLE),
        Failed(p("meta.RAF"), FailedReason.METADATA_INDETERMINATE),
        Failed(p("minor.RAF"), FailedReason.UNRECOGNISED_MINOR_WARNING),
        Failed(p("container.RAF"), FailedReason.WRITE_UNSUPPORTED_CONTAINER, **PAYLOAD),
        Failed(p("werror.RAF"), FailedReason.WRITE_ERROR, **PAYLOAD),
        Failed(p("unattr.RAF"), FailedReason.WRITE_UNATTRIBUTED, **PAYLOAD),
        Planned(p("planned.RAF"), at(0), Position(1, 2), Via.INTERPOLATED, Interpolated(SEG, 5.0)),
        Geotagged(p("done.RAF"), at(0), Position(1, 2), Via.INTERPOLATED, Interpolated(SEG, 5.0)),
        *(Ignored(p(f"{r.name}.x"), r) for r in IgnoredReason),
    ]


def test_the_review_writer_links_exactly_the_registry_subfolders_and_nothing_else(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    outcomes = one_of_every_candidate_reason(media)

    report = rebuild(tmp_path / "rev" / "_unprocessed", media, outcomes)

    assert not report.incomplete
    linked = {
        p.relative_to(tmp_path / "rev" / "_unprocessed").parts[0]
        for p in (tmp_path / "rev" / "_unprocessed").rglob("*")
        if p.is_symlink()
    }
    expected = {r.subfolder for r in REGISTRY.values() if r.subfolder}
    assert linked == expected
    for key, row in REGISTRY.items():
        if row.subfolder is None:
            continue
        names = [
            p.name
            for p in (tmp_path / "rev" / "_unprocessed" / row.subfolder).rglob("*")
            if p.is_symlink()
        ]
        assert len(names) >= 1, key


def test_the_reporter_handles_every_variant_and_every_reason_at_once(tmp_path):
    outcomes = one_of_every_candidate_reason(tmp_path)

    text = render_report(outcomes, summarise(outcomes), run_info())

    for key, row in REGISTRY.items():
        if row.variant != "run-level":
            assert row.label in text, key


def test_the_report_names_skipped_symlinks_duplicates_and_the_pruned_path_once(tmp_path):
    outcomes = [
        Ignored("/m/link.RAF", IgnoredReason.SYMLINK, "/m/real.RAF"),
        Ignored("/m/b.RAF", IgnoredReason.DUPLICATE_INODE, "/m/a.RAF"),
        Ignored("/m/notes.txt", IgnoredReason.NOT_A_STILL),
    ]

    text = render_report(
        outcomes, summarise(outcomes), run_info(pruned=("/m/review/_unprocessed",))
    )

    assert "/m/link.RAF" in text and "/m/real.RAF" in text
    assert "/m/b.RAF" in text and "/m/a.RAF" in text
    assert text.count("pruned from discovery: /m/review/_unprocessed") == 1
    assert "notes.txt" not in text


def test_per_file_gpx_drop_counts_appear_in_the_report_and_the_json(tmp_path):
    world = World(tmp_path)
    world.media.mkdir()
    world.tracks.mkdir()
    world.home.mkdir()
    (world.tracks / "walk.gpx").write_text(
        track_text()
        .replace('lat="42.000100"', 'lat="91"')
        .replace("<time>2026-10-26T04:00:30Z</time>", "")
    )
    world.add("a.RAF", raf_json())

    report = world.run()
    as_json = json.loads(world.run(world.options(json=True)).out)

    assert "coordinate-out-of-range 1" in report.out
    assert "time-absent 1" in report.out
    [track] = as_json["tracks"]
    assert track["dropped"] == {"coordinate-out-of-range": 1, "time-absent": 1}


def test_a_media_root_that_is_a_symlink_is_resolved_and_scanned(tmp_path):
    world = World(tmp_path)
    world.media.mkdir()
    world.tracks.mkdir()
    world.home.mkdir()
    (world.tracks / "walk.gpx").write_text(track_text())
    path = world.add("a.RAF")
    alias = tmp_path / "alias"
    alias.symlink_to(world.media, target_is_directory=True)

    result = world.run(world.options(media=alias, json=True))

    files = json.loads(result.out)["files"]
    assert [f["path"] for f in files] == [path]
    assert files[0]["variant"] == "Planned"


def test_a_directory_symlink_inside_the_tree_is_reported_not_silently_empty(tmp_path):
    world = World(tmp_path)
    world.media.mkdir()
    world.tracks.mkdir()
    world.home.mkdir()
    (world.tracks / "walk.gpx").write_text(track_text())
    world.add("real/a.RAF")
    os.symlink(world.media / "real", world.media / "linked", target_is_directory=True)

    result = world.run()

    assert "linked" in result.out and "symlink skipped" in result.out
    assert "Planned: 1" in result.out


def test_the_intent_record_contains_only_planned_entries_with_their_coordinates(tmp_path):
    from tests.unit.test_run_contract import block_batch, records_in

    world = World(tmp_path)
    world.media.mkdir()
    world.tracks.mkdir()
    world.home.mkdir()
    (world.tracks / "walk.gpx").write_text(track_text())
    planned = world.add("planned.RAF")
    world.add("protected.RAF", raf_json(gps="1 2"))
    world.add("late.RAF", raf_json(dto="2026:10:26 14:00:00"))
    seen: list[dict] = []

    def on_write(_argfile):
        seen.extend(records_in(world)[0]["intended"])
        return block_batch([planned], ["applied"])

    world.run(world.options(write=True), world.transport(on_write=on_write))

    assert [e["path"] for e in seen] == [planned]
    assert {"lat", "lon"} <= set(seen[0]["position"]) and seen[0]["via"] == "INTERPOLATED"
    [complete] = records_in(world)
    assert complete["conclusions"][0]["variant"] == "Geotagged"
    assert complete["conclusions"][0]["position"] == seen[0]["position"]


def test_a_skipped_hard_link_is_accompanied_by_the_consequence_note():
    outcomes = [Ignored("/m/b.RAF", IgnoredReason.DUPLICATE_INODE, "/m/a.RAF")]

    text = render_report(outcomes, summarise(outcomes), run_info())

    assert "other links keep pointing at the pre-write content" in text
    assert "pre-write content" not in render_report([], summarise([]), run_info())
