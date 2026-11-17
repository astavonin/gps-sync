"""Per-container UTC instant resolution, with ``--time-shift`` applied at exactly one point."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from gps_sync.durations import DurationError, parse_utc_offset
from gps_sync.records import TagRecord

_TIME = re.compile(
    r"^(?P<y>\d{4})[:-](?P<mo>\d{2})[:-](?P<d>\d{2})[ T]"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?P<frac>\.\d+)?$"
)
_QUICKTIME_FAMILY = frozenset({"MOV", "MP4", "M4V", "QUICKTIME"})
_CREATION_DATE = re.compile(
    r"^(?P<local>\d{4}[:-]\d{2}[:-]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)
#: Major brands whose creation-time field is the ISO base media format's UTC reading, stripped.
#: The classic QuickTime brand is deliberately absent: the one file that can rule on it is local.
UTC_ATOM_BRANDS = frozenset({"isom", "mp42"})


@dataclass(frozen=True, slots=True)
class TimeFailure:
    """Why no instant could be resolved.

    ``unverified`` selects ``TIME_SEMANTICS_UNVERIFIED`` over ``TIME_UNRESOLVABLE``: the container
    states a time this project cannot interpret, as opposed to stating none.
    """

    detail: str
    unverified: bool = False


def is_quicktime_family(file_type: str | None) -> bool:
    """Return whether the content-derived container identity is a QuickTime-family one.

    This is a statement about the container identity and nothing to do with ``MajorBrand``.
    """
    return file_type is not None and file_type.upper() in _QUICKTIME_FAMILY


def _parse_naive(text: str) -> datetime | None:
    match = _TIME.fullmatch(text)
    if match is None:
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


def _corroborates(creation_date: str, atom: datetime) -> bool:
    """Return whether a zone-bearing ``CreationDate`` is the atom's own instant plus its offset."""
    match = _CREATION_DATE.fullmatch(creation_date)
    if match is None:
        return False
    local = _parse_naive(match["local"])
    if local is None:
        return False
    zone = match["offset"]
    try:
        offset = timedelta(0) if zone == "Z" else parse_utc_offset(zone)
    except DurationError:
        return False
    return local - offset == atom


def _resolve_quicktime_atom(record: TagRecord) -> datetime | TimeFailure:
    """Rows 8 to 11: no ``DateTimeOriginal``, so only the container atom could supply an instant."""
    if not record.create_date:
        return TimeFailure(
            "no CreateDate on a QuickTime-family container"
            if record.create_date is None
            else "CreateDate is present but blank on a QuickTime-family container"
        )
    atom = _parse_naive(record.create_date.removesuffix("Z"))
    if atom is None:
        return TimeFailure(f"malformed CreateDate {record.create_date!r}")
    single = record.create_date_count == 1
    corroborated = (
        single and record.creation_date is not None and _corroborates(record.creation_date, atom)
    )
    sole_witness = single and record.creation_date is None and record.major_brand in UTC_ATOM_BRANDS
    if corroborated or sole_witness:
        return atom.replace(tzinfo=UTC)
    return TimeFailure(
        "the container's creation time has no zone this project can verify "
        f"(CreateDate instances {record.create_date_count}, "
        f"CreationDate {record.creation_date!r}, MajorBrand {record.major_brand!r})",
        unverified=True,
    )


def _resolve_exif(record: TagRecord, utc_offset: timedelta | None) -> datetime | TimeFailure:
    """Rows 1 to 6: a ``DateTimeOriginal`` is present, so EXIF offset evidence outranks any atom."""
    if not record.date_time_original:
        return TimeFailure("DateTimeOriginal is present but blank; refusing to resolve a time")
    local = _parse_naive(record.date_time_original)
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
    elif is_quicktime_family(record.file_type):
        return TimeFailure(
            "a QuickTime-family container holds a second, competing time witness and carries no "
            "OffsetTimeOriginal; --utc-offset is never consulted for it",
            unverified=True,
        )
    elif utc_offset is not None:
        offset = utc_offset
    else:
        return TimeFailure(
            "OffsetTimeOriginal is absent and --utc-offset was not given for this local-time still"
        )
    return (local - offset).replace(tzinfo=UTC)


def resolve_instant(
    record: TagRecord, utc_offset: timedelta | None, time_shift: timedelta
) -> datetime | TimeFailure:
    """Resolve a record's UTC instant from the time evidence it carries, then add ``time_shift``.

    The dispatch is on which time tags are present and parse, never on the container family
    alone, and its default is refusal. ``--time-shift`` is applied here, once, to whatever any
    admitting row produced.
    """
    if record.date_time_original is not None:
        resolved = _resolve_exif(record, utc_offset)
    elif is_quicktime_family(record.file_type):
        resolved = _resolve_quicktime_atom(record)
    else:
        resolved = TimeFailure(
            "DateTimeOriginal is absent; CreateDate is not admissible for a still"
        )
    if isinstance(resolved, TimeFailure):
        return resolved
    return resolved + time_shift
