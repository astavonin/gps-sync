"""GPX parsing: the ``trkpt`` subset, value validation and per-point segment identity."""

import math
import os
import re
import xml.parsers.expat
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from xml.etree import ElementTree as ET

from gps_sync.errors import ToolFailure

_DECIMAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class DropReason(StrEnum):
    """Why a trackpoint was dropped before entering the index."""

    TIME_ABSENT = "time-absent"
    TIME_UNPARSEABLE = "time-unparseable"
    COORDINATE_INVALID = "coordinate-invalid"
    COORDINATE_OUT_OF_RANGE = "coordinate-out-of-range"
    ZERO_SENTINEL = "zero-sentinel"


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """A validated trackpoint tagged with the identity of the segment it came from."""

    time: datetime
    lat: float
    lon: float
    ele: float | None
    segment: str


@dataclass(frozen=True, slots=True)
class TrackSegment:
    """The points of one ``trkseg`` in file order."""

    id: str
    source: str
    points: tuple[TrackPoint, ...]


@dataclass(frozen=True, slots=True)
class GpxFile:
    """The parse result of one GPX file."""

    relative_path: str
    segments: tuple[TrackSegment, ...]
    dropped: dict[DropReason, int] = field(default_factory=dict)

    @property
    def kept(self) -> int:
        """Number of points that survived validation."""
        return sum(len(s.points) for s in self.segments)


@dataclass(frozen=True, slots=True)
class TracksParse:
    """The parse result of a whole tracks root."""

    files: tuple[GpxFile, ...]
    skipped_symlinks: tuple[str, ...]


class _ForbiddenDeclaration(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_decimal(text: str | None) -> float | None:
    if text is None or _DECIMAL.fullmatch(text.strip()) is None:
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def _parse_time(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def _read_root(path: Path) -> ET.Element:
    def reject(*_args: object) -> None:
        raise _ForbiddenDeclaration

    try:
        data = path.read_bytes()
        # A declaration-rejecting pre-pass, because the C ElementTree parser exposes no hooks.
        guard = xml.parsers.expat.ParserCreate()
        guard.StartDoctypeDeclHandler = reject
        guard.EntityDeclHandler = reject
        guard.Parse(data, True)
        return ET.fromstring(data)
    except _ForbiddenDeclaration:
        raise ToolFailure(f"{path}: DOCTYPE and entity declarations are not accepted") from None
    except (ET.ParseError, xml.parsers.expat.ExpatError) as error:
        raise ToolFailure(f"{path}: GPX is not well-formed: {error}") from None
    except OSError as error:
        raise ToolFailure(f"{path}: cannot read GPX: {error}") from None


def _point(element: ET.Element, segment: str) -> TrackPoint | DropReason:
    lat = _parse_decimal(element.get("lat"))
    lon = _parse_decimal(element.get("lon"))
    if lat is None or lon is None:
        return DropReason.COORDINATE_INVALID
    if abs(lat) > 90 or abs(lon) > 180:
        return DropReason.COORDINATE_OUT_OF_RANGE
    if lat == 0 and lon == 0:
        return DropReason.ZERO_SENTINEL
    time_text: str | None = None
    ele_text: str | None = None
    for child in element:
        name = _local(child.tag)
        if name == "time":
            time_text = child.text or ""
        elif name == "ele":
            ele_text = child.text
    if time_text is None:
        return DropReason.TIME_ABSENT
    instant = _parse_time(time_text)
    if instant is None:
        return DropReason.TIME_UNPARSEABLE
    return TrackPoint(instant, lat, lon, _parse_decimal(ele_text), segment)


def segment_id(relative_path: str, trk: int, seg: int) -> str:
    """Return the identity ``<tracks-root-relative-path>#trk<i>seg<j>``."""
    return f"{relative_path}#trk{trk}seg{seg}"


def parse_gpx_file(path: Path, relative_path: str) -> GpxFile:
    """Parse and validate one GPX file; a file with no usable point is a tool failure."""
    root = _read_root(path)
    dropped: Counter[DropReason] = Counter()
    segments: list[TrackSegment] = []
    trks = [c for c in root if _local(c.tag) == "trk"]
    for trk_index, trk in enumerate(trks):
        segs = [c for c in trk if _local(c.tag) == "trkseg"]
        for seg_index, seg in enumerate(segs):
            ident = segment_id(relative_path, trk_index, seg_index)
            kept: list[TrackPoint] = []
            for element in seg:
                if _local(element.tag) != "trkpt":
                    continue
                result = _point(element, ident)
                if isinstance(result, DropReason):
                    dropped[result] += 1
                else:
                    kept.append(result)
            if kept:
                segments.append(TrackSegment(ident, relative_path, tuple(kept)))
    parsed = GpxFile(relative_path, tuple(segments), dict(dropped))
    if parsed.kept == 0:
        raise ToolFailure(
            f"{path}: no usable timed trackpoint (dropped: {dict(dropped) or 'none'})"
        )
    return parsed


def parse_tracks(tracks_root: Path) -> TracksParse:
    """Parse every ``.gpx`` file under an already resolved tracks root.

    Symlinks are skipped and counted, for the same reasons discovery skips them.
    """
    if not tracks_root.is_dir():
        raise ToolFailure(f"tracks root {tracks_root} is not a readable directory")
    files: list[GpxFile] = []
    skipped: list[str] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as error:
            raise ToolFailure(f"cannot read tracks directory {directory}: {error}") from None
        for entry in entries:
            if entry.is_symlink():
                skipped.append(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                walk(Path(entry.path))
            elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".gpx"):
                relative = Path(entry.path).relative_to(tracks_root).as_posix()
                files.append(parse_gpx_file(Path(entry.path), relative))

    walk(tracks_root)
    if not files:
        raise ToolFailure(f"no GPX file found under {tracks_root}")
    return TracksParse(tuple(files), tuple(skipped))
