"""Helpers for assertions over real files."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from tests.support import EXIFTOOL, exiftool


def tags(path: Path, *names: str) -> dict[str, object]:
    """Read tags numerically (``-n``) with the group-less names exiftool returns."""
    completed = exiftool("-j", "-n", *names, str(path))
    return json.loads(completed.stdout)[0]


def tag_names(path: Path) -> set[str]:
    """Return every ``Group:Tag`` name in the file, duplicates across groups included."""
    completed = exiftool("-j", "-a", "-G1", "-s", "-n", str(path))
    return {key for key in json.loads(completed.stdout)[0] if key != "SourceFile"}


def common_suffix(first: Path, second: Path, chunk: int = 1 << 20) -> int:
    """Return the length of the longest byte run the two files share at their ends."""
    size_a, size_b = first.stat().st_size, second.stat().st_size
    shared = 0
    with first.open("rb") as fa, second.open("rb") as fb:
        while shared < min(size_a, size_b):
            step = min(chunk, min(size_a, size_b) - shared)
            fa.seek(size_a - shared - step)
            fb.seek(size_b - shared - step)
            a, b = fa.read(step), fb.read(step)
            if a == b:
                shared += step
                continue
            for i in range(step - 1, -1, -1):
                if a[i] != b[i]:
                    return shared + (step - 1 - i)
            return shared
    return shared


def preview_digest(path: Path) -> str:
    """MD5 of the embedded preview's entropy-coded scan, ignoring its header segments.

    A metadata write rewrites the preview's APP1 (EXIF) segment and drops padding there; the
    compressed scan that follows the SOS marker is the part that must survive byte for byte.
    """
    out = subprocess.run(
        [EXIFTOOL, "-b", "-PreviewImage", str(path)], capture_output=True, check=True
    ).stdout
    position = 2
    while out[position + 1] != 0xDA:
        position += 2 + int.from_bytes(out[position + 2 : position + 4], "big")
    scan_start = position + 2 + int.from_bytes(out[position + 2 : position + 4], "big")
    end = out.rindex(b"\xff\xd9") + 2
    return hashlib.md5(out[scan_start:end], usedforsecurity=False).hexdigest()


def file_state(path: Path) -> tuple[str, int, int]:
    """Return (sha256, mtime_ns, size) of a file."""
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_mtime_ns,
        path.stat().st_size,
    )


def is_real_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def walk_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if is_real_file(p))


def stat_identity(path: Path) -> tuple[int, int, int]:
    info = os.stat(path)
    return info.st_mode & 0o7777, info.st_uid, info.st_gid
