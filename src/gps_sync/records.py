"""The tag record the batched reader returns for each candidate."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from gps_sync.errors import ToolFailure
from gps_sync.model import Position
from gps_sync.transport import MINIMUM_VERSION

_TESTED_VERSION = ".".join(map(str, MINIMUM_VERSION))
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite_number(text: str | None) -> float | None:
    if text is None or _NUMBER.fullmatch(text) is None:
        return None
    number = float(text)
    return number if number - number == 0 else None


def _diagnostics(values: list[object]) -> tuple[str, ...]:
    items: list[object] = []
    for value in values:
        items.extend(value if isinstance(value, list) else [value])
    return tuple(str(item) for item in items)


_INSTANCE_KEY = re.compile(r"(?:Copy(?P<copy>[1-9]\d*))?:(?P<name>\w+)")
_SOURCE_FILE = "SourceFile"


def _tags_by_name(raw: Mapping[str, object]) -> dict[str, list[object]]:
    """Group a ``-a -G4`` record's values by bare tag name, lowest copy number first.

    Keys must be exactly ``:Name`` or ``CopyN:Name`` (N >= 1, Name a bare word); ``SourceFile`` is
    the one key exiftool prints unprefixed. Index 0 of each list is the primary instance (copy 0,
    exiftool's preferred one), which the scalar readers rely on.

    Any other shape is refused rather than reinterpreted: a key such as ``Composite:GPSPosition``
    would otherwise become a distinct tag name, hide a present position and read as "no position",
    the unsafe direction.
    """
    ranked: dict[str, list[tuple[int, object]]] = {}
    for key, value in raw.items():
        if key == _SOURCE_FILE:
            continue
        match = _INSTANCE_KEY.fullmatch(key)
        if match is None:
            source = raw.get(_SOURCE_FILE, "<unknown>")
            raise ToolFailure(
                f"exiftool returned the key {key!r} for {source}, which is not the ':Tag' or "
                "'CopyN:Tag' shape that -a -G4 produces; refusing to guess which tag it names. "
                f"Pin exiftool to {_TESTED_VERSION}, the version this reader was verified "
                "against, or report this output shape"
            )
        rank = int(match.group("copy")) if match.group("copy") else 0
        ranked.setdefault(match.group("name"), []).append((rank, value))
    # The sort is load-bearing, not redundant with dict order: exiftool emits keys in extraction
    # order, which on TIFF-family containers is the reverse of copy order (IFD0's XMP pointer is
    # processed before the ExifIFD one), so a non-preferred XMP "Copy1:" can precede the
    # preferred ":" instance. Dropping the sort silently selects the wrong instance's timestamp.
    return {
        name: [v for _, v in sorted(items, key=lambda i: i[0])] for name, items in ranked.items()
    }


def _primary(tags: Mapping[str, list[object]], name: str) -> str | None:
    """Return the primary instance's stripped text, ``""`` when it is present but blank.

    The primary instance decides even when it is empty: falling through to a copy would resolve a
    file's instant from an instance exiftool did not prefer, where the contract is to leave the
    time unresolvable and refuse the file.
    """
    values = tags.get(name)
    return (_text(values[0]) or "") if values else None


def _first(tags: Mapping[str, list[object]], name: str) -> str | None:
    """Return the primary instance's text, or None when it is absent or blank."""
    return _primary(tags, name) or None


@dataclass(frozen=True, slots=True)
class TagRecord:
    """One candidate's tags plus its per-file diagnostics.

    ``has_gps_key`` distinguishes a tag that is absent from one that is present with an empty
    or unparseable value, which the three-valued gate treats differently.
    """

    path: str
    has_gps_key: bool
    gps_position: object
    # The four time tags keep "" for a present-but-blank value and None for an absent one: a
    # corroborating witness that is present and unreadable must refuse, not read as no witness.
    date_time_original: str | None
    offset_time_original: str | None
    create_date: str | None
    creation_date: str | None
    create_date_count: int
    major_brand: str | None
    duration_s: float | None
    handler_types: tuple[str, ...]
    atom_instances: tuple[object, ...]
    file_type: str | None
    make: str | None
    model: str | None
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    @staticmethod
    def from_json(raw: Mapping[str, object]) -> TagRecord:
        """Build a record from one exiftool ``-j`` object."""
        tags = _tags_by_name(raw)
        positions = tags.get("GPSPosition", [])
        # Any parseable copy counts as a position: reading a file as positioned is the safe error.
        parseable = next((p for p in positions if parse_gps_position(p) is not None), None)
        return TagRecord(
            path=str(raw["SourceFile"]),
            has_gps_key=bool(positions),
            gps_position=parseable if parseable is not None else (positions or [None])[0],
            date_time_original=_primary(tags, "DateTimeOriginal"),
            # Kept as "" when blank: a blank offset is unreadable, and reading it as absent would
            # let --utc-offset stand in for it and write a position hours out.
            offset_time_original=_primary(tags, "OffsetTimeOriginal"),
            create_date=_primary(tags, "CreateDate"),
            creation_date=_primary(tags, "CreationDate"),
            create_date_count=len(tags.get("CreateDate", [])),
            major_brand=_first(tags, "MajorBrand"),
            duration_s=_finite_number(_primary(tags, "Duration")),
            handler_types=tuple(
                text for value in tags.get("HandlerType", []) if (text := _text(value)) is not None
            ),
            atom_instances=tuple(tags.get("GPSCoordinates", [])),
            file_type=_first(tags, "FileType"),
            make=_first(tags, "Make"),
            model=_first(tags, "Model"),
            warnings=_diagnostics(tags.get("Warning", [])),
            errors=_diagnostics(tags.get("Error", [])),
        )


def parse_gps_position(value: object) -> Position | None:
    """Parse a ``-n`` ``Composite:GPSPosition`` value ``"<lat> <lon>"``; None when unparseable."""
    if not isinstance(value, str):
        return None
    parts = value.split()
    if len(parts) != 2 or not all(_NUMBER.fullmatch(p) for p in parts):
        return None
    lat, lon = float(parts[0]), float(parts[1])
    if lat != lat or lon != lon or abs(lat) == float("inf") or abs(lon) == float("inf"):
        return None
    return Position(lat, lon)


def parse_atom_coordinates(value: object) -> Position | None:
    """Parse a ``-n`` ``GPSCoordinates`` atom value: two or three finite numbers.

    Like ``parse_gps_position`` this is a presence-probe grammar, so it applies no range check and
    does not refuse ``(0,0)``: any parseable value means hands-off. The third number is the
    elevation, which the position parser would reject.
    """
    if not isinstance(value, str):
        return None
    parts = value.split()
    if len(parts) not in (2, 3) or not all(_NUMBER.fullmatch(p) for p in parts):
        return None
    numbers = [float(p) for p in parts]
    if any(n - n != 0 for n in numbers):
        return None
    return Position(numbers[0], numbers[1], numbers[2] if len(numbers) == 3 else None)


@dataclass(frozen=True, slots=True)
class TelemetryRecord:
    """One clip's per-sample telemetry lists plus its own atom question.

    This is deliberately a different type from ``TagRecord``: it carries no ``Composite`` value,
    so a presence verdict cannot be taken from a value produced under ``-ee``. Each list holds
    the raw values in sample order; interpreting them is the derivation's job.
    """

    atom_instances: tuple[object, ...]
    latitudes: tuple[object, ...]
    longitudes: tuple[object, ...]
    altitudes: tuple[object, ...]
    sample_times: tuple[object, ...]

    @staticmethod
    def from_json(raw: Mapping[str, object]) -> TelemetryRecord:
        """Build a record from one ``-ee -j`` object."""
        tags = _tags_by_name(raw)
        return TelemetryRecord(
            atom_instances=tuple(tags.get("GPSCoordinates", [])),
            latitudes=tuple(tags.get("GPSLatitude", [])),
            longitudes=tuple(tags.get("GPSLongitude", [])),
            altitudes=tuple(tags.get("AbsoluteAltitude", [])),
            sample_times=tuple(tags.get("SampleTime", [])),
        )
