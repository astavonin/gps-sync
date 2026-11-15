"""Orchestrator-level behaviour against a substituted transport."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from gps_sync import audit as audit_module
from gps_sync.audit import AuditWriter, find_incomplete_records, record_name
from gps_sync.errors import ToolFailure
from gps_sync.transport import ReadResult, TransportError
from tests.conftest import FIXED_NOW, World, track_text
from tests.support import FakeTransport, batch, marked, raf_json

APPLIED = "    1 image files updated"
REFUSED = "    1 files failed condition"
ERROR_COUNTS = ("    0 image files updated", "    1 files weren't updated due to errors")


def runs_dir(world: World) -> Path:
    return world.review_root() / "runs"


def records_in(world: World) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(runs_dir(world).glob("*.json"))]


def digest_tree(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def block_batch(paths, kinds):
    """Build a write batch whose blocks follow ``kinds``: applied, refused, error or missing."""
    stdout, stderr = [], []
    for path, kind in zip(paths, kinds, strict=True):
        if kind == "missing":
            continue
        lines = {"applied": (APPLIED,), "refused": (REFUSED,), "error": ERROR_COUNTS}[kind]
        stdout.append(marked(path, *lines))
        stderr.append(
            marked(path, f"Error: Error creating file: {path}_exiftool_tmp")
            if kind == "error"
            else marked(path)
        )
    return batch(stdout, stderr, 1 if "error" in kinds else 0)


class TestModeEquivalence:
    def test_every_refusal_is_reported_identically_in_dry_run_and_write_mode(self, tmp_path):
        def refusals(write: bool) -> dict[str, dict]:
            world = World(tmp_path / ("write" if write else "dry"))
            world.media.mkdir(parents=True)
            world.tracks.mkdir()
            (world.tracks / "walk.gpx").write_text(track_text())
            world.home.mkdir()
            world.add("tagged.RAF", raf_json(gps="42.5 78.25"))
            world.add("zero.RAF", raf_json(gps="0 0"))
            world.add("broken.RAF", raf_json(Error="File format error"))
            world.add("late.RAF", raf_json(dto="2026:10:26 11:00:00"))
            world.add("notime.RAF", raf_json(dto="", offset=None))
            world.add("odd.ORF", raf_json(file_type="ORF"))
            world.add("ok.RAF")
            result = world.run(world.options(json=True, write=write), world.applying())
            records = json.loads(result.out)["files"]
            return {
                Path(r["path"]).name: {k: v for k, v in r.items() if k != "path"}
                for r in records
                if r["variant"] not in ("Planned", "Geotagged")
            }

        dry, written = refusals(False), refusals(True)

        assert set(dry) >= {"tagged.RAF", "zero.RAF", "broken.RAF", "late.RAF", "notime.RAF"}
        assert dry == written


class TestDryRun:
    def test_dry_run_never_invokes_the_writer_and_writes_no_audit_record(self, world):
        world.add("a.RAF")
        transport = world.transport()  # a write call would raise AssertionError

        result = world.run(transport=transport)

        assert result.code == 0
        assert transport.write_calls == []
        assert list(runs_dir(world).iterdir()) == []
        assert "Planned: 1" in result.out

    def test_a_dry_run_changes_no_media_byte_or_mtime_and_adds_nothing_to_the_tree(self, world):
        world.add("a.RAF")
        world.add("sub/b.RAF", raf_json(offset=None))
        world.add("c.RAF", raf_json(dto="2026:10:26 11:00:00"))
        before = digest_tree(world.media)
        names_before = sorted(str(p) for p in world.media.rglob("*"))

        world.run()

        assert digest_tree(world.media) == before
        assert sorted(str(p) for p in world.media.rglob("*")) == names_before

    def test_a_dry_run_removes_its_argfile_and_temp_directory(self, world, monkeypatch):
        world.add("a.RAF")
        seen: list[Path] = []
        transport = world.transport()
        original = transport.read

        def spy(argfile):
            seen.append(argfile)
            assert argfile.exists()
            return original(argfile)

        transport.read = spy  # type: ignore[method-assign]
        world.run(transport=transport)

        assert seen and not seen[0].exists() and not seen[0].parent.exists()

    def test_verbose_retains_both_argfiles_and_prints_paths_that_resolve(self, world):
        world.add("a.RAF")
        seen: list[Path] = []
        transport = world.transport(
            on_write=lambda argfile: (
                seen.append(argfile),
                block_batch([str((world.media / "a.RAF").resolve())], ["applied"]),
            )[1]
        )
        original = transport.read
        transport.read = lambda argfile: (seen.append(argfile), original(argfile))[1]  # type: ignore[method-assign]

        result = world.run(world.options(write=True, verbose=True), transport)

        assert len(seen) == 2
        assert all(p.exists() for p in seen)
        for path in seen:
            assert str(path) in result.err
        # Outside the media tree and the run-artifact root.
        assert not seen[0].is_relative_to(world.media)
        assert not seen[0].is_relative_to(world.review_root())

    def test_without_verbose_the_write_argfile_is_also_removed(self, world):
        target = world.add("a.RAF")
        seen: list[Path] = []

        def on_write(argfile):
            seen.append(argfile)
            return block_batch([target], ["applied"])

        world.run(world.options(write=True), world.transport(on_write=on_write))

        assert not seen[0].exists()


class TestWritePhaseContract:
    def two_targets(self, world):
        return world.add("a.RAF"), world.add("b.RAF")

    def test_the_intent_record_exists_and_is_synced_before_the_writer_is_invoked(self, world):
        a, b = self.two_targets(world)
        observed: dict[str, object] = {}

        def on_write(argfile):
            [record] = records_in(world)
            observed["phase"] = record["phase"]
            observed["paths"] = [e["path"] for e in record["intended"]]
            observed["coordinates"] = [e["position"] for e in record["intended"]]
            return block_batch([a, b], ["applied", "applied"])

        world.run(world.options(write=True), world.transport(on_write=on_write))

        assert observed["phase"] == "intent"
        assert observed["paths"] == [a, b]
        assert all({"lat", "lon"} <= set(c) for c in observed["coordinates"])

    def test_intent_syncs_the_file_and_its_directory_before_the_spawn(self, world, monkeypatch):
        a, _ = self.two_targets(world)
        synced: list[str] = []
        real_fsync = os.fsync
        monkeypatch.setattr(os, "fsync", lambda fd: (synced.append("fsync"), real_fsync(fd))[1])

        def on_write(argfile):
            assert len(synced) >= 2
            return block_batch([a, _], ["applied", "applied"])

        world.run(world.options(write=True), world.transport(on_write=on_write))

        assert synced

    def test_a_mixed_batch_yields_five_conclusions_exit_one_and_a_complete_record(self, world):
        paths = [world.add(f"{n}.RAF") for n in "ABCDE"]
        kinds = ["applied", "applied", "refused", "error", "missing"]
        transport = world.transport(on_write=lambda _argfile: block_batch(paths, kinds))

        result = world.run(world.options(write=True), transport)

        [record] = records_in(world)
        assert result.code == 1
        assert record["phase"] == "complete"
        conclusions = {Path(c["path"]).name: c for c in record["conclusions"]}
        assert conclusions["A.RAF"]["variant"] == "Geotagged"
        assert conclusions["B.RAF"]["variant"] == "Geotagged"
        assert conclusions["C.RAF"]["reason"] == "POSITION_APPEARED"
        assert "position" not in conclusions["C.RAF"]
        assert conclusions["D.RAF"]["reason"] == "WRITE_ERROR"
        assert conclusions["E.RAF"]["reason"] == "WRITE_UNATTRIBUTED"
        assert len(record["conclusions"]) == 5
        assert "Geotagged: 2" in result.out

    def test_a_whole_process_failure_leaves_every_path_unattributed_and_exits_one(self, world):
        paths = [world.add(f"{n}.RAF") for n in "AB"]

        def on_write(_argfile):
            raise TransportError("exiftool killed")

        result = world.run(world.options(write=True), world.transport(on_write=on_write))

        assert result.code == 1
        [record] = records_in(world)
        assert {c["reason"] for c in record["conclusions"]} == {"WRITE_UNATTRIBUTED"}
        assert record["phase"] == "complete"
        assert set(paths) == {c["path"] for c in record["conclusions"]}

    def test_a_transport_that_raises_leaves_an_intent_record_the_next_run_names_first(self, world):
        a, b = self.two_targets(world)

        def on_write(_argfile):
            raise RuntimeError("power cut after two applied")

        with pytest.raises(RuntimeError):
            world.run(world.options(write=True), world.transport(on_write=on_write))
        [record] = records_in(world)
        assert record["phase"] == "intent" and len(record["intended"]) == 2

        follow_up = world.run()

        first_line = follow_up.out.splitlines()[0]
        assert first_line.startswith("WARNING: incomplete prior run")
        assert record_name(FIXED_NOW, os.getpid()) in first_line
        assert "lists 2 path(s)" in first_line

    def test_a_failing_completion_rewrite_is_exit_three_with_the_tally_and_intent_record_intact(
        self, world, monkeypatch
    ):
        a, _ = self.two_targets(world)
        monkeypatch.setattr(
            AuditWriter,
            "write_complete",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
        )

        result = world.run(
            world.options(write=True),
            world.transport(on_write=lambda _a: block_batch([a, _], ["applied", "applied"])),
        )

        assert result.code == 3
        assert "Geotagged: 2" in result.out
        assert "intent-phase record remains" in result.out
        [record] = records_in(world)
        assert record["phase"] == "intent"

    def test_a_write_run_with_nothing_to_do_still_writes_a_complete_record(self, world):
        world.add("a.RAF", raf_json(gps="42.5 78.25"))

        result = world.run(world.options(write=True))

        assert result.code == 0
        [record] = records_in(world)
        assert record["phase"] == "complete"
        assert record["intended"] == [] and record["conclusions"] == []

    def test_an_unwritable_intent_record_aborts_before_the_writer_is_spawned_exit_two(
        self, world, monkeypatch
    ):
        world.add("a.RAF")
        monkeypatch.setattr(
            AuditWriter, "write_intent", lambda *_a, **_k: (_ for _ in ()).throw(OSError("ro"))
        )
        transport = world.transport()

        result = world.run(world.options(write=True), transport)

        assert result.code == 2
        assert transport.write_calls == []
        assert result.out == ""

    def test_an_unintended_conclusion_alone_raises_the_exit_code_to_one(self, world):
        target = world.add("a.RAF")

        def on_write(_argfile):
            good = block_batch([target], ["applied"])
            stranger = "/elsewhere/X.RAF"
            return batch(
                [*good.stdout_blocks, marked(stranger, APPLIED)],
                [*good.stderr_blocks, marked(stranger)],
            )

        result = world.run(world.options(write=True), world.transport(on_write=on_write))

        assert result.code == 1
        assert "WRITE_UNINTENDED_PATH" in result.out and "/elsewhere/X.RAF" in result.out
        [record] = records_in(world)
        assert record["reconciliation"]["unintended_conclusions"][0]["path"] == "/elsewhere/X.RAF"

    def test_a_symlink_target_aborts_the_run_exit_two_before_any_write(self, world, monkeypatch):
        world.add("a.RAF")
        # Discovery cannot produce this; force the guarantee to fail at the writer boundary.
        monkeypatch.setattr(os.path, "islink", lambda p: str(p).endswith("a.RAF"))
        transport = world.transport()

        result = world.run(world.options(write=True), transport)

        assert result.code == 2 and "symlink" in result.err
        assert transport.write_calls == []
        assert records_in(world) == []

    def test_the_write_flag_never_reaches_classification(self, world):
        target = world.add("a.RAF")
        dry = world.run()
        transport = world.transport(on_write=lambda _a: block_batch([target], ["applied"]))

        written = world.run(world.options(write=True), transport)

        dry_files = {
            f["path"]: f for f in json.loads(world.run(world.options(json=True)).out)["files"]
        }
        assert dry.code == written.code == 0
        assert dry_files[target]["variant"] == "Planned"
        assert "Geotagged: 1" in written.out

    def test_the_write_run_applies_exactly_the_planned_files_with_the_dry_run_coordinates(
        self, world
    ):
        a, b = self.two_targets(world)
        planned = {
            f["path"]: f["position"]
            for f in json.loads(world.run(world.options(json=True)).out)["files"]
        }
        transport = world.transport(on_write=lambda _a: block_batch([a, b], ["applied", "applied"]))

        result = world.run(world.options(write=True, json=True), transport)

        files = {f["path"]: f for f in json.loads(result.out)["files"]}
        assert {p: files[p]["position"] for p in (a, b)} == planned
        assert {files[p]["variant"] for p in (a, b)} == {"Geotagged"}
        [record] = records_in(world)
        assert [e["position"] for e in record["intended"]] == [planned[a], planned[b]]


class TestToolLevelFailures:
    def test_a_read_that_misses_a_requested_path_aborts_with_no_per_file_outcome(self, world):
        world.add("a.RAF")
        world.add("b.RAF")
        transport = FakeTransport()
        transport.read = (
            lambda argfile: ReadResult(  # type: ignore[method-assign]
                {p: {"SourceFile": p} for p in argfile.read_text().splitlines()[:1]}
            )
        )

        result = world.run(transport=transport)

        assert result.code == 2
        assert result.out == ""
        assert "did not return exactly the requested records" in result.err

    def test_a_read_returning_an_unrequested_record_also_aborts(self, world):
        world.add("a.RAF")
        transport = FakeTransport()
        transport.read = lambda argfile: ReadResult(  # type: ignore[method-assign]
            {"/not/requested.RAF": {"SourceFile": "/not/requested.RAF"}}
        )

        assert world.run(transport=transport).code == 2

    def test_a_record_carrying_a_diagnostic_reaches_the_gate_and_is_a_per_file_failure(self, world):
        world.add("a.RAF", raf_json(Error="Unknown file type"))

        result = world.run()

        assert result.code == 1
        assert "METADATA_INDETERMINATE" in result.out and "Unknown file type" in result.out

    def test_a_below_minimum_version_is_rejected_with_an_actionable_message(self, world):
        world.add("a.RAF")

        result = world.run(transport=world.transport(version="12.76"))

        assert result.code == 2
        assert "13.55" in result.err and "upgrade" in result.err
        assert result.out == ""

    @pytest.mark.parametrize("version", ["not-a-version", "", "13.55-beta"])
    def test_an_unparseable_version_is_rejected(self, world, version):
        assert world.run(transport=world.transport(version=version)).code == 2

    def test_newer_versions_are_accepted(self, world):
        world.add("a.RAF")

        assert world.run(transport=world.transport(version="13.60")).code == 0

    def test_unparseable_gpx_exits_two_before_any_outcome(self, world):
        world.add("a.RAF")
        (world.tracks / "walk.gpx").write_text("<gpx><trk>")

        result = world.run()

        assert result.code == 2 and result.out == "" and "walk.gpx" in result.err

    def test_a_gpx_without_timed_points_exits_two_not_a_universal_unmatchable(self, world):
        world.add("a.RAF")
        (world.tracks / "walk.gpx").write_text(
            '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
            '<trkpt lat="42" lon="77"/></trkseg></trk></gpx>'
        )

        result = world.run()

        assert result.code == 2 and result.out == ""

    def test_missing_tracks_and_media_roots_exit_two(self, world, tmp_path):
        assert world.run(world.options(tracks=tmp_path / "nope")).code == 2
        assert world.run(world.options(media=tmp_path / "nope")).code == 2

    def test_tool_failures_and_a_reported_tally_are_mutually_exclusive_in_one_run(self, world):
        world.add("a.RAF")
        (world.tracks / "walk.gpx").write_text("not xml")

        result = world.run()

        assert result.code == 2
        assert "Planned" not in result.out and "Outcomes" not in result.out

    def test_a_segment_identity_collision_is_exit_two_before_discovery(self, world, monkeypatch):
        from gps_sync import orchestrator

        world.add("a.RAF")
        monkeypatch.setattr(
            orchestrator, "build_index", lambda _s: (_ for _ in ()).throw(ToolFailure("collision"))
        )

        result = world.run()

        assert result.code == 2 and "collision" in result.err and result.out == ""

    def test_a_path_with_a_line_break_is_refused_rather_than_passed_to_exiftool(self, world):
        bad = world.media / "a\nb.RAF"
        bad.write_bytes(b"x")
        transport = world.transport()

        result = world.run(transport=transport)

        assert result.code == 1
        assert "line break" in result.out
        assert transport.read_calls == []


class TestAuditNaming:
    def test_the_name_is_the_documented_utc_basic_form_with_the_pid(self):
        assert record_name(FIXED_NOW, 41288) == "20261115T120000Z-41288.json"

    def test_records_from_different_runs_sort_chronologically(self):
        from datetime import timedelta

        names = [
            record_name(FIXED_NOW + timedelta(seconds=s), pid)
            for s, pid in [(30, 9), (0, 100), (5, 2)]
        ]

        assert sorted(names) == [names[1], names[2], names[0]]

    def test_a_same_second_pair_with_different_pids_does_not_collide(self, tmp_path):
        first = AuditWriter(tmp_path, lambda: FIXED_NOW, pid=1)
        second = AuditWriter(tmp_path, lambda: FIXED_NOW, pid=2)

        assert first.path != second.path

    def test_the_clock_is_injected_and_names_the_record(self, world):
        world.add("a.RAF")

        world.run(world.options(write=True), world.applying())

        assert [p.name for p in runs_dir(world).glob("*.json")] == [
            record_name(FIXED_NOW, os.getpid())
        ]

    def test_the_tool_never_deletes_a_prior_record(self, world):
        world.add("a.RAF", raf_json(gps="1 2"))
        world.run(world.options(write=True))
        prior = next(runs_dir(world).glob("*.json"))
        prior.write_text(json.dumps({"phase": "complete"}))

        world.run(world.options(write=True), clock=lambda: FIXED_NOW.replace(second=5))

        assert prior.read_text() == json.dumps({"phase": "complete"})
        assert len(list(runs_dir(world).glob("*.json"))) == 2

    def test_a_record_still_in_its_intent_phase_is_found_and_a_complete_one_is_not(self, tmp_path):
        (tmp_path / "1.json").write_text(
            json.dumps({"phase": "intent", "created": "t", "intended": [{}, {}]})
        )
        (tmp_path / "2.json").write_text(json.dumps({"phase": "complete"}))
        (tmp_path / "3.json").write_text("garbage")

        [found] = find_incomplete_records(tmp_path)

        assert (found.name, found.intended_paths) == ("1.json", 2)

    def test_audit_module_declares_the_two_phases(self):
        assert (audit_module.INTENT, audit_module.COMPLETE) == ("intent", "complete")


def test_a_rerun_after_a_write_reports_every_written_file_protected_and_rate_not_applicable(world):
    a = world.add("a.RAF")
    first = world.run(
        world.options(write=True),
        world.transport(on_write=lambda _a: block_batch([a], ["applied"])),
    )
    world.records[a] = raf_json(gps="42.0003 77.0")

    second = world.run(world.options(write=True))

    assert first.code == second.code == 0
    assert "Protected: 1" in second.out
    assert "Coverage: not applicable" in second.out
    assert "healthy" in second.out
    records = records_in(world)
    assert records[-1]["intended"] == []
