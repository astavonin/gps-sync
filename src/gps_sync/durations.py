"""Parsing of the shared duration grammar and the ``--utc-offset`` form."""

import re
from datetime import timedelta

_DURATION = re.compile(r"^(?P<sign>[+-]?)(?P<number>\d+(?:\.\d+)?)(?P<unit>[smh]?)$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
_OFFSET = re.compile(r"^(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2})$")


class DurationError(ValueError):
    """The text is not a valid duration or offset."""


def parse_duration(text: str, *, signed: bool = False) -> timedelta:
    """Parse ``<number>[s|m|h]``; a leading sign is accepted only when ``signed``.

    A bare number is seconds, no compound form (``1h30m``) is accepted, and no
    whitespace is allowed between number and unit.
    """
    match = _DURATION.fullmatch(text)
    if match is None:
        raise DurationError(
            f"invalid duration {text!r}: expected a number with an optional s, m or h suffix"
        )
    sign = match["sign"]
    if sign and not signed:
        raise DurationError(f"invalid duration {text!r}: a sign is not allowed here")
    seconds = float(match["number"]) * _UNIT_SECONDS[match["unit"]]
    value = timedelta(seconds=seconds)
    return -value if sign == "-" else value


def parse_utc_offset(text: str) -> timedelta:
    """Parse ``±HH:MM`` into a timedelta."""
    match = _OFFSET.fullmatch(text)
    if match is None:
        raise DurationError(f"invalid UTC offset {text!r}: expected ±HH:MM")
    hours, minutes = int(match["hours"]), int(match["minutes"])
    if hours > 23 or minutes > 59:
        raise DurationError(f"invalid UTC offset {text!r}: out of range")
    value = timedelta(hours=hours, minutes=minutes)
    return -value if match["sign"] == "-" else value
