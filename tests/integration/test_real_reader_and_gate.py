"""The batched reader and the three-valued gate against the real binary."""

import json
import os
from datetime import timedelta

import pytest

from gps_sync.errors import ToolFailure
from gps_sync.reader import read_records
from gps_sync.transport import ExiftoolTransport
from tests.assets import RAF_LOCAL_TIME, XMP_COMPETING_TIME, gps_keys
from tests.conftest import World, track_text
from tests.integration.conftest import spawn_counts
from tests.support import EXIFTOOL, exiftool


@pytest.fixture
def real_world(tmp_path) -> World:
    w = World(tmp_path)
    w.media.mkdir()
    w.tracks.mkdir()
    (w.tracks / "walk.gpx").write_text(track_text())
    w.home.mkdir()
    return w


def outcomes_by_name(result) -> dict[str, dict]:
    return {os.path.basename(f["path"]): f for f in json.loads(result.out)["files"]}


def test_every_candidate_returns_a_record_and_one_process_serves_the_whole_tree(
    real_world, assets, counting_exiftool
):
    script, log = counting_exiftool
    for i in range(12):
        real_world.put(f"d{i}/frame{i}.RAF", assets.raf_gps)

    result = real_world.run(real_world.options(json=True), ExiftoolTransport(str(script)))

    assert result.code == 0
    assert json.loads(result.out)["summary"]["variant_counts"]["Protected"] == 12
    counts = spawn_counts(log)
    assert counts["read"] == 1
    assert counts["write"] == 0


def test_a_path_exiftool_cannot_open_is_a_tool_level_failure_and_never_a_record(
    tmp_path, real_transport, assets
):
    real = assets.raf_gps
    missing = str(tmp_path / "gone.RAF")

    with pytest.raises(ToolFailure, match="did not return exactly the requested records"):
        read_records(real_transport, [str(real), missing], tmp_path)


def test_a_truncated_container_is_a_diagnostic_and_a_refusal_not_a_write_target(real_world, assets):
    real_world.put("truncated.RAF", assets.raf_truncated)

    result = real_world.run(real_world.options(json=True), ExiftoolTransport(EXIFTOOL))

    record = outcomes_by_name(result)["truncated.RAF"]
    assert record["variant"] == "Failed"
    assert record["reason"] == "METADATA_INDETERMINATE"
    assert record["detail"]
    assert result.code == 1


class TestThreeGateValuesUnderTheReadersOwnInvocation:
    @pytest.fixture
    def outcomes(self, real_world, assets):
        real_world.put("exif_gps.RAF", assets.raf_gps)
        real_world.put("xmp_only.RAF", assets.raf_xmp_only)
        real_world.put("dji_vendor_xmp.DNG", assets.dng_original)
        real_world.put("partial_block.RAF", assets.raf_partial_block)
        real_world.put("truncated.RAF", assets.raf_truncated)
        result = real_world.run(real_world.options(json=True), ExiftoolTransport(EXIFTOOL))
        return outcomes_by_name(result)

    def test_an_exif_gps_position_is_present(self, outcomes):
        assert outcomes["exif_gps.RAF"]["reason"] == "POSITION_PRESENT"
        assert outcomes["exif_gps.RAF"]["position"]["lat"] == pytest.approx(42.5)

    def test_a_position_only_in_xmp_is_present(self, outcomes):
        assert outcomes["xmp_only.RAF"]["reason"] == "POSITION_PRESENT"

    def test_a_position_only_in_the_dji_vendor_namespace_is_present_despite_the_benign_warning(
        self, outcomes
    ):
        assert outcomes["dji_vendor_xmp.DNG"]["variant"] == "Protected"
        assert outcomes["dji_vendor_xmp.DNG"]["reason"] == "POSITION_PRESENT"

    def test_a_partial_block_is_not_protected_and_continues_to_planning(self, outcomes, real_world):
        # The one state whose correct reading creates a write target. The file really holds a
        # GPSLatitudeRef and no position anywhere, so "Planned" is the reading of that block.
        assert gps_keys(real_world.media / "partial_block.RAF") == {"GPSLatitudeRef"}
        assert outcomes["partial_block.RAF"]["variant"] == "Planned"

    def test_the_truncated_copy_is_indeterminate(self, outcomes):
        assert outcomes["truncated.RAF"]["reason"] == "METADATA_INDETERMINATE"


def test_a_file_carrying_two_distinct_warnings_yields_both_in_its_record(
    real_transport, assets, tmp_path
):
    records = read_records(real_transport, [str(assets.dng_original)], tmp_path)

    warnings = records[str(assets.dng_original)].warnings
    assert len(warnings) == 2
    assert any("Possibly incorrect maker notes offsets" in w for w in warnings)
    assert any("Not decoding some large array" in w for w in warnings)


def put_covered_control(real_world: World, assets) -> str:
    """Add a clean positionless DNG whose instant is the refused files' own, inside the track.

    It is the precondition made observable: when this control is written, the shared instant is
    covered, so a refused sibling with that instant is held back by the gate alone. If the track
    or the instant drifts, the control stops being ``Geotagged`` and the test fails instead of
    quietly becoming vacuous.
    """
    control = real_world.put("control.DNG", assets.dng_positionless)
    exiftool("-overwrite_original", f"-DateTimeOriginal={RAF_LOCAL_TIME}", str(control))
    return control.name


def test_a_serious_warning_behind_the_benign_one_makes_the_file_indeterminate_not_absent(
    real_world, assets, counting_exiftool
):
    script, log = counting_exiftool
    real_world.put("defective.DNG", assets.dng_defective_gps)
    before = (real_world.media / "defective.DNG").read_bytes()
    # Precondition: the fixture really has no position anywhere the reader could find one.
    with_all = exiftool("-j", "-n", "-a", "-Composite:GPSPosition", str(assets.dng_defective_gps))
    assert "GPSPosition" not in json.loads(with_all.stdout)[0]

    control = put_covered_control(real_world, assets)

    result = real_world.run(
        real_world.options(json=True, write=True, utc_offset=timedelta(hours=6)),
        ExiftoolTransport(str(script)),
    )

    outcomes = outcomes_by_name(result)
    record = outcomes["defective.DNG"]
    assert outcomes[control]["variant"] == "Geotagged"
    assert record["variant"] == "Failed"
    assert record["reason"] == "METADATA_INDETERMINATE"
    assert "Bad GPS directory" in record["detail"]
    assert spawn_counts(log)["write"] == 1, "the control's batch is the only write"
    assert (real_world.media / "defective.DNG").read_bytes() == before


def test_a_positionless_dji_dng_is_refused_for_the_minor_warning_it_carries_on_every_read(
    real_world, assets, counting_exiftool
):
    """Documents an incomplete benign-warning allowlist against 13.55.

    A DJI DNG reads with the allowlisted maker-notes warning and, beside it, `Not decoding some
    large array(s)` from the profile arrays it carries as it left the camera; both are visible on
    every read and no rewrite is needed to reach the second. The exiftool strip only makes the
    fixture positionless. Its instant lies inside the track, so the gate alone keeps it from
    being written: it is refused, under a reason that says the file read but for a warning the
    tool does not recognise.
    """
    script, log = counting_exiftool
    copy = real_world.put("stripped.DNG", assets.dng_stripped)
    exiftool("-overwrite_original", f"-DateTimeOriginal={RAF_LOCAL_TIME}", str(copy))
    before = copy.read_bytes()
    control = put_covered_control(real_world, assets)

    result = real_world.run(
        real_world.options(json=True, write=True, utc_offset=timedelta(hours=6)),
        ExiftoolTransport(str(script)),
    )

    outcomes = outcomes_by_name(result)
    record = outcomes["stripped.DNG"]
    assert outcomes[control]["variant"] == "Geotagged"
    assert record["variant"] == "Failed"
    assert record["reason"] == "UNRECOGNISED_MINOR_WARNING"
    assert "Not decoding some large array" in record["detail"]
    assert spawn_counts(log)["write"] == 1, "the control's batch is the only write"
    assert copy.read_bytes() == before


def test_a_scalar_tag_with_two_instances_reads_the_exif_one_exactly_as_a_plain_read_does(
    real_transport, assets, tmp_path
):
    path = assets.dng_xmp_competing_time
    raw = json.loads(exiftool("-j", "-n", "-a", "-G4", "-DateTimeOriginal", str(path)).stdout)[0]
    # The fixture's discriminating property: both instances are returned, the non-preferred XMP
    # one first, so only an explicit rank can pick the EXIF one.
    assert [k for k in raw if k.endswith("DateTimeOriginal")] == [
        "Copy1:DateTimeOriginal",
        ":DateTimeOriginal",
    ]
    assert raw["Copy1:DateTimeOriginal"] == XMP_COMPETING_TIME
    plain = json.loads(exiftool("-j", "-n", "-DateTimeOriginal", str(path)).stdout)[0]

    record = read_records(real_transport, [str(path)], tmp_path)[str(path)]

    assert record.date_time_original == RAF_LOCAL_TIME
    assert record.date_time_original == plain["DateTimeOriginal"]


def test_a_blank_primary_offset_is_refused_even_when_utc_offset_is_supplied(
    real_world, assets, blank_primary_offset_exiftool
):
    # The supplied offset equals the real one, so a blank read as "absent" would fall through to
    # it, resolve an instant inside the track and write a position.
    copy = real_world.put("blank.RAF", assets.raf)
    before = copy.read_bytes()
    assert "+06:00" in exiftool("-OffsetTimeOriginal", str(copy)).stdout, "fixture precondition"

    result = real_world.run(
        real_world.options(json=True, write=True, utc_offset=timedelta(hours=6)),
        ExiftoolTransport(str(blank_primary_offset_exiftool)),
    )

    [record] = json.loads(result.out)["files"]
    assert record["variant"] == "Failed"
    assert record["reason"] == "TIME_UNRESOLVABLE"
    assert "blank" in record["detail"]
    assert copy.read_bytes() == before
