"""The mutation baseline tooling: renumber-proof identity, admissions, and no in-place rewrite."""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO = SCRIPTS.parent


def load():
    spec = importlib.util.spec_from_file_location(
        "mutation_baseline", SCRIPTS / "mutation_baseline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load()

SOURCE = '''
def alpha(x):
    """Doc."""
    return x + 1  # a comment


class Box:
    def open(self, y):
        return y * 2
'''


def survivor(name="gps_sync.m.x_alpha__mutmut_1", diff="d" * 12, func="f" * 12, committed="f" * 12):
    return tool.Survivor(name, diff, func, committed)


def entry(name="gps_sync.m.x_alpha__mutmut_1", diff="d" * 12, func="f" * 12, cat="BENIGN", why="r"):
    return tool.Entry(name, diff, func, cat, why)


class TestParsingAndRendering:
    def test_a_current_format_line_carries_its_identity_category_and_reason(self):
        [parsed] = tool.parse_baseline(
            f"gps_sync.m.x_a__mutmut_3  {'a' * 12}  {'b' * 12}  # BENIGN: why not\n"
        )

        assert parsed == tool.Entry(
            "gps_sync.m.x_a__mutmut_3", "a" * 12, "b" * 12, "BENIGN", "why not"
        )

    def test_a_legacy_line_has_no_identity(self):
        [parsed] = tool.parse_baseline("gps_sync.m.x_a__mutmut_3  # EQUIVALENT: same\n")

        assert (parsed.diff, parsed.func, parsed.category) == (None, None, "EQUIVALENT")

    def test_comments_and_blank_lines_are_skipped(self):
        assert tool.parse_baseline("# header\n\n   \n") == []

    def test_an_unclassified_entry_parses_so_that_the_gate_can_refuse_it(self):
        [parsed] = tool.parse_baseline(f"n  {'a' * 12}  {'b' * 12}  # UNCLASSIFIED: UNCLASSIFIED\n")

        assert parsed.category == "UNCLASSIFIED"

    def test_a_malformed_line_is_refused_with_its_number(self):
        with pytest.raises(ValueError, match="line 2"):
            tool.parse_baseline("# ok\nnot a baseline line\n")

    def test_the_header_is_everything_before_the_first_entry(self):
        assert tool.header_of("# a\n\n# b\nname  # BENIGN: r\n# later\n") == ["# a", "", "# b"]

    def test_rendering_round_trips_and_sorts_by_name(self):
        entries = [entry("gps_sync.m.x_b__mutmut_1"), entry("gps_sync.m.x_a__mutmut_1")]

        text = tool.render(entries, ["# h"])

        assert text.splitlines()[0] == "# h"
        assert [e.name for e in tool.parse_baseline(text)] == [
            "gps_sync.m.x_a__mutmut_1",
            "gps_sync.m.x_b__mutmut_1",
        ]


class TestIdentity:
    def test_a_mutant_name_locates_its_module_and_function(self):
        assert tool.locate("gps_sync.classifier.x__after_match__mutmut_10") == (
            "classifier",
            ("_after_match",),
        )
        assert tool.locate("gps_sync.records.xǁTagRecordǁfrom_json__mutmut_57") == (
            "records",
            ("TagRecord", "from_json"),
        )

    @pytest.mark.parametrize("name", ["nonsense", "gps_sync.m.y_a__mutmut_1", "gps_sync.m.x_a"])
    def test_anything_else_is_not_a_mutant_name(self, name):
        with pytest.raises(ValueError, match="mutant"):
            tool.locate(name)

    def test_the_function_digest_ignores_comments_and_formatting(self):
        reformatted = SOURCE.replace("x + 1  # a comment", "x+1")

        assert tool.function_digest(SOURCE, ("alpha",)) == tool.function_digest(
            reformatted, ("alpha",)
        )

    def test_the_function_digest_changes_with_behaviour(self):
        edited = SOURCE.replace("x + 1", "x + 2")

        assert tool.function_digest(SOURCE, ("alpha",)) != tool.function_digest(edited, ("alpha",))

    def test_a_method_is_found_through_its_class_and_a_missing_name_is_none(self):
        assert tool.function_digest(SOURCE, ("Box", "open"))
        assert tool.function_digest(SOURCE, ("Box", "close")) is None
        assert tool.function_digest(SOURCE, ("beta",)) is None

    def test_editing_one_function_leaves_another_digest_alone(self):
        edited = SOURCE.replace("y * 2", "y * 3")

        assert tool.function_digest(SOURCE, ("alpha",)) == tool.function_digest(edited, ("alpha",))

    def test_the_diff_digest_covers_the_changed_lines_and_not_the_numbering(self):
        one = "--- a\n+++ b\n@@ -1,2 +1,2 @@\n x\n-a = 1\n+a = 2\n"
        shifted = "--- a\n+++ b\n@@ -9,2 +9,2 @@\n y\n-a = 1\n+a = 2\n"
        other = "--- a\n+++ b\n@@ -1,2 +1,2 @@\n x\n-a = 1\n+a = 3\n"

        assert tool.diff_digest(one) == tool.diff_digest(shifted) != tool.diff_digest(other)


class TestAdmission:
    def test_a_current_format_entry_admits_by_identity_even_after_a_renumbering(self):
        renumbered = survivor(name="gps_sync.m.x_alpha__mutmut_9")

        assert tool.admitted_by(renumbered, [entry()]) is not None

    def test_a_different_diff_or_a_rewritten_function_is_not_admitted(self):
        assert tool.admitted_by(survivor(diff="e" * 12), [entry()]) is None
        assert tool.admitted_by(survivor(func="e" * 12), [entry()]) is None

    def test_a_legacy_entry_admits_by_name_only_while_its_function_is_unchanged(self):
        legacy = tool.Entry("gps_sync.m.x_alpha__mutmut_1", None, None, "BENIGN", "r")

        assert tool.admitted_by(survivor(), [legacy]) is legacy
        assert tool.admitted_by(survivor(committed="0" * 12), [legacy]) is None
        assert tool.admitted_by(survivor(name="gps_sync.m.x_alpha__mutmut_2"), [legacy]) is None

    def test_a_function_new_since_the_commit_never_inherits_a_legacy_entry(self):
        legacy = tool.Entry("gps_sync.m.x_alpha__mutmut_1", None, None, "BENIGN", "r")

        assert tool.admitted_by(survivor(committed=None), [legacy]) is None


class TestTheGate:
    def test_an_admitted_survivor_passes(self):
        assert tool.gate([survivor()], [entry()]).refused == ()

    def test_a_survivor_outside_the_baseline_fails(self):
        [line] = tool.gate([survivor()], []).refused

        assert "not in the baseline" in line

    @pytest.mark.parametrize("category", ["UNCLASSIFIED", "", "WHATEVER"])
    def test_an_entry_without_an_admitting_category_fails_because_the_gate_reads_the_column(
        self, category
    ):
        [line] = tool.gate([survivor()], [entry(cat=category)]).refused

        assert "is not an admission" in line

    def test_an_entry_with_no_justification_fails(self):
        assert tool.gate([survivor()], [entry(why="")]).refused

    @pytest.mark.parametrize("category", ["GENUINE-GAP", "EQUIVALENT", "BENIGN"])
    def test_each_declared_category_admits(self, category):
        assert tool.gate([survivor()], [entry(cat=category)]).refused == ()

    def test_no_survivor_is_admitted_in_the_telemetry_module_whatever_the_baseline_says(self):
        name = "gps_sync.telemetry.x_derive_centroid__mutmut_4"

        verdict = tool.gate([survivor(name)], [entry(name)])

        assert "no survivor is admitted in this module" in verdict.refused[0]

    def test_an_entry_that_no_longer_survives_is_reported_stale_not_failed(self):
        verdict = tool.gate([], [entry()])

        assert verdict.refused == () and verdict.stale == ("gps_sync.m.x_alpha__mutmut_1",)


class TestALegacyEntryWithNoCommittedSourceFailsLoudly:
    def test_the_gate_says_why_rather_than_reporting_the_survivor_as_new(self):
        legacy = tool.Entry("gps_sync.m.x_alpha__mutmut_1", None, None, "BENIGN", "r")

        verdict = tool.gate([survivor(committed=None)], [legacy])

        [line] = verdict.refused
        assert "no git history" in line and "make mutants-propose" in line
        assert "not in the baseline" not in line

    def test_a_survivor_with_no_entry_at_all_is_still_reported_as_outside_the_baseline(self):
        verdict = tool.gate([survivor(committed=None)], [])

        assert verdict.refused == ("gps_sync.m.x_alpha__mutmut_1: not in the baseline",)


class TestProposalNeverBypassesTheGate:
    def test_a_justification_is_carried_forward_by_identity(self):
        candidate, problems = tool.propose([survivor()], [entry(why="kept")], [])

        assert problems == []
        assert candidate == [entry(why="kept")]

    def test_a_new_survivor_in_an_unchanged_function_is_a_regression_not_a_justification(self):
        candidate, problems = tool.propose([survivor(diff="e" * 12)], [entry()], [])

        assert "a test has regressed" in problems[0]
        assert candidate[0].category == "UNCLASSIFIED"

    def test_a_survivor_in_a_rewritten_function_needs_a_justification_written_against_it(self):
        candidate, problems = tool.propose([survivor(func="e" * 12)], [entry()], [])

        assert "rewritten source" in problems[0]
        assert candidate[0].category == "UNCLASSIFIED"

    def test_a_person_edited_candidate_supplies_the_missing_justification(self):
        rewritten = survivor(func="e" * 12)
        edited = [entry(func="e" * 12, why="written against the new source")]

        candidate, problems = tool.propose([rewritten], [entry()], edited)

        assert problems == []
        assert candidate[0].reason == "written against the new source"

    def test_an_unclassified_edit_does_not_satisfy_the_check(self):
        rewritten = survivor(func="e" * 12)

        _, problems = tool.propose([rewritten], [], [entry(func="e" * 12, cat="UNCLASSIFIED")])

        assert problems

    def test_a_telemetry_survivor_is_refused_even_with_an_inherited_justification(self):
        name = "gps_sync.telemetry.x_derive_centroid__mutmut_4"

        _, problems = tool.propose([survivor(name)], [entry(name)], [])

        assert any("no survivor is admitted" in p for p in problems)


class TestPromotion:
    @pytest.fixture
    def files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tool, "BASELINE", tmp_path / "mutation-baseline.txt")
        monkeypatch.setattr(tool, "CANDIDATE", tmp_path / "mutation-baseline.candidate.txt")
        monkeypatch.setattr(tool, "STAMP", tmp_path / "mutation-baseline.candidate.txt.checked")
        sources = tmp_path / "sources"
        sources.mkdir()
        (sources / "m.py").write_text("x = 1\n")
        monkeypatch.setattr(tool, "SOURCES", (sources,))
        tool.BASELINE.write_text("# committed\n")
        return tool

    def run_propose(self, survivors, tmp_path):
        names = tmp_path / "survivors"
        names.write_text("".join(f"{s.name}\n" for s in survivors))
        return tool.main(
            ["propose", "--survivors", str(names)], identifier=lambda _names: survivors
        )

    def test_propose_never_touches_the_committed_baseline(self, files, tmp_path, capsys):
        before = tool.BASELINE.read_text()

        code = self.run_propose([survivor()], tmp_path)

        assert code == 1
        assert tool.BASELINE.read_text() == before
        assert tool.CANDIDATE.exists()
        assert not tool.STAMP.exists()
        assert "FAIL" in capsys.readouterr().err

    def test_a_passing_candidate_is_stamped_and_the_baseline_is_still_untouched(
        self, files, tmp_path
    ):
        tool.BASELINE.write_text(f"# committed\n{entry().render()}\n")
        before = tool.BASELINE.read_text()

        code = self.run_propose([survivor()], tmp_path)

        assert code == 0
        assert tool.BASELINE.read_text() == before
        assert tool.STAMP.read_text() == tool._stamp_text()

    def test_promotion_is_refused_without_a_passed_check(self, files, tmp_path, capsys):
        tool.CANDIDATE.write_text("# candidate\n")

        assert tool.main(["promote"]) == 1
        assert tool.BASELINE.read_text() == "# committed\n"
        assert "admissions check" in capsys.readouterr().err

    def test_promotion_is_refused_when_the_candidate_changed_after_its_check(
        self, files, tmp_path, capsys
    ):
        tool.BASELINE.write_text(f"# committed\n{entry().render()}\n")
        assert self.run_propose([survivor()], tmp_path) == 0
        tool.CANDIDATE.write_text(tool.CANDIDATE.read_text() + "tampered\n")

        assert tool.main(["promote"]) == 1
        assert "changed after its admissions check" in capsys.readouterr().err
        assert "tampered" not in tool.BASELINE.read_text()

    def test_a_change_in_a_tier_the_gate_never_runs_does_not_invalidate_the_check(
        self, files, tmp_path
    ):
        tool.BASELINE.write_text(f"# committed\n{entry().render()}\n")
        assert self.run_propose([survivor()], tmp_path) == 0
        for tier in ("integration", "e2e"):
            (tmp_path / "sources" / tier).mkdir()
            (tmp_path / "sources" / tier / "test_x.py").write_text("x = 2\n")

        assert tool.STAMP.read_text() == tool._stamp_text()

    def test_a_change_in_a_tier_the_gate_runs_invalidates_the_check(self, files, tmp_path):
        tool.BASELINE.write_text(f"# committed\n{entry().render()}\n")
        assert self.run_propose([survivor()], tmp_path) == 0
        (tmp_path / "sources" / "unit").mkdir()
        (tmp_path / "sources" / "unit" / "test_x.py").write_text("x = 2\n")

        assert tool.STAMP.read_text() != tool._stamp_text()

    def test_promotion_is_refused_when_the_source_changed_after_the_check(
        self, files, tmp_path, capsys
    ):
        tool.BASELINE.write_text(f"# committed\n{entry().render()}\n")
        assert self.run_propose([survivor()], tmp_path) == 0
        (tmp_path / "sources" / "m.py").write_text("x = 2\n")

        assert tool.main(["promote"]) == 1
        assert "code it was checked against changed" in capsys.readouterr().err
        assert tool.CANDIDATE.exists()

    def test_a_forged_candidate_and_stamp_pair_is_refused_on_its_own_content(
        self, files, tmp_path, capsys
    ):
        tool.CANDIDATE.write_text(
            f"# candidate\n{entry(cat='UNCLASSIFIED', why='UNCLASSIFIED').render()}\n"
        )
        tool.STAMP.write_text(tool._stamp_text())

        assert tool.main(["promote"]) == 1
        assert "is not an admission or has no justification" in capsys.readouterr().err
        assert tool.BASELINE.read_text() == "# committed\n"

    def test_a_legacy_name_only_entry_in_a_candidate_is_refused(self, files, capsys):
        tool.CANDIDATE.write_text("# candidate\ngps_sync.m.x_a__mutmut_1  # BENIGN: why\n")
        tool.STAMP.write_text(tool._stamp_text())

        assert tool.main(["promote"]) == 1
        assert "a legacy name-only entry cannot be promoted" in capsys.readouterr().err

    def test_a_checked_candidate_replaces_the_baseline_and_is_consumed(self, files, tmp_path):
        tool.BASELINE.write_text(f"# committed\n{entry().render()}\n")
        assert self.run_propose([survivor(name="gps_sync.m.x_alpha__mutmut_7")], tmp_path) == 0
        candidate = tool.CANDIDATE.read_text()

        assert tool.main(["promote"]) == 0
        assert tool.BASELINE.read_text() == candidate
        assert not tool.CANDIDATE.exists() and not tool.STAMP.exists()

    def test_the_gate_mode_needs_survivors(self, files, capsys):
        assert tool.main(["gate"]) == 2


class TestTheEntryPointsInTheRepository:
    def test_the_makefile_replaces_the_in_place_target_with_propose_and_promote(self):
        makefile = (REPO / "Makefile").read_text()

        assert re.search(r"^mutants-baseline:", makefile, re.MULTILINE) is None
        assert "--update" not in makefile
        assert re.search(
            r"^mutants-propose:\n\tscripts/mutants.sh --propose$", makefile, re.MULTILINE
        )
        assert re.search(
            r"^mutants-promote:\n\tscripts/mutants.sh --promote$", makefile, re.MULTILINE
        )

    def test_the_script_has_no_path_that_rewrites_the_committed_baseline_in_place(self):
        script = (SCRIPTS / "mutants.sh").read_text()

        assert "--update" not in script.split("case", 1)[1].split("esac", 1)[0]
        assert 'mv "${BASELINE}' not in script
        assert "BASELINE}.new" not in script

    def test_the_candidate_and_its_stamp_are_never_committed(self):
        ignored = (REPO / ".gitignore").read_text().splitlines()

        assert tool.CANDIDATE.name in ignored and tool.STAMP.name in ignored

    def test_the_committed_baseline_parses(self):
        entries = tool.parse_baseline((REPO / "mutation-baseline.txt").read_text())

        assert entries and all(e.category in tool.CATEGORIES for e in entries)


MUTMUT_RESULTS = """\
Results
-------
    gps_sync.matcher.x__speed__mutmut_5: survived
    gps_sync.writer.x_plan__mutmut_9: timeout
    gps_sync.records.x_build__mutmut_2: suspicious
"""
ALL_RESULTS = MUTMUT_RESULTS + "    gps_sync.matcher.x_match__mutmut_1: killed\n"


def partition(tmp_path, results=MUTMUT_RESULTS, all_results=ALL_RESULTS):
    files = {n: tmp_path / n for n in ("r", "a", "s", "u")}
    files["r"].write_text(results)
    files["a"].write_text(all_results)
    code = tool.main(
        [
            "partition",
            "--results",
            str(files["r"]),
            "--all-results",
            str(files["a"]),
            "--survivors",
            str(files["s"]),
            "--undetermined",
            str(files["u"]),
        ]
    )
    return code, files["s"], files["u"]


class TestTheGateReadsTheStatusColumn:
    def test_results_are_grouped_by_the_status_token(self):
        assert tool.parse_results(MUTMUT_RESULTS) == {
            "survived": ["gps_sync.matcher.x__speed__mutmut_5"],
            "timeout": ["gps_sync.writer.x_plan__mutmut_9"],
            "suspicious": ["gps_sync.records.x_build__mutmut_2"],
        }

    def test_only_the_survived_mutant_reaches_the_survivor_list(self, tmp_path, capsys):
        code, survivors, undetermined = partition(tmp_path)

        assert code == 0
        assert survivors.read_text() == "gps_sync.matcher.x__speed__mutmut_5\n"
        assert undetermined.read_text().splitlines() == [
            "gps_sync.records.x_build__mutmut_2\tsuspicious",
            "gps_sync.writer.x_plan__mutmut_9\ttimeout",
        ]
        assert capsys.readouterr().out.splitlines() == [
            "mutants: 4",
            "killed: 1",
            "survived: 1",
            "suspicious: 1",
            "timeout: 1",
        ]

    def test_partition_needs_all_four_files(self, capsys):
        assert tool.main(["partition"]) == 2

    def test_a_run_with_no_mutants_is_refused(self, tmp_path, capsys):
        code, _, _ = partition(tmp_path, results="", all_results="")

        assert code == 1
        assert "does not account for 0 mutants" in capsys.readouterr().err

    def test_a_mutant_the_default_listing_omits_is_refused_as_unaccounted_for(
        self, tmp_path, capsys
    ):
        code, _, _ = partition(tmp_path, results="", all_results=ALL_RESULTS)

        assert code == 1
        assert "does not account for 4 mutants" in capsys.readouterr().err

    def test_the_script_no_longer_takes_the_first_field_of_every_status_line(self):
        script = (SCRIPTS / "mutants.sh").read_text()

        assert "awk" not in script
        assert "partition --results" in script


class TestAnUndeterminedMutantIsNeverAdmitted:
    def test_a_timed_out_name_gets_no_entry_even_when_a_prior_entry_matches_its_digests(self):
        timed_out = survivor(name="gps_sync.m.x_alpha__mutmut_4")
        prior = entry(name="gps_sync.m.x_alpha__mutmut_1")

        candidate, problems = tool.propose([timed_out], [prior], [], [timed_out.name])

        assert candidate == []
        assert [p for p in problems if "undetermined" in p] == [
            f"{timed_out.name}: status undetermined (neither killed nor survived); rerun on a "
            "quiet machine, no entry is written for it"
        ]

    def test_propose_refuses_and_stamps_nothing_when_a_mutant_timed_out(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(tool, "BASELINE", tmp_path / "b")
        monkeypatch.setattr(tool, "CANDIDATE", tmp_path / "c")
        monkeypatch.setattr(tool, "STAMP", tmp_path / "s")
        tool.BASELINE.write_text("# committed\n")
        names, hung = tmp_path / "survivors", tmp_path / "undetermined"
        names.write_text("")
        hung.write_text("gps_sync.m.x_alpha__mutmut_4\ttimeout\n")

        code = tool.main(
            ["propose", "--survivors", str(names), "--undetermined", str(hung)],
            identifier=lambda _names: [],
        )

        assert code == 1
        assert not tool.STAMP.exists()


class TestTheGateRefusesWhatNoTestEvaluated:
    @staticmethod
    def run_gate(tmp_path, monkeypatch, lines):
        monkeypatch.setattr(tool, "BASELINE", tmp_path / "b")
        tool.BASELINE.write_text("# committed\n")
        names, other = tmp_path / "survivors", tmp_path / "undetermined"
        names.write_text("")
        other.write_text("".join(f"{line}\n" for line in lines))
        return tool.main(
            ["gate", "--survivors", str(names), "--undetermined", str(other)],
            identifier=lambda _names: [],
        )

    def test_a_timeout_within_budget_is_noted_and_passes(self, tmp_path, monkeypatch, capsys):
        code = self.run_gate(tmp_path, monkeypatch, ["gps_sync.m.x_alpha__mutmut_4\ttimeout"])

        assert code == 0
        assert (
            "note: timeout, counted as detected within the timeout budget: "
            "gps_sync.m.x_alpha__mutmut_4" in capsys.readouterr().out
        )

    def test_timeouts_over_the_budget_fail(self, tmp_path, monkeypatch, capsys):
        lines = [f"gps_sync.m.x_alpha__mutmut_{n}\ttimeout" for n in range(tool.TIMEOUT_BUDGET + 1)]

        code = self.run_gate(tmp_path, monkeypatch, lines)

        assert code == 1
        assert "over the budget" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "status",
        [
            "no tests",
            "not checked",
            "skipped",
            "check was interrupted by user",
            "suspicious",
            "segfault",
            "caught by type check",
            "a status mutmut has not shipped yet",
        ],
    )
    def test_every_status_that_means_never_evaluated_fails_the_gate(
        self, status, tmp_path, monkeypatch, capsys
    ):
        code = self.run_gate(tmp_path, monkeypatch, [f"gps_sync.m.x_alpha__mutmut_4\t{status}"])

        assert code == 1
        assert "no test evaluated this mutant" in capsys.readouterr().err

    def test_a_line_with_no_status_fails_rather_than_passes(self, tmp_path, monkeypatch):
        assert self.run_gate(tmp_path, monkeypatch, ["gps_sync.m.x_alpha__mutmut_4"]) == 1

    def test_a_timeout_in_a_zero_survivor_module_fails_whatever_the_budget(
        self, tmp_path, monkeypatch, capsys
    ):
        code = self.run_gate(
            tmp_path, monkeypatch, ["gps_sync.telemetry.x_read__mutmut_4\ttimeout"]
        )

        assert code == 1
        assert "must be killed by a test" in capsys.readouterr().err

    def test_the_declared_sets_do_not_overlap_and_omit_survived_and_killed(self):
        detected, unevaluated = tool.TREATED_AS_DETECTED, tool.NEVER_EVALUATED

        assert not detected & unevaluated
        assert not {tool.SURVIVED, tool.KILLED} & (detected | unevaluated)


FAKE_MUTMUT = """\
#!/bin/sh
case "$1" in
  run) exit 0 ;;
  show) printf -- '--- a\\n+++ b\\n-    return 1\\n+    return 2\\n' ;;
  results)
    if [ "$2" = "--all" ]; then cat "$FAKE_RESULTS_ALL"; else cat "$FAKE_RESULTS"; fi ;;
esac
"""
SURVIVOR_LINE = "    gps_sync.matcher.x_match__mutmut_70: survived\n"
KILLED_LINE = "    gps_sync.matcher.x_match__mutmut_71: killed\n"


class TestTheScriptWiresTheDecisionToItsExitCode:
    @pytest.fixture
    def repo(self, tmp_path):
        root = tmp_path / "repo"
        (root / "scripts").mkdir(parents=True)
        for name in ("mutants.sh", "mutation_baseline.py"):
            (root / "scripts" / name).write_text((SCRIPTS / name).read_text())
        (root / "scripts" / "mutants.sh").chmod(0o755)
        (root / ".venv" / "bin").mkdir(parents=True)
        python = root / ".venv" / "bin" / "python"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        mutmut = root / ".venv" / "bin" / "mutmut"
        mutmut.write_text(FAKE_MUTMUT)
        for tool_path in (python, mutmut):
            tool_path.chmod(0o755)
        (root / "mutation-baseline.txt").write_text("# committed\n")
        (root / "testdata").mkdir()
        (root / "src" / "gps_sync").mkdir(parents=True)
        (root / "src" / "gps_sync" / "matcher.py").write_text("def _match():\n    return 1\n")
        return root

    @staticmethod
    def run(root, results, all_results, tmp_path):
        (tmp_path / "results").write_text(results)
        (tmp_path / "results_all").write_text(all_results)
        env = {
            **os.environ,
            "FAKE_RESULTS": str(tmp_path / "results"),
            "FAKE_RESULTS_ALL": str(tmp_path / "results_all"),
            "TMPDIR": str(tmp_path / "tmp"),
        }
        (tmp_path / "tmp").mkdir(exist_ok=True)
        return subprocess.run(
            [str(root / "scripts" / "mutants.sh")],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_an_unadmitted_survivor_makes_the_script_exit_non_zero(self, repo, tmp_path):
        done = self.run(repo, SURVIVOR_LINE, SURVIVOR_LINE + KILLED_LINE, tmp_path)

        assert done.returncode == 1, done.stdout + done.stderr
        assert "not in the baseline" in done.stderr

    def test_a_run_with_only_killed_mutants_passes(self, repo, tmp_path):
        done = self.run(repo, "", KILLED_LINE, tmp_path)

        assert done.returncode == 0, done.stdout + done.stderr
        assert "OK: no survivor outside the baseline" in done.stdout

    def test_a_mutant_no_test_covered_makes_the_script_exit_non_zero(self, repo, tmp_path):
        line = "    gps_sync.writeset.x_helper__mutmut_1: no tests\n"

        done = self.run(repo, line, line + KILLED_LINE, tmp_path)

        assert done.returncode == 1, done.stdout + done.stderr
        assert "no test evaluated this mutant" in done.stderr

    def test_an_empty_run_makes_the_script_exit_non_zero(self, repo, tmp_path):
        assert self.run(repo, "", "", tmp_path).returncode == 1

    def test_the_script_leaves_no_temporary_directory_behind(self, repo, tmp_path):
        self.run(repo, "", KILLED_LINE, tmp_path)

        assert list((tmp_path / "tmp").iterdir()) == []


class TestIdentifyingASurvivor:
    @pytest.fixture
    def checkout(self, tmp_path, monkeypatch):
        root = tmp_path / "checkout"
        package = root / "src" / "gps_sync"
        package.mkdir(parents=True)
        (package / "m.py").write_text("def alpha(x):\n    return x + 1\n")
        stub = root / "mutmut"
        stub.write_text(
            "#!/bin/sh\n"
            '[ "$1" = show ] || exit 3\n'
            "printf -- '--- a\\n+++ b\\n@@ -1 +1 @@\\n-    return x + 1\\n+    return x + 2\\n'\n"
        )
        stub.chmod(0o755)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(root)]
        for command in (["init", "-q"], ["add", "src"], ["commit", "-q", "-m", "x"]):
            subprocess.run([*git, *command], check=True, capture_output=True)
        monkeypatch.setattr(tool, "ROOT", root)
        monkeypatch.setattr(tool, "MUTMUT", stub)
        return package

    def test_the_diff_the_function_and_the_committed_function_are_each_digested(self, checkout):
        (checkout / "m.py").write_text("def alpha(x):\n    return x + 10\n")

        [found] = tool.identify(["gps_sync.m.x_alpha__mutmut_1"])

        assert found.diff == tool.diff_digest("-    return x + 1\n+    return x + 2\n")
        assert found.func == tool.function_digest("def alpha(x):\n    return x + 10\n", ("alpha",))
        assert found.committed_func == tool.function_digest(
            "def alpha(x):\n    return x + 1\n", ("alpha",)
        )
        assert found.func != found.committed_func

    def test_an_unedited_function_has_the_same_digest_as_its_committed_self(self, checkout):
        [found] = tool.identify(["gps_sync.m.x_alpha__mutmut_1"])

        assert found.func == found.committed_func

    def test_a_module_with_no_committed_version_has_no_committed_digest(self, checkout):
        (checkout / "n.py").write_text("def beta():\n    return 1\n")

        [found] = tool.identify(["gps_sync.n.x_beta__mutmut_1"])

        assert found.committed_func is None
        assert found.func == tool.function_digest("def beta():\n    return 1\n", ("beta",))
