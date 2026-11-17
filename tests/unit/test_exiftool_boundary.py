import ast
import dataclasses
import re
from pathlib import Path

import pytest

import gps_sync
from gps_sync.argfile import emit_argfile, path_line
from gps_sync.errors import InternalConsistencyError, ToolFailure
from gps_sync.model import Failed, FailedReason, Position
from gps_sync.reader import read_records
from gps_sync.records import TagRecord, parse_gps_position
from gps_sync.transport import (
    EXIFTOOL_ENV,
    MINIMUM_VERSION,
    READ_TAGS,
    TELEMETRY_TAGS,
    ReadResult,
    check_version,
    locate_exiftool,
    parse_version,
    read_command,
    write_command,
)
from gps_sync.writer import build_write_lines
from tests.support import FakeTransport, classify, config, index_of, raf_json, segment

SRC = Path(gps_sync.__file__).parent
# Inside the track at the record's instant, so only the position gate can keep a file from Planned.
TRACK = index_of(segment("t.gpx#trk0seg0", [(0, 42.0, 77.0, 100.0), (60, 42.001, 77.0, 100.0)]))
CLEAN = {":DateTimeOriginal": "2026:10:26 10:00:30", ":OffsetTimeOriginal": "+06:00"}
# Exiftool's own geotagging machinery: -geotag, -geosync and the Geo* API options. The lookarounds
# keep gps-sync's own "Geotagged" outcome name from matching.
GEO_OPTIONS = re.compile(r"(?i)(?<![a-z])(geotag|geosync|geo(max|min)\w*|geolocation\w*)(?![a-z])")
TRACK_FILES = re.compile(r"(?i)\.gpx\b|\.nmea\b")
FORBIDDEN = re.compile(f"(?i)(?:{GEO_OPTIONS.pattern[4:]})|(?:{TRACK_FILES.pattern[4:]})")


class TestVersion:
    @pytest.mark.parametrize("text", ["13.55", "13.55\n", "14.0", "13.100", "20"])
    def test_versions_at_or_above_the_minimum_are_accepted(self, text):
        assert check_version(text) >= MINIMUM_VERSION

    @pytest.mark.parametrize("text", ["13.54", "12.99", "9.0"])
    def test_a_below_minimum_version_is_rejected_with_an_actionable_message(self, text):
        with pytest.raises(ToolFailure, match=r"13\.55.*upgrade"):
            check_version(text)

    @pytest.mark.parametrize("text", ["", "abc", "13.55-beta", "13..5"])
    def test_an_unparseable_version_is_rejected(self, text):
        with pytest.raises(ToolFailure):
            parse_version(text)

    def test_minor_versions_compare_numerically_not_lexically(self):
        assert parse_version("13.100") > parse_version("13.55")


class TestLocate:
    def test_the_environment_variable_wins(self):
        assert locate_exiftool({EXIFTOOL_ENV: "/custom/exiftool"}) == "/custom/exiftool"

    def test_otherwise_path_then_the_documented_default(self, tmp_path):
        stub = tmp_path / "exiftool"
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)

        assert locate_exiftool({"PATH": str(tmp_path)}) == str(stub)
        assert locate_exiftool({"PATH": ""}) == "/opt/homebrew/bin/exiftool"


class TestInvocations:
    def test_the_read_invocation_is_a_batched_json_numeric_read_with_diagnostics_requested(self):
        command = read_command("exiftool", Path("/tmp/r.args"))

        assert command[0] == "exiftool"
        assert {"-j", "-n", "-a", "-G4"} <= set(command)
        assert {"-Warning", "-Error", "-Composite:GPSPosition", "-FileType"} <= set(command)
        assert command[-2:] == ["-@", "/tmp/r.args"]

    def test_every_requested_tag_has_a_declared_consumer(self):
        consumers = {
            "-Composite:GPSPosition": "gate",
            "-DateTimeOriginal": "time",
            "-OffsetTimeOriginal": "time",
            "-CreateDate": "time",
            "-FileType": "write gate",
            "-Make": "context",
            "-Model": "context",
            "-Warning": "gate",
            "-Error": "gate",
            "-CreationDate": "time",
            "-MajorBrand": "time",
            "-Duration": "telemetry coverage check",
            "-HandlerType": "path verdict",
            "-Keys:GPSCoordinates": "gate",
            "-UserData:GPSCoordinates": "gate",
            "-ItemList:GPSCoordinates": "gate",
        }

        assert set(READ_TAGS) == set(consumers)

    def test_every_telemetry_tag_has_a_declared_consumer_listed_independently_of_the_tuple(self):
        consumers = {
            "-GPSLatitude": "centroid aggregation",
            "-GPSLongitude": "centroid aggregation",
            "-AbsoluteAltitude": "centroid elevation",
            "-SampleTime": "coverage check",
            "-Keys:GPSCoordinates": "atom question",
            "-UserData:GPSCoordinates": "atom question",
            "-ItemList:GPSCoordinates": "atom question",
        }

        assert set(TELEMETRY_TAGS) == set(consumers)

    def test_no_invocation_carries_a_prohibited_capability(self, tmp_path):
        from tests.unit.test_writer import planned

        commands = [
            read_command("exiftool", tmp_path / "r"),
            write_command("exiftool", tmp_path / "w"),
        ]
        argfile = build_write_lines(
            [planned("/m/a.RAF"), planned("/m/b.RAF", ele=None)], keep_original=True
        )
        argfile += build_write_lines([planned("/m/c.RAF")], keep_original=False)

        for token in [*(t for c in commands for t in c), *argfile]:
            assert FORBIDDEN.search(token) is None, token

    def test_no_string_constant_in_the_shipped_source_names_a_prohibited_capability(self):
        offenders = []
        for path in SRC.glob("*.py"):
            tree = ast.parse(path.read_text())
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and (
                        GEO_OPTIONS.search(node.value)
                        # Only gpx.py, our own parser, may name the GPX extension.
                        or (path.name != "gpx.py" and TRACK_FILES.search(node.value))
                    )
                ):
                    offenders.append((path.name, node.value))

        assert offenders == []

    def test_no_test_builds_or_rewrites_a_tag_record_around_the_parser(self):
        # Records must come from TagRecord.from_json (via tag_record), so no test can hold a
        # combination of derived fields the parser cannot produce.
        record_fields = {field.name for field in dataclasses.fields(TagRecord)}
        offenders = []
        for path in sorted((SRC.parent.parent / "tests").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                direct = name == "TagRecord"
                rewrite = name.endswith("replace") and any(
                    keyword.arg in record_fields for keyword in node.keywords
                )
                if direct or rewrite:
                    offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node)[:60]}")

        assert offenders == []

    def test_shipped_code_never_passes_a_gpx_path_to_exiftool(self):
        sources = "\n".join(p.read_text() for p in SRC.glob("*.py") if p.name != "gpx.py")

        assert "-geotag" not in sources and "-geosync" not in sources


class TestArgfileSafety:
    def test_a_relative_path_is_refused(self):
        with pytest.raises(InternalConsistencyError):
            path_line("relative.RAF")

    def test_a_dash_named_file_is_safe_only_because_it_is_absolute(self):
        assert path_line("/m/-fast2.RAF") == "/m/-fast2.RAF"
        with pytest.raises(InternalConsistencyError):
            path_line("-fast2.RAF")

    def test_every_read_argfile_line_begins_with_a_slash(self, tmp_path):
        transport = FakeTransport()
        paths = ["/m/-fast2.RAF", "/m/sp dir/a b.RAF", "/m/dé.RAF"]

        read_records(transport, paths, tmp_path)

        assert transport.read_calls == [paths]
        assert all(line.startswith("/") for line in transport.read_calls[0])

    def test_emit_argfile_round_trips_unicode_and_spaces(self, tmp_path):
        target = tmp_path / "x.args"

        emit_argfile(target, ["/m/dé ü 日本/a b.RAF"])

        assert target.read_text() == "/m/dé ü 日本/a b.RAF\n"


class TestBatchedRead:
    def test_one_batch_serves_everything_at_or_below_the_chunk_size(self, tmp_path):
        transport = FakeTransport()
        paths = [f"/m/{i}.RAF" for i in range(50)]

        records = read_records(transport, paths, tmp_path)

        assert len(transport.read_calls) == 1
        assert set(records) == set(paths)

    def test_chunking_is_bounded_above_the_envelope_and_does_not_grow_per_file(self, tmp_path):
        transport = FakeTransport()
        paths = [f"/m/{i}.RAF" for i in range(25)]

        read_records(transport, paths, tmp_path, chunk_size=10)

        assert [len(c) for c in transport.read_calls] == [10, 10, 5]

    def test_a_missing_record_is_fatal_and_never_read_as_no_position(self, tmp_path):
        transport = FakeTransport()
        transport.read = lambda argfile: ReadResult({}, "Error: File not found - /m/a.RAF")  # type: ignore[method-assign]

        with pytest.raises(ToolFailure, match="File not found"):
            read_records(transport, ["/m/a.RAF"], tmp_path)

    def test_records_carry_diagnostics_through_to_the_classifier(self, tmp_path):
        transport = FakeTransport(lambda p: {":Warning": "w1", ":Error": ["e1", "e2"]})

        records = read_records(transport, ["/m/a.RAF"], tmp_path)

        assert records["/m/a.RAF"].warnings == ("w1",)
        assert records["/m/a.RAF"].errors == ("e1", "e2")

    def test_instance_keyed_diagnostics_are_collected_whatever_their_copy_number(self, tmp_path):
        raw = {
            "SourceFile": "/m/a.DNG",
            "Copy10:Warning": "fourth",
            "Copy2:Warning": "third",
            ":Warning": "first",
            "Copy1:Warning": "second",
            "Copy1:Error": "broken",
            ":FileType": "DNG",
            "Copy1:Make": "DJI",
        }
        transport = FakeTransport(lambda p: raw)

        record = read_records(transport, ["/m/a.DNG"], tmp_path)["/m/a.DNG"]

        assert record.warnings == ("first", "second", "third", "fourth")
        assert record.errors == ("broken",)
        assert record.file_type == "DNG"
        assert record.make == "DJI"

    @pytest.mark.parametrize("primary_first", [True, False], ids=["primary-first", "copy-first"])
    @pytest.mark.parametrize(
        ("tag", "field", "primary", "copy"),
        [
            (
                "DateTimeOriginal",
                "date_time_original",
                "2026:10:26 10:05:00",
                "2026:10:26 10:09:00",
            ),
            ("OffsetTimeOriginal", "offset_time_original", "+06:00", "+00:00"),
            ("CreateDate", "create_date", "2026:10:26 09:46:31", "2026:10:26 09:46:31+06:00"),
            ("FileType", "file_type", "DNG", "XMP"),
            ("Make", "make", "DJI", "Other"),
            ("Model", "model", "FC3582", "Other"),
        ],
    )
    def test_the_primary_instance_of_a_scalar_tag_wins_whatever_the_key_order(
        self, tag, field, primary, copy, primary_first
    ):
        # exiftool emits keys in extraction order, which on TIFF-family containers puts the
        # non-preferred XMP copy ahead of the preferred one.
        instances = [(f":{tag}", primary), (f"Copy1:{tag}", copy)]
        if not primary_first:
            instances.reverse()

        record = TagRecord.from_json({"SourceFile": "/m/a.DNG", **dict(instances)})

        assert getattr(record, field) == primary

    def test_the_lowest_copy_number_is_the_primary_instance_beyond_a_single_digit(self):
        # Copy2 sorts after Copy10 as text but before it as a number; with no Copy1 beside them
        # only a numeric rank selects "two".
        raw = {"SourceFile": "/m/a.DNG", "Copy10:Make": "ten", "Copy2:Make": "two"}

        assert TagRecord.from_json(raw).make == "two"

    def test_the_primary_instance_outranks_every_copy_in_any_key_order(self):
        copies = [("Copy10:Make", "ten"), ("Copy2:Make", "two"), ("Copy1:Make", "one")]

        for position in range(len(copies) + 1):
            instances = [*copies[:position], (":Make", "primary"), *copies[position:]]

            record = TagRecord.from_json({"SourceFile": "/m/a.DNG", **dict(instances)})

            assert record.make == "primary", instances

    def test_a_blank_primary_instance_is_not_replaced_by_a_copys_value(self):
        raw = {
            "SourceFile": "/m/a.DNG",
            ":DateTimeOriginal": "  ",
            "Copy1:DateTimeOriginal": "2026:10:26 10:09:00",
        }

        record = TagRecord.from_json(raw)

        assert record.date_time_original == ""
        outcome = classify(record, TRACK, config())
        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.TIME_UNRESOLVABLE

    def test_a_position_in_any_copy_counts_and_an_unparseable_copy_does_not_hide_it(self):
        raw = {
            "SourceFile": "/m/a.DNG",
            ":GPSPosition": "garbage",
            "Copy1:GPSPosition": "42.5 78.25",
        }

        record = TagRecord.from_json(raw)

        assert record.has_gps_key
        assert parse_gps_position(record.gps_position) == Position(42.5, 78.25)

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "42.5", "1 2 3", "not coordinates", "nan nan", ["42.5 78.25"]],
        ids=["empty", "blank", "one-number", "three-numbers", "text", "nan", "list"],
    )
    def test_a_present_but_unparseable_position_is_marked_present_and_never_planned(self, value):
        record = TagRecord.from_json({"SourceFile": "/m/a.RAF", ":GPSPosition": value, **CLEAN})

        outcome = classify(record, TRACK, config())

        assert record.has_gps_key is True
        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE

    def test_unparseable_positions_in_every_instance_still_mark_the_tag_present(self):
        raw = {
            "SourceFile": "/m/a.RAF",
            ":GPSPosition": "garbage",
            "Copy1:GPSPosition": "more garbage",
            **CLEAN,
        }

        record = TagRecord.from_json(raw)

        assert record.has_gps_key is True
        outcome = classify(record, TRACK, config())
        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE

    def test_only_an_instance_prefixed_gps_key_marks_the_tag_present(self):
        absent = TagRecord.from_json({"SourceFile": "/m/a.DNG", ":Warning": "w"})
        present = TagRecord.from_json({"SourceFile": "/m/a.DNG", "Copy2:GPSPosition": "1 2"})

        assert not absent.has_gps_key
        assert present.has_gps_key

    @pytest.mark.parametrize(
        "key",
        [
            "GPSPosition",
            "Composite:GPSPosition",
            "Copy:GPSPosition",
            "copy1:GPSPosition",
            "Copy1",
            "Copy1:Composite:GPSPosition",
            "Copy1:Doc1:GPSPosition",
            "Copy1:ExifTool:Warning",
            ":Make\n",
            "Copy0:GPSPosition",
        ],
        ids=[
            "bare",
            "named-group",
            "copy-without-number",
            "lowercase-copy",
            "no-colon-copy",
            "group-behind-copy",
            "document-behind-copy",
            "exiftool-group-behind-copy",
            "trailing-newline",
            "copy-zero",
        ],
    )
    def test_a_key_outside_the_g4_shape_is_refused_not_read_as_another_tag(self, key):
        # A position under an unrecognised key must not turn into a distinct tag name and read
        # as "no position"; the run stops instead.
        with pytest.raises(ToolFailure, match="not the ':Tag' or 'CopyN:Tag' shape"):
            TagRecord.from_json({"SourceFile": "/m/a.RAF", key: "42.5 78.25"})

    def test_a_refused_key_names_the_file_the_version_and_a_remedy(self, tmp_path):
        transport = FakeTransport(lambda p: {"Composite:GPSPosition": "1 2"})

        with pytest.raises(ToolFailure) as caught:
            read_records(transport, ["/m/a.RAF"], tmp_path)

        message = str(caught.value)
        assert "/m/a.RAF" in message
        assert transport.version().strip() in message
        assert "Pin exiftool to 13.55" in message

    def test_source_file_is_the_only_unprefixed_key_accepted(self):
        record = TagRecord.from_json({"SourceFile": "/m/a.RAF", ":Make": "FUJIFILM"})

        assert record.path == "/m/a.RAF"
        assert record.make == "FUJIFILM"

    def test_the_synthetic_raf_builder_emits_only_keys_the_real_reader_produces(self):
        raw = raf_json(gps="1 2", **{"Copy1:Warning": "w"}, Error="e")

        assert all(re.fullmatch(r"(Copy\d+)?:\w+", key) for key in raw)
        assert TagRecord.from_json({"SourceFile": "/m/a.RAF", **raw}).has_gps_key


def test_an_unwritable_argfile_destination_is_a_tool_level_failure(tmp_path):
    with pytest.raises(ToolFailure, match="cannot write argfile"):
        emit_argfile(tmp_path / "missing-dir" / "x.args", ["/m/a.RAF"])
