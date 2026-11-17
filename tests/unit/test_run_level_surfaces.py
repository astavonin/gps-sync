"""Every run-level figure on every surface a run produces: report, JSON record and audit record."""

import dataclasses
import json

import pytest

from gps_sync.model import RunLevelReason, Unmatchable, UnmatchableReason
from gps_sync.summary import run_level_figures
from gps_sync.transport import WriteBatchResult
from gps_sync.writer import Reconciliation, UnintendedConclusion
from tests.support import at, marked
from tests.unit.test_run_contract import APPLIED, block_batch, records_in
from tests.unit.test_video_run import GOOD, add_clip
from tests.unit.test_writer import planned


class TestTheBlockCountReconciliationFigure:
    def three(self, world):
        return [world.add(f"{n}.RAF") for n in "ABC"]

    def shortfall(self, paths):
        return lambda _argfile: block_batch(paths, ["applied", "missing", "missing"])

    def test_the_figure_counts_affected_paths_and_the_audit_verdict_carries_it(self, world):
        paths = self.three(world)

        result = world.run(
            world.options(write=True), world.transport(on_write=self.shortfall(paths))
        )

        [record] = records_in(world)
        assert result.code == 1
        assert record["verdict"]["run_level"] == {"BLOCK_COUNT_UNRECONCILED": 2}
        assert record["reconciliation"]["unreconciled_paths"] == paths[1:]
        [shape] = record["reconciliation"]["unreconciled_blocks"]
        assert shape.startswith("shortfall: 2 intended path(s) have no block on a stream: ")
        assert all(p in shape for p in paths[1:])

    def test_the_json_record_carries_the_figure_the_shape_and_the_paths(self, world):
        paths = self.three(world)

        result = world.run(
            world.options(write=True, json=True), world.transport(on_write=self.shortfall(paths))
        )

        record = json.loads(result.out)
        assert record["run_level"] == {"BLOCK_COUNT_UNRECONCILED": 2}
        assert record["reconciliation"]["unreconciled_paths"] == paths[1:]
        assert len(record["reconciliation"]["unreconciled_blocks"]) == 1

    def test_the_human_report_names_the_shape_and_its_paths(self, world):
        paths = self.three(world)

        text = world.run(
            world.options(write=True), world.transport(on_write=self.shortfall(paths))
        ).out

        assert "Run-level diagnostic: " in text and "BLOCK_COUNT_UNRECONCILED (" in text
        line = next(
            x for x in text.splitlines() if x.lstrip().startswith("BLOCK_COUNT_UNRECONCILED")
        )
        assert "shortfall: 2 intended path(s) have no block on a stream" in line
        assert paths[1] in line and paths[2] in line

    def test_a_trailing_run_names_no_path_and_counts_as_one(self, world):
        [a] = [world.add("A.RAF")]

        def on_write(_argfile):
            return WriteBatchResult(
                (marked(a, APPLIED),), (marked(a),), 0, stdout_tail=("half a block",)
            )

        result = world.run(world.options(write=True, json=True), world.transport(on_write=on_write))

        record = json.loads(result.out)
        assert record["run_level"] == {"BLOCK_COUNT_UNRECONCILED": 1}
        assert record["reconciliation"]["unreconciled_paths"] == []
        assert record["reconciliation"]["unreconciled_blocks"] == [
            "trailing run of unterminated lines"
        ]

    def test_a_clean_batch_leaves_the_audit_verdict_run_level_empty(self, world):
        a = world.add("A.RAF")

        world.run(
            world.options(write=True),
            world.transport(on_write=lambda _f: block_batch([a], ["applied"])),
        )

        [record] = records_in(world)
        assert record["verdict"]["run_level"] == {}
        assert "unreconciled_blocks" not in record["reconciliation"]


class TestTheOtherRunLevelFiguresReachTheAuditVerdict:
    def test_an_omitted_tracks_root_is_counted_in_the_audit_verdict(self, world):
        add_clip(world, "b.MP4", {":HandlerType": "vide"})

        world.run(world.options(write=True, video=True, tracks=None), world.transport())

        [record] = records_in(world)
        assert record["verdict"]["run_level"] == {"NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES": 1}

    def test_a_dropped_unrepresentable_elevation_is_counted_in_the_audit_verdict(self, world):
        a = add_clip(world)
        huge = {**GOOD, ":AbsoluteAltitude": 1.7e308, "Copy1:AbsoluteAltitude": 1.7e308}

        world.run(
            world.options(write=True, video=True),
            world.transport(
                telemetry=lambda p: huge, on_write=lambda _f: block_batch([a], ["applied"])
            ),
        )

        [record] = records_in(world)
        assert record["verdict"]["run_level"] == {"ELEVATION_NOT_REPRESENTABLE": 1}


TRIGGERS = {
    RunLevelReason.WRITE_UNINTENDED_PATH: (
        [],
        False,
        Reconciliation(unintended=(UnintendedConclusion("/m/x.RAF", ("1 image files updated",)),)),
        1,
    ),
    RunLevelReason.NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES: (
        [Unmatchable("/m/a.RAF", at(0), UnmatchableReason.OUTSIDE_COVERAGE)],
        False,
        None,
        1,
    ),
    RunLevelReason.ELEVATION_NOT_REPRESENTABLE: (
        [dataclasses.replace(planned("/m/a.RAF"), elevation_dropped=True)],
        True,
        None,
        1,
    ),
    RunLevelReason.BLOCK_COUNT_UNRECONCILED: (
        [],
        True,
        Reconciliation(unreconciled=("shortfall",), unreconciled_paths=("/m/a", "/m/b")),
        2,
    ),
}


class TestEveryRunLevelReasonHasAFigure:
    """§5's one field per member: a member no input can produce is declared and unreachable."""

    def test_the_trigger_table_names_every_member(self):
        assert set(TRIGGERS) == set(RunLevelReason)

    @pytest.mark.parametrize("member", list(RunLevelReason), ids=lambda m: m.value)
    def test_a_member_is_produced_by_its_own_condition_and_by_no_other_key(self, member):
        outcomes, tracks_given, reconciliation, count = TRIGGERS[member]

        figures = run_level_figures(
            outcomes, tracks_given=tracks_given, reconciliation=reconciliation
        )

        assert figures == {member.value: count}

    def test_a_run_that_triggers_nothing_has_no_figure(self):
        assert run_level_figures([], tracks_given=True, reconciliation=Reconciliation()) == {}
