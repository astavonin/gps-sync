"""The write-phase contract against the real binary: re-gate, attribution, interruption."""

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from gps_sync.model import (
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
from gps_sync.transport import ExiftoolTransport, write_command
from gps_sync.writer import run_write
from tests.conftest import FIXED_NOW, World, track_text
from tests.integration.helpers import tags
from tests.support import EXIFTOOL, at, clone, exiftool


def target(path: Path, lat: float = 42.5, lon: float = 78.25, container: str = "RAF") -> Planned:
    return Planned(
        str(path),
        at(300),
        Position(lat, lon, 1500.5),
        Via.INTERPOLATED,
        Interpolated("t.gpx#trk0seg0", 10.0),
        0.5,
        FileContext("FUJIFILM", "X-T5", container),
    )


class Recording(ExiftoolTransport):
    def __init__(self) -> None:
        super().__init__(EXIFTOOL)
        self.batches: list = []

    def write(self, argfile):
        batch = super().write(argfile)
        self.batches.append(batch)
        return batch


class TestReGate:
    def test_a_positionless_file_is_written(self, tmp_path, assets):
        copy = clone(assets.raf, tmp_path / "a.RAF")

        result = run_write(Recording(), [target(copy)], tmp_path, keep_original=False)

        assert isinstance(result.outcomes[0], Geotagged)
        assert tags(copy, "-GPSLatitude")["GPSLatitude"] == pytest.approx(42.5)

    def test_a_position_that_appears_between_read_and_write_is_not_overwritten(
        self, tmp_path, assets
    ):
        copy = clone(assets.raf, tmp_path / "a.RAF")
        planned = target(copy, lat=10.0, lon=20.0)
        # Another writer (Capture One, a second gps-sync) gives the file a position after the read.
        exiftool(
            "-overwrite_original",
            "-n",
            "-GPSLatitude=55.5",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=66.5",
            "-GPSLongitudeRef=E",
            str(copy),
        )
        transport = Recording()

        result = run_write(transport, [planned], tmp_path, keep_original=False)

        [outcome] = result.outcomes
        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.POSITION_APPEARED
        assert outcome.position is None and outcome.via is Via.INTERPOLATED
        values = tags(copy, "-GPSLatitude", "-GPSLongitude")
        assert (values["GPSLatitude"], values["GPSLongitude"]) == (55.5, 66.5)
        # The refusal is its own signal inside the path's own block, not the absence of output.
        [batch] = transport.batches
        [block] = batch.stdout_blocks
        assert block.lines == ("1 files failed condition",)
        assert result.reconciliation.unintended == ()

    def test_the_condition_is_asserted_in_both_directions_in_one_batch(self, tmp_path, assets):
        fresh = clone(assets.raf, tmp_path / "fresh.RAF")
        taken = clone(assets.raf_gps, tmp_path / "taken.RAF")

        result = run_write(
            Recording(),
            [target(fresh), target(taken, lat=1.0, lon=2.0)],
            tmp_path,
            keep_original=False,
        )

        assert isinstance(result.outcomes[0], Geotagged)
        assert isinstance(result.outcomes[1], Protected)
        assert tags(taken, "-GPSLatitude")["GPSLatitude"] == pytest.approx(42.5)


class TestAttributionOnARealBatch:
    def build(self, tmp_path: Path, assets):
        files = [clone(assets.raf, tmp_path / "w" / f"f{i}.RAF") for i in range(4)]
        locked = tmp_path / "ro"
        locked.mkdir()
        victim = clone(assets.raf, locked / "victim.RAF")
        locked.chmod(0o555)
        order = [files[0], files[1], victim, files[2], files[3]]
        return order, victim, locked

    def test_a_write_error_in_a_real_batch_lands_on_the_right_path_only(self, tmp_path, assets):
        order, victim, locked = self.build(tmp_path, assets)
        planned = [target(p, lat=40.0 + i, lon=70.0 + i) for i, p in enumerate(order)]
        transport = Recording()
        try:
            result = run_write(transport, planned, tmp_path, keep_original=False)
        finally:
            locked.chmod(0o755)

        errored = [o for o in result.outcomes if o.path == str(victim)]
        assert len(errored) == 1 and errored[0].reason is FailedReason.WRITE_ERROR
        assert "Error creating file" in errored[0].detail
        for i, path in enumerate(order):
            if path == victim:
                continue
            outcome = next(o for o in result.outcomes if o.path == str(path))
            assert isinstance(outcome, Geotagged), path
            assert tags(path, "-GPSLatitude")["GPSLatitude"] == pytest.approx(40.0 + i)
        assert result.reconciliation.returncode == 1
        assert result.reconciliation.unintended == ()

    def test_markers_terminate_their_block_on_both_streams_and_the_error_block_has_two_lines(
        self, tmp_path, assets
    ):
        order, victim, locked = self.build(tmp_path, assets)
        transport = Recording()
        try:
            run_write(transport, [target(p) for p in order], tmp_path, keep_original=False)
        finally:
            locked.chmod(0o755)

        [batch] = transport.batches
        assert [b.marker for b in batch.stdout_blocks] == [str(p) for p in order]
        assert [b.marker for b in batch.stderr_blocks] == [str(p) for p in order]
        by_marker = {b.marker: b for b in batch.stdout_blocks}
        assert by_marker[str(order[0])].lines == ("1 image files updated",)
        assert by_marker[str(victim)].lines == (
            "0 image files updated",
            "1 files weren't updated due to errors",
        )
        stderr_by_marker = {b.marker: b for b in batch.stderr_blocks}
        assert any(line.startswith("Error:") for line in stderr_by_marker[str(victim)].lines)
        assert batch.stdout_tail == () and batch.stderr_tail == ()

    def test_a_nonexistent_path_in_the_intent_set_still_gets_its_own_conclusion(
        self, tmp_path, assets
    ):
        good = clone(assets.raf, tmp_path / "good.RAF")
        gone = tmp_path / "gone.RAF"

        result = run_write(Recording(), [target(good), target(gone)], tmp_path, keep_original=False)

        assert isinstance(result.outcomes[0], Geotagged)
        assert result.outcomes[1].reason is FailedReason.WRITE_ERROR
        assert "File not found" in result.outcomes[1].detail


class TestDngBatch:
    def test_three_paths_conclude_applied_refused_and_errored_with_their_own_wordings(
        self, tmp_path, assets
    ):
        applied = clone(assets.dng_positionless, tmp_path / "w" / "applied.DNG")
        positioned = clone(assets.dng_original, tmp_path / "w" / "positioned.DNG")
        locked = tmp_path / "ro"
        locked.mkdir()
        victim = clone(assets.dng_positionless, locked / "victim.DNG")
        locked.chmod(0o555)
        order = [applied, positioned, victim]
        transport = Recording()
        try:
            result = run_write(
                transport,
                [target(p, container="DNG") for p in order],
                tmp_path,
                keep_original=False,
            )
        finally:
            locked.chmod(0o755)

        [batch] = transport.batches
        lines = {b.marker: b.lines for b in batch.stdout_blocks}
        assert lines[str(applied)] == ("1 image files updated",)
        assert lines[str(positioned)] == ("1 files failed condition",)
        assert lines[str(victim)] == (
            "0 image files updated",
            "1 files weren't updated due to errors",
        )
        by_path = {o.path: o for o in result.outcomes}
        assert isinstance(by_path[str(applied)], Geotagged)
        assert isinstance(by_path[str(positioned)], Protected)
        assert by_path[str(victim)].reason is FailedReason.WRITE_ERROR
        assert tags(applied, "-GPSLatitude")["GPSLatitude"] == pytest.approx(42.5)

    def test_a_positionless_dng_is_written_when_it_arrives_through_the_classifier(
        self, tmp_path, assets
    ):
        world = World(tmp_path)
        world.media.mkdir()
        world.tracks.mkdir()
        (world.tracks / "walk.gpx").write_text(track_text())
        world.home.mkdir()
        copy = world.put("drone/clean.DNG", assets.dng_positionless)
        exiftool("-overwrite_original", "-DateTimeOriginal=2026:10:26 10:05:00", str(copy))

        result = world.run(
            world.options(
                json=True, write=True, utc_offset=timedelta(hours=6), review_dir=tmp_path / "rv"
            ),
            ExiftoolTransport(EXIFTOOL),
        )

        record = json.loads(result.out)["files"][0]
        assert (record["variant"], result.code) == ("Geotagged", 0)
        assert tags(copy, "-GPSLatitude")["GPSLatitude"] == pytest.approx(record["position"]["lat"])


class KillingTransport(ExiftoolTransport):
    """Kills the real writer partway through the batch, then simulates the tool dying."""

    def __init__(self, after_markers: int) -> None:
        super().__init__(EXIFTOOL)
        self._after = after_markers

    def write(self, argfile):
        process = subprocess.Popen(
            write_command(EXIFTOOL, argfile), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        seen = 0
        assert process.stderr is not None
        for line in process.stderr:
            if line.startswith(b"/"):
                seen += 1
                if seen >= self._after:
                    break
        process.kill()
        process.wait()
        raise RuntimeError("the tool was killed mid-batch")


TAGS = ("-GPSLatitude", "-GPSLongitude", "-GPSAltitude", "-GPSDateStamp", "-GPSTimeStamp")


def test_a_killed_batch_leaves_every_file_untouched_or_complete_and_an_intent_record(
    tmp_path, assets
):
    world = World(tmp_path)
    world.media.mkdir()
    world.tracks.mkdir()
    world.home.mkdir()
    (world.tracks / "walk.gpx").write_text(track_text())
    paths = [world.put(f"f{i:02d}.RAF", assets.raf) for i in range(12)]
    before = {p: p.read_bytes() for p in paths}

    with pytest.raises(RuntimeError):
        world.run(world.options(write=True), KillingTransport(after_markers=2))

    written, untouched = [], []
    for path in paths:
        if path.read_bytes() == before[path]:
            untouched.append(path)
            continue
        values = tags(path, *TAGS)
        assert set(values) >= {t[1:] for t in TAGS} | {"SourceFile"}, f"partial GPS block in {path}"
        written.append(path)
    assert len(written) >= 1 and untouched, "the kill should land mid-batch"
    [record_path] = list((world.review_root() / "runs").glob("*.json"))
    record = json.loads(record_path.read_text())
    assert record["phase"] == "intent"
    assert [e["path"] for e in record["intended"]] == [str(p.resolve()) for p in paths]

    stale = [p for p in world.media.rglob("*") if p.name.endswith("_exiftool_tmp")]
    resumed = world.run(
        world.options(write=True, json=True),
        ExiftoolTransport(EXIFTOOL),
        clock=lambda: FIXED_NOW + timedelta(minutes=5),
    )

    assert resumed.code == 0
    files = {os.path.basename(f["path"]): f for f in json.loads(resumed.out)["files"]}
    for path in written:
        assert files[path.name]["reason"] == "POSITION_PRESENT"
    for path in untouched:
        assert files[path.name]["variant"] == "Geotagged"
    for leftover in stale:
        assert files[leftover.name]["reason"] == "NOT_A_STILL"
    assert json.loads(resumed.out)["incomplete_prior_records"][0]["name"] == record_path.name
    report = world.run(world.options())
    assert report.out.splitlines()[0].startswith("WARNING: incomplete prior run")
