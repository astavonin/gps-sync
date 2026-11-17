"""The time-evidence dispatch: total over the record's time tags, refusal by default."""

from datetime import UTC, datetime, timedelta

import pytest

from gps_sync.timeres import TimeFailure, is_quicktime_family, resolve_instant
from tests.support import tag_record

PLUS_SIX = timedelta(hours=6)
NO_SHIFT = timedelta(0)
ATOM = "2026:10:26 03:42:14"
ATOM_INSTANT = datetime(2026, 10, 26, 3, 42, 14, tzinfo=UTC)


def video(**kwargs):
    defaults = {
        "file_type": "MP4",
        "date_time_original": None,
        "offset": None,
        "create_date": ATOM,
        "major_brand": "isom",
    }
    defaults.update(kwargs)
    return tag_record("/m/a.MP4", **defaults)


def resolve(record, utc_offset=None):
    return resolve_instant(record, utc_offset, NO_SHIFT)


def refused(record, utc_offset=None, *, unverified: bool) -> TimeFailure:
    result = resolve(record, utc_offset)
    assert isinstance(result, TimeFailure), result
    assert result.unverified is unverified
    return result


class TestExifEvidenceOutranksAnyAtom:
    def test_row1_a_fujifilm_mov_resolves_from_its_exif_time_and_offset_not_its_atom(self):
        record = tag_record(
            "/m/a.MOV",
            file_type="MOV",
            date_time_original="2026:10:26 09:46:31",
            offset="+06:00",
            create_date="2026:10:26 03:47:20",
            major_brand="qt",
        )

        assert resolve(record) == datetime(2026, 10, 26, 3, 46, 31, tzinfo=UTC)

    def test_row1_the_utc_offset_flag_is_ignored_when_the_embedded_offset_is_present(self):
        record = tag_record("/m/a.MOV", file_type="MOV", offset="-05:00")

        assert resolve(record, PLUS_SIX) == resolve(record)

    def test_row2_a_blank_offset_refuses_whatever_the_flag(self):
        record = tag_record("/m/a.MOV", file_type="MOV", offset="")

        assert "present but blank" in refused(record, PLUS_SIX, unverified=False).detail

    def test_row3_an_unparseable_offset_refuses_quoting_it(self):
        record = tag_record("/m/a.MOV", file_type="MOV", offset="six")

        assert "'six'" in refused(record, PLUS_SIX, unverified=False).detail

    @pytest.mark.parametrize("stamp", ["", "garbage", "2026:13:01 00:00:00"])
    def test_row4_a_blank_or_unparseable_exposure_time_refuses_even_with_an_atom_available(
        self, stamp
    ):
        record = video(date_time_original=stamp)

        refused(record, unverified=False)

    def test_row5_a_still_without_an_offset_uses_the_flag(self):
        record = tag_record(file_type="RAF", offset=None)

        assert resolve(record, PLUS_SIX) == datetime(2026, 10, 26, 4, 0, 30, tzinfo=UTC)

    def test_row5_a_still_without_an_offset_and_without_the_flag_refuses(self):
        refused(tag_record(file_type="RAF", offset=None), unverified=False)

    def test_row6_a_quicktime_container_with_exif_time_and_no_offset_is_unverified_flag_or_not(
        self,
    ):
        record = tag_record("/m/a.MOV", file_type="MOV", offset=None)

        assert refused(record, PLUS_SIX, unverified=True).detail
        assert refused(record, None, unverified=True).detail

    def test_row7_a_still_with_no_exposure_time_refuses_whatever_its_create_date_holds(self):
        record = tag_record(file_type="DNG", date_time_original=None, create_date=ATOM)

        refused(record, PLUS_SIX, unverified=False)


class TestTheContainerAtomAsSoleEvidence:
    @pytest.mark.parametrize("brand", ["isom", "mp42"])
    def test_row9_a_sole_atom_is_utc_under_a_declared_brand(self, brand):
        assert resolve(video(major_brand=brand)) == ATOM_INSTANT

    @pytest.mark.parametrize("brand", ["qt", "mp41", "iso2", "avc1", "dash", None])
    def test_row10_a_brand_outside_the_declared_set_is_unverified(self, brand):
        refused(video(major_brand=brand), unverified=True)

    def test_row9_the_utc_offset_flag_is_never_consulted_for_a_video_container(self):
        assert resolve(video(), PLUS_SIX) == ATOM_INSTANT
        refused(video(major_brand="qt"), PLUS_SIX, unverified=True)

    def test_row10_two_create_date_instances_are_unverified_even_under_a_declared_brand(self):
        record = tag_record(
            "/m/a.MP4",
            file_type="MP4",
            date_time_original=None,
            offset=None,
            create_date=ATOM,
            major_brand="isom",
            extra={"Copy1:CreateDate": "2026:10:26 09:42:14"},
        )

        refused(record, unverified=True)

    def test_row8_a_zone_bearing_creation_date_that_equals_the_atom_corroborates_it(self):
        record = video(creation_date="2026:10:26 09:42:14+06:00", major_brand="qt", file_type="MOV")

        assert resolve(record) == ATOM_INSTANT

    @pytest.mark.parametrize(
        "creation",
        [
            "2026:10:26 09:42:14",
            "2026:10:26 09:42:14+05:00",
            "garbage",
            "",
        ],
        ids=["zoneless", "disagreeing-offset", "unparseable", "blank"],
    )
    def test_row10_a_creation_date_that_does_not_corroborate_leaves_the_atom_unverified(
        self, creation
    ):
        refused(video(creation_date=creation), unverified=True)

    def test_row8_a_zulu_creation_date_equal_to_the_atom_corroborates_it(self):
        assert resolve(video(creation_date="2026:10:26 03:42:14Z")) == ATOM_INSTANT

    def test_row8_corroboration_rescues_a_brand_outside_the_declared_set(self):
        record = video(creation_date="2026:10:26 09:42:14+06:00", major_brand="qt")

        assert resolve(record) == ATOM_INSTANT

    @pytest.mark.parametrize("atom", [None, "", "garbage", "2026:13:01 00:00:00"])
    def test_row11_an_absent_blank_or_unparseable_atom_is_unresolvable_not_unverified(self, atom):
        refused(video(create_date=atom), unverified=False)

    def test_the_atom_is_not_admissible_for_a_still_container(self):
        refused(video(file_type="RAF"), unverified=False)


class TestTheSingleShiftPoint:
    @pytest.mark.parametrize(
        "record",
        [
            tag_record(file_type="RAF"),
            video(),
            video(creation_date="2026:10:26 09:42:14+06:00", major_brand="qt"),
        ],
        ids=["exif", "sole-witness", "corroborated"],
    )
    def test_the_shift_is_applied_exactly_once_whichever_row_resolved(self, record):
        base = resolve(record)

        assert resolve_instant(record, None, timedelta(seconds=90)) == base + timedelta(seconds=90)

    def test_a_refusal_carries_no_shifted_instant(self):
        assert isinstance(
            resolve_instant(video(major_brand="qt"), None, timedelta(seconds=5)), TimeFailure
        )


class TestTheQuickTimeFamilyPredicate:
    @pytest.mark.parametrize("identity", ["MOV", "mp4", "M4V", "QuickTime", "quicktime"])
    def test_the_members_are_case_insensitive(self, identity):
        assert is_quicktime_family(identity)

    @pytest.mark.parametrize("identity", ["RAF", "DNG", "JPEG", "", None])
    def test_everything_else_is_not_in_the_family(self, identity):
        assert not is_quicktime_family(identity)


class TestEveryRefusalSaysWhatIsWrong:
    def test_row4_a_blank_exposure_time(self):
        record = video(date_time_original="")

        assert refused(record, unverified=False).detail == (
            "DateTimeOriginal is present but blank; refusing to resolve a time"
        )

    def test_row4_an_unparseable_exposure_time_is_quoted(self):
        assert refused(video(date_time_original="soon"), unverified=False).detail == (
            "malformed DateTimeOriginal 'soon'"
        )

    def test_row2_a_blank_offset(self):
        record = tag_record(file_type="RAF", offset="")

        assert refused(record, unverified=False).detail == (
            "OffsetTimeOriginal is present but blank; refusing to assume an offset"
        )

    def test_row3_a_malformed_offset_is_quoted(self):
        assert refused(tag_record(offset="six"), unverified=False).detail == (
            "malformed OffsetTimeOriginal 'six'"
        )

    def test_row5_no_offset_and_no_flag_on_a_still(self):
        assert refused(tag_record(offset=None), unverified=False).detail == (
            "OffsetTimeOriginal is absent and --utc-offset was not given for this local-time still"
        )

    def test_row6_a_competing_witness_on_a_quicktime_container(self):
        record = tag_record("/m/a.MOV", file_type="MOV", offset=None)

        assert refused(record, unverified=True).detail == (
            "a QuickTime-family container holds a second, competing time witness and carries "
            "no OffsetTimeOriginal; --utc-offset is never consulted for it"
        )

    def test_row7_a_still_with_no_exposure_time(self):
        record = tag_record(file_type="RAF", date_time_original=None)

        assert refused(record, unverified=False).detail == (
            "DateTimeOriginal is absent; CreateDate is not admissible for a still"
        )

    def test_row11_no_atom_at_all(self):
        assert refused(video(create_date=None), unverified=False).detail == (
            "no CreateDate on a QuickTime-family container"
        )

    def test_row11_a_blank_atom_is_not_called_absent(self):
        assert refused(video(create_date=""), unverified=False).detail == (
            "CreateDate is present but blank on a QuickTime-family container"
        )

    def test_row11_an_unparseable_atom_is_quoted(self):
        assert refused(video(create_date="never"), unverified=False).detail == (
            "malformed CreateDate 'never'"
        )

    def test_a_zulu_atom_is_admitted_as_utc(self):
        assert resolve(video(create_date="2026:10:26 03:42:14Z")) == ATOM_INSTANT

    def test_row10_names_the_evidence_it_refused_on(self):
        record = video(creation_date="2026:10:26 09:42:14", major_brand="isom")

        assert refused(record, unverified=True).detail == (
            "the container's creation time has no zone this project can verify "
            "(CreateDate instances 1, CreationDate '2026:10:26 09:42:14', MajorBrand 'isom')"
        )

    @pytest.mark.parametrize(
        "creation",
        ["2026:13:26 09:42:14+06:00", "2026:10:26 09:42:14+99:99", "2026:10:26 09:42:14+0600"],
        ids=["impossible-date", "impossible-offset", "no-colon-in-offset"],
    )
    def test_a_creation_date_that_cannot_be_read_never_corroborates(self, creation):
        refused(video(creation_date=creation), unverified=True)

    def test_corroboration_is_required_to_be_exact_to_the_microsecond(self):
        refused(video(creation_date="2026:10:26 09:42:14.5+06:00"), unverified=True)

    def test_a_fractional_atom_keeps_its_fraction(self):
        assert resolve(video(create_date="2026:10:26 03:42:14.250")).microsecond == 250_000

    def test_a_blank_creation_date_is_not_read_as_an_absent_one_under_a_declared_brand(self):
        refused(video(creation_date="", major_brand="isom"), unverified=True)


class TestZuluHandling:
    def test_a_still_exposure_time_with_a_zulu_suffix_is_malformed(self):
        record = tag_record(file_type="RAF", date_time_original="2026:10:26 10:00:30Z")

        assert refused(record, unverified=False).detail == (
            "malformed DateTimeOriginal '2026:10:26 10:00:30Z'"
        )

    def test_an_atom_with_a_zulu_suffix_is_read_as_utc(self):
        assert resolve(video(create_date="2026:10:26 03:42:14Z")) == ATOM_INSTANT

    def test_an_atom_with_two_zulu_suffixes_is_malformed(self):
        assert refused(video(create_date="2026:10:26 03:42:14ZZ"), unverified=False).detail == (
            "malformed CreateDate '2026:10:26 03:42:14ZZ'"
        )

    def test_a_corroborating_creation_date_with_a_zulu_zone_is_read_as_offset_zero(self):
        assert (
            resolve(video(creation_date="2026:10:26 03:42:14Z", major_brand="qt")) == ATOM_INSTANT
        )


def test_the_fraction_is_taken_to_the_microsecond_and_never_rounds_up_a_second():
    record = tag_record(file_type="RAF", date_time_original="2026:10:26 10:00:30.9999999999")

    assert resolve(record).microsecond == 999_999
