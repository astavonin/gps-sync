from pathlib import Path

import pytest

from gps_sync.errors import InternalConsistencyError
from gps_sync.model import (
    Failed,
    FailedReason,
    FileContext,
    Geotagged,
    Interpolated,
    Planned,
    Position,
    Protected,
    ProtectedReason,
    Via,
)
from gps_sync.transport import TransportError, partition_stream
from gps_sync.writer import (
    ERRORED_MESSAGE,
    assert_targets_writable,
    attribute,
    build_write_lines,
    run_write,
)
from gps_sync.writeset import STILL_REGATE
from tests.support import FakeTransport, at, batch, block_text, marked

APPLIED = "    1 image files updated"
REFUSED = "    1 files failed condition"
ERROR_COUNTS = ("    0 image files updated", "    1 files weren't updated due to errors")


def planned(
    path: str, *, lat=42.5, lon=78.25, ele: float | None = 1500.5, container="RAF"
) -> Planned:
    return Planned(
        path,
        at(30),
        Position(lat, lon, ele),
        Via.INTERPOLATED,
        Interpolated("t.gpx#trk0seg0", 5.0),
        bracket_speed=0.8,
        context=FileContext("FUJIFILM", "X-T5", container),
    )


def blocks_for(lines: list[str]) -> list[list[str]]:
    groups: list[list[str]] = [[]]
    for line in lines:
        groups[-1].append(line)
        if line == "-execute":
            groups.append([])
    return [g for g in groups if g]


class TestEveryBlockIsShapeCheckedAtEmission:
    def test_build_write_lines_checks_each_block_it_emits(self, monkeypatch):
        import gps_sync.writer as writer_module

        checked: list[tuple[str, ...]] = []
        real = writer_module.assert_block_shape
        monkeypatch.setattr(
            writer_module,
            "assert_block_shape",
            lambda block: (checked.append(block.lines), real(block))[1],
        )

        lines = build_write_lines([planned("/m/a.RAF"), planned("/m/b.RAF")], keep_original=False)

        assert [block[-2] for block in map(list, checked)] == ["/m/a.RAF", "/m/b.RAF"]
        assert [line for block in checked for line in block] == lines

    def test_a_block_carrying_an_undeclared_member_is_refused_before_any_line_is_emitted(
        self, monkeypatch, tmp_path
    ):
        import gps_sync.writer as writer_module
        from gps_sync.writeset import Assignment

        real = writer_module.assignments
        monkeypatch.setattr(
            writer_module,
            "assignments",
            lambda *args: (*real(*args), Assignment("Artist", "forged", False)),
        )

        with pytest.raises(InternalConsistencyError, match="Artist"):
            run_write(FakeTransport(), [planned("/m/a.RAF")], tmp_path, keep_original=False)

        assert not (tmp_path / "write.args").exists()


class TestWriteArgfile:
    def test_each_target_is_one_self_contained_block_with_its_own_markers(self):
        lines = build_write_lines([planned("/m/a.RAF"), planned("/m/b.RAF")], keep_original=False)

        blocks = blocks_for(lines)
        assert len(blocks) == 2
        for block, path in zip(blocks, ["/m/a.RAF", "/m/b.RAF"], strict=True):
            assert block[-1] == "-execute"
            assert block[block.index("-echo3") + 1] == path
            assert block[block.index("-echo4") + 1] == path
            assert block.count(path) == 3
            assert block[-2] == path

    def test_the_marker_text_is_the_absolute_path_and_nothing_else(self):
        lines = build_write_lines([planned("/m/a.RAF")], keep_original=False)

        assert lines[lines.index("-echo3") + 1] == "/m/a.RAF"
        assert lines[lines.index("-echo4") + 1] == "/m/a.RAF"

    def test_eight_tags_are_assigned_individually_when_elevation_is_derivable(self):
        lines = build_write_lines([planned("/m/a.RAF")], keep_original=False)

        assignments = sorted(line.split("=")[0] for line in lines if line.startswith("-GPS"))
        assert assignments == sorted(
            [
                "-GPSLatitude",
                "-GPSLatitudeRef",
                "-GPSLongitude",
                "-GPSLongitudeRef",
                "-GPSAltitude",
                "-GPSAltitudeRef",
                "-GPSDateStamp",
                "-GPSTimeStamp",
            ]
        )

    def test_six_tags_and_no_altitude_when_elevation_is_not_derivable(self):
        lines = build_write_lines([planned("/m/a.RAF", ele=None)], keep_original=False)

        assignments = [line for line in lines if line.startswith("-GPS")]
        assert len(assignments) == 6
        assert not any("Altitude" in line for line in assignments)

    def test_hemisphere_refs_follow_the_sign_and_values_are_written_unsigned(self):
        lines = build_write_lines(
            [planned("/m/a.RAF", lat=-33.5, lon=-70.25, ele=-12.0)], keep_original=False
        )

        assert "-GPSLatitude=33.500000000" in lines and "-GPSLatitudeRef=S" in lines
        assert "-GPSLongitude=70.250000000" in lines and "-GPSLongitudeRef=W" in lines
        assert "-GPSAltitude=12.0000" in lines and "-GPSAltitudeRef=1" in lines

    @pytest.mark.parametrize(
        ("lat", "lon", "lat_ref", "lon_ref"),
        [
            (0.5, 0.5, "N", "E"),
            (-0.5, -0.5, "S", "W"),
            (0.0, 0.0, "N", "E"),
            (-0.0, -0.0, "N", "E"),
            (0.999999, 0.999999, "N", "E"),
            (-0.999999, -0.999999, "S", "W"),
            (1.0, 1.0, "N", "E"),
            (-1.0, -1.0, "S", "W"),
        ],
    )
    def test_horizontal_refs_are_decided_by_sign_alone_including_below_one_degree(
        self, lat, lon, lat_ref, lon_ref
    ):
        lines = build_write_lines([planned("/m/a.RAF", lat=lat, lon=lon)], keep_original=False)

        assert f"-GPSLatitudeRef={lat_ref}" in lines
        assert f"-GPSLongitudeRef={lon_ref}" in lines

    @pytest.mark.parametrize(
        ("ele", "ref"),
        [(0.5, 0), (-0.5, 1), (0.0, 0), (0.999, 0), (-0.999, 1), (1.0, 0), (-1.0, 1)],
    )
    def test_altitude_ref_marks_below_sea_level_by_sign_alone_including_below_one_metre(
        self, ele, ref
    ):
        lines = build_write_lines([planned("/m/a.RAF", ele=ele)], keep_original=False)

        assert f"-GPSAltitudeRef={ref}" in lines

    def test_gps_date_and_time_stamps_come_from_the_matched_instant_in_utc(self):
        lines = build_write_lines([planned("/m/a.RAF")], keep_original=False)

        assert "-GPSDateStamp=2026:10:26" in lines
        assert "-GPSTimeStamp=04:00:30" in lines

    def test_no_xmp_tags_are_written(self):
        lines = build_write_lines([planned("/m/a.RAF")], keep_original=False)

        assert not any("XMP" in line for line in lines)

    def test_the_regate_condition_is_present_in_every_block_and_never_paired_with_f(self):
        lines = build_write_lines([planned("/m/a.RAF"), planned("/m/b.RAF")], keep_original=False)

        for block in blocks_for(lines):
            assert block[block.index("-if") + 1] == STILL_REGATE
            assert "-f" not in block

    def test_overwrite_original_is_the_default_and_keep_original_omits_it(self):
        default = build_write_lines([planned("/m/a.RAF")], keep_original=False)
        kept = build_write_lines([planned("/m/a.RAF")], keep_original=True)

        assert "-overwrite_original" in default
        assert "-overwrite_original" not in kept

    def test_every_path_line_begins_with_a_slash_even_for_a_dash_named_file(self):
        targets = [planned("/m/-fast2.RAF"), planned("/m/sp dir/a b.RAF")]
        lines = build_write_lines(targets, keep_original=False)

        optionish = [
            line for line in lines if not line.startswith(("-", "/")) and line != STILL_REGATE
        ]
        assert optionish == []
        assert [line for line in lines if "fast2" in line] == ["/m/-fast2.RAF"] * 3

    def test_a_relative_path_is_refused_at_emission(self):
        with pytest.raises(InternalConsistencyError):
            build_write_lines([planned("relative/a.RAF")], keep_original=False)

    def test_a_path_with_a_line_break_is_refused_at_emission(self):
        with pytest.raises(InternalConsistencyError):
            build_write_lines([planned("/m/a\nb.RAF")], keep_original=False)

    def test_no_prohibited_exiftool_capability_appears(self):
        lines = build_write_lines([planned("/m/a.RAF")], keep_original=False)

        joined = "\n".join(lines).lower()
        assert "geotag" not in joined and "geosync" not in joined and ".gpx" not in joined


class TestPreEmissionRecheck:
    def test_a_symlink_target_aborts_before_the_writer_is_spawned(self, tmp_path):
        real = tmp_path / "real.RAF"
        real.write_bytes(b"x")
        link = tmp_path / "link.RAF"
        link.symlink_to(real)

        with pytest.raises(InternalConsistencyError, match="symlink"):
            assert_targets_writable([planned(str(link))])

    def test_a_relative_target_aborts(self):
        with pytest.raises(InternalConsistencyError, match="absolute"):
            assert_targets_writable([planned("rel/a.RAF")])

    @pytest.mark.parametrize("container", ["JPEG", "HEIC", "TIFF"])
    def test_a_container_outside_the_allowlist_aborts(self, tmp_path, container):
        with pytest.raises(InternalConsistencyError, match="allowlist"):
            assert_targets_writable([planned(str(tmp_path / "a.RAF"), container=container)])

    def test_a_non_planned_outcome_aborts(self):
        protected = Protected("/m/a.RAF", ProtectedReason.POSITION_PRESENT, position=Position(1, 2))

        with pytest.raises(InternalConsistencyError, match="non-Planned"):
            assert_targets_writable([protected])  # type: ignore[list-item]

    def test_a_duplicate_target_aborts(self, tmp_path):
        target = planned(str(tmp_path / "a.RAF"))

        with pytest.raises(InternalConsistencyError, match="twice"):
            assert_targets_writable([target, target])

    def test_ordinary_targets_pass(self, tmp_path):
        assert_targets_writable(
            [planned(str(tmp_path / "a.RAF")), planned(str(tmp_path / "b.RAF"))]
        )


PATHS = [f"/m/{name}.RAF" for name in "ABCDE"]


def five_path_batch():
    """Two applied, one refused, one errored, one with no conclusion at all.

    Lines per block are the measured ones: one for an applied write and for a refusal, two for
    an error, and the error's path-bearing line arrives on stderr before its own marker.
    """
    a, b, c, d, e = PATHS
    stdout = [
        marked(a, APPLIED),
        marked(b, APPLIED),
        marked(c, REFUSED),
        marked(d, *ERROR_COUNTS),
    ]
    stderr = [
        marked(a),
        marked(b),
        marked(c),
        marked(d, f"Error: Error creating file: {d}_exiftool_tmp"),
    ]
    return batch(stdout, stderr, returncode=1)


class TestAttribution:
    def test_a_mixed_five_path_batch_gives_each_path_its_own_conclusion(self):
        targets = [planned(p) for p in PATHS]

        result = attribute(targets, five_path_batch())

        a, b, c, d, e = result.outcomes
        assert isinstance(a, Geotagged) and isinstance(b, Geotagged)
        assert isinstance(c, Protected) and c.reason is ProtectedReason.POSITION_APPEARED
        assert c.position is None and c.via is Via.INTERPOLATED and c.provenance is not None
        assert isinstance(d, Failed) and d.reason is FailedReason.WRITE_ERROR
        assert "Error creating file" in d.detail
        assert isinstance(e, Failed) and e.reason is FailedReason.WRITE_UNATTRIBUTED
        assert result.reconciliation.returncode == 1
        assert result.reconciliation.last_marker == PATHS[3]

    def test_a_nonzero_process_status_does_not_fail_the_paths_that_applied(self):
        result = attribute(
            [planned(PATHS[0])], batch([marked(PATHS[0], APPLIED)], [marked(PATHS[0])], 1)
        )

        assert isinstance(result.outcomes[0], Geotagged)

    def test_a_zero_process_status_does_not_excuse_a_path_without_a_conclusion(self):
        result = attribute([planned(PATHS[0])], batch([], [], 0))

        assert isinstance(result.outcomes[0], Failed)
        assert result.outcomes[0].reason is FailedReason.WRITE_UNATTRIBUTED

    def test_the_marker_terminates_its_block_so_the_second_error_line_stays_with_its_path(self):
        a, b, c = PATHS[:3]
        stdout_text = block_text(([APPLIED], a), (ERROR_COUNTS, b), ([APPLIED], c))
        stderr_text = block_text(([], a), ([f"Error: boom {b}"], b), ([], c))
        stdout_blocks, _ = partition_stream(stdout_text)
        stderr_blocks, _ = partition_stream(stderr_text)
        assert [len(blk.lines) for blk in stdout_blocks] == [1, 2, 1]

        result = attribute([planned(p) for p in (a, b, c)], batch(stdout_blocks, stderr_blocks, 1))

        assert [type(o) for o in result.outcomes] == [Geotagged, Failed, Geotagged]
        assert result.outcomes[1].reason is FailedReason.WRITE_ERROR

    def test_a_positional_reading_would_misattribute_and_this_one_does_not(self):
        # The error block has two lines; zipping lines to blocks would give C the "0 updated" line.
        a, b, c = PATHS[:3]
        stdout_blocks, _ = partition_stream(
            block_text(([APPLIED], a), (ERROR_COUNTS, b), ([REFUSED], c))
        )
        stderr_blocks, _ = partition_stream(block_text(([], a), ([f"Error: x {b}"], b), ([], c)))

        result = attribute([planned(p) for p in (a, b, c)], batch(stdout_blocks, stderr_blocks))

        assert isinstance(result.outcomes[2], Protected)

    def test_a_block_whose_marker_never_appears_is_exactly_that_paths_unattributed(self):
        a, b, c = PATHS[:3]
        stdout = [marked(a, APPLIED), marked(c, APPLIED)]
        stderr = [marked(a), marked(c)]

        result = attribute([planned(p) for p in (a, b, c)], batch(stdout, stderr))

        assert [type(o) for o in result.outcomes] == [Geotagged, Failed, Geotagged]
        assert result.outcomes[1].reason is FailedReason.WRITE_UNATTRIBUTED
        assert result.reconciliation.last_marker == c

    def test_a_marker_on_only_one_stream_is_not_enough_to_attribute(self):
        result = attribute([planned(PATHS[0])], batch([marked(PATHS[0], APPLIED)], []))

        assert result.outcomes[0].reason is FailedReason.WRITE_UNATTRIBUTED

    def test_a_smaller_conclusion_set_makes_exactly_the_missing_paths_unattributed(self):
        a, b = PATHS[:2]

        result = attribute([planned(a), planned(b)], batch([marked(a, APPLIED)], [marked(a)]))

        assert isinstance(result.outcomes[0], Geotagged)
        assert result.outcomes[1].reason is FailedReason.WRITE_UNATTRIBUTED

    def test_a_conclusion_for_a_path_outside_the_intent_set_is_recorded_verbatim(self):
        a = PATHS[0]
        stranger = "/m/STRANGER.RAF"

        result = attribute(
            [planned(a)],
            batch(
                [marked(a, APPLIED), marked(stranger, APPLIED)],
                [marked(a), marked(stranger)],
            ),
        )

        assert isinstance(result.outcomes[0], Geotagged)
        [unintended] = result.reconciliation.unintended
        assert unintended.path == stranger and APPLIED.strip() in [
            x.strip() for x in unintended.lines
        ]

    def test_a_whole_process_failure_makes_every_intent_path_unattributed(self):
        targets = [planned(p) for p in PATHS[:3]]

        result = attribute(targets, None, "write process failed: killed")

        assert [o.reason for o in result.outcomes] == [FailedReason.WRITE_UNATTRIBUTED] * 3
        assert all("killed" in o.detail for o in result.outcomes)
        assert not any(isinstance(o, (Geotagged, Protected)) for o in result.outcomes)

    def test_a_marker_appearing_twice_makes_the_path_unattributed(self):
        a = PATHS[0]

        result = attribute(
            [planned(a)],
            batch([marked(a, APPLIED), marked(a, APPLIED)], [marked(a), marked(a)]),
        )

        assert result.outcomes[0].reason is FailedReason.WRITE_UNATTRIBUTED

    @pytest.mark.parametrize("duplicated_on", ["stdout", "stderr"])
    def test_a_marker_repeated_on_one_stream_only_is_still_unattributed(self, duplicated_on):
        a = PATHS[0]
        copies = 2
        stdout = [marked(a, APPLIED)] * (copies if duplicated_on == "stdout" else 1)
        stderr = [marked(a)] * (copies if duplicated_on == "stderr" else 1)

        result = attribute([planned(a)], batch(stdout, stderr))

        assert result.outcomes[0].reason is FailedReason.WRITE_UNATTRIBUTED
        assert "more than once" in result.outcomes[0].detail

    def test_a_repeated_marker_does_not_cost_the_later_paths_their_conclusions(self):
        a, b, c, d = PATHS[:4]
        stdout = [marked(a, APPLIED), marked(a, APPLIED), marked(b, APPLIED), marked(c, REFUSED)]
        stderr = [marked(a), marked(a), marked(b), marked(c)]

        result = attribute([planned(p) for p in (a, b, c, d)], batch(stdout, stderr))

        assert [o.path for o in result.outcomes] == [a, b, c, d]
        assert [type(o) for o in result.outcomes] == [Failed, Geotagged, Protected, Failed]
        assert result.outcomes[0].reason is FailedReason.WRITE_UNATTRIBUTED
        assert result.outcomes[3].reason is FailedReason.WRITE_UNATTRIBUTED

    @pytest.mark.parametrize(
        ("stdout_lines", "stderr_lines"),
        [
            ((), ()),
            ((APPLIED, REFUSED), ()),
            ((APPLIED,), ("Error: late failure",)),
            ((REFUSED,), ("Error: late failure",)),
            (ERROR_COUNTS, ()),
            (("    1 image files unchanged",), ()),
        ],
        ids=[
            "silent",
            "applied-and-refused",
            "applied-with-error",
            "refused-with-error",
            "error-without-detail",
            "unchanged",
        ],
    )
    def test_ambiguous_or_contradictory_blocks_are_never_resolved_by_inference(
        self, stdout_lines, stderr_lines
    ):
        a = PATHS[0]

        result = attribute(
            [planned(a)], batch([marked(a, *stdout_lines)], [marked(a, *stderr_lines)])
        )

        assert result.outcomes[0].reason is FailedReason.WRITE_UNATTRIBUTED

    def test_partial_output_from_a_failed_process_is_still_attributed(self, tmp_path):
        from gps_sync.writer import run_write

        a, b = PATHS[:2]
        partial = batch([marked(a, APPLIED)], [marked(a)], -9)

        class Dying:
            def write(self, argfile):
                raise TransportError("killed", partial)

        result = run_write(Dying(), [planned(a), planned(b)], tmp_path, keep_original=False)  # type: ignore[arg-type]

        assert isinstance(result.outcomes[0], Geotagged)
        assert result.outcomes[1].reason is FailedReason.WRITE_UNATTRIBUTED
        assert result.reconciliation.process_failure is not None

    def test_error_message_constants_match_the_measured_wordings(self):
        assert ERRORED_MESSAGE == "1 files weren't updated due to errors"


class TestPartitionStream:
    def test_the_measured_stdout_shape_partitions_into_terminated_blocks(self):
        text = (
            "    1 image files updated\n/p/a\n"
            "    1 files failed condition\n/p/c\n"
            "    0 image files updated\n    1 files weren't updated due to errors\n/p/e\n"
        )

        blocks, tail = partition_stream(text)

        assert [b.marker for b in blocks] == ["/p/a", "/p/c", "/p/e"]
        assert [len(b.lines) for b in blocks] == [1, 1, 2]
        assert tail == ()

    def test_lines_after_the_last_marker_are_an_unfinished_block_not_a_conclusion(self):
        blocks, tail = partition_stream(
            "    1 image files updated\n/p/a\n    1 image files updated\n"
        )

        assert [b.marker for b in blocks] == ["/p/a"]
        assert tail == ("1 image files updated",)

    def test_blank_lines_are_ignored_and_a_stream_without_markers_has_no_blocks(self):
        blocks, tail = partition_stream("\n\nError: File not found\n")

        assert blocks == () and tail == ("Error: File not found",)

    def test_a_path_with_spaces_and_unicode_is_a_single_marker(self):
        blocks, _ = partition_stream("    1 image files updated\n/p/dé ü 日本/a b.RAF\n")

        assert blocks[0].marker == "/p/dé ü 日本/a b.RAF"


def exiftool_backup_semantics(argfile: Path):
    """Write handler that honours the argfile as exiftool does for backups.

    ``<path>_original`` is retained unless the block carries ``-overwrite_original``; then the
    file's bytes change, so a test can tell a retained original from a rewritten one.
    """
    blocks = blocks_for(argfile.read_text().splitlines())
    paths = [block[block.index("-echo3") + 1] for block in blocks]
    for block, path in zip(blocks, paths, strict=True):
        target = Path(path)
        if "-overwrite_original" not in block:
            target.with_name(target.name + "_original").write_bytes(target.read_bytes())
        target.write_bytes(target.read_bytes() + b"+gps")
    return batch([marked(p, APPLIED) for p in paths], [marked(p) for p in paths])


class TestKeepOriginalEndToEnd:
    @pytest.mark.parametrize("keep", [True, False])
    def test_run_write_retains_a_byte_identical_sibling_only_when_asked(self, tmp_path, keep):
        path = tmp_path / "a.RAF"
        path.write_bytes(b"pristine")

        result = run_write(
            FakeTransport(on_write=exiftool_backup_semantics),
            [planned(str(path))],
            tmp_path,
            keep_original=keep,
        )

        sibling = tmp_path / "a.RAF_original"
        assert isinstance(result.outcomes[0], Geotagged)
        assert path.read_bytes() == b"pristine+gps"
        assert sibling.exists() is keep
        if keep:
            assert sibling.read_bytes() == b"pristine"

    @pytest.mark.parametrize("keep", [True, False])
    def test_the_run_carries_keep_original_from_the_options_to_the_writer(self, world, keep):
        path = Path(world.add("a.RAF"))
        pre = path.read_bytes()

        result = world.run(
            world.options(write=True, keep_original=keep),
            world.transport(on_write=exiftool_backup_semantics),
        )

        sibling = path.with_name("a.RAF_original")
        assert result.code == 0
        assert path.read_bytes() == pre + b"+gps"
        assert sibling.exists() is keep
        if keep:
            assert sibling.read_bytes() == pre
