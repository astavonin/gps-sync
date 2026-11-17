"""One serialisation of the outcome sequence, shared by the JSON record and the audit record.

Absent fields are omitted rather than rendered as null or zero, so an absent position, elevation
or speed can never be mistaken for a legitimate value of ``0``.
"""

from datetime import UTC, datetime
from typing import Any

from gps_sync.model import (
    Centroid,
    Extended,
    Failed,
    Geotagged,
    Ignored,
    Interpolated,
    Outcome,
    Planned,
    Position,
    Protected,
    Provenance,
    Unmatchable,
    assert_never,
    reason_key,
    variant_name,
)
from gps_sync.registry import spec


def instant_text(instant: datetime) -> str:
    """Render an instant as ISO 8601 in UTC."""
    return instant.astimezone(UTC).isoformat()


def position_dict(position: Position) -> dict[str, float]:
    """Render a position; ``ele`` is omitted when elevation is not derivable."""
    data = {"lat": position.lat, "lon": position.lon}
    if position.ele is not None:
        data["ele"] = position.ele
    return data


def provenance_dict(provenance: Provenance) -> dict[str, Any]:
    """Render a provenance with its kind."""
    match provenance:
        case Interpolated():
            return {
                "kind": "Interpolated",
                "segment": provenance.segment,
                "bracket_s": provenance.bracket_s,
            }
        case Extended():
            return {
                "kind": "Extended",
                "segment": provenance.segment,
                "clamp_s": provenance.clamp_s,
                "overridden_reason": provenance.overridden_reason.value,
            }
        case Centroid():
            return {
                "kind": "Centroid",
                "samples_read": provenance.samples_read,
                "samples_used": provenance.samples_used,
                "dropped": {reason.value: count for reason, count in provenance.dropped},
                "span_s": provenance.span_s,
                "shortfall_s": provenance.shortfall_s,
                "rule": provenance.rule.value,
                "spread_m": provenance.spread_m,
                "altitude": provenance.altitude.value,
            }
        case _:
            assert_never(provenance)


def _put(data: dict[str, Any], key: str, value: object) -> None:
    if value is not None:
        data[key] = value


def outcome_to_dict(outcome: Outcome) -> dict[str, Any]:
    """Render one outcome; exhaustive over the six variants."""
    key = reason_key(outcome)
    data: dict[str, Any] = {"path": outcome.path, "variant": variant_name(outcome)}
    match outcome:
        case Ignored():
            data["reason"] = spec(key).json
            _put(data, "detail", outcome.detail)
            return data
        case Protected() | Failed() | Unmatchable():
            data["reason"] = spec(key).json
        case Planned() | Geotagged():
            pass
        case _:
            assert_never(outcome)
    _put(data, "instant", instant_text(outcome.instant) if outcome.instant else None)
    match outcome:
        case Protected() | Planned() | Geotagged():
            if outcome.position is not None:
                data["position"] = position_dict(outcome.position)
        case Failed() | Unmatchable():
            pass
    match outcome:
        case Protected() | Failed() | Planned() | Geotagged():
            _put(data, "via", outcome.via.value if outcome.via else None)
            if outcome.provenance is not None:
                data["provenance"] = provenance_dict(outcome.provenance)
            _put(data, "bracket_speed", outcome.bracket_speed)
        case Unmatchable():
            _put(data, "nearest_segment", outcome.nearest_segment)
            _put(data, "miss_s", outcome.miss_s)
            _put(data, "bracket_s", outcome.bracket_s)
        case _:
            assert_never(outcome)
    match outcome:
        case Failed() | Unmatchable() | Protected():
            _put(data, "detail", outcome.detail)
        case Planned() | Geotagged():
            pass
    if outcome.context is not None:
        context = {
            k: v
            for k, v in (
                ("make", outcome.context.make),
                ("model", outcome.context.model),
                ("file_type", outcome.context.file_type),
            )
            if v is not None
        }
        if context:
            data["context"] = context
    return data
