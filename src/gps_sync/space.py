"""The per-target free-space check a whole-file rewrite needs.

A write, of a still as much as of a clip, creates a full-size temporary file in the target's own
directory and renames it over the target, so the peak demand is one target at a time and is
checked per file, on the volume that holds that file. Under ``--keep-original`` the ``_original``
sibling persists, so demand also accumulates per volume, in intent order, which is the order the
audit record lists targets in.
"""

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gps_sync.model import Failed, FailedReason, Planned

#: Headroom above the file's own size, declared as a value with its basis: the measured metadata
#: growth is 99 to 164 bytes across four container producers, and 1 MiB sits comfortably above
#: that while staying negligible against the smallest clip in the population at 2.6 MB.
SLACK_BYTES = 1 << 20


class SpaceCheck(Protocol):
    """The injectable free-space stage: split the planned targets into writable and refused."""

    def __call__(
        self, planned: Sequence[Planned], *, keep_original: bool
    ) -> tuple[list[Planned], dict[str, Failed]]:
        """Return the targets the volumes can hold and a refusal for each they cannot."""


@dataclass(frozen=True, slots=True)
class VolumeDemand:
    """What a run's targets ask of one volume, in the two figures its check evaluates."""

    volume: int
    peak_bytes: int
    retained_bytes: int


def _size(path: str) -> int:
    return os.stat(path).st_size


def _volume(path: str) -> int:
    return os.stat(Path(path).parent).st_dev


def _free(path: str) -> int:
    return shutil.disk_usage(Path(path).parent).free


def demand(
    planned: Sequence[Planned],
    *,
    size_of: Callable[[str], int] = _size,
    volume_of: Callable[[str], int] = _volume,
) -> tuple[VolumeDemand, ...]:
    """Return per volume the largest target plus slack, and the sum of every target's size.

    The peak is what the check evaluates without ``--keep-original``; the sum is the figure that
    flag turns it into, and it is not the peak, which a sum would overstate by the set's ratio of
    total to largest member.
    """
    peaks: dict[int, int] = {}
    totals: dict[int, int] = {}
    for target in planned:
        try:
            volume, size = volume_of(target.path), size_of(target.path)
        except OSError:
            # A forecast has nothing to refuse: a target that vanished since discovery is
            # reported by the write-time check, where it has a conclusion to carry.
            continue
        peaks[volume] = max(peaks.get(volume, 0), size + SLACK_BYTES)
        totals[volume] = totals.get(volume, 0) + size
    return tuple(VolumeDemand(v, peaks[v], totals[v]) for v in sorted(peaks))


def check_space(
    planned: Sequence[Planned],
    *,
    keep_original: bool,
    size_of: Callable[[str], int] = _size,
    volume_of: Callable[[str], int] = _volume,
    free_of: Callable[[str], int] = _free,
) -> tuple[list[Planned], dict[str, Failed]]:
    """Split targets into those the volume can hold and a refusal for each it cannot.

    One oversized target never denies the others, and neither does one that can no longer be
    examined: a target that vanished or became unreadable since discovery is refused as
    ``WRITE_TARGET_UNEXAMINABLE``, which keeps the match payload because the target had matched,
    with the operating system's own words. This runs after the intent record is flushed, and an
    uncaught error there would read as an interrupted write when nothing was written.
    Examination follows intent order, so under ``--keep-original`` the first targets that fit are
    the ones written.
    """
    writable: list[Planned] = []
    refused: dict[str, Failed] = {}
    retained: dict[int, int] = {}
    for target in planned:
        try:
            volume, size = volume_of(target.path), size_of(target.path)
            free = free_of(target.path)
        except OSError as error:
            refused[target.path] = Failed(
                target.path,
                FailedReason.WRITE_TARGET_UNEXAMINABLE,
                f"the target could not be examined for the free-space check: {error}",
                instant=target.instant,
                via=target.via,
                provenance=target.provenance,
                bracket_speed=target.bracket_speed,
                context=target.context,
            )
            continue
        accumulated = retained.get(volume, 0) if keep_original else 0
        if free < size + SLACK_BYTES + accumulated:
            detail = f"size {size} bytes, free {free} bytes on volume {volume}"
            if keep_original:
                detail += f", retained originals already planned on it {accumulated} bytes"
            refused[target.path] = Failed(
                target.path,
                FailedReason.WRITE_INSUFFICIENT_SPACE,
                detail,
                instant=target.instant,
                via=target.via,
                provenance=target.provenance,
                bracket_speed=target.bracket_speed,
                context=target.context,
            )
            continue
        writable.append(target)
        if keep_original:
            retained[volume] = retained.get(volume, 0) + size
    return writable, refused
