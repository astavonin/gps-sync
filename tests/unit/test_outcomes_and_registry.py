import pytest

from gps_sync import model
from gps_sync.exitcode import ArtifactStatus, fold_exit_code
from gps_sync.model import (
    Extended,
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
    RunLevelReason,
    SuccessState,
    Unmatchable,
    UnmatchableReason,
    Via,
    reason_key,
)
from gps_sync.registry import (
    NOT_APPLICABLE,
    PRINTED,
    REGISTRY,
    SUPPRESSED,
    review_subfolders,
    spec,
)
from gps_sync.writer import Reconciliation, UnintendedConclusion
from tests.support import at

INTERP = Interpolated("s#trk0seg0", 5.0)
EXT = Extended("s#trk0seg0", 60.0, UnmatchableReason.OUTSIDE_COVERAGE)
POS = Position(42.0, 77.0)

ALL_REASON_ENUMS = (
    IgnoredReason,
    ProtectedReason,
    UnmatchableReason,
    FailedReason,
    SuccessState,
    RunLevelReason,
)


# Rows with nothing to remedy: the two success states.
REMEDY_FREE = frozenset({SuccessState.PLANNED, SuccessState.GEOTAGGED})


class TestRegistryCompleteness:
    def test_every_reason_enumeration_member_has_a_registry_row(self):
        members = [m for enum in ALL_REASON_ENUMS for m in enum]

        assert [m for m in members if m not in REGISTRY] == []
        assert len(REGISTRY) == len(members) == 30

    def test_every_row_declares_all_five_renderings(self):
        for key, row in REGISTRY.items():
            assert row.json, key
            assert row.label, key
            assert row.exit_term in (0, 1), key
            assert row.subfolder is None or row.subfolder.strip(), key
            if key in REMEDY_FREE:
                assert row.remedy is None, key
            else:
                assert row.remedy and row.remedy.strip(), key

    def test_every_row_declares_one_of_the_three_per_file_line_policies(self):
        assert {row.per_file_lines for row in REGISTRY.values()} <= {
            PRINTED,
            SUPPRESSED,
            NOT_APPLICABLE,
        }

    def test_the_per_file_line_policy_of_every_row_is_pinned(self):
        expected = {
            IgnoredReason.NOT_A_STILL: SUPPRESSED,
            IgnoredReason.HIDDEN_ENTRY: SUPPRESSED,
            RunLevelReason.NO_TRACKS_ROOT_FOR_MATCHED_CANDIDATES: NOT_APPLICABLE,
            RunLevelReason.ELEVATION_NOT_REPRESENTABLE: NOT_APPLICABLE,
            RunLevelReason.BLOCK_COUNT_UNRECONCILED: NOT_APPLICABLE,
            RunLevelReason.WRITE_UNINTENDED_PATH: NOT_APPLICABLE,
        }

        actual = {key: row.per_file_lines for key, row in REGISTRY.items() if key in expected}
        printed = {key for key, row in REGISTRY.items() if row.per_file_lines == PRINTED}

        assert actual == expected
        assert printed == set(REGISTRY) - set(expected)
        assert {k for k, row in REGISTRY.items() if row.variant == "run-level"} <= set(expected)

    def test_the_stable_json_string_is_the_reason_name_verbatim_over_the_whole_registry(self):
        assert {key.name: row.json for key, row in REGISTRY.items()} == {
            key.name: key.name for key in REGISTRY
        }

    def test_every_reason_that_can_leave_a_candidate_without_a_position_has_a_subfolder(self):
        needing = [
            *ProtectedReason,
            *UnmatchableReason,
            *(r for r in FailedReason),
        ]

        assert all(spec(r).subfolder for r in needing)

    @pytest.mark.parametrize(
        "key",
        [
            *IgnoredReason,
            SuccessState.PLANNED,
            SuccessState.GEOTAGGED,
            RunLevelReason.WRITE_UNINTENDED_PATH,
        ],
    )
    def test_the_none_decisions_are_explicit_rows_with_no_subfolder(self, key):
        assert key in REGISTRY
        assert spec(key).subfolder is None

    def test_subfolders_are_unique_and_named_for_the_diagnoses(self):
        folders = [r.subfolder for r in REGISTRY.values() if r.subfolder]

        assert len(folders) == len(set(folders)) == 20
        assert {
            "sampling-dropout",
            "between-segments",
            "no-track-coverage",
            "target-unexaminable",
        } <= set(folders)
        assert review_subfolders() == set(folders)

    def test_the_two_gap_reasons_stay_distinct_in_every_rendering(self):
        gap, boundary = (
            spec(UnmatchableReason.MAX_GAP_EXCEEDED),
            spec(UnmatchableReason.SEGMENT_BOUNDARY),
        )

        assert gap.json != boundary.json
        assert gap.subfolder != boundary.subfolder
        assert gap.label != boundary.label
        assert "not the remedy" in boundary.remedy

    def test_only_failed_reasons_and_the_run_level_row_raise_the_exit_code(self):
        raising = {key for key, row in REGISTRY.items() if row.exit_term == 1}

        assert raising == {
            *FailedReason,
            RunLevelReason.WRITE_UNINTENDED_PATH,
            RunLevelReason.BLOCK_COUNT_UNRECONCILED,
        }

    def test_unattributed_remedy_says_manual_inspection_not_a_rerun(self):
        remedy = spec(FailedReason.WRITE_UNATTRIBUTED).remedy

        assert "inspect by hand" in remedy and "audit record" in remedy

    def test_every_variant_has_a_reason_key_handler(self):
        samples = {
            "Ignored": Ignored("/a", IgnoredReason.SYMLINK),
            "Protected": Protected("/a", ProtectedReason.POSITION_PRESENT, position=POS),
            "Failed": Failed("/a", FailedReason.TIME_UNRESOLVABLE),
            "Unmatchable": Unmatchable("/a", at(0), UnmatchableReason.OUTSIDE_COVERAGE),
            "Planned": Planned("/a", at(0), POS, Via.INTERPOLATED, INTERP),
            "Geotagged": Geotagged("/a", at(0), POS, Via.INTERPOLATED, INTERP),
        }

        assert set(samples) == set(model.VARIANT_NAMES)
        for name, outcome in samples.items():
            assert model.variant_name(outcome) == name
            assert reason_key(outcome) in REGISTRY


class TestPayloadPresenceRules:
    def test_protected_present_and_zero_carry_a_position_and_no_match_payload(self):
        for reason in (ProtectedReason.POSITION_PRESENT, ProtectedReason.ZERO_POSITION):
            Protected("/a", reason, position=POS)
            with pytest.raises(ValueError):
                Protected("/a", reason)
            with pytest.raises(ValueError):
                Protected("/a", reason, position=POS, via=Via.INTERPOLATED, provenance=INTERP)

    def test_protected_appeared_carries_the_match_payload_and_no_position(self):
        Protected("/a", ProtectedReason.POSITION_APPEARED, via=Via.INTERPOLATED, provenance=INTERP)
        with pytest.raises(ValueError):
            Protected(
                "/a",
                ProtectedReason.POSITION_APPEARED,
                position=POS,
                via=Via.EXTENDED,
                provenance=EXT,
            )
        with pytest.raises(ValueError):
            Protected("/a", ProtectedReason.POSITION_APPEARED)

    @pytest.mark.parametrize("reason", sorted(model.FAILED_AFTER_MATCH, key=lambda r: r.value))
    def test_failed_after_a_match_requires_the_payload(self, reason):
        Failed("/a", reason, via=Via.EXTENDED, provenance=EXT, bracket_speed=1.0)
        with pytest.raises(ValueError):
            Failed("/a", reason)

    @pytest.mark.parametrize(
        "reason",
        [r for r in FailedReason if r not in model.FAILED_AFTER_MATCH],
        ids=lambda r: r.value,
    )
    def test_failed_before_a_match_forbids_the_payload(self, reason):
        Failed("/a", reason)
        with pytest.raises(ValueError):
            Failed("/a", reason, via=Via.INTERPOLATED, provenance=INTERP)

    def test_via_and_provenance_must_agree(self):
        with pytest.raises(ValueError):
            Planned("/a", at(0), POS, Via.EXTENDED, INTERP)
        with pytest.raises(ValueError):
            Geotagged("/a", at(0), POS, Via.INTERPOLATED, EXT)

    def test_nearest_segment_and_miss_are_absent_together_or_not_at_all(self):
        reason = UnmatchableReason.OUTSIDE_COVERAGE
        Unmatchable("/a", at(0), reason)
        Unmatchable("/a", at(0), reason, nearest_segment="s", miss_s=1.0)
        with pytest.raises(ValueError):
            Unmatchable("/a", at(0), reason, nearest_segment="s")
        with pytest.raises(ValueError):
            Unmatchable("/a", at(0), reason, miss_s=1.0)


class TestExitCodeFold:
    ok = ArtifactStatus()

    def planned(self):
        return Planned("/a", at(0), POS, Via.INTERPOLATED, INTERP)

    def test_planned_and_geotagged_only_is_zero(self):
        outcomes = [self.planned(), Geotagged("/b", at(0), POS, Via.INTERPOLATED, INTERP)]

        assert fold_exit_code(outcomes, self.ok, None) == 0

    def test_the_reference_archive_shape_of_matched_and_unmatchable_is_zero(self):
        outcomes = [self.planned()] * 3 + [
            Unmatchable("/u", at(0), UnmatchableReason.OUTSIDE_COVERAGE, "s", 3000.0)
        ] * 2

        assert fold_exit_code(outcomes, self.ok, None) == 0

    def test_an_all_protected_idempotent_rerun_is_zero(self):
        outcomes = [Protected("/p", ProtectedReason.POSITION_PRESENT, position=POS)] * 5

        assert fold_exit_code(outcomes, self.ok, None) == 0

    def test_ignored_only_and_empty_tallies_are_zero(self):
        assert fold_exit_code([Ignored("/i", IgnoredReason.NOT_A_STILL)], self.ok, None) == 0
        assert fold_exit_code([], self.ok, None) == 0

    def test_one_failed_among_many_successes_is_one(self):
        outcomes = [self.planned()] * 300 + [Failed("/f", FailedReason.TIME_UNRESOLVABLE)]

        assert fold_exit_code(outcomes, self.ok, None) == 1

    @pytest.mark.parametrize("reason", list(FailedReason))
    def test_every_failed_reason_alone_yields_one(self, reason):
        kwargs = (
            {"via": Via.INTERPOLATED, "provenance": INTERP}
            if reason in model.FAILED_AFTER_MATCH
            else {}
        )

        assert fold_exit_code([Failed("/f", reason, **kwargs)], self.ok, None) == 1

    def test_failed_dominates_unmatchable_rather_than_being_diluted(self):
        outcomes = [Unmatchable("/u", at(0), UnmatchableReason.OUTSIDE_COVERAGE, "s", 1.0)] * 26
        outcomes.append(
            Failed("/f", FailedReason.WRITE_ERROR, via=Via.INTERPOLATED, provenance=INTERP)
        )

        assert fold_exit_code(outcomes, self.ok, None) == 1

    def test_a_conclusion_for_an_unintended_path_raises_the_code_with_a_clean_tally(self):
        reconciliation = Reconciliation(unintended=(UnintendedConclusion("/x", ("a",)),))

        assert fold_exit_code([self.planned()], self.ok, reconciliation) == 1
        assert spec(RunLevelReason.WRITE_UNINTENDED_PATH).exit_term == 1

    def test_a_clean_tally_with_a_failed_completion_record_is_three(self):
        assert fold_exit_code([self.planned()], ArtifactStatus(audit_complete=False), None) == 3

    def test_artifact_failure_outranks_a_failed_file_in_the_zero_one_three_order(self):
        outcomes = [Failed("/f", FailedReason.TIME_UNRESOLVABLE)]

        assert fold_exit_code(outcomes, ArtifactStatus(review_complete=False), None) == 3

    @pytest.mark.parametrize("flag", ["audit_complete", "review_complete", "stdout_ok"])
    def test_each_artifact_kind_alone_yields_three(self, flag):
        assert fold_exit_code([], ArtifactStatus(**{flag: False}), None) == 3

    def test_the_same_tally_gives_the_same_code_in_dry_run_and_write_mode(self):
        dry = [
            self.planned(),
            Unmatchable("/u", at(0), UnmatchableReason.OUTSIDE_COVERAGE, "s", 1.0),
        ]
        written = [Geotagged("/a", at(0), POS, Via.INTERPOLATED, INTERP), dry[1]]

        assert fold_exit_code(dry, self.ok, None) == fold_exit_code(written, self.ok, None)

    def test_write_only_outcomes_are_not_produced_by_the_classifier(self):
        from tests.support import classify, config, index_of, segment, tag_record

        covered = index_of(segment("s#trk0seg0", [(0, 1.0, 1.0, None), (60, 2.0, 2.0, None)]))
        records = [
            tag_record(gps="42.5 78.25"),
            tag_record(gps="0 0"),
            tag_record(errors=("File format error",)),
            tag_record(date_time_original=None, offset=None),
            tag_record(date_time_original="2026:10:26 18:00:00"),
            tag_record(file_type="ORF"),
            tag_record(date_time_original="2026:10:26 10:00:30"),
        ]

        outcomes = [classify(record, covered, config()) for record in records]

        assert any(isinstance(o, Planned) for o in outcomes), "the sweep must reach the matcher"
        assert len({type(o) for o in outcomes}) >= 4, "the sweep must span the variants"
        for outcome in outcomes:
            assert not isinstance(outcome, Geotagged)
            if isinstance(outcome, Protected):
                assert outcome.reason is not ProtectedReason.POSITION_APPEARED
            if isinstance(outcome, Failed):
                assert outcome.reason not in {
                    FailedReason.WRITE_ERROR,
                    FailedReason.WRITE_UNATTRIBUTED,
                }


class TestTheTelemetryInconsistentRowIsTrueOfEveryConditionThatEmitsIt:
    ROW = REGISTRY[FailedReason.TELEMETRY_INCONSISTENT]

    def test_the_label_claims_no_more_than_a_disagreement(self):
        assert "internally" not in self.ROW.label
        assert "container record" in self.ROW.label

    @pytest.mark.parametrize(
        "phrase",
        [
            "latitude, longitude and sample-time lists",
            "no readable Duration",
            "more than a second short",
            "truncated",
            "a re-run will not change it",
        ],
    )
    def test_the_remedy_separates_the_conditions_a_re_run_can_clear_from_those_it_cannot(
        self, phrase
    ):
        assert phrase in self.ROW.remedy

    def test_a_blanket_re_run_instruction_is_not_given(self):
        assert not self.ROW.remedy.startswith("re-run")
        assert self.ROW.subfolder == "inconsistent-telemetry"


class TestTheInsufficientSpaceRowIsTrueOfAStillAsWellAsAClip:
    ROW = REGISTRY[FailedReason.WRITE_INSUFFICIENT_SPACE]

    def test_the_remedy_does_not_frame_the_refusal_as_a_video_only_one(self):
        assert "video" not in self.ROW.remedy and "clip's own" not in self.ROW.remedy
        assert "a still as well as a clip" in self.ROW.remedy

    def test_a_still_target_is_refused_with_the_registry_wording(self):
        from gps_sync.space import check_space
        from tests.unit.test_writer import planned

        target = planned("/m/a.RAF")

        _, refused = check_space(
            [target],
            keep_original=False,
            size_of=lambda _p: 10,
            volume_of=lambda _p: 1,
            free_of=lambda _p: 0,
        )

        assert refused[target.path].reason is FailedReason.WRITE_INSUFFICIENT_SPACE
