"""Pure position derivation over a merged track index.

Interpolation happens only between two points of the same segment; ``max_gap`` is a second,
independent constraint inside a segment. The matcher performs no I/O and never builds an
``Outcome``: it returns a ``Match`` or a ``NoCoverage`` refusal.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

from gps_sync.geo import haversine_m
from gps_sync.gpx import TrackPoint
from gps_sync.index import TrackIndex
from gps_sync.model import (
    Extended,
    Interpolated,
    Position,
    Provenance,
    UnmatchableReason,
    Via,
)


@dataclass(frozen=True, slots=True)
class Match:
    """A derived position with its provenance and, where defined, the bracket's speed."""

    position: Position
    via: Via
    provenance: Provenance
    bracket_speed: float | None


@dataclass(frozen=True, slots=True)
class NoCoverage:
    """A refusal. ``nearest_segment`` and ``miss_s`` are absent only against an empty index."""

    reason: UnmatchableReason
    nearest_segment: str | None
    miss_s: float | None
    bracket_s: float | None = None
    detail: str | None = None


def _speed(a: TrackPoint, b: TrackPoint) -> float | None:
    seconds = abs((b.time - a.time).total_seconds())
    if seconds == 0:
        return None
    return haversine_m(a.lat, a.lon, b.lat, b.lon) / seconds


def _position(point: TrackPoint) -> Position:
    return Position(point.lat, point.lon, point.ele)


def _exact_speed(index: TrackIndex, i: int) -> float | None:
    later = index.next_in_segment[i]
    if later is not None:
        return _speed(index.points[i], index.points[later])
    earlier = index.prev_in_segment[i]
    if earlier is not None:
        return _speed(index.points[earlier], index.points[i])
    return None


def _clamp_speed(
    index: TrackIndex, i: int, *, away_is_prev: bool, partner: int | None
) -> float | None:
    """Speed of the interval adjacent to a clamped endpoint, inside that endpoint's own segment.

    The side away from the refused gap is preferred; the other side is used only when its
    neighbour is not the refused bracket's other endpoint, since a speed across the gap is
    meaningless.
    """
    away = index.prev_in_segment[i] if away_is_prev else index.next_in_segment[i]
    other = index.next_in_segment[i] if away_is_prev else index.prev_in_segment[i]
    chosen = away if away is not None else (other if other != partner else None)
    if chosen is None:
        return None
    return _speed(index.points[i], index.points[chosen])


def _clamp(
    index: TrackIndex,
    i: int,
    miss_s: float,
    overridden: UnmatchableReason,
    *,
    away_is_prev: bool,
    partner: int | None,
) -> Match:
    point = index.points[i]
    return Match(
        position=_position(point),
        via=Via.EXTENDED,
        provenance=Extended(point.segment, miss_s, overridden),
        bracket_speed=_clamp_speed(index, i, away_is_prev=away_is_prev, partner=partner),
    )


def _refuse_or_clamp_bracket(
    index: TrackIndex,
    instant: datetime,
    lo: int,
    hi: int,
    reason: UnmatchableReason,
    extend: timedelta,
    *,
    clamp_allowed: bool = True,
) -> Match | NoCoverage:
    before = (instant - index.times[lo]).total_seconds()
    after = (index.times[hi] - instant).total_seconds()
    use_lo = before <= after
    nearest = lo if use_lo else hi
    miss = before if use_lo else after
    if clamp_allowed and extend > timedelta(0) and timedelta(seconds=miss) <= extend:
        return _clamp(
            index,
            nearest,
            miss,
            reason,
            away_is_prev=use_lo,
            partner=hi if use_lo else lo,
        )
    low, high = index.points[lo], index.points[hi]
    detail = None
    if reason is UnmatchableReason.BRACKET_UNSOUND:
        detail = (
            f"bracketing trackpoints {low.segment} @ {low.time.isoformat()} (lon {low.lon}) and "
            f"{high.segment} @ {high.time.isoformat()} (lon {high.lon})"
        )
    return NoCoverage(
        reason,
        index.points[nearest].segment,
        miss,
        bracket_s=(index.times[hi] - index.times[lo]).total_seconds(),
        detail=detail,
    )


def _refuse_or_clamp_outer(
    index: TrackIndex, instant: datetime, extend: timedelta, *, at_start: bool
) -> Match | NoCoverage:
    i = 0 if at_start else len(index) - 1
    miss = abs((index.times[i] - instant).total_seconds())
    if extend > timedelta(0) and timedelta(seconds=miss) <= extend:
        return _clamp(
            index,
            i,
            miss,
            UnmatchableReason.OUTSIDE_COVERAGE,
            away_is_prev=not at_start,
            partner=None,
        )
    return NoCoverage(UnmatchableReason.OUTSIDE_COVERAGE, index.points[i].segment, miss)


def match(
    index: TrackIndex, instant: datetime, max_gap: timedelta, extend: timedelta
) -> Match | NoCoverage:
    """Derive a position for ``instant`` or refuse, in the order the design fixes.

    Refusal order: no points, exact hit (ambiguous across segments is a boundary), outside the
    outer ends, segment boundary, bracket wider than ``max_gap``, antimeridian. Segment identity
    dominates width because ``--max-gap`` is the wrong remedy for a boundary.
    """
    if len(index) == 0:
        return NoCoverage(UnmatchableReason.OUTSIDE_COVERAGE, None, None)
    first = bisect_left(index.times, instant)
    stop = bisect_right(index.times, instant)
    if stop > first:
        hits = index.points[first:stop]
        if len({p.segment for p in hits}) == 1:
            hit = hits[0]
            return Match(
                _position(hit),
                Via.INTERPOLATED,
                Interpolated(hit.segment, 0.0),
                _exact_speed(index, first),
            )
        return _refuse_or_clamp_bracket(
            index, instant, first, first + 1, UnmatchableReason.SEGMENT_BOUNDARY, extend
        )
    if first == 0:
        return _refuse_or_clamp_outer(index, instant, extend, at_start=True)
    if first == len(index):
        return _refuse_or_clamp_outer(index, instant, extend, at_start=False)
    lo, hi = first - 1, first
    low, high = index.points[lo], index.points[hi]
    if low.segment != high.segment:
        return _refuse_or_clamp_bracket(
            index, instant, lo, hi, UnmatchableReason.SEGMENT_BOUNDARY, extend
        )
    width = high.time - low.time
    if width > max_gap:
        return _refuse_or_clamp_bracket(
            index, instant, lo, hi, UnmatchableReason.MAX_GAP_EXCEEDED, extend
        )
    if abs(low.lon - high.lon) > 180:
        return _refuse_or_clamp_bracket(
            index,
            instant,
            lo,
            hi,
            UnmatchableReason.BRACKET_UNSOUND,
            extend,
            clamp_allowed=False,
        )
    weight = (instant - low.time) / width
    ele = (
        low.ele + (high.ele - low.ele) * weight
        if low.ele is not None and high.ele is not None
        else None
    )
    return Match(
        Position(
            low.lat + (high.lat - low.lat) * weight,
            low.lon + (high.lon - low.lon) * weight,
            ele,
        ),
        Via.INTERPOLATED,
        Interpolated(low.segment, width.total_seconds()),
        _speed(low, high),
    )
