from datetime import timedelta
from pathlib import Path

import pytest

from gps_sync.durations import DurationError, parse_duration, parse_utc_offset
from gps_sync.errors import ToolFailure
from gps_sync.options import DEFAULT_MAX_GAP, parse_options

BASE = ["--media", "/m", "--tracks", "/t"]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("120", 120), ("120s", 120), ("2m", 120), ("0.5h", 1800), ("0", 0), ("90m", 5400)],
)
def test_parse_duration_accepts_equivalent_spellings_as_the_same_value(text, seconds):
    assert parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["1h30m", "120 s", "2M", "", "s", "1.", ".5", "1e3", "5x", " 5"])
def test_parse_duration_rejects_text_outside_the_grammar(text):
    with pytest.raises(DurationError):
        parse_duration(text)


@pytest.mark.parametrize("text", ["+60s", "-60s", "-1", "+1h"])
def test_parse_duration_rejects_a_sign_when_unsigned(text):
    with pytest.raises(DurationError):
        parse_duration(text)


def test_parse_duration_signed_form_carries_opposite_signs():
    assert parse_duration("+60s", signed=True) == timedelta(seconds=60)
    assert parse_duration("-60s", signed=True) == timedelta(seconds=-60)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("+06:00", timedelta(hours=6)), ("-05:30", -timedelta(hours=5, minutes=30))],
)
def test_parse_utc_offset_accepts_signed_hh_mm(text, expected):
    assert parse_utc_offset(text) == expected


@pytest.mark.parametrize("text", ["6", "+6", "+06:00:00", "06:00", "+24:00", "+06:60", ""])
def test_parse_utc_offset_rejects_everything_else(text):
    with pytest.raises(DurationError):
        parse_utc_offset(text)


def test_defaults_match_the_documented_inventory():
    options = parse_options(BASE)

    assert options.write is False
    assert options.max_gap == DEFAULT_MAX_GAP == timedelta(seconds=120)
    assert options.extend == timedelta(0)
    assert options.time_shift == timedelta(0)
    assert options.utc_offset is None
    assert options.review_dir is None
    assert options.keep_original is False
    assert options.json is False
    assert options.verbose is False


def test_every_flag_in_the_inventory_is_accepted():
    options = parse_options(
        [
            *BASE,
            "--write",
            "--max-gap",
            "30s",
            "--extend",
            "90m",
            "--time-shift",
            "-60s",
            "--utc-offset",
            "+06:00",
            "--review-dir",
            "/r",
            "--keep-original",
            "--json",
            "--verbose",
        ]
    )

    assert options.write and options.keep_original and options.json and options.verbose
    assert options.max_gap == timedelta(seconds=30)
    assert options.extend == timedelta(minutes=90)
    assert options.time_shift == timedelta(seconds=-60)
    assert options.utc_offset == timedelta(hours=6)
    assert options.review_dir == Path("/r")


def test_time_shift_accepts_both_the_glued_and_the_separate_negative_form():
    assert parse_options([*BASE, "--time-shift=-60s"]).time_shift == timedelta(seconds=-60)
    assert parse_options([*BASE, "--time-shift", "+60s"]).time_shift == timedelta(seconds=60)


@pytest.mark.parametrize(
    "bad",
    [
        ["--max-gap", "-5"],
        ["--extend", "+5"],
        ["--max-gap", "1h30m"],
        ["--time-shift", "1h30m"],
        ["--utc-offset", "6"],
        ["--no-such-flag"],
        ["--wri"],
    ],
)
def test_invalid_arguments_are_tool_level_failures(bad):
    with pytest.raises(ToolFailure):
        parse_options([*BASE, *bad])


def test_missing_required_arguments_are_a_tool_level_failure():
    with pytest.raises(ToolFailure):
        parse_options(["--media", "/m"])
