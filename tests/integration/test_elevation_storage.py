"""What the real backend does to an elevation it is given: the premise of the upstream guard."""

import pytest

from gps_sync.writeset import ELEVATION_CEILING, is_representable_elevation
from tests.integration.helpers import tags
from tests.support import clone, exiftool


class TestAStillsElevation:
    def test_inside_the_ceiling_the_stored_value_is_the_nearest_rational_not_the_written_decimal(
        self, tmp_path, assets
    ):
        raf = clone(assets.raf, tmp_path / "a.RAF")

        done = exiftool("-q", "-overwrite_original", "-n", "-GPSAltitude=1234.5678", str(raf))

        stored = tags(raf, "-GPS:GPSAltitude")["GPSAltitude"]
        assert done.returncode == 0 and done.stderr == ""
        assert stored == pytest.approx(1234.5678, abs=1e-5)
        assert is_representable_elevation(1234.5678)

    @pytest.mark.parametrize("written", [ELEVATION_CEILING + 1.0, 1e12])
    def test_above_the_ceiling_the_backend_saturates_silently_at_the_ceiling(
        self, tmp_path, assets, written
    ):
        raf = clone(assets.raf, tmp_path / "a.RAF")

        done = exiftool("-n", "-overwrite_original", f"-GPSAltitude={written:.4f}", str(raf))

        assert done.returncode == 0
        assert "1 image files updated" in done.stdout
        assert done.stderr == ""
        assert tags(raf, "-GPS:GPSAltitude")["GPSAltitude"] == ELEVATION_CEILING
        assert not is_representable_elevation(written)

    def test_the_ceiling_itself_is_stored_and_is_representable(self, tmp_path, assets):
        raf = clone(assets.raf, tmp_path / "a.RAF")

        exiftool("-q", "-overwrite_original", "-n", f"-GPSAltitude={ELEVATION_CEILING}", str(raf))

        assert tags(raf, "-GPS:GPSAltitude")["GPSAltitude"] == ELEVATION_CEILING
        assert is_representable_elevation(ELEVATION_CEILING)


class TestAClipsElevation:
    def test_inside_the_ceiling_the_atom_keeps_the_written_decimal(self, tmp_path, assets):
        clip = clone(assets.mp4, tmp_path / "c.MP4")

        exiftool(
            "-q", "-overwrite_original", "-n", "-Keys:GPSCoordinates=42.0 77.0 1234.5678", str(clip)
        )

        assert tags(clip, "-Keys:GPSCoordinates")["GPSCoordinates"] == "42 77 1234.5678"

    def test_a_third_component_the_atom_cannot_hold_is_dropped_without_a_refusal(
        self, tmp_path, assets
    ):
        clip = clone(assets.mp4, tmp_path / "c.MP4")

        done = exiftool(
            "-n",
            "-overwrite_original",
            "-Keys:GPSCoordinates=42.0 77.0 100000000000000000000.0",
            str(clip),
        )

        assert done.returncode == 0 and done.stderr == ""
        assert tags(clip, "-Keys:GPSCoordinates")["GPSCoordinates"] == "42 77"
