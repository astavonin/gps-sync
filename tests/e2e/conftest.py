"""Fixtures for end-to-end tests: a copied, GPS-stripped tree driven through the real CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support import EXIFTOOL, REPO, TESTDATA, check_corpus, exiftool

SRC = REPO / "src"

# The ignore-path pair each tree plants for itself. The expected "Ignored" total counts these two
# on top of the corpus's own three non-stills (the .xmp sidecar, readme.txt and the MP4), which is
# why a tree's figure and the bare corpus's differ by construction. They are deliberately not
# committed to testdata/: they are hand-made placeholders, and a committed copy at the same path
# would let the planting be deleted unnoticed.
PLANTED_IGNORED = ("media/.DS_Store", "media/day1/.DS_Store")


def plant_ignored_fixtures(root: Path) -> None:
    """Replace any stray ``.DS_Store`` in ``root`` with exactly the ``PLANTED_IGNORED`` files."""
    for stray in root.rglob(".DS_Store"):
        stray.unlink()
    for relative in PLANTED_IGNORED:
        (root / relative).write_bytes(b"\x00\x00\x00\x01Bud1")


def missing_ignored_fixtures(root: Path) -> list[str]:
    """Name each planted ignore-path file absent from ``root``."""
    return [relative for relative in PLANTED_IGNORED if not (root / relative).is_file()]


@dataclass
class Cli:
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> dict:
        return json.loads(self.stdout)


@dataclass
class Tree:
    root: Path
    home: Path

    @property
    def media(self) -> Path:
        return self.root / "media"

    @property
    def tracks(self) -> Path:
        return self.root / "tracks"

    def cli(self, *args: str, env: dict[str, str] | None = None) -> Cli:
        environment = {
            **os.environ,
            "PYTHONPATH": str(SRC),
            "HOME": str(self.home),
            **(env or {}),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "gps_sync",
                "--media",
                str(self.media),
                "--tracks",
                str(self.tracks),
                *args,
            ],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        return Cli(completed.returncode, completed.stdout, completed.stderr)

    def review_root(self) -> Path:
        [root] = list((self.home / "gps-sync").iterdir())
        return root


@pytest.fixture(scope="session")
def tree_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The reference-shaped fixture: 19 files (12 media, 7 tracks), every RAF position-stripped."""
    if not Path(EXIFTOOL).exists():
        pytest.skip("exiftool is not installed")
    if not (TESTDATA / "media").is_dir() or not (TESTDATA / "tracks").is_dir():
        pytest.skip("testdata/ is not present")
    check_corpus()
    template = tmp_path_factory.mktemp("template")
    subprocess.run(["cp", "-c", "-R", str(TESTDATA / "media"), str(template / "media")], check=True)
    shutil.copytree(TESTDATA / "tracks", template / "tracks")
    exiftool(
        "-q",
        "-overwrite_original",
        "-gps:all=",
        "-xmp:gps:all=",
        "-r",
        "-ext",
        "RAF",
        str(template / "media"),
    )
    plant_ignored_fixtures(template)
    for path in (template / "media").rglob("*"):
        if path.is_file():
            path.chmod(0o644)
    return template


@pytest.fixture
def tree(tree_template: Path, tmp_path: Path) -> Tree:
    missing = missing_ignored_fixtures(tree_template)
    assert not missing, (
        f"the e2e template lacks its planted ignore-path files {missing}; the expected 'Ignored' "
        "total counts them, so every e2e total would shift"
    )
    root = tmp_path / "tree"
    root.mkdir()
    subprocess.run(
        ["cp", "-c", "-R", str(tree_template / "media"), str(root / "media")], check=True
    )
    shutil.copytree(tree_template / "tracks", root / "tracks")
    home = tmp_path / "home"
    home.mkdir()
    return Tree(root, home)
