"""The telemetry derivation: a clip's own per-sample fixes reduced to one position.

Everything here is a pure function of a container record, a telemetry record and the declared
domain; no file is opened and no process spawned. The steps run in a declared order and each has
an outcome for an empty or unsound input, so no figure is ever computed over an empty set:
the atom question, the co-indexing rule, the value domain, the empty check, the coverage check,
the longitude-wrap check and the aggregation.
"""

import math
from dataclasses import dataclass

from gps_sync.geo import haversine_m
from gps_sync.model import (
    AggregationRule,
    AltitudeOutcome,
    Centroid,
    FailedReason,
    Position,
    SampleRejection,
)
from gps_sync.records import TagRecord, TelemetryRecord, parse_atom_coordinates
from gps_sync.writeset import is_representable_elevation

#: A scan may fall short of the clip's own duration by this much and no more. The measured
#: shortfall is one 59.94 Hz sample interval (0.0167 s) on clips from 24 MB to 8.87 GB.
COVERAGE_CEILING_S = 1.0
#: A used-fix list spanning more than this many degrees of longitude is refused, not averaged.
WRAP_LIMIT_DEG = 180.0


@dataclass(frozen=True, slots=True)
class AtomPresent:
    """The telemetry read found exactly one readable container atom; nothing is derived."""

    position: Position


@dataclass(frozen=True, slots=True)
class AtomUnreadable:
    """A container atom exists and its position cannot be established."""

    detail: str


@dataclass(frozen=True, slots=True)
class TelemetryRefusal:
    """No position could be derived, for a declared registry reason."""

    reason: FailedReason
    detail: str


@dataclass(frozen=True, slots=True)
class TelemetryCentroid:
    """The derived position and the provenance that makes it auditable."""

    position: Position
    provenance: Centroid


TelemetryResult = AtomPresent | AtomUnreadable | TelemetryRefusal | TelemetryCentroid


def _number(value: object) -> float | None:
    """Return ``value`` as a finite float, or None when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _atom_question(telemetry: TelemetryRecord) -> AtomPresent | AtomUnreadable | None:
    """Answer the three-valued atom question; None means provably absent."""
    values = telemetry.atom_instances
    if not values:
        return None
    if len(values) > 1:
        return AtomUnreadable(
            f"{len(values)} container position atoms were read and may disagree: "
            + "; ".join(repr(value) for value in values)
        )
    position = parse_atom_coordinates(values[0])
    if position is None:
        return AtomUnreadable(
            f"a container position atom is present but unparseable: {values[0]!r}"
        )
    return AtomPresent(position)


def _inconsistent(detail: str) -> TelemetryRefusal:
    return TelemetryRefusal(FailedReason.TELEMETRY_INCONSISTENT, detail)


def _unusable(detail: str) -> TelemetryRefusal:
    return TelemetryRefusal(FailedReason.TELEMETRY_UNUSABLE, detail)


def _fix(lat: object, lon: object) -> tuple[float, float] | SampleRejection:
    """Apply the value domain to one sample's pair: a usable fix, or the reason it is not one."""
    lat_value, lon_value = _number(lat), _number(lon)
    if lat_value is None or lon_value is None:
        return SampleRejection.UNPARSEABLE
    if abs(lat_value) > 90 or abs(lon_value) > 180:
        return SampleRejection.OUT_OF_RANGE
    if lat_value == 0 and lon_value == 0:
        return SampleRejection.ZERO_SENTINEL
    return lat_value, lon_value


def _mean(values: list[float]) -> float:
    """Sum and divide in binary double precision, which returns ``inf`` rather than raising."""
    return sum(values) / len(values)


def _altitude(
    telemetry: TelemetryRecord, fixes: int, kept: list[int]
) -> tuple[float | None, AltitudeOutcome]:
    """Derive the elevation over the used fixes, or the declared reason none was derived."""
    available = len(telemetry.altitudes)
    if available == 0:
        return None, AltitudeOutcome.NONE_USABLE
    if available != fixes:
        return None, AltitudeOutcome.NOT_CO_INDEXED
    parsed = [number for i in kept if (number := _number(telemetry.altitudes[i])) is not None]
    if not parsed:
        return None, AltitudeOutcome.NONE_USABLE
    mean = _mean(parsed)
    if not is_representable_elevation(mean):
        return None, AltitudeOutcome.NOT_REPRESENTABLE
    return mean, AltitudeOutcome.ABSOLUTE_ALTITUDE


def derive_centroid(container: TagRecord, telemetry: TelemetryRecord) -> TelemetryResult:
    """Reduce a clip's telemetry to the centroid of its usable fixes, or a declared refusal."""
    atom = _atom_question(telemetry)
    if atom is not None:
        return atom
    fixes = len(telemetry.latitudes)
    if fixes != len(telemetry.longitudes):
        return _inconsistent(
            f"{fixes} GPSLatitude samples but {len(telemetry.longitudes)} GPSLongitude samples"
        )
    times = len(telemetry.sample_times)
    if fixes == 0 and times == 0:
        return _unusable("the telemetry read returned no samples")
    if times != fixes:
        return _inconsistent(f"{times} SampleTime samples for {fixes} fixes")

    dropped = dict.fromkeys(SampleRejection, 0)
    kept: list[int] = []
    used: list[tuple[float, float]] = []
    for i in range(fixes):
        fix = _fix(telemetry.latitudes[i], telemetry.longitudes[i])
        if isinstance(fix, SampleRejection):
            dropped[fix] += 1
        else:
            kept.append(i)
            used.append(fix)
    if not used:
        counts = ", ".join(f"{reason.value} {count}" for reason, count in dropped.items())
        return _unusable(f"none of {fixes} samples is a usable fix ({counts})")

    sample_times = [_number(value) for value in telemetry.sample_times]
    if any(value is None for value in sample_times):
        return _inconsistent("a SampleTime value is not a finite number")
    present = [value for value in sample_times if value is not None]
    span = max(present) - min(present)
    if container.duration_s is None:
        return _inconsistent("the container record carries no readable Duration")
    shortfall = container.duration_s - span
    if not 0 <= shortfall <= COVERAGE_CEILING_S:
        return _inconsistent(
            f"Duration {container.duration_s:.4f} s, sample span {span:.4f} s, "
            f"shortfall {shortfall:.4f} s is outside 0 to {COVERAGE_CEILING_S} s"
        )

    lats = [lat for lat, _ in used]
    lons = [lon for _, lon in used]
    longitude_span = max(lons) - min(lons)
    if longitude_span > WRAP_LIMIT_DEG:
        return _unusable(
            f"the usable fixes span {longitude_span:.1f} degrees of longitude and are not averaged"
        )
    lat, lon = _mean(lats), _mean(lons)
    elevation, altitude = _altitude(telemetry, fixes, kept)
    spread = max(haversine_m(lat, lon, a, b) for a, b in used)
    return TelemetryCentroid(
        Position(lat, lon, elevation),
        Centroid(
            samples_read=fixes,
            samples_used=len(kept),
            dropped=tuple(dropped.items()),
            span_s=span,
            shortfall_s=shortfall,
            rule=AggregationRule.ARITHMETIC_MEAN,
            spread_m=spread,
            altitude=altitude,
        ),
    )
