import hashlib
import json
import os
from pathlib import Path

import pytest

from gps_sync import review
from gps_sync.errors import ToolFailure
from gps_sync.model import Unmatchable, UnmatchableReason
from gps_sync.review import (
    MARKER_NAME,
    check_precondition,
    check_review_root,
    derive_default_review_root,
    rebuild,
    resolve_layout,
)
from tests.conftest import World
from tests.support import at, raf_json


def tree_digest(root: Path) -> dict[str, str]:
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            digest[str(path)] = "link:" + os.readlink(path)
        elif path.is_file():
            digest[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest[str(path)] = "dir"
    return digest


def marker(media: Path, version: int = 1) -> str:
    return json.dumps({"tool": "gps-sync", "marker_version": version, "media_root": str(media)})


def link_count(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_symlink())


MISS = raf_json(dto="2026:10:26 11:30:00")


class TestReviewRootContainment:
    HOME = Path("/Users/someone")

    @pytest.mark.parametrize(
        "root",
        ["/Users/someone/media", "/Users/someone", "/", "/Users"],
        ids=["is-the-media-root", "home-itself", "slash", "ancestor-of-media"],
    )
    def test_a_root_that_is_or_contains_the_media_root_or_is_slash_or_home_is_refused(self, root):
        with pytest.raises(ToolFailure):
            check_review_root(Path(root), Path("/Users/someone/media"), self.HOME)

    def test_a_root_inside_the_media_tree_is_permitted(self):
        check_review_root(
            Path("/Users/someone/media/review"), Path("/Users/someone/media"), self.HOME
        )

    def test_an_unrelated_root_is_permitted(self):
        check_review_root(Path("/var/review"), Path("/Users/someone/media"), self.HOME)

    def test_the_derived_default_root_goes_through_the_same_rule(self, tmp_path, monkeypatch):
        media = tmp_path / "media"
        media.mkdir()
        monkeypatch.setattr(review, "derive_default_review_root", lambda m, h: m.parent)

        with pytest.raises(ToolFailure, match="contains"):
            resolve_layout(media, None, tmp_path / "home")

    def test_media_equal_to_home_places_the_default_root_inside_and_is_accepted(self, tmp_path):
        layout = resolve_layout(tmp_path, None, tmp_path)

        assert layout.review_root.is_relative_to(tmp_path)

    def test_the_default_root_is_one_per_media_tree_even_for_a_shared_basename(self, tmp_path):
        a = derive_default_review_root(tmp_path / "x" / "photos", tmp_path)
        b = derive_default_review_root(tmp_path / "y" / "photos", tmp_path)

        assert a != b and a.parent == b.parent == tmp_path / "gps-sync"

    def test_the_default_root_is_derived_from_home_so_a_test_can_redirect_it(self, tmp_path):
        layout = resolve_layout(tmp_path / "media", None, tmp_path / "h")

        assert layout.review_root.is_relative_to((tmp_path / "h").resolve())


class TestPreflightRefusals:
    @pytest.mark.parametrize("mode", ["dry-run", "write"])
    def test_a_review_dir_that_contains_the_media_root_is_exit_two_in_both_modes(self, world, mode):
        world.add("a.RAF")

        result = world.run(
            world.options(review_dir=world.root, write=mode == "write"),
            world.applying() if mode == "write" else None,
        )

        assert result.code == 2 and "contains" in result.err and result.out == ""

    def test_a_review_dir_equal_to_the_media_root_is_exit_two(self, world):
        assert world.run(world.options(review_dir=world.media)).code == 2

    def test_slash_and_home_as_review_dir_are_refused(self, world):
        assert world.run(world.options(review_dir=Path("/"))).code == 2
        assert world.run(world.options(review_dir=world.home)).code == 2

    def test_a_foreign_review_dir_without_a_marker_is_refused_and_nothing_is_removed(self, world):
        world.add("a.RAF")
        target = world.root / "review"
        (target / "_unprocessed" / "precious").mkdir(parents=True)
        (target / "_unprocessed" / "precious" / "notes.txt").write_text("irreplaceable")
        (target / "_unprocessed" / "catalog.cocatalog").write_bytes(b"c1")
        before = tree_digest(target)

        result = world.run(world.options(review_dir=target))

        assert result.code == 2 and "will not remove" in result.err
        assert tree_digest(target) == before

    def test_an_empty_unmarked_target_is_accepted_marked_and_populated(self, world):
        world.add("a.RAF", MISS)
        target = world.root / "review"
        (target / "_unprocessed").mkdir(parents=True)

        result = world.run(world.options(review_dir=target))

        assert result.code == 0
        assert (target / "_unprocessed" / MARKER_NAME).is_file()
        assert link_count(target / "_unprocessed") == 1

    def test_a_marked_target_with_prior_links_is_rebuilt_without_them(self, world):
        world.add("a.RAF", MISS)
        target = world.root / "review"
        world.run(world.options(review_dir=target))
        stale = target / "_unprocessed" / "already-tagged" / "stale"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.symlink_to("/nonexistent")

        world.run(world.options(review_dir=target))

        assert not os.path.lexists(stale)
        assert link_count(target / "_unprocessed") == 1

    def test_a_marker_naming_a_different_media_root_is_refused_naming_both_and_removes_nothing(
        self, world, tmp_path
    ):
        world.add("a.RAF", MISS)
        target = world.root / "shared-review"
        world.run(world.options(review_dir=target))
        before = tree_digest(target)

        other = World(tmp_path / "other")
        other.media.mkdir(parents=True)
        other.tracks.mkdir()
        (other.tracks / "walk.gpx").write_text((world.tracks / "walk.gpx").read_text())
        other.home.mkdir()
        other.add("z.RAF", MISS)
        result = other.run(other.options(review_dir=target))

        assert result.code == 2
        assert str(world.media.resolve()) in result.err and str(other.media.resolve()) in result.err
        assert tree_digest(target) == before

    def test_a_marker_with_an_unrecognised_version_is_refused_naming_it(self, world):
        world.add("a.RAF")
        target = world.root / "review"
        (target / "_unprocessed").mkdir(parents=True)
        (target / "_unprocessed" / MARKER_NAME).write_text(marker(world.media.resolve(), 99))
        (target / "_unprocessed" / "x").mkdir()

        result = world.run(world.options(review_dir=target))

        assert result.code == 2 and "99" in result.err

    def test_a_marked_target_that_also_holds_a_foreign_regular_file_is_refused_naming_it(
        self, world
    ):
        world.add("a.RAF", MISS)
        target = world.root / "review"
        world.run(world.options(review_dir=target))
        foreign = target / "_unprocessed" / "no-track-coverage" / "capture-one.cot"
        foreign.write_text("sidecar")
        before = tree_digest(target)

        result = world.run(world.options(review_dir=target))

        assert result.code == 2 and "capture-one.cot" in result.err
        assert tree_digest(target) == before

    def test_a_marked_target_with_an_undeclared_top_level_directory_is_refused(self, world):
        world.add("a.RAF", MISS)
        target = world.root / "review"
        world.run(world.options(review_dir=target))
        (target / "_unprocessed" / "my-folder").mkdir()

        assert world.run(world.options(review_dir=target)).code == 2

    def test_a_malformed_marker_is_refused(self, world):
        target = world.root / "review"
        (target / "_unprocessed").mkdir(parents=True)
        (target / "_unprocessed" / MARKER_NAME).write_text("{")
        (target / "_unprocessed" / "x").mkdir()

        assert world.run(world.options(review_dir=target)).code == 2

    def test_runs_is_never_touched_by_a_rebuild(self, world):
        world.add("a.RAF", MISS)
        world.run()
        prior = world.review_root() / "runs" / "20250101T000000Z-1.json"
        prior.write_text('{"phase": "complete"}')

        world.run()

        assert prior.read_text() == '{"phase": "complete"}'

    def test_an_unwritable_run_artifact_root_is_exit_two_before_the_media_walk(self, world):
        world.add("a.RAF")
        locked = world.root / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        transport = world.transport()
        try:
            result = world.run(world.options(review_dir=locked / "review"), transport)
        finally:
            locked.chmod(0o755)

        assert result.code == 2
        assert transport.read_calls == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_an_unreadable_review_folder_is_exit_two_naming_it_with_no_traceback(self, world):
        world.add("a.RAF")
        artifacts = world.root / "artifacts"
        unprocessed = artifacts / "_unprocessed"
        unprocessed.mkdir(parents=True)
        unprocessed.chmod(0)
        transport = world.transport()
        try:
            result = world.run(world.options(review_dir=artifacts), transport)
        finally:
            unprocessed.chmod(0o755)

        assert result.code == 2
        assert str(unprocessed) in result.err
        assert "Traceback" not in result.err
        assert result.out == ""
        assert transport.read_calls == []

    def test_a_temp_directory_base_that_cannot_be_created_is_exit_two_naming_it(self, world):
        world.add("a.RAF")
        blocker = world.root / "blocker"
        blocker.write_text("a file, so nothing can be created beneath it")
        base = blocker / "tmp"
        transport = world.transport()

        result = world.run(world.options(), transport, temp_base=base)

        assert result.code == 2
        assert str(base) in result.err
        assert "Traceback" not in result.err
        assert result.out == ""
        assert transport.read_calls == []

    def test_the_temp_directory_may_not_lie_inside_the_media_tree(self, world, monkeypatch):
        import tempfile

        world.add("a.RAF")
        monkeypatch.setattr(tempfile, "tempdir", str(world.media))

        result = world.run()

        assert result.code == 2 and "temporary directory" in result.err


class TestReviewFolderInsideTheMediaTree:
    def test_it_is_accepted_populated_and_pruned_on_a_second_pass(self, world):
        world.add("a.RAF", MISS)
        world.add("b.RAF")
        inside = world.media / "review"

        first = world.run(world.options(review_dir=inside, json=True))
        second = world.run(world.options(review_dir=inside, json=True))

        import json as json_module

        a, b = (json_module.loads(r.out) for r in (first, second))
        assert first.code == second.code == 0
        assert a["summary"]["variant_counts"] == b["summary"]["variant_counts"]
        assert link_count(inside / "_unprocessed") == 1
        assert all("/review/" not in f["path"] for f in b["files"])
        assert str((inside / "_unprocessed").resolve()) in b["artifacts"]["pruned"]
        assert str((inside / "runs").resolve()) in b["artifacts"]["pruned"]


class TestBoundedRebuild:
    def outcomes(self, media: Path, *names: str):
        return [
            Unmatchable(str(media / n), at(0), UnmatchableReason.OUTSIDE_COVERAGE, "s", 1.0)
            for n in names
        ]

    def prepare(self, tmp_path: Path):
        media = tmp_path / "media"
        media.mkdir()
        unprocessed = tmp_path / "rev" / "_unprocessed"
        return media, unprocessed

    def test_links_mirror_the_media_relative_path_so_equal_basenames_cannot_collide(self, tmp_path):
        media, unprocessed = self.prepare(tmp_path)
        outcomes = self.outcomes(media, "a/DSCF1.RAF", "b/DSCF1.RAF")

        report = rebuild(unprocessed, media, outcomes)

        bucket = unprocessed / "no-track-coverage"
        assert not report.incomplete and report.links_created == 2
        assert sorted(
            p.relative_to(bucket).as_posix() for p in bucket.rglob("*") if p.is_symlink()
        ) == [
            "a/DSCF1.RAF",
            "b/DSCF1.RAF",
        ]
        assert os.readlink(bucket / "a" / "DSCF1.RAF") == str(media / "a/DSCF1.RAF")

    def test_a_foreign_file_appearing_after_preflight_is_refused_at_removal_time(self, tmp_path):
        media, unprocessed = self.prepare(tmp_path)
        rebuild(unprocessed, media, self.outcomes(media, "a.RAF"))
        check_precondition(unprocessed, media)
        late = unprocessed / "no-track-coverage" / "capture-one-sidecar.cos"
        late.write_text("appeared after preflight")

        report = rebuild(unprocessed, media, self.outcomes(media, "a.RAF"))

        assert late.read_text() == "appeared after preflight"
        assert report.incomplete and str(late) in report.refused

    def test_a_regular_file_standing_where_a_mirrored_directory_goes_is_never_replaced(
        self, tmp_path
    ):
        media, unprocessed = self.prepare(tmp_path)
        bucket = unprocessed / "no-track-coverage"
        bucket.mkdir(parents=True)
        blocker = bucket / "sub"
        blocker.write_bytes(b"i am a file")

        report = rebuild(unprocessed, media, self.outcomes(media, "sub/a.RAF"))

        assert blocker.read_bytes() == b"i am a file"
        assert report.incomplete
        assert str(blocker) in report.refused
        assert report.links_created == 0

    def test_a_symlink_standing_where_a_mirrored_directory_goes_is_never_replaced(
        self, tmp_path, monkeypatch
    ):
        media, unprocessed = self.prepare(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        bucket = unprocessed / "no-track-coverage"
        bucket.mkdir(parents=True)
        (bucket / "sub").symlink_to(elsewhere, target_is_directory=True)
        # The removal pass would delete a stale symlink; keep it so only the link-creation rule
        # for mirrored components is exercised.
        monkeypatch.setattr(review, "_remove_prior", lambda *_args: None)

        report = rebuild(unprocessed, media, self.outcomes(media, "sub/a.RAF"))

        assert (bucket / "sub").is_symlink()
        assert list(elsewhere.iterdir()) == []
        assert report.incomplete and report.links_created == 0

    def test_a_failure_partway_through_leaves_earlier_buckets_and_reports_incomplete(
        self, tmp_path, monkeypatch
    ):
        media, unprocessed = self.prepare(tmp_path)
        real = os.symlink
        calls = {"n": 0}

        def flaky(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "symlink", flaky)

        report = rebuild(unprocessed, media, self.outcomes(media, "a.RAF", "b.RAF", "c.RAF"))

        assert report.incomplete and report.links_created == 2
        assert any("disk full" in e for e in report.errors)

    def test_only_outcomes_with_a_subfolder_are_linked(self, tmp_path):
        from gps_sync.model import Ignored, IgnoredReason, Interpolated, Planned, Position, Via

        media, unprocessed = self.prepare(tmp_path)
        outcomes = [
            Ignored(str(media / "x.txt"), IgnoredReason.NOT_A_STILL),
            Planned(
                str(media / "p.RAF"), at(0), Position(1, 2), Via.INTERPOLATED, Interpolated("s", 1)
            ),
        ]

        report = rebuild(unprocessed, media, outcomes)

        assert report.links_created == 0 and link_count(unprocessed) == 0


class TestReviewFolderFailuresAreExitThree:
    @pytest.mark.parametrize("write", [False, True])
    def test_a_partial_symlink_failure_is_exit_three_with_the_tally_intact(
        self, world, monkeypatch, write
    ):
        world.add("a.RAF", MISS)
        world.add("b.RAF", raf_json(dto="2026:10:26 11:31:00"))
        real = os.symlink
        calls = {"n": 0}

        def flaky(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("no space")
            return real(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "symlink", flaky)

        result = world.run(world.options(write=write))

        assert result.code == 3
        assert "Unmatchable: 2" in result.out
        assert "Run artifacts incomplete" in result.out and "no space" in result.out

    def test_a_foreign_file_that_appears_between_preflight_and_rebuild_is_exit_three(self, world):
        world.add("a.RAF", MISS)
        world.run()
        bucket = world.review_root() / "_unprocessed" / "no-track-coverage"
        foreign = bucket / "capture-one-sidecar.cos"
        transport = world.transport()
        original = transport.read

        def read_then_intrude(argfile):
            foreign.write_text("appeared after preflight")
            return original(argfile)

        transport.read = read_then_intrude  # type: ignore[method-assign]

        result = world.run(transport=transport)

        assert result.code == 3
        assert foreign.read_text() == "appeared after preflight"
        assert "capture-one-sidecar.cos" in result.out
