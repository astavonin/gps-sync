"""The corpus gate names every drifted fixture and refuses to pass without a usable baseline."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import (
    BASELINE,
    CORPUS_BASELINE_ENV,
    EXIFTOOL,
    REPO,
    TESTDATA,
    CorpusError,
    check_corpus,
    corpus_mismatches,
)


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def write_baseline(root, entries: dict[str, bytes]):
    baseline = root / ".baseline.md5"
    baseline.write_text("".join(f"{md5(data)}  {name}\n" for name, data in entries.items()))
    return baseline


@pytest.fixture(autouse=True)
def enforcing(monkeypatch):
    monkeypatch.delenv(CORPUS_BASELINE_ENV, raising=False)


class TestMismatches:
    def test_an_unchanged_corpus_has_no_mismatches(self, tmp_path):
        (tmp_path / "a.RAF").write_bytes(b"one")
        baseline = write_baseline(tmp_path, {"a.RAF": b"one"})

        assert corpus_mismatches(baseline, tmp_path) == []

    def test_every_modified_file_is_named_and_unmodified_ones_are_not(self, tmp_path):
        (tmp_path / "a.RAF").write_bytes(b"changed")
        (tmp_path / "b.RAF").write_bytes(b"two")
        (tmp_path / "c.RAF").write_bytes(b"also changed")
        baseline = write_baseline(tmp_path, {"a.RAF": b"one", "b.RAF": b"two", "c.RAF": b"three"})

        problems = corpus_mismatches(baseline, tmp_path)

        assert [p.split(":")[0] for p in problems] == ["a.RAF", "c.RAF"]

    def test_a_missing_file_is_reported_as_missing(self, tmp_path):
        baseline = write_baseline(tmp_path, {"gone.RAF": b"x"})

        assert corpus_mismatches(baseline, tmp_path) == ["gone.RAF: missing"]

    def test_a_file_absent_from_the_baseline_is_reported_wherever_it_sits(self, tmp_path):
        (tmp_path / "a.RAF").write_bytes(b"one")
        (tmp_path / "_unprocessed" / "outside-track").mkdir(parents=True)
        (tmp_path / "_unprocessed" / "outside-track" / "a.RAF").symlink_to(tmp_path / "a.RAF")
        (tmp_path / "stray.txt").write_bytes(b"x")
        baseline = write_baseline(tmp_path, {"a.RAF": b"one"})

        problems = corpus_mismatches(baseline, tmp_path)

        assert sorted(problems) == [
            "_unprocessed/outside-track/a.RAF: not in the baseline",
            "stray.txt: not in the baseline",
        ]

    def test_the_manifest_is_pinned_like_any_fixture(self, tmp_path):
        (tmp_path / "MANIFEST.json").write_bytes(b"{}")
        baseline = write_baseline(tmp_path, {"MANIFEST.json": b"{ }"})

        assert corpus_mismatches(baseline, tmp_path) == [
            "MANIFEST.json: md5 differs from the baseline"
        ]

    def test_finder_metadata_is_not_a_fixture(self, tmp_path):
        (tmp_path / "a.RAF").write_bytes(b"one")
        (tmp_path / ".DS_Store").write_bytes(b"x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".DS_Store").write_bytes(b"y")
        baseline = write_baseline(tmp_path, {"a.RAF": b"one", "sub/.DS_Store": b"stale"})

        assert corpus_mismatches(baseline, tmp_path) == []


class TestGate:
    def test_an_absent_baseline_raises_rather_than_reading_as_clean(self, tmp_path):
        with pytest.raises(CorpusError, match="cannot read the corpus baseline"):
            check_corpus(tmp_path / ".baseline.md5", tmp_path)

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("", "lists no files"),
            ("\n\n", "lists no files"),
            ("not a baseline\n", "is not '<md5>  <path>'"),
            (f"{md5(b'x')}  a.RAF\n{md5(b'y')[:20]}\n", "is not '<md5>  <path>'"),
            (f"{md5(b'x')[:-1]}  a.RAF\n", "is not '<md5>  <path>'"),
            (f"{md5(b'x')} a.RAF\n", "is not '<md5>  <path>'"),
        ],
        ids=["empty", "blank", "garbage", "truncated-line", "short-digest", "single-space"],
    )
    def test_an_unparseable_baseline_raises_rather_than_reading_as_clean(
        self, tmp_path, text, message
    ):
        (tmp_path / "a.RAF").write_bytes(b"x")
        baseline = tmp_path / ".baseline.md5"
        baseline.write_text(text)

        with pytest.raises(CorpusError, match=message):
            check_corpus(baseline, tmp_path)

    def test_a_zero_byte_baseline_in_an_otherwise_empty_directory_is_not_a_clean_corpus(
        self, tmp_path
    ):
        # Nothing else is in the directory, so only the empty-baseline rule can refuse it: with no
        # rows and no files, the drift walk finds nothing to report.
        baseline = tmp_path / ".baseline.md5"
        baseline.write_bytes(b"")

        with pytest.raises(CorpusError, match="lists no files"):
            check_corpus(baseline, tmp_path)

    def test_drift_raises_by_default_naming_every_mismatched_file(self, tmp_path):
        (tmp_path / "a.RAF").write_bytes(b"changed")
        (tmp_path / "b.RAF").write_bytes(b"two")
        (tmp_path / "stray.RAF").write_bytes(b"new")
        baseline = write_baseline(
            tmp_path, {"a.RAF": b"one", "b.RAF": b"two", "gone.RAF": b"three"}
        )

        with pytest.raises(CorpusError) as raised:
            check_corpus(baseline, tmp_path)

        message = str(raised.value)
        assert "a.RAF: md5 differs" in message
        assert "gone.RAF: missing" in message
        assert "stray.RAF: not in the baseline" in message
        assert "b.RAF:" not in message

    def test_a_pristine_corpus_passes(self, tmp_path):
        (tmp_path / "a.RAF").write_bytes(b"one")

        check_corpus(write_baseline(tmp_path, {"a.RAF": b"one"}), tmp_path)

    @pytest.mark.parametrize("baseline_present", [True, False], ids=["drifted", "no-baseline"])
    def test_the_opt_out_does_not_raise(self, tmp_path, monkeypatch, baseline_present):
        (tmp_path / "a.RAF").write_bytes(b"changed")
        baseline = tmp_path / ".baseline.md5"
        if baseline_present:
            write_baseline(tmp_path, {"a.RAF": b"one"})
        monkeypatch.setenv(CORPUS_BASELINE_ENV, "ignore")

        check_corpus(baseline, tmp_path)

    @pytest.mark.parametrize("value", ["enforce", "warn", "1", ""])
    def test_only_the_literal_ignore_value_opts_out(self, tmp_path, monkeypatch, value):
        (tmp_path / "a.RAF").write_bytes(b"changed")
        baseline = write_baseline(tmp_path, {"a.RAF": b"one"})
        monkeypatch.setenv(CORPUS_BASELINE_ENV, value)

        with pytest.raises(CorpusError):
            check_corpus(baseline, tmp_path)

    @pytest.mark.skipif(not (TESTDATA / "media").is_dir(), reason="testdata/ is not present")
    def test_the_committed_corpus_matches_its_baseline(self):
        check_corpus()


# The two session fixtures that derive files from the corpus must each run the gate. A probe
# test requesting the fixture runs in a child pytest whose baseline is deliberately wrong.
PROBE_PLUGINS = {
    "assets": ("tests.conftest",),
    "tree_template": ("tests.conftest", "tests.e2e.conftest"),
}


@pytest.mark.skipif(not (TESTDATA / "media").is_dir(), reason="testdata/ is not present")
@pytest.mark.skipif(not Path(EXIFTOOL).exists(), reason="exiftool is not installed")
@pytest.mark.parametrize("fixture", sorted(PROBE_PLUGINS))
def test_each_corpus_deriving_fixture_fails_its_requesting_test_on_drift(fixture, tmp_path):
    recorded = BASELINE.read_text().splitlines()
    _, name = recorded[0].split("  ", 1)
    # Absolute rows let the drifted baseline live outside the repository, so nothing under
    # testdata/ is ever touched to stage the drift.
    rows = [f"{'0' * 32}  {REPO / name}"] + [
        f"{md5sum}  {REPO / path}" for md5sum, path in (r.split("  ", 1) for r in recorded[1:])
    ]
    drifted = tmp_path / ".baseline.md5"
    drifted.write_text("\n".join(rows) + "\n")
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from pathlib import Path\n"
        "import tests.support as support\n"
        f"support.BASELINE = Path({str(drifted)!r})\n"
        f"def test_probe({fixture}):\n    pass\n"
    )
    plugins = [arg for plugin in PROBE_PLUGINS[fixture] for arg in ("-p", plugin)]

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *plugins, str(probe)],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={
            **{k: v for k, v in os.environ.items() if k != CORPUS_BASELINE_ENV},
            "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{REPO}",
        },
        check=False,
    )

    assert result.returncode != 0
    assert "1 error" in result.stdout
    assert f"{REPO / name}: md5 differs from the baseline" in result.stdout
