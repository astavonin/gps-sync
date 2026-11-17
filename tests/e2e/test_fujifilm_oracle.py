"""The ``-geotag`` cross-check extended to the Fujifilm-shaped MOV path.

exiftool's own geotagging runs here and nowhere in ``src/``: it is an independent oracle for the
position the GPX path plans, exactly as the stills cross-check in ``test_full_tree`` uses it.
"""

import subprocess
from pathlib import Path

import pytest

from tests.conftest import track_text
from tests.e2e.conftest import Tree
from tests.e2e.test_full_tree import by_path
from tests.integration.helpers import tags
from tests.support import EXIFTOOL, exiftool


@pytest.fixture
def mov_tree(tmp_path: Path, assets) -> Tree:
    root = tmp_path / "tree"
    (root / "media").mkdir(parents=True)
    (root / "tracks").mkdir()
    (root / "tracks" / "walk.gpx").write_text(track_text())
    subprocess.run(
        ["cp", "-c", str(assets.mov_fujifilm), str(root / "media" / "covered.MOV")], check=True
    )
    subprocess.run(
        ["cp", "-c", str(assets.mov_fujifilm_uncovered), str(root / "media" / "uncovered.MOV")],
        check=True,
    )
    home = tmp_path / "home"
    home.mkdir()
    return Tree(root, home)


def test_independent_cross_check_against_exiftools_own_geotag_on_the_mov_path(
    mov_tree: Tree, tmp_path: Path
):
    """The oracle agrees on the clip the track covers and writes nothing for the one it does not."""
    oracle = tmp_path / "oracle"
    subprocess.run(["cp", "-c", "-R", str(mov_tree.media), str(oracle)], check=True)
    gpx_args = [arg for g in sorted(mov_tree.tracks.glob("*.gpx")) for arg in ("-geotag", str(g))]
    exiftool("-q", "-q", "-overwrite_original", *gpx_args, "-r", "-ext", "MOV", str(oracle))

    ours = by_path(mov_tree.cli("--json", "--video").json())
    compared = refused = 0
    for path, outcome in ours.items():
        theirs = tags(
            oracle / Path(path).relative_to(mov_tree.media), "-GPSLatitude", "-GPSLongitude"
        )
        if outcome["variant"] == "Planned":
            assert theirs["GPSLatitude"] == pytest.approx(outcome["position"]["lat"], abs=1e-6)
            assert theirs["GPSLongitude"] == pytest.approx(outcome["position"]["lon"], abs=1e-6)
            compared += 1
        else:
            assert "GPSLatitude" not in theirs, f"exiftool matched a file we refused: {path}"
            refused += 1
    assert (compared, refused) == (1, 1)
    assert Path(EXIFTOOL).exists()
