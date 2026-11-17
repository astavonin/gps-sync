"""The write set declared per container class, and the value formatters and guards it depends on.

The verified-safe write allowlist is a mapping from container identity to a write-set class, so a
container cannot be admitted without declaring what is written to it and under which re-gate
condition. Nothing here touches a file or a process: a block's assignments are a pure function of
a target's identity and its derived position.
"""

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from gps_sync.model import Position


class WriteSetClass(StrEnum):
    """A container family sharing one write set and one re-gate condition."""

    EXIF_STILL = "exif-still"
    QUICKTIME_ATOM = "quicktime-atom"


#: Container identity (upper case) to its write-set class; membership is the write allowlist.
CONTAINER_CLASS: dict[str, WriteSetClass] = {
    "RAF": WriteSetClass.EXIF_STILL,
    "DNG": WriteSetClass.EXIF_STILL,
    "MP4": WriteSetClass.QUICKTIME_ATOM,
    "MOV": WriteSetClass.QUICKTIME_ATOM,
}

#: The class a filename's own final extension declares, compared against the content's class.
EXTENSION_CLASS: dict[str, WriteSetClass] = {
    ".raf": WriteSetClass.EXIF_STILL,
    ".dng": WriteSetClass.EXIF_STILL,
    ".mp4": WriteSetClass.QUICKTIME_ATOM,
    ".mov": WriteSetClass.QUICKTIME_ATOM,
}

#: The unsigned-rational numerator ceiling ``GPSAltitude`` saturates at; the backend neither
#: refuses nor warns above it, so the condition is stated here rather than discovered at write time.
ELEVATION_CEILING = 4_294_967_295.0

STILL_REGATE = "not $Composite:GPSPosition"
#: A payload the backend cannot parse emits no ``Composite`` key, so the atom tags are named too.
QUICKTIME_REGATE = (
    "not $Composite:GPSPosition and not $Keys:GPSCoordinates"
    " and not $UserData:GPSCoordinates and not $ItemList:GPSCoordinates"
)
_REGATE: dict[WriteSetClass, str] = {
    WriteSetClass.EXIF_STILL: STILL_REGATE,
    WriteSetClass.QUICKTIME_ATOM: QUICKTIME_REGATE,
}

#: Members declared for each class, as ``(tag, position_bearing)``.
_DECLARED: dict[WriteSetClass, tuple[tuple[str, bool], ...]] = {
    WriteSetClass.EXIF_STILL: (
        ("GPSLatitude", True),
        ("GPSLatitudeRef", True),
        ("GPSLongitude", True),
        ("GPSLongitudeRef", True),
        ("GPSAltitude", False),
        ("GPSAltitudeRef", False),
        ("GPSDateStamp", False),
        ("GPSTimeStamp", False),
    ),
    WriteSetClass.QUICKTIME_ATOM: (("Keys:GPSCoordinates", True),),
}


@dataclass(frozen=True, slots=True)
class Assignment:
    """One tag assignment of a block.

    ``position_bearing`` holds when the target on disk would not carry the planned position if
    this member alone were refused.
    """

    tag: str
    value: str
    position_bearing: bool

    @property
    def line(self) -> str:
        """The argfile line, an option and its value on one line."""
        return f"-{self.tag}={self.value}"

    @property
    def name(self) -> str:
        """The tag name alone, with any group part discarded."""
        return self.tag.rpartition(":")[2]


def write_set_class(identity: str | None) -> WriteSetClass | None:
    """Return the class of a content-derived container identity, or None outside the allowlist."""
    return CONTAINER_CLASS.get(identity.upper()) if identity else None


def regate_condition(write_class: WriteSetClass) -> str:
    """Return the per-file condition the backend evaluates immediately before writing."""
    return _REGATE[write_class]


def declared_tags(write_class: WriteSetClass) -> frozenset[str]:
    """Return every tag the class may assign."""
    return frozenset(tag for tag, _ in _DECLARED[write_class])


def is_representable_elevation(ele: float) -> bool:
    """Return whether the backend keeps ``ele`` rather than saturating or dropping it.

    Inside the ceiling a still stores the nearest unsigned rational, so the stored value agrees
    with the written decimal to about a micrometre and is not necessarily bit-identical to it; a
    clip's atom keeps the decimal text. Above the ceiling a still saturates at it silently and an
    atom drops its third component, which is why this is decided upstream of the write.
    ``nan`` and ``inf`` both fail the magnitude comparison on its own, so ``isfinite`` is
    redundant to the result; it is kept so the exclusion reads without a reader needing to know
    how floating-point comparison treats them.
    """
    return math.isfinite(ele) and abs(ele) <= ELEVATION_CEILING


def without_unrepresentable_elevation(position: Position) -> tuple[Position, bool]:
    """Drop an elevation the backend cannot represent; report whether one was dropped."""
    if position.ele is None or is_representable_elevation(position.ele):
        return position, False
    return replace(position, ele=None), True


def _signed(value: float, places: int) -> str:
    # Adding 0.0 turns -0.0 into 0.0 so a zero never carries a sign the position did not have.
    return f"{value + 0.0:.{places}f}"


def iso6709(position: Position) -> str:
    """Format a position for ``Keys:GPSCoordinates`` with the sign taken from the value.

    The atom has no reference member to carry a hemisphere, so ``abs()`` here would mirror a
    southern or western position in both components.
    """
    parts = [_signed(position.lat, 9), _signed(position.lon, 9)]
    if position.ele is not None:
        parts.append(_signed(position.ele, 4))
    return " ".join(parts)


def _member(write_class: WriteSetClass, tag: str, value: str) -> Assignment:
    bearing = dict(_DECLARED[write_class])[tag]
    return Assignment(tag, value, bearing)


def assignments(
    write_class: WriteSetClass, position: Position, instant: datetime
) -> tuple[Assignment, ...]:
    """Build the class's assignments for one target, in declared order.

    A still carries six members when no elevation is derived and eight when one is.
    """

    def member(tag: str, value: str) -> Assignment:
        return _member(write_class, tag, value)

    if write_class is WriteSetClass.QUICKTIME_ATOM:
        return (member("Keys:GPSCoordinates", iso6709(position)),)
    stamp = instant.astimezone(UTC)
    members = [
        member("GPSLatitude", f"{abs(position.lat):.9f}"),
        member("GPSLatitudeRef", "N" if position.lat >= 0 else "S"),
        member("GPSLongitude", f"{abs(position.lon):.9f}"),
        member("GPSLongitudeRef", "E" if position.lon >= 0 else "W"),
    ]
    if position.ele is not None:
        members += [
            member("GPSAltitude", f"{abs(position.ele):.4f}"),
            member("GPSAltitudeRef", "0" if position.ele >= 0 else "1"),
        ]
    members += [
        member("GPSDateStamp", f"{stamp:%Y:%m:%d}"),
        member("GPSTimeStamp", f"{stamp:%H:%M:%S}"),
    ]
    return tuple(members)
