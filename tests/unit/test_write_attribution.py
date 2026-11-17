"""The attribution partition: three line classes and the ordered table over a path's two blocks."""

import pytest

from gps_sync.model import Failed, FailedReason, Geotagged, Protected, ProtectedReason
from gps_sync.writer import attribute
from tests.support import batch, marked
from tests.unit.test_writer import planned

APPLIED = "1 image files updated"
REFUSED = "1 files failed condition"
ERRORED = "1 files weren't updated due to errors"
PATH = "/m/a.RAF"
CLIP = "/m/a.MP4"

SUFFIXED_LATITUDE = (
    'Warning: Argument "notanumber" isn\'t numeric in abs in GPS:GPSLatitude (ValueConvInv)'
)
QUOTED_COLON = 'Warning: Argument "abc:def" isn\'t numeric in abs in GPS:GPSLatitude (ValueConvInv)'
BARE_LATITUDE = "Warning: Not a floating point number for GPS:GPSLatitude"
BARE_REFERENCE = "Warning: String too long for GPS:GPSLatitudeRef"
BARE_TIMESTAMP = "Warning: Not a floating point number for GPS:GPSTimeStamp"
SUFFIXED_ALTITUDE = "Warning: Error converting value for GPS:GPSAltitude (ValueConvInv)"
ATOM_RANGE = "Warning: Longitude out of range in Keys:GPSCoordinates (ValueConvInv)"
ATOM_FORMAT = "Warning: Error converting value for Keys:GPSCoordinates (ValueConvInv)"
BENIGN_MAKERNOTES = "Warning: [minor] Possibly incorrect maker notes offsets (fix by -F)"
BENIGN_ARRAYS = "Warning: [Minor] Not decoding some large array(s)"
ERROR_LINE = "Error: Error creating file: /m/a.RAF_exiftool_tmp - /m/a.RAF"


def conclude(stdout, stderr, *, target=None):
    target = target or planned(PATH)
    result = attribute(
        [target], batch([marked(target.path, *stdout)], [marked(target.path, *stderr)], 0)
    )
    return result.outcomes[0]


def reason_of(outcome) -> FailedReason:
    assert isinstance(outcome, Failed), outcome
    return outcome.reason


class TestEveryMeasuredWordingIsARefusal:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (SUFFIXED_LATITUDE, FailedReason.WRITE_PARTIAL),
            (QUOTED_COLON, FailedReason.WRITE_PARTIAL),
            (BARE_LATITUDE, FailedReason.WRITE_PARTIAL),
            (BARE_REFERENCE, FailedReason.WRITE_PARTIAL),
            (BARE_TIMESTAMP, FailedReason.WRITE_INCOMPLETE),
            (SUFFIXED_ALTITUDE, FailedReason.WRITE_INCOMPLETE),
        ],
        ids=[
            "suffix-latitude",
            "quoted-colon-value",
            "bare-latitude",
            "bare-reference",
            "bare-time-stamp",
            "suffix-altitude",
        ],
    )
    def test_a_refusal_beside_an_applied_message_concludes_by_its_member(self, line, expected):
        outcome = conclude([APPLIED], [line])

        assert reason_of(outcome) is expected
        assert outcome.detail == line

    @pytest.mark.parametrize("line", [BARE_LATITUDE, BARE_REFERENCE, BARE_TIMESTAMP])
    def test_a_refusal_with_no_suffix_is_not_read_as_a_benign_advisory(self, line):
        assert not isinstance(conclude([APPLIED], [line]), Geotagged)

    @pytest.mark.parametrize(
        "line",
        [
            "Warning: Argument x:GPSLatitude isn't numeric in abs in GPS:GPSAltitude "
            "(ValueConvInv)",
            "Warning: Argument x:GPSAltitude isn't numeric in abs in GPS:GPSLatitude "
            "(ValueConvInv)",
        ],
        ids=["position-named-first", "position-named-last"],
    )
    def test_a_line_naming_two_members_resolves_to_the_position_bearing_one_in_either_order(
        self, line
    ):
        # Which of the two was refused cannot be told from word order, and WRITE_INCOMPLETE's
        # remedy asserts the target carries the planned position, so the ambiguity must not
        # resolve to it. Each order defeats a scan that picks one end of the line.
        outcome = conclude([APPLIED], [line])

        assert reason_of(outcome) is FailedReason.WRITE_PARTIAL
        assert outcome.detail == line

    def test_a_member_named_inside_the_final_parenthesis_is_still_a_refusal(self):
        line = "Warning: Error converting value (GPS:GPSLatitude)"

        outcome = conclude([APPLIED], [line])

        assert reason_of(outcome) is FailedReason.WRITE_PARTIAL

    def test_a_member_named_inside_the_final_parenthesis_of_a_non_position_tag_is_incomplete(self):
        line = "Warning: Error converting value (GPS:GPSAltitude)"

        assert reason_of(conclude([APPLIED], [line])) is FailedReason.WRITE_INCOMPLETE

    def test_a_quoted_value_that_names_no_member_does_not_hide_the_real_token(self):
        assert reason_of(conclude([APPLIED], [QUOTED_COLON])) is FailedReason.WRITE_PARTIAL

    def test_token_matching_is_case_insensitive_and_ignores_the_group(self):
        line = "Warning: Not a floating point number for gps:gpslatitude"

        assert reason_of(conclude([APPLIED], [line])) is FailedReason.WRITE_PARTIAL

    def test_a_refusal_naming_a_member_the_block_never_emitted_is_unattributable(self):
        target = planned(PATH, ele=None)

        outcome = conclude([APPLIED], [SUFFIXED_ALTITUDE], target=target)

        assert reason_of(outcome) is FailedReason.WRITE_UNATTRIBUTED
        assert "never assigned" in outcome.detail

    def test_a_suffixed_line_naming_nothing_this_block_made_is_a_refusal_of_an_unknown_tag(self):
        line = "Warning: Error converting value for GPS:GPSSpeed (ValueConvInv)"

        assert reason_of(conclude([APPLIED], [line])) is FailedReason.WRITE_UNATTRIBUTED

    @pytest.mark.parametrize("suffix", ["(ValueConvInv)", "(PrintConvInv)"])
    def test_either_conversion_suffix_makes_a_member_free_line_a_refusal_of_an_unknown_tag(
        self, suffix
    ):
        line = f"Warning: Error converting value for GPS:GPSSpeed {suffix}"

        assert reason_of(conclude([APPLIED], [line])) is FailedReason.WRITE_UNATTRIBUTED

    def test_a_conversion_suffix_that_does_not_end_the_line_is_not_a_refusal(self):
        line = "Warning: Something GPS:GPSSpeed (PrintConvInv) was noted"

        assert isinstance(conclude([APPLIED], [line]), Geotagged)

    def test_an_unknown_conversion_suffix_does_not_make_a_member_free_line_a_refusal(self):
        line = "Warning: Error converting value for GPS:GPSSpeed (ExifTool)"

        assert isinstance(conclude([APPLIED], [line]), Geotagged)

    @pytest.mark.parametrize("punctuation", list("()[]\"',;"))
    def test_every_punctuation_character_is_stripped_from_around_a_token(self, punctuation):
        closing = {"(": ")", "[": "]"}.get(punctuation, punctuation)
        line = f"Warning: Not a floating point number for {punctuation}GPS:GPSLatitude{closing}"

        assert reason_of(conclude([APPLIED], [line])) is FailedReason.WRITE_PARTIAL

    def test_a_token_naming_a_member_without_a_group_is_not_a_member_reference(self):
        line = "Warning: Not a floating point number for GPSLatitude"

        assert isinstance(conclude([APPLIED], [line]), Geotagged)

    def test_a_suffix_free_line_naming_nothing_this_block_made_is_benign(self):
        assert isinstance(conclude([APPLIED], ["Warning: Something else GPS:GPSSpeed"]), Geotagged)


class TestBenignAdvisories:
    @pytest.mark.parametrize("line", [BENIGN_MAKERNOTES, BENIGN_ARRAYS, "Nothing to do."])
    def test_an_advisory_that_resolves_no_member_leaves_the_conclusion_geotagged(self, line):
        assert isinstance(conclude([APPLIED], [line]), Geotagged)

    def test_several_advisories_together_stay_geotagged(self):
        assert isinstance(conclude([APPLIED], [BENIGN_MAKERNOTES, BENIGN_ARRAYS]), Geotagged)

    def test_a_block_carrying_a_refusal_and_an_advisory_reads_the_whole_multiset(self):
        outcome = conclude([APPLIED], [SUFFIXED_ALTITUDE, BENIGN_MAKERNOTES])

        assert reason_of(outcome) is FailedReason.WRITE_INCOMPLETE
        assert outcome.detail == SUFFIXED_ALTITUDE

    def test_a_clean_applied_block_is_geotagged_and_keeps_its_payload(self):
        outcome = conclude([APPLIED], [])

        assert isinstance(outcome, Geotagged)
        assert outcome.via is planned(PATH).via


class TestTheOrderedTable:
    def test_row2_an_abandoned_block_is_a_write_error_quoting_every_refusal(self):
        outcome = conclude(
            [], [ATOM_RANGE, "Nothing to do."], target=planned(CLIP, container="MP4")
        )

        assert reason_of(outcome) is FailedReason.WRITE_ERROR
        assert outcome.detail == ATOM_RANGE

    def test_row2_the_format_refusal_wording_is_an_abandoned_block_too(self):
        outcome = conclude(
            [], [ATOM_FORMAT, "Nothing to do."], target=planned(CLIP, container="MP4")
        )

        assert reason_of(outcome) is FailedReason.WRITE_ERROR

    def test_row3_an_empty_stdout_with_a_silent_stderr_is_unattributable(self):
        assert reason_of(conclude([], [])) is FailedReason.WRITE_UNATTRIBUTED

    def test_row3_a_refusal_without_nothing_to_do_and_without_a_result_is_unattributable(self):
        assert reason_of(conclude([], [SUFFIXED_LATITUDE])) is FailedReason.WRITE_UNATTRIBUTED

    def test_row3_nothing_to_do_without_a_refusal_is_unattributable(self):
        assert reason_of(conclude([], ["Nothing to do."])) is FailedReason.WRITE_UNATTRIBUTED

    def test_row3_two_result_messages_are_ambiguous(self):
        outcome = conclude([APPLIED, REFUSED], [])

        assert reason_of(outcome) is FailedReason.WRITE_UNATTRIBUTED
        assert "ambiguous result lines" in outcome.detail

    def test_row4_an_error_line_with_the_errored_signal_is_a_write_error_quoting_it(self):
        outcome = conclude([ERRORED], [ERROR_LINE])

        assert reason_of(outcome) is FailedReason.WRITE_ERROR
        assert outcome.detail == ERROR_LINE

    def test_row4_an_error_line_outranks_a_refusal_line_in_the_same_block(self):
        outcome = conclude([ERRORED], [SUFFIXED_LATITUDE, ERROR_LINE])

        assert reason_of(outcome) is FailedReason.WRITE_ERROR
        assert outcome.detail == ERROR_LINE

    def test_row5_an_errored_signal_with_no_error_line_is_unattributable(self):
        outcome = conclude([ERRORED], [])

        assert reason_of(outcome) is FailedReason.WRITE_UNATTRIBUTED
        assert "contradictory signals" in outcome.detail

    def test_row5_an_error_line_beside_an_applied_message_is_unattributable(self):
        assert reason_of(conclude([APPLIED], [ERROR_LINE])) is FailedReason.WRITE_UNATTRIBUTED

    def test_row5_an_error_line_beside_a_refused_condition_is_unattributable(self):
        assert reason_of(conclude([REFUSED], [ERROR_LINE])) is FailedReason.WRITE_UNATTRIBUTED

    def test_row6_a_declined_condition_beside_a_refusal_contradicts_itself(self):
        outcome = conclude([REFUSED], [SUFFIXED_LATITUDE])

        assert reason_of(outcome) is FailedReason.WRITE_UNATTRIBUTED
        assert "declined the re-gate condition" in outcome.detail

    def test_row7_a_declined_condition_alone_is_a_position_that_appeared(self):
        outcome = conclude([REFUSED], [BENIGN_MAKERNOTES])

        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.POSITION_APPEARED
        assert outcome.position is None
        assert outcome.via is planned(PATH).via

    def test_row8_unknown_outranks_known(self):
        outcome = conclude(
            [APPLIED],
            [SUFFIXED_LATITUDE, "Warning: Error converting value for GPS:GPSSpeed (ValueConvInv)"],
        )

        assert reason_of(outcome) is FailedReason.WRITE_UNATTRIBUTED

    def test_row9_a_position_bearing_refusal_outranks_a_non_position_bearing_one(self):
        outcome = conclude([APPLIED], [SUFFIXED_ALTITUDE, SUFFIXED_LATITUDE])

        assert reason_of(outcome) is FailedReason.WRITE_PARTIAL
        assert outcome.detail == f"{SUFFIXED_ALTITUDE} | {SUFFIXED_LATITUDE}"

    def test_a_split_conclusion_keeps_the_match_payload_and_the_instant(self):
        target = planned(PATH)

        outcome = conclude([APPLIED], [BARE_LATITUDE], target=target)

        assert (outcome.via, outcome.provenance, outcome.instant) == (
            target.via,
            target.provenance,
            target.instant,
        )

    def test_a_missing_block_is_unattributable_naming_the_process_failure(self):
        target = planned(PATH)

        result = attribute([target], None, "boom")

        assert reason_of(result.outcomes[0]) is FailedReason.WRITE_UNATTRIBUTED
        assert "boom" in result.outcomes[0].detail


class TestQuickTimeBlocks:
    def test_a_clip_refusal_resolves_against_its_single_emitted_member(self):
        outcome = conclude([APPLIED], [ATOM_RANGE], target=planned(CLIP, container="MP4"))

        assert reason_of(outcome) is FailedReason.WRITE_PARTIAL

    def test_a_still_member_name_is_not_a_clip_member(self):
        outcome = conclude([APPLIED], [SUFFIXED_LATITUDE], target=planned(CLIP, container="MP4"))

        assert reason_of(outcome) is FailedReason.WRITE_UNATTRIBUTED


class TestBlockCountReconciliation:
    def test_a_clean_batch_reconciles(self):
        target = planned(PATH)

        result = attribute([target], batch([marked(PATH, APPLIED)], [marked(PATH)]))

        assert result.reconciliation.unreconciled == ()

    def test_a_path_with_no_block_is_a_shortfall(self):
        result = attribute([planned(PATH)], batch([], []))

        assert result.reconciliation.unreconciled == (
            f"shortfall: 1 intended path(s) have no block on a stream: {PATH}",
        )
        assert result.reconciliation.unreconciled_paths == (PATH,)

    def test_a_repeated_marker_is_named(self):
        result = attribute(
            [planned(PATH)],
            batch([marked(PATH, APPLIED), marked(PATH, APPLIED)], [marked(PATH), marked(PATH)]),
        )

        assert any("repeated marker" in shape for shape in result.reconciliation.unreconciled)

    def test_a_trailing_run_of_unterminated_lines_is_named(self):
        from gps_sync.transport import WriteBatchResult

        done = WriteBatchResult(
            (marked(PATH, APPLIED),), (marked(PATH),), 0, stdout_tail=("half a block",)
        )

        result = attribute([planned(PATH)], done)

        assert result.reconciliation.unreconciled == ("trailing run of unterminated lines",)


class TestTheConclusionsCarryTheTargetsFullPayload:
    def failed_expected(self, target, reason, detail):
        return Failed(
            target.path,
            reason,
            detail,
            instant=target.instant,
            via=target.via,
            provenance=target.provenance,
            bracket_speed=target.bracket_speed,
            context=target.context,
        )

    @pytest.mark.parametrize(
        ("stdout", "stderr", "reason", "detail"),
        [
            (
                [APPLIED],
                [SUFFIXED_ALTITUDE, BARE_TIMESTAMP],
                FailedReason.WRITE_INCOMPLETE,
                f"{SUFFIXED_ALTITUDE} | {BARE_TIMESTAMP}",
            ),
            (
                [APPLIED],
                [BARE_LATITUDE, SUFFIXED_ALTITUDE],
                FailedReason.WRITE_PARTIAL,
                f"{BARE_LATITUDE} | {SUFFIXED_ALTITUDE}",
            ),
            (
                [],
                [BARE_LATITUDE, BARE_REFERENCE, "Nothing to do."],
                FailedReason.WRITE_ERROR,
                f"{BARE_LATITUDE} | {BARE_REFERENCE}",
            ),
            (
                [ERRORED],
                [ERROR_LINE, ERROR_LINE],
                FailedReason.WRITE_ERROR,
                f"{ERROR_LINE} | {ERROR_LINE}",
            ),
            (
                [APPLIED],
                ["Warning: x GPS:GPSSpeed (ValueConvInv)", SUFFIXED_ALTITUDE],
                FailedReason.WRITE_UNATTRIBUTED,
                "a refusal names a tag this block never assigned: "
                "Warning: x GPS:GPSSpeed (ValueConvInv) | " + SUFFIXED_ALTITUDE,
            ),
            (
                [REFUSED],
                [BARE_LATITUDE, SUFFIXED_ALTITUDE],
                FailedReason.WRITE_UNATTRIBUTED,
                "the backend declined the re-gate condition and also refused a value: "
                f"{BARE_LATITUDE} | {SUFFIXED_ALTITUDE}",
            ),
        ],
    )
    def test_each_failure_equals_the_expected_outcome_field_for_field(
        self, stdout, stderr, reason, detail
    ):
        target = planned(PATH)

        assert conclude(stdout, stderr, target=target) == self.failed_expected(
            target, reason, detail
        )

    def test_a_success_equals_the_expected_outcome_field_for_field(self):
        target = planned(PATH)

        assert conclude([APPLIED], [], target=target) == Geotagged(
            target.path,
            target.instant,
            target.position,
            target.via,
            target.provenance,
            target.bracket_speed,
            target.context,
            target.elevation_dropped,
        )

    def test_a_flagged_elevation_drop_survives_the_write(self):
        target = planned(PATH)
        object.__setattr__(target, "elevation_dropped", True)

        assert conclude([APPLIED], [], target=target).elevation_dropped

    def test_an_appeared_position_equals_the_expected_outcome_field_for_field(self):
        target = planned(PATH)

        assert conclude([REFUSED], [], target=target) == Protected(
            target.path,
            ProtectedReason.POSITION_APPEARED,
            instant=target.instant,
            via=target.via,
            provenance=target.provenance,
            bracket_speed=target.bracket_speed,
            context=target.context,
        )


class TestClassOrderDoesNotMatter:
    def test_a_benign_line_before_a_refusal_does_not_hide_it(self):
        assert reason_of(conclude([APPLIED], [BENIGN_MAKERNOTES, BARE_LATITUDE])) is (
            FailedReason.WRITE_PARTIAL
        )

    def test_an_error_line_before_a_refusal_does_not_hide_it_from_the_abandoned_block_row(self):
        outcome = conclude([], [ERROR_LINE, BARE_LATITUDE, "Nothing to do."])

        assert reason_of(outcome) is FailedReason.WRITE_ERROR
        assert outcome.detail == BARE_LATITUDE

    def test_a_trailing_token_without_a_colon_does_not_stop_the_scan(self):
        line = "Warning: Not a floating point number for GPS:GPSLatitude today"

        assert reason_of(conclude([APPLIED], [line])) is FailedReason.WRITE_PARTIAL

    def test_only_the_part_after_the_last_colon_names_the_member(self):
        line = "Warning: Not a floating point number for Group:Sub:GPSLatitude"

        assert reason_of(conclude([APPLIED], [line])) is FailedReason.WRITE_PARTIAL

    def test_a_part_before_the_first_colon_alone_is_not_a_member(self):
        line = "Warning: Not a floating point number for GPSLatitude:other"

        assert isinstance(conclude([APPLIED], [line]), Geotagged)


class TestReconciliationDetails:
    def test_a_path_missing_from_one_stream_only_is_a_shortfall(self):
        result = attribute([planned(PATH)], batch([marked(PATH, APPLIED)], []))

        assert result.reconciliation.unreconciled == (
            f"shortfall: 1 intended path(s) have no block on a stream: {PATH}",
        )

    def test_a_marker_repeated_on_one_stream_only_is_named(self):
        result = attribute(
            [planned(PATH)],
            batch([marked(PATH, APPLIED), marked(PATH, APPLIED)], [marked(PATH)]),
        )

        assert f"repeated marker: 1 path(s): {PATH}" in result.reconciliation.unreconciled
        assert result.reconciliation.unreconciled_paths == (PATH,)

    def test_an_unintended_conclusion_carries_both_streams_lines_in_order(self):
        result = attribute(
            [planned(PATH)],
            batch(
                [marked(PATH, APPLIED), marked("/m/other.RAF", APPLIED)],
                [marked(PATH), marked("/m/other.RAF", "Warning: x")],
            ),
        )

        [unintended] = result.reconciliation.unintended
        assert unintended.path == "/m/other.RAF"
        assert unintended.lines == (APPLIED, "Warning: x")

    def test_an_unintended_path_seen_on_stderr_only_still_carries_its_lines(self):
        result = attribute(
            [planned(PATH)],
            batch([marked(PATH, APPLIED)], [marked(PATH), marked("/m/other.RAF", "Warning: x")]),
        )

        [unintended] = result.reconciliation.unintended
        assert unintended.lines == ("Warning: x",)

    def test_an_unintended_path_seen_on_stdout_only_still_carries_its_lines(self):
        result = attribute(
            [planned(PATH)],
            batch([marked(PATH, APPLIED), marked("/m/other.RAF", APPLIED)], [marked(PATH)]),
        )

        [unintended] = result.reconciliation.unintended
        assert unintended.lines == (APPLIED,)


class TestMessagesAndStructure:
    def test_a_path_with_no_block_and_no_process_failure_says_the_batch_never_reached_it(self):
        outcome = attribute([planned(PATH)], batch([], [])).outcomes[0]

        assert outcome.detail == (
            "no conclusion attributable to this path: the batch never reached this path's marker"
        )

    def test_a_repeated_marker_is_unattributable_with_its_own_detail(self):
        outcome = attribute(
            [planned(PATH)],
            batch([marked(PATH, APPLIED), marked(PATH, APPLIED)], [marked(PATH), marked(PATH)]),
        ).outcomes[0]

        assert outcome.detail == "this path's marker appeared more than once"

    @pytest.mark.parametrize("prefix", ["error:", "ERROR:", "XXError:XX", " Error"])
    def test_only_the_exact_error_prefix_makes_an_error_line(self, prefix):
        outcome = conclude([ERRORED], [f"{prefix} Error creating file"])

        assert reason_of(outcome) is FailedReason.WRITE_UNATTRIBUTED

    def test_a_block_exposes_the_assignments_it_emits(self):
        from gps_sync.writer import build_block

        block = build_block(planned(PATH), keep_original=False)

        assert [m.tag for m in block.assignments][:2] == ["GPSLatitude", "GPSLatitudeRef"]
        assert len(block.assignments) == 8


class TestNamingTheAffectedPaths:
    def test_up_to_five_paths_are_listed_in_full_separated_by_a_comma_and_a_space(self):
        from gps_sync.writer import _named

        assert _named(["/a", "/b"]) == "/a, /b"
        assert _named(["/1", "/2", "/3", "/4", "/5"]) == "/1, /2, /3, /4, /5"

    def test_beyond_five_the_rest_are_counted_not_listed(self):
        from gps_sync.writer import _named

        assert _named([f"/{n}" for n in range(1, 8)]) == "/1, /2, /3, /4, /5 and 2 more"


class TestMergingTheSpaceStageIntoTheConclusions:
    def test_a_path_nothing_concluded_is_an_internal_inconsistency_naming_the_first_three(self):
        import pytest

        from gps_sync.errors import InternalConsistencyError
        from gps_sync.writer import merge_conclusions

        targets = [planned(f"/m/{n}.RAF") for n in "abcde"]

        with pytest.raises(InternalConsistencyError) as raised:
            merge_conclusions(targets, None, {})

        message = str(raised.value)
        assert message.startswith("intended paths with no conclusion: ")
        assert "/m/a.RAF" in message and "/m/c.RAF" in message
        assert "/m/d.RAF" not in message

    def test_refusals_and_write_conclusions_merge_in_intent_order(self):
        from gps_sync.writer import merge_conclusions

        targets = [planned(f"/m/{n}.RAF") for n in "abc"]
        written = attribute(
            [targets[0], targets[2]],
            batch(
                [marked(targets[0].path, APPLIED), marked(targets[2].path, APPLIED)],
                [marked(targets[0].path), marked(targets[2].path)],
            ),
        )
        refusal = Failed(
            targets[1].path,
            FailedReason.WRITE_INSUFFICIENT_SPACE,
            "x",
            via=targets[1].via,
            provenance=targets[1].provenance,
        )

        merged = merge_conclusions(targets, written, {targets[1].path: refusal})

        assert [o.path for o in merged.outcomes] == [t.path for t in targets]
        assert merged.outcomes[1] is refusal
        assert merged.reconciliation == written.reconciliation
