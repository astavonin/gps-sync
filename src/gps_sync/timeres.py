"""Per-container UTC instant resolution, with ``--time-shift`` applied at exactly one point."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from gps_sync.durations import DurationError, parse_utc_offset
from gps_sync.records import TagRecord

_TIME = re.compile(
    r"^(?P<y>\d{4})[:-](?P<mo>\d{2})[:-](?P<d>\d{2})[ T]"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?P<frac>\.\d+)?(?P<z>Z)?$"
)
_QUICKTIME_TYPES = frozenset({"MOV", "MP4", "M4V", "QUICKTIME"})


class ContainerClass(StrEnum):
    """The container classes the time-resolution table is total over."""

    STILL = "still"
    QUICKTIME_UTC = "quicktime-utc"


@dataclass(frozen=True, slots=True)
class TimeFailure:
    """Why no instant could be resolved; the classifier renders it as ``TIME_UNRESOLVABLE``."""

    detail: str


def container_class(file_type: str | None) -> ContainerClass:
    """Derive the class from the content-derived container identity."""
    if file_type is not None and file_type.upper() in _QUICKTIME_TYPES:
        return ContainerClass.QUICKTIME_UTC
    return ContainerClass.STILL


def _parse_naive(text: str, *, allow_z: bool) -> datetime | None:
    match = _TIME.fullmatch(text)
    if match is None or (match["z"] and not allow_z):
        return None
    micro = int(float(match["frac"]) * 1_000_000) if match["frac"] else 0
    try:
        return datetime(
            int(match["y"]),
            int(match["mo"]),
            int(match["d"]),
            int(match["h"]),
            int(match["mi"]),
            int(match["s"]),
            micro,
        )
    except ValueError:
        return None


def resolve_instant(
    record: TagRecord, utc_offset: timedelta | None, time_shift: timedelta
) -> datetime | TimeFailure:
    """Resolve a record's UTC instant per the container table, then add ``time_shift``.

    Stills use ``DateTimeOriginal`` with the embedded ``OffsetTimeOriginal`` and fall back to
    ``--utc-offset`` only when the offset tag is absent, never when it is blank. ``CreateDate`` is
    never admissible for a still, and ``GPSDateTime`` is never read at all.
    """
    if container_class(record.file_type) is ContainerClass.QUICKTIME_UTC:
        if record.create_date is None:
            return TimeFailure("no CreateDate on a QuickTime-family container")
        stamp = _parse_naive(record.create_date, allow_z=True)
        if stamp is None:
            return TimeFailure(f"malformed CreateDate {record.create_date!r}")
        return stamp.replace(tzinfo=UTC) + time_shift
    if record.date_time_original is None:
        return TimeFailure("DateTimeOriginal is absent; CreateDate is not admissible for a still")
    local = _parse_naive(record.date_time_original, allow_z=False)
    if local is None:
        return TimeFailure(f"malformed DateTimeOriginal {record.date_time_original!r}")
    if record.offset_time_original == "":
        # Present but blank is unreadable, not absent: --utc-offset must not stand in for it.
        return TimeFailure("OffsetTimeOriginal is present but blank; refusing to assume an offset")
    if record.offset_time_original is not None:
        try:
            offset = parse_utc_offset(record.offset_time_original)
        except DurationError:
            return TimeFailure(f"malformed OffsetTimeOriginal {record.offset_time_original!r}")
    elif utc_offset is not None:
        offset = utc_offset
    else:
        return TimeFailure(
            "OffsetTimeOriginal is absent and --utc-offset was not given for this local-time still"
        )
    return (local - offset).replace(tzinfo=UTC) + time_shift
