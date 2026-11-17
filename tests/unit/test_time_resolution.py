from datetime import UTC, datetime, timedelta

import pytest

from gps_sync.model import Failed, FailedReason
from gps_sync.records import TagRecord
from gps_sync.timeres import TimeFailure, resolve_instant
from tests.support import classify, config, index_of, segment, tag_record

TRACK = index_of(segment("t.gpx#trk0seg0", [(0, 42.0, 77.0, 100.0), (600, 42.006, 77.0, 100.0)]))

NO_SHIFT = timedelta(0)
PLUS_SIX = timedelta(hours=6)


def resolved(record, utc_offset=None, shift=NO_SHIFT):
    result = resolve_instant(record, utc_offset, shift)
    assert isinstance(result, datetime), result
    return result


def failed(record, utc_offset=None) -> TimeFailure:
    result = resolve_instant(record, utc_offset, NO_SHIFT)
    assert isinstance(result, TimeFailure), result
    return result


def test_row1_embedded_offset_converts_local_time_to_utc():
    record = tag_record(date_time_original="2026:10:26 09:59:06", offset="+06:00")

    assert resolved(record) == datetime(2026, 10, 26, 3, 59, 6, tzinfo=UTC)


def test_row1_embedded_offset_wins_over_a_supplied_utc_offset():
    record = tag_record(date_time_original="2026:10:26 09:59:06", offset="+06:00")

    assert resolved(record, utc_offset=timedelta(hours=-5)) == datetime(
        2026, 10, 26, 3, 59, 6, tzinfo=UTC
    )


def test_row2_without_an_embedded_offset_the_flag_is_applied():
    record = tag_record(file_type="DNG", offset=None, date_time_original="2026:10:26 09:46:31")

    assert resolved(record, utc_offset=PLUS_SIX) == datetime(2026, 10, 26, 3, 46, 31, tzinfo=UTC)


def test_a_blank_offset_is_unreadable_and_is_not_replaced_by_the_flag():
    record = tag_record(offset="")

    assert "blank" in failed(record, utc_offset=PLUS_SIX).detail


def test_a_blank_primary_offset_beside_a_copy_value_fails_rather_than_planning():
    raw = {
        "SourceFile": "/m/a.RAF",
        ":DateTimeOriginal": "2026:10:26 10:05:00",
        ":FileType": "RAF",
        ":OffsetTimeOriginal": "",
        "Copy1:OffsetTimeOriginal": "+06:00",
    }
    record = TagRecord.from_json(raw)

    assert isinstance(resolve_instant(record, PLUS_SIX, NO_SHIFT), TimeFailure)
    outcome = classify(record, TRACK, config(utc_offset=PLUS_SIX))
    assert isinstance(outcome, Failed)
    assert outcome.reason is FailedReason.TIME_UNRESOLVABLE


def test_row2_without_the_flag_fails_naming_the_missing_tag():
    record = tag_record(file_type="DNG", offset=None)

    assert "OffsetTimeOriginal" in failed(record).detail


def test_row3_create_date_is_not_admissible_for_a_still():
    record = tag_record(date_time_original=None, create_date="2026:10:26 09:59:06")

    assert "CreateDate is not admissible" in failed(record, utc_offset=PLUS_SIX).detail


def test_row4_a_still_with_no_time_tag_fails():
    assert "DateTimeOriginal" in failed(tag_record(date_time_original=None)).detail


def test_row5_a_utc_native_container_ignores_the_utc_offset_flag():
    record = tag_record(
        file_type="MP4",
        date_time_original=None,
        offset=None,
        create_date="2026:10:26 03:35:26",
        major_brand="isom",
    )

    assert resolved(record, utc_offset=PLUS_SIX) == datetime(2026, 10, 26, 3, 35, 26, tzinfo=UTC)


def test_row6_a_utc_native_container_without_create_date_fails():
    record = tag_record(file_type="MOV", date_time_original=None, create_date=None)

    assert "CreateDate" in failed(record).detail


@pytest.mark.parametrize(
    "stamp",
    ["garbage", "2026:13:01 00:00:00", "2026:10:26", "2026:10:26 25:00:00", "2026:10:26 10:00:30Z"],
)
def test_a_malformed_timestamp_is_a_failure_and_not_an_exception(stamp):
    assert "malformed" in failed(tag_record(date_time_original=stamp)).detail


def test_a_malformed_offset_is_a_failure():
    assert "malformed OffsetTimeOriginal" in failed(tag_record(offset="six")).detail


def test_fractional_seconds_are_kept():
    record = tag_record(date_time_original="2026:10:26 09:59:06.250")

    assert resolved(record).microsecond == 250_000


@pytest.mark.parametrize(
    ("record", "offset"),
    [
        (tag_record(date_time_original="2026:10:26 09:59:06", offset="+06:00"), None),
        (
            tag_record(
                file_type="MP4",
                date_time_original=None,
                offset=None,
                create_date="2026:10:26 03:59:06",
                major_brand="isom",
            ),
            None,
        ),
    ],
    ids=["local-plus-offset", "utc-native"],
)
def test_time_shift_is_applied_after_utc_resolution_for_every_container_class(record, offset):
    base = resolved(record, offset)

    assert resolved(record, offset, shift=timedelta(seconds=60)) == base + timedelta(seconds=60)
    assert resolved(record, offset, shift=timedelta(seconds=-60)) == base - timedelta(seconds=60)
