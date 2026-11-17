"""Record fields, the closed option vocabularies, telemetry chunking and the via table."""

import dataclasses
import gc
import subprocess
from pathlib import Path

import pytest

from gps_sync.errors import InternalConsistencyError, ToolFailure
from gps_sync.model import (
    VIA_SPECS,
    AggregationRule,
    AltitudeOutcome,
    Centroid,
    Extended,
    Interpolated,
    Planned,
    Position,
    SampleRejection,
    UnmatchableReason,
    Via,
)
from gps_sync.reader import read_telemetry
from gps_sync.records import TagRecord, TelemetryRecord, parse_atom_coordinates
from gps_sync.serialise import provenance_dict
from gps_sync.transport import (
    TELEMETRY_CHUNK_SIZE,
    TELEMETRY_OPTIONS,
    TELEMETRY_TAGS,
    VERSION_OPTIONS,
    WRITE_OPTIONS,
    ExiftoolTransport,
    _assert_vocabulary,
    read_command,
    telemetry_command,
    write_command,
)
from tests.support import FakeTransport, at, telemetry_json


class TestTheContainerRecord:
    def raw(self, **tags):
        return {
            "SourceFile": "/m/a.MP4",
            **{f":{k}" if ":" not in k else k: v for k, v in tags.items()},
        }

    def test_every_handler_instance_is_kept_not_only_the_primary(self):
        record = TagRecord.from_json(
            {
                "SourceFile": "/m/a.MP4",
                "Copy3:HandlerType": "meta",
                "Copy1:HandlerType": "vide",
                ":HandlerType": "mdta",
            }
        )

        assert record.handler_types == ("mdta", "vide", "meta")

    @pytest.mark.parametrize(
        "tag", ["DateTimeOriginal", "CreateDate", "CreationDate", "OffsetTimeOriginal"]
    )
    def test_a_blank_time_tag_is_kept_distinct_from_an_absent_one(self, tag):
        field = {
            "DateTimeOriginal": "date_time_original",
            "CreateDate": "create_date",
            "CreationDate": "creation_date",
            "OffsetTimeOriginal": "offset_time_original",
        }[tag]

        blank = TagRecord.from_json(self.raw(**{tag: "  "}))
        absent = TagRecord.from_json(self.raw())

        assert getattr(blank, field) == ""
        assert getattr(absent, field) is None

    def test_the_create_date_instance_count_comes_from_the_tag_already_requested(self):
        record = TagRecord.from_json(
            {"SourceFile": "/m/a.MOV", ":CreateDate": "x", "Copy1:CreateDate": "y"}
        )

        assert record.create_date_count == 2
        assert TagRecord.from_json(self.raw()).create_date_count == 0

    def test_the_brand_is_stripped_so_the_quicktime_brand_arrives_without_its_spaces(self):
        assert TagRecord.from_json(self.raw(MajorBrand="qt  ")).major_brand == "qt"
        assert TagRecord.from_json(self.raw(MajorBrand="    ")).major_brand is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0.4004, 0.4004), ("681.5142", 681.5142), ("x", None), ("inf", None), (None, None)],
    )
    def test_the_duration_is_a_finite_number_or_absent(self, value, expected):
        raw = self.raw() if value is None else self.raw(Duration=value)

        assert TagRecord.from_json(raw).duration_s == expected

    def test_an_unparseable_position_is_kept_verbatim_for_the_diagnostic(self):
        record = TagRecord.from_json({"SourceFile": "/m/a.MP4", ":GPSPosition": "garbage"})

        assert record.gps_position == "garbage"

    def test_an_overflowing_duration_is_not_a_number(self):
        assert TagRecord.from_json(self.raw(Duration="1e999")).duration_s is None

    def test_a_telemetry_object_with_no_samples_parses_to_empty_lists(self):
        record = TelemetryRecord.from_json({"SourceFile": "/m/a.MP4"})

        assert record.latitudes == () and record.atom_instances == ()

    def test_every_atom_instance_is_kept_for_the_gate(self):
        record = TagRecord.from_json(
            {"SourceFile": "/m/a.MP4", ":GPSCoordinates": "1 2", "Copy1:GPSCoordinates": "3 4"}
        )

        assert record.atom_instances == ("1 2", "3 4")


class TestTheAtomGrammar:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("42.1 77.2", Position(42.1, 77.2, None)),
            ("42.1 77.2 2189.9", Position(42.1, 77.2, 2189.9)),
            ("-33.8 -151.2 -12.5", Position(-33.8, -151.2, -12.5)),
            ("1e2 -3.5e-1", Position(100.0, -0.35, None)),
            ("200 400", Position(200.0, 400.0, None)),
            ("0 0", Position(0.0, 0.0, None)),
        ],
    )
    def test_two_or_three_finite_numbers_parse_without_a_range_check(self, value, expected):
        assert parse_atom_coordinates(value) == expected

    @pytest.mark.parametrize(
        "value", ["", "1", "1 2 3 4", "a b", "1,5 2", "nan 1", "1 inf", "1e999 1", None, 5, ["1 2"]]
    )
    def test_anything_else_is_unparseable(self, value):
        assert parse_atom_coordinates(value) is None


class TestTheClosedVocabularies:
    def test_the_container_read_never_carries_ee(self):
        assert "-ee" not in read_command("exiftool", Path("/t/a"))

    def test_the_telemetry_read_carries_ee_and_the_numeric_flag(self):
        command = telemetry_command("exiftool", Path("/t/a"))

        assert "-ee" in command and "-n" in command
        assert command[command.index("-charset") + 1] == "filename=utf8"
        assert [e for e in command if e in TELEMETRY_TAGS] == list(TELEMETRY_TAGS)

    def test_ee_is_in_exactly_one_vocabulary(self):
        assert "-ee" in TELEMETRY_OPTIONS
        assert "-ee" not in WRITE_OPTIONS and "-ee" not in VERSION_OPTIONS

    def test_the_write_invocation_carries_no_tag_and_no_ee(self):
        command = write_command("exiftool", Path("/t/w"))

        assert command == [
            "exiftool",
            "-@",
            "/t/w",
            "-common_args",
            "-charset",
            "filename=utf8",
        ]

    @pytest.mark.parametrize("option", ["-geotag", "-geosync", "-ee", "-api", "-r", "*.jpg"])
    def test_an_option_outside_an_operations_vocabulary_is_an_internal_failure(self, option):
        with pytest.raises(InternalConsistencyError, match="outside its declared options"):
            _assert_vocabulary("write", ["exiftool", option, "-@", "/t"], WRITE_OPTIONS, ())

    def test_the_chunk_size_is_the_byte_budget_over_the_measured_maximum_record(self):
        assert TELEMETRY_CHUNK_SIZE == 256_000_000 // 8_894_679 == 28


class TestTheTelemetryReadChunking:
    def paths(self, n):
        return [f"/m/{i:02d}.MP4" for i in range(n)]

    def test_chunks_are_sized_by_the_chunk_size_and_cover_the_paths_in_order(self, tmp_path):
        paths = self.paths(7)
        transport = FakeTransport(
            telemetry=lambda p: telemetry_json(p, lat=[1], lon=[1], times=[0])
        )

        chunks = list(read_telemetry(transport, paths, tmp_path, chunk_size=3))

        assert [len(c) for c in chunks] == [3, 3, 1]
        assert [p for call in transport.telemetry_calls for p in call] == paths
        assert max(len(call) for call in transport.telemetry_calls) == 3

    @staticmethod
    def reachable_records():
        gc.collect()
        return sum(1 for o in gc.get_objects() if type(o) is TelemetryRecord)

    def observe(self, tmp_path, consume, reader=read_telemetry):
        """Return the records reachable at each observation point of ``consume``."""
        transport = FakeTransport(
            telemetry=lambda p: telemetry_json(p, lat=[1], lon=[1], times=[0])
        )
        baseline = self.reachable_records()
        readings: list[int] = []
        consume(
            reader(transport, self.paths(7), tmp_path, chunk_size=3),
            lambda: readings.append(self.reachable_records() - baseline),
        )
        return readings, self.reachable_records() - baseline

    @staticmethod
    def dropping(chunks, observe):
        for chunk in chunks:
            observe()
            del chunk
            observe()

    @staticmethod
    def accumulating(chunks, observe):
        kept = []
        for chunk in chunks:
            kept.append(chunk)
            observe()
        return kept

    def test_the_reader_holds_at_most_one_chunk_and_none_between_steps(self, tmp_path):
        readings, after = self.observe(tmp_path, self.dropping)

        held, between = readings[0::2], readings[1::2]
        assert held == [3, 3, 1]
        assert between == [0, 0, 0]
        assert after == 0

    def test_the_retention_measure_fails_an_accumulating_consumer(self, tmp_path):
        readings, _ = self.observe(tmp_path, self.accumulating)

        assert readings == [3, 6, 7]

    def test_a_reader_that_accumulates_what_it_yields_is_caught_by_the_measure(self, tmp_path):
        def accumulating_reader(*args, **kwargs):
            seen = []
            for chunk in read_telemetry(*args, **kwargs):
                seen.append(chunk)
                yield chunk

        readings, _ = self.observe(tmp_path, self.dropping, reader=accumulating_reader)

        assert readings[1::2] == [3, 6, 7]

    def test_no_paths_means_no_process(self, tmp_path):
        transport = FakeTransport()

        assert list(read_telemetry(transport, [], tmp_path)) == []
        assert transport.telemetry_calls == []

    def test_a_chunk_that_does_not_return_what_it_was_asked_for_aborts(self, tmp_path):
        class Dropping(FakeTransport):
            def read_telemetry(self, argfile):
                result = super().read_telemetry(argfile)
                first = next(iter(result.records))
                return dataclasses.replace(
                    result, records={k: v for k, v in result.records.items() if k != first}
                )

        with pytest.raises(ToolFailure, match="did not return exactly the requested records"):
            list(read_telemetry(Dropping(), self.paths(2), tmp_path))

    def test_records_parse_into_the_telemetry_type_not_the_container_type(self, tmp_path):
        transport = FakeTransport(telemetry=lambda p: telemetry_json(p, lat=[5], times=[0]))

        [chunk] = read_telemetry(transport, ["/m/a.MP4"], tmp_path)

        assert isinstance(chunk["/m/a.MP4"], TelemetryRecord)
        assert not hasattr(chunk["/m/a.MP4"], "has_gps_key")


class TestTheViaTable:
    def test_every_via_member_has_a_row(self):
        assert set(VIA_SPECS) == set(Via)

    def test_only_interpolated_is_admitted_to_the_sensitivity_fold(self):
        assert {via for via, spec in VIA_SPECS.items() if spec.fold_admitted} == {Via.INTERPOLATED}

    def test_each_excluded_member_keeps_its_own_key(self):
        assert VIA_SPECS[Via.EXTENDED].exclusion_key == "extended"
        assert VIA_SPECS[Via.CENTROID].exclusion_key == "centroid"
        assert VIA_SPECS[Via.INTERPOLATED].exclusion_key is None

    CENTROID = Centroid(
        3,
        2,
        tuple((r, int(r is SampleRejection.ZERO_SENTINEL)) for r in SampleRejection),
        0.4,
        0.0167,
        AggregationRule.ARITHMETIC_MEAN,
        1.5,
        AltitudeOutcome.NONE_USABLE,
    )

    @pytest.mark.parametrize(
        ("via", "provenance", "speed"),
        [
            (Via.INTERPOLATED, Extended("s", 1.0, UnmatchableReason.OUTSIDE_COVERAGE), None),
            (Via.EXTENDED, Interpolated("s", 1.0), None),
            (Via.CENTROID, Interpolated("s", 1.0), None),
            (Via.INTERPOLATED, CENTROID, None),
            (Via.CENTROID, CENTROID, 0.5),
        ],
    )
    def test_a_via_with_the_wrong_provenance_or_a_speed_it_forbids_cannot_be_constructed(
        self, via, provenance, speed
    ):
        with pytest.raises(ValueError, match=r"provenance|bracket speed"):
            Planned("/a", at(0), Position(1, 2), via, provenance, speed)

    def test_a_centroid_outcome_is_constructible_with_no_speed(self):
        outcome = Planned("/a", at(0), Position(1, 2), Via.CENTROID, self.CENTROID)

        assert outcome.bracket_speed is None

    def test_the_centroid_provenance_serialises_every_field_with_its_drop_counts(self):
        data = provenance_dict(self.CENTROID)

        assert data == {
            "kind": "Centroid",
            "samples_read": 3,
            "samples_used": 2,
            "dropped": {"UNPARSEABLE": 0, "OUT_OF_RANGE": 0, "ZERO_SENTINEL": 1},
            "span_s": 0.4,
            "shortfall_s": 0.0167,
            "rule": "ARITHMETIC_MEAN",
            "spread_m": 1.5,
            "altitude": "NONE_USABLE",
        }


class _CannedTransport(ExiftoolTransport):
    """The real transport's parsing over a fixed stdout, with no process."""

    def __init__(self, stdout: bytes) -> None:
        super().__init__("exiftool")
        self._stdout = stdout

    def _run(self, command):
        return subprocess.CompletedProcess(command, 0, self._stdout, b"")


class TestAMalformedJsonPayloadIsAToolFailure:
    @pytest.mark.parametrize(
        "payload",
        [b'{"SourceFile": "/m/a.RAF"}', b"[1, 2]", b'[{"Make": "x"}]', b'"text"', b"42"],
        ids=["object", "numbers", "record-without-source", "string", "number"],
    )
    def test_a_payload_that_is_not_a_list_of_records_raises_tool_failure(self, payload):
        with pytest.raises(ToolFailure, match="not a list of records naming a SourceFile"):
            _CannedTransport(payload).read(Path("/a"))

    def test_a_list_of_records_is_keyed_by_source_file(self):
        result = _CannedTransport(b'[{"SourceFile": "/m/a.RAF", "Make": "x"}]').read(Path("/a"))

        assert result.records == {"/m/a.RAF": {"SourceFile": "/m/a.RAF", "Make": "x"}}

    def test_empty_output_is_no_records(self):
        assert _CannedTransport(b"  ").read(Path("/a")).records == {}
