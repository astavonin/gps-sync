import os
from pathlib import Path

import pytest

from gps_sync.discovery import discover
from gps_sync.errors import ToolFailure
from gps_sync.model import IgnoredReason


def touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def reasons(result) -> dict[str, IgnoredReason]:
    return {Path(i.path).name: i.reason for i in result.ignored}


def names(result) -> list[str]:
    return [Path(p).name for p in result.candidates]


def test_video_is_ignored_as_not_a_still_for_issue_001(tmp_path):
    touch(tmp_path / "clip.MP4")
    touch(tmp_path / "clip.MOV")

    result = discover(tmp_path, frozenset())

    assert result.candidates == ()
    assert reasons(result) == {
        "clip.MP4": IgnoredReason.NOT_A_STILL,
        "clip.MOV": IgnoredReason.NOT_A_STILL,
    }


@pytest.mark.parametrize("name", [".DS_Store", "a.xmp", "notes.txt", "noext", "a.RAF.xmp"])
def test_sidecars_and_system_files_are_ignored_silently_never_failed(tmp_path, name):
    touch(tmp_path / name)

    result = discover(tmp_path, frozenset())

    assert result.candidates == ()
    assert reasons(result) == {name: IgnoredReason.NOT_A_STILL}


@pytest.mark.parametrize(
    "name", ["a.raf", "a.RAF", "a.Raf", "b.dng", "c.JPG", "d.jpeg", "e.TIF", "f.tiff", "g.HEIC"]
)
def test_extension_matching_is_case_insensitive_and_covers_the_candidate_allowlist(tmp_path, name):
    touch(tmp_path / name)

    assert names(discover(tmp_path, frozenset())) == [name]


def test_an_original_sibling_is_never_a_candidate_and_is_ignored(tmp_path):
    touch(tmp_path / "DSCF1234.RAF")
    touch(tmp_path / "DSCF1234.RAF_original")
    touch(tmp_path / "DSCF9999.raf_original")
    touch(tmp_path / "DSCF1234.DNG_original")

    result = discover(tmp_path, frozenset())

    assert names(result) == ["DSCF1234.RAF"]
    assert set(reasons(result).values()) == {IgnoredReason.NOT_A_STILL}
    assert len(result.ignored) == 3


def test_a_recognised_still_outside_the_write_allowlist_is_still_a_candidate(tmp_path):
    touch(tmp_path / "a.HEIC")

    assert names(discover(tmp_path, frozenset())) == ["a.HEIC"]


def test_recursion_is_deterministic_and_paths_are_absolute(tmp_path):
    touch(tmp_path / "b" / "2.RAF")
    touch(tmp_path / "a" / "1.RAF")
    touch(tmp_path / "c.RAF")

    first = discover(tmp_path, frozenset()).candidates
    second = discover(tmp_path, frozenset()).candidates

    assert first == second
    assert all(os.path.isabs(p) for p in first)
    assert [Path(p).name for p in first] == ["1.RAF", "2.RAF", "c.RAF"]


def test_a_symlink_to_a_file_is_ignored_with_a_reason_and_names_its_in_tree_target(tmp_path):
    real = touch(tmp_path / "real.RAF")
    (tmp_path / "link.RAF").symlink_to(real)

    result = discover(tmp_path, frozenset())

    assert names(result) == ["real.RAF"]
    [ignored] = result.ignored
    assert ignored.reason is IgnoredReason.SYMLINK
    assert ignored.detail == str(real)


def test_a_symlink_whose_target_is_outside_the_tree_is_skipped_without_naming_it(tmp_path):
    outside = touch(tmp_path / "outside" / "x.RAF")
    media = tmp_path / "media"
    media.mkdir()
    (media / "link.RAF").symlink_to(outside)

    result = discover(media, frozenset())

    assert result.candidates == ()
    assert result.ignored[0].reason is IgnoredReason.SYMLINK
    assert result.ignored[0].detail is None


def test_a_symlinked_directory_contributes_its_reason_instead_of_vanishing(tmp_path):
    touch(tmp_path / "real" / "a.RAF")
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)

    result = discover(tmp_path, frozenset())

    assert names(result) == ["a.RAF"]
    assert [i.reason for i in result.ignored] == [IgnoredReason.SYMLINK]


def test_a_dangling_symlink_is_ignored(tmp_path):
    (tmp_path / "dangling.RAF").symlink_to(tmp_path / "gone")

    result = discover(tmp_path, frozenset())

    assert result.ignored[0].reason is IgnoredReason.SYMLINK


def test_the_media_root_itself_is_scanned_when_the_caller_resolved_it_from_a_symlink(tmp_path):
    touch(tmp_path / "real" / "a.RAF")
    (tmp_path / "root").symlink_to(tmp_path / "real", target_is_directory=True)

    result = discover((tmp_path / "root").resolve(), frozenset())

    assert names(result) == ["a.RAF"]


def test_two_hard_links_to_one_inode_yield_one_candidate_and_one_duplicate(tmp_path):
    first = touch(tmp_path / "a.RAF")
    os.link(first, tmp_path / "b.RAF")

    result = discover(tmp_path, frozenset())

    assert names(result) == ["a.RAF"]
    [duplicate] = result.ignored
    assert duplicate.reason is IgnoredReason.DUPLICATE_INODE
    assert duplicate.path == str(tmp_path / "b.RAF")
    assert duplicate.detail == str(first)


@pytest.mark.parametrize("inside", [True, False])
def test_the_run_artifact_subtrees_are_pruned_wherever_they_sit(tmp_path, inside):
    media = tmp_path / "media"
    touch(media / "keep.RAF")
    review = media / "review" if inside else tmp_path / "elsewhere"
    touch(review / "_unprocessed" / "x" / "linked.RAF")
    touch(review / "runs" / "20261114T000000Z-1.json")
    prune = frozenset({review / "_unprocessed", review / "runs"})

    result = discover(media, prune)

    assert names(result) == ["keep.RAF"]
    assert len(result.pruned) == (2 if inside else 0)


def test_only_the_run_artifact_subtrees_are_pruned_and_not_the_whole_review_root(tmp_path):
    touch(tmp_path / "review" / "other" / "real.RAF")
    prune = frozenset({tmp_path / "review" / "_unprocessed", tmp_path / "review" / "runs"})

    assert names(discover(tmp_path, prune)) == ["real.RAF"]


def test_a_missing_media_root_is_a_tool_failure(tmp_path):
    with pytest.raises(ToolFailure):
        discover(tmp_path / "absent", frozenset())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unreadable_subdirectory_is_a_tool_failure_rather_than_a_silent_hole(tmp_path):
    locked = tmp_path / "locked"
    touch(locked / "a.RAF")
    locked.chmod(0)
    try:
        with pytest.raises(ToolFailure, match="locked"):
            discover(tmp_path, frozenset())
    finally:
        locked.chmod(0o755)
