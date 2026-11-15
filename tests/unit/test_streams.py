"""Stream contract: stdout carries the primary output, stderr the diagnostics, in both modes."""

import io
import json

import pytest

from tests.conftest import track_text
from tests.support import raf_json


class ClosedStream(io.StringIO):
    def write(self, text):
        raise ValueError("I/O operation on closed file")


class BrokenPipe(io.StringIO):
    def write(self, text):
        raise BrokenPipeError("pipe closed")


def overlapping_tracks(world):
    (world.tracks / "other.gpx").write_text(track_text())


class TestJsonFlag:
    def test_json_replaces_the_report_on_stdout_and_nothing_human_appears_on_either_stream(
        self, world
    ):
        world.add("a.RAF")

        result = world.run(world.options(json=True))

        json.loads(result.out)
        assert "gps-sync dry run" not in result.out + result.err
        assert result.err == ""

    def test_without_json_stdout_carries_the_human_report_and_is_never_empty(self, world):
        world.add("a.RAF")

        result = world.run()

        assert result.out.startswith("gps-sync dry run")
        assert result.err == ""

    @pytest.mark.parametrize("as_json", [False, True])
    def test_a_warning_is_on_stderr_in_both_modes_so_json_stays_parseable(self, world, as_json):
        world.add("a.RAF")
        overlapping_tracks(world)

        result = world.run(world.options(json=as_json))

        assert "overlap in time" in result.err
        if as_json:
            assert json.loads(result.out)["overlaps"]
        assert "overlap in time" not in result.out

    def test_json_with_verbose_keeps_the_trace_on_stderr_and_stdout_parseable(self, world):
        world.add("a.RAF")

        result = world.run(world.options(json=True, verbose=True))

        json.loads(result.out)
        assert "trace:" in result.err
        assert "trace:" not in result.out

    def test_verbose_lists_every_ignore_reason_and_the_pruned_path_on_stderr(self, world):
        world.add("a.RAF")
        (world.media / "notes.txt").write_text("x")
        (world.media / "link.RAF").symlink_to(world.media / "a.RAF")

        result = world.run(world.options(verbose=True, review_dir=world.media / "review"))

        assert "ignored NOT_A_STILL" in result.err
        assert "ignored SYMLINK" in result.err
        assert "pruned run-artifact path" in result.err

    def test_json_exit_code_matches_the_process_status(self, world):
        world.add("a.RAF", raf_json(offset=None))

        result = world.run(world.options(json=True))

        assert result.code == 1
        assert json.loads(result.out)["exit_code"] == 1


class TestStdoutFailure:
    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize("stream", [ClosedStream, BrokenPipe])
    def test_a_stdout_write_error_after_the_work_completed_is_exit_three_with_a_stderr_message(
        self, world, as_json, stream
    ):
        world.add("a.RAF")

        result = world.run(world.options(json=as_json), stdout=stream())

        assert result.code == 3
        assert "cannot write the primary output" in result.err
        # The per-file work completed: the review folder was still rebuilt.
        assert world.review_root().exists()

    def test_exit_three_outranks_a_failed_file(self, world):
        world.add("a.RAF", raf_json(offset=None))

        assert world.run(stdout=ClosedStream()).code == 3
