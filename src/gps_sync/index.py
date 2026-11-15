"""The merged, immutable, time-ordered track index with per-point segment identity."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from gps_sync.errors import TrackIdentityCollision
from gps_sync.gpx import TrackPoint, TrackSegment


@dataclass(frozen=True, slots=True)
class Overlap:
    """Two segments covering the same period; every bracket inside it is a boundary."""

    segment_a: str
    segment_b: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class TrackIndex:
    """All tracks as one structure suitable for bisect, without losing segment boundaries.

    ``prev_in_segment`` and ``next_in_segment`` hold, for each point, the index of its neighbour
    within its own segment, which is not ``i - 1`` or ``i + 1`` where segments interleave.
    """

    points: tuple[TrackPoint, ...] = ()
    times: tuple[datetime, ...] = ()
    prev_in_segment: tuple[int | None, ...] = ()
    next_in_segment: tuple[int | None, ...] = ()
    overlaps: tuple[Overlap, ...] = ()
    duplicates_dropped: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.points)


def _assert_injective(segments: list[TrackSegment]) -> None:
    seen: dict[str, str] = {}
    for segment in segments:
        if segment.id in seen:
            raise TrackIdentityCollision(
                f"segment identity {segment.id!r} is assigned to both "
                f"{seen[segment.id]!r} and {segment.source!r}"
            )
        seen[segment.id] = segment.source


def _find_overlaps(segments: list[TrackSegment]) -> tuple[Overlap, ...]:
    spans = sorted(
        (
            (min(p.time for p in s.points), max(p.time for p in s.points), s.id)
            for s in segments
            if s.points
        ),
        key=lambda span: (span[0], span[2]),
    )
    found: list[Overlap] = []
    for i, (_start_a, end_a, id_a) in enumerate(spans):
        for start_b, end_b, id_b in spans[i + 1 :]:
            if start_b >= end_a:
                break
            found.append(Overlap(id_a, id_b, start_b, min(end_a, end_b)))
    return tuple(found)


def build_index(segments: Iterable[TrackSegment]) -> TrackIndex:
    """Merge segments into one index.

    Duplicate instants inside a segment collapse to the first in file order. Duplicate instants
    across segments are never collapsed: that rule may not remove either endpoint of a boundary.
    """
    ordered = list(segments)
    _assert_injective(ordered)
    retained: list[TrackPoint] = []
    duplicates: dict[str, int] = {}
    for segment in ordered:
        seen: set[datetime] = set()
        for point in segment.points:
            if point.time in seen:
                duplicates[segment.source] = duplicates.get(segment.source, 0) + 1
                continue
            seen.add(point.time)
            retained.append(point)
    retained.sort(key=lambda p: (p.time, p.segment))
    last_seen: dict[str, int] = {}
    prev: list[int | None] = []
    nxt: list[int | None] = [None] * len(retained)
    for i, point in enumerate(retained):
        before = last_seen.get(point.segment)
        prev.append(before)
        if before is not None:
            nxt[before] = i
        last_seen[point.segment] = i
    return TrackIndex(
        points=tuple(retained),
        times=tuple(p.time for p in retained),
        prev_in_segment=tuple(prev),
        next_in_segment=tuple(nxt),
        overlaps=_find_overlaps(ordered),
        duplicates_dropped=duplicates,
    )
