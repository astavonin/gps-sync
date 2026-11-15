"""Fixtures for tests that run the real exiftool; corpus-derived assets live in tests.assets."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gps_sync.transport import ExiftoolTransport
from tests.assets import require_environment
from tests.support import EXIFTOOL, exiftool


@pytest.fixture
def real_transport() -> ExiftoolTransport:
    require_environment()
    return ExiftoolTransport(EXIFTOOL)


@pytest.fixture
def counting_exiftool(tmp_path: Path) -> tuple[Path, Path]:
    """A wrapper that logs each invocation's argv then runs the real exiftool."""
    log = tmp_path / "invocations.log"
    script = tmp_path / "exiftool-counting"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec "{EXIFTOOL}" "$@"\n')
    script.chmod(0o755)
    return script, log


@pytest.fixture
def blank_primary_offset_exiftool(tmp_path: Path) -> Path:
    """A wrapper that runs the real exiftool but reports each offset as a blank primary instance.

    exiftool cannot author a blank ``OffsetTimeOriginal`` (it deletes the tag instead), so the
    shape a real RAF yields when two IFDs carry the tag, with the blank one preferred, is produced
    by rewriting the batched read's JSON: ``:OffsetTimeOriginal`` blank, the real value under
    ``Copy1:``. Writes and ``-ver`` pass through untouched.
    """
    script = tmp_path / "exiftool-blank-offset"
    script.write_text(
        f"""#!{sys.executable}
import json, subprocess, sys
done = subprocess.run(["{EXIFTOOL}", *sys.argv[1:]], capture_output=True, text=True)
if "-j" in sys.argv:
    records = json.loads(done.stdout)
    for record in records:
        if ":OffsetTimeOriginal" in record:
            record["Copy1:OffsetTimeOriginal"] = record[":OffsetTimeOriginal"]
            record[":OffsetTimeOriginal"] = ""
    done.stdout = json.dumps(records)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
"""
    )
    script.chmod(0o755)
    return script


def spawn_counts(log: Path) -> dict[str, int]:
    lines = log.read_text().splitlines() if log.exists() else []
    return {
        "version": sum(line.strip() == "-ver" for line in lines),
        "read": sum(" -j " in f" {line} " for line in lines),
        "write": sum(" -@ " in f" {line} " and " -j " not in f" {line} " for line in lines),
        "total": len(lines),
    }


def set_local_times(paths_and_times: dict[Path, str]) -> None:
    """Set DateTimeOriginal on test-owned copies, one process for the lot."""
    lines: list[str] = []
    for path, stamp in paths_and_times.items():
        lines += ["-overwrite_original", f"-DateTimeOriginal={stamp}", str(path), "-execute"]
    argfile = next(iter(paths_and_times)).parent / ".set-times.args"
    argfile.write_text("\n".join(lines) + "\n")
    exiftool("-@", str(argfile))
    argfile.unlink()
