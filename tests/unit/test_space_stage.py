"""The free-space stage between "planned" and "written", reached through whole runs."""

import json
import os
from pathlib import Path

from gps_sync.model import Failed, FailedReason
from gps_sync.space import SLACK_BYTES, check_space, demand
from tests.unit.test_run_contract import block_batch, records_in
from tests.unit.test_writer import planned


def refusing(*names):
    """A space check that refuses the targets whose file name is in ``names``."""

    def check(targets, *, keep_original):
        writable, refused = [], {}
        for target in targets:
            if Path(target.path).name in names:
                refused[target.path] = Failed(
                    target.path,
                    FailedReason.WRITE_INSUFFICIENT_SPACE,
                    "size 1 bytes, free 0 bytes on volume 1",
                    instant=target.instant,
                    via=target.via,
                    provenance=target.provenance,
                    bracket_speed=target.bracket_speed,
                    context=target.context,
                )
            else:
                writable.append(target)
        return writable, refused

    return check


class TestARunWhoseSpaceCheckRefusesSomeTargets:
    def test_every_intended_path_leaves_the_audit_record_with_exactly_one_conclusion(self, world):
        a, b, c = (world.add(f"{n}.RAF") for n in "ABC")
        transport = world.transport(on_write=lambda _f: block_batch([a, b], ["applied", "applied"]))

        result = world.run(world.options(write=True), transport, space_check=refusing("C.RAF"))

        [record] = records_in(world)
        intended = [e["path"] for e in record["intended"]]
        conclusions = [x["path"] for x in record["conclusions"]]
        assert intended == [a, b, c]
        assert conclusions == intended
        refusal = record["conclusions"][2]
        assert refusal["reason"] == "WRITE_INSUFFICIENT_SPACE"
        assert refusal["via"] == "INTERPOLATED"
        assert refusal["provenance"]["kind"] == "Interpolated"
        assert result.code == 1

    def test_the_writer_is_given_only_the_writable_targets(self, world):
        a, _b = world.add("A.RAF"), world.add("B.RAF")
        transport = world.transport(on_write=lambda _f: block_batch([a], ["applied"]))

        world.run(world.options(write=True), transport, space_check=refusing("B.RAF"))

        [call] = transport.write_calls
        assert [line for line in call if line.startswith("/")] == [a, a, a]

    def test_a_refusal_alone_does_not_make_the_block_counts_unreconciled(self, world):
        a = world.add("A.RAF")
        world.add("B.RAF")
        transport = world.transport(on_write=lambda _f: block_batch([a], ["applied"]))

        world.run(world.options(write=True), transport, space_check=refusing("B.RAF"))

        [record] = records_in(world)
        assert "unreconciled_blocks" not in record["reconciliation"]
        assert record["verdict"]["run_level"] == {}


class TestARunWhoseSpaceCheckRefusesEverything:
    def test_the_writer_is_never_spawned(self, world):
        world.add("A.RAF")
        world.add("B.RAF")
        transport = world.transport()  # a write call would raise AssertionError

        world.run(world.options(write=True), transport, space_check=refusing("A.RAF", "B.RAF"))

        assert transport.write_calls == []

    def test_the_exit_code_comes_from_the_per_file_terms_alone(self, world):
        world.add("A.RAF")
        world.add("B.RAF")

        result = world.run(
            world.options(write=True, json=True),
            world.transport(),
            space_check=refusing("A.RAF", "B.RAF"),
        )

        record = json.loads(result.out)
        assert result.code == 1
        assert record["summary"]["variant_counts"]["Failed"] == 2
        assert record.get("unreconciled_blocks", []) == []
        assert record["reconciliation"]["unintended_conclusions"] == []
        [audit] = records_in(world)
        assert audit["phase"] == "complete"
        assert audit["reconciliation"] == {"unintended_conclusions": [], "returncode": None}
        assert len(audit["conclusions"]) == 2
        assert "no conclusion" not in result.err


class TestAVanishedTarget:
    def test_it_is_a_per_file_failure_and_never_denies_the_others(self, tmp_path):
        gone, kept = planned(str(tmp_path / "gone.MP4"), container="MP4"), None
        real = tmp_path / "kept.MP4"
        real.write_bytes(b"x" * 10)
        kept = planned(str(real), container="MP4")

        writable, refused = check_space([gone, kept], keep_original=False)

        assert writable == [kept]
        failure = refused[gone.path]
        assert failure.reason is FailedReason.WRITE_TARGET_UNEXAMINABLE
        assert "could not be examined" in failure.detail
        assert "No such file" in failure.detail

    def test_it_keeps_the_match_payload_the_planned_outcome_carried(self, tmp_path):
        gone = planned(str(tmp_path / "gone.MP4"), container="MP4")

        _, refused = check_space([gone], keep_original=False)

        failure = refused[gone.path]
        assert (failure.instant, failure.via, failure.provenance) == (
            gone.instant,
            gone.via,
            gone.provenance,
        )
        assert failure.bracket_speed == gone.bracket_speed and failure.context == gone.context

    def test_it_counts_as_reaching_the_matcher_and_matched_in_the_coverage_figures(self):
        from gps_sync.summary import summarise

        outcomes = [planned("/m/a.RAF"), planned("/m/b.RAF")]
        dry = summarise(outcomes).coverage
        refused = Failed(
            "/m/b.RAF",
            FailedReason.WRITE_TARGET_UNEXAMINABLE,
            "gone",
            instant=outcomes[1].instant,
            via=outcomes[1].via,
            provenance=outcomes[1].provenance,
            bracket_speed=outcomes[1].bracket_speed,
            context=outcomes[1].context,
        )

        written = summarise([outcomes[0], refused]).coverage

        assert (dry.matched, dry.reached_matcher) == (2, 2)
        assert (written.matched, written.reached_matcher) == (2, 2)

    def test_the_dry_run_forecast_skips_it_instead_of_raising(self, tmp_path):
        gone = planned(str(tmp_path / "gone.MP4"), container="MP4")
        real = tmp_path / "kept.MP4"
        real.write_bytes(b"x" * 10)

        [figure] = demand([gone, planned(str(real), container="MP4")])

        assert figure.peak_bytes == 10 + SLACK_BYTES
        assert figure.retained_bytes == 10

    def test_the_write_runs_coverage_equals_the_dry_runs_when_a_matched_target_vanishes(
        self, world
    ):
        a = world.add("A.RAF")
        b = world.add("B.RAF")
        dry = json.loads(world.run(world.options(json=True)).out)["summary"]["coverage"]
        transport = world.transport(on_write=lambda _f: block_batch([b], ["applied"]))

        def vanishing(targets, *, keep_original):
            os.unlink(a)
            return check_space(targets, keep_original=keep_original)

        written = json.loads(
            world.run(world.options(json=True, write=True), transport, space_check=vanishing).out
        )

        assert (dry["matched"], dry["reached_matcher"]) == (2, 2)
        assert written["summary"]["coverage"] == dry
        [lost] = [f for f in written["files"] if f.get("reason") == "WRITE_TARGET_UNEXAMINABLE"]
        assert lost["via"] == "INTERPOLATED"

    def test_a_run_over_a_target_that_vanished_after_the_intent_record_concludes_it(self, world):
        a = world.add("A.RAF")
        b = world.add("B.RAF")
        transport = world.transport(on_write=lambda _f: block_batch([b], ["applied"]))

        def vanishing(targets, *, keep_original):
            os.unlink(a)
            return check_space(targets, keep_original=keep_original)

        result = world.run(world.options(write=True), transport, space_check=vanishing)

        [record] = records_in(world)
        assert result.code == 1 and "Traceback" not in result.err
        assert record["phase"] == "complete"
        by_name = {Path(c["path"]).name: c for c in record["conclusions"]}
        assert by_name["A.RAF"]["reason"] == "WRITE_TARGET_UNEXAMINABLE"
        assert by_name["B.RAF"]["variant"] == "Geotagged"


class TestRetainedOriginalsAccumulatePerVolume:
    SIZE = 10

    def check(self, paths, free, *, keep_original):
        targets = [planned(p) for p in paths]
        volumes = {"/a/1.RAF": 1, "/a/2.RAF": 1, "/b/1.RAF": 2}
        writable, refused = check_space(
            targets,
            keep_original=keep_original,
            size_of=lambda _p: self.SIZE,
            volume_of=lambda p: volumes[p],
            free_of=lambda p: free[volumes[p]],
        )
        return [t.path for t in writable], refused

    PATHS = ("/a/1.RAF", "/a/2.RAF", "/b/1.RAF")

    def test_a_second_target_on_the_same_volume_is_refused_by_the_first_ones_retained_original(
        self,
    ):
        free = {1: self.SIZE + SLACK_BYTES + self.SIZE - 1, 2: self.SIZE + SLACK_BYTES}

        writable, refused = self.check(self.PATHS, free, keep_original=True)

        assert writable == ["/a/1.RAF", "/b/1.RAF"]
        assert "retained originals already planned on it 10 bytes" in refused["/a/2.RAF"].detail
        assert refused["/a/2.RAF"].reason is FailedReason.WRITE_INSUFFICIENT_SPACE

    def test_a_target_on_another_volume_is_not_charged_for_the_first_volumes_originals(self):
        free = {1: self.SIZE + SLACK_BYTES + self.SIZE, 2: self.SIZE + SLACK_BYTES}

        writable, refused = self.check(self.PATHS, free, keep_original=True)

        assert writable == list(self.PATHS) and refused == {}

    def test_without_keep_original_nothing_accumulates(self):
        free = {1: self.SIZE + SLACK_BYTES, 2: self.SIZE + SLACK_BYTES}

        writable, refused = self.check(self.PATHS, free, keep_original=False)

        assert writable == list(self.PATHS) and refused == {}
