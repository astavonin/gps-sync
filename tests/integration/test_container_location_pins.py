"""Pinning tests for the video container location work, against the real exiftool binary.

Each pins a defect that survived design review and was found only by measuring: a sign lost in
the ISO 6709 value, an unreadable atom read as no atom, a non-finite derived elevation, a refusal
wording the write attribution did not recognise, and a warning-only block reported as success.
"""

import hashlib
from pathlib import Path

import pytest

from gps_sync.argfile import emit_argfile
from gps_sync.classifier import TelemetryPath, decide_path, derive_outcome
from gps_sync.model import (
    Failed,
    FailedReason,
    FileContext,
    Geotagged,
    Interpolated,
    Planned,
    Position,
    Protected,
    ProtectedReason,
    Via,
)
from gps_sync.reader import read_records, read_telemetry
from gps_sync.transport import ExiftoolTransport
from gps_sync.writer import attribute, build_write_lines, run_write
from tests.integration.helpers import tags
from tests.support import at, batch, classify, clone, config, index_of, marked, segment

CLIP_INSTANT_S = -1066  # the clip's atom is 03:42:14Z, 17 min 46 s before the track epoch
STILL_INSTANT_S = 300


def planned(
    path: Path, container: str, lat: float = 42.5, lon: float = 78.25, ele: float | None = 1500.5
) -> Planned:
    return Planned(
        str(path),
        at(STILL_INSTANT_S),
        Position(lat, lon, ele),
        Via.INTERPOLATED,
        Interpolated("t.gpx#trk0seg0", 10.0),
        0.5,
        FileContext("X", "Y", container),
    )


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


def coordinates(path: Path) -> list[float]:
    raw = tags(path, "-Keys:GPSCoordinates")["GPSCoordinates"]
    return [float(part) for part in str(raw).split()]


class TestSignedIso6709Emission:
    """``Keys:GPSCoordinates`` stores signs verbatim, so ``abs()`` would mirror a hemisphere."""

    @pytest.mark.parametrize(
        ("lat", "lon", "ele"),
        [
            (33.868812345, 151.209387654, 25.5),
            (33.868812345, -151.209387654, 25.5),
            (-33.868812345, 151.209387654, 25.5),
            (-33.868812345, -151.209387654, -1234.5678),
        ],
        ids=["north-east", "north-west", "south-east", "south-west-below-sea-level"],
    )
    def test_each_quadrant_round_trips_its_signs(
        self, tmp_path, assets, real_transport, lat, lon, ele
    ):
        clip = clone(assets.mp4, tmp_path / "clip.MP4")

        result = run_write(
            real_transport, [planned(clip, "MP4", lat, lon, ele)], tmp_path, keep_original=False
        )

        assert isinstance(result.outcomes[0], Geotagged)
        stored = coordinates(clip)
        assert stored == pytest.approx([lat, lon, ele], abs=1e-9)
        position = tags(clip, "-Composite:GPSPosition")["GPSPosition"]
        assert [float(v) for v in str(position).split()] == pytest.approx([lat, lon], abs=1e-9)

    def test_a_clip_receives_the_atom_and_no_xmp_position(self, tmp_path, assets, real_transport):
        clip = clone(assets.mp4, tmp_path / "clip.MP4")

        run_write(real_transport, [planned(clip, "MP4")], tmp_path, keep_original=False)

        names = set(tags(clip, "-XMP-exif:all", "-XMP-x:all")) - {"SourceFile"}
        assert names == set()

    def test_a_still_keeps_its_magnitude_and_reference_encoding(
        self, tmp_path, assets, real_transport
    ):
        raf = clone(assets.raf, tmp_path / "a.RAF")

        run_write(
            real_transport,
            [planned(raf, "RAF", -33.5, -151.25, -12.0)],
            tmp_path,
            keep_original=False,
        )

        values = tags(
            raf,
            "-GPS:GPSLatitude",
            "-GPS:GPSLatitudeRef",
            "-GPS:GPSLongitude",
            "-GPS:GPSAltitudeRef",
        )
        assert values["GPSLatitude"] == pytest.approx(33.5)
        assert values["GPSLatitudeRef"] == "S"
        assert values["GPSLongitude"] == pytest.approx(151.25)
        assert values["GPSAltitudeRef"] == 1


class TestUnparseableAtomThroughTheGate:
    """An atom this backend cannot parse emits no ``Composite`` key and must still be hands-off."""

    @staticmethod
    def _classify(path: Path, transport: ExiftoolTransport, tmp_path: Path):
        index = index_of(
            segment("clip#0", [(-1070, 42.0, 77.0, 1000.0), (-1060, 42.01, 77.0, 1010.0)]),
            segment("still#0", [(290, 42.0, 77.0, 1000.0), (310, 42.002, 77.0, 1002.0)]),
        )
        record = read_records(transport, [str(path)], tmp_path)[str(path)]
        verdict = decide_path(record, config())
        if not isinstance(verdict, TelemetryPath):
            return classify(record, index, config())
        [chunk] = read_telemetry(transport, [str(path)], tmp_path)
        return derive_outcome(record, verdict, chunk[str(path)], index, config())

    def test_an_unreadable_atom_is_indeterminate_never_planned(
        self, tmp_path, assets, real_transport
    ):
        outcome = self._classify(assets.mp4_atom_unreadable, real_transport, tmp_path)

        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.METADATA_INDETERMINATE
        assert "+42,0721+077,1292+2189,900/" in (outcome.detail or "")

    def test_the_regate_leaves_an_unreadable_atom_byte_identical(
        self, tmp_path, assets, real_transport
    ):
        clip = clone(assets.mp4_atom_unreadable, tmp_path / "clip.MP4")
        before = digest(clip)

        result = run_write(real_transport, [planned(clip, "MP4")], tmp_path, keep_original=False)

        assert digest(clip) == before
        assert isinstance(result.outcomes[0], Protected)
        assert result.outcomes[0].reason is ProtectedReason.POSITION_APPEARED

    def test_a_readable_atom_is_protected_with_its_position(self, tmp_path, assets, real_transport):
        outcome = self._classify(assets.mp4_atom, real_transport, tmp_path)

        assert isinstance(outcome, Protected)
        assert outcome.reason is ProtectedReason.POSITION_PRESENT
        assert outcome.position == Position(42.0721, 77.1292, 2189.9)

    def test_the_container_gate_alone_protects_a_clip_with_an_atom_and_no_meta_track(
        self, tmp_path, assets, real_transport
    ):
        clip = assets.mp4_atom_without_meta_track
        record = read_records(real_transport, [str(clip)], tmp_path)[str(clip)]

        verdict = decide_path(record, config())

        assert "meta" not in record.handler_types
        assert record.atom_instances == ("42.0721 77.1292 2189.9",)
        assert isinstance(verdict, Protected)
        assert verdict.reason is ProtectedReason.POSITION_PRESENT
        assert verdict.position == Position(42.0721, 77.1292, 2189.9)

    def test_the_container_gate_alone_refuses_an_unreadable_atom_on_a_clip_with_no_meta_track(
        self, tmp_path, assets, real_transport
    ):
        # Composite emits no position for an atom this backend cannot parse, so with no meta track
        # to send the clip to the telemetry read, only the container read's atom question stands
        # between this clip and a GPX plan.
        clip = assets.mp4_unreadable_atom_without_meta_track
        record = read_records(real_transport, [str(clip)], tmp_path)[str(clip)]

        verdict = decide_path(record, config())

        assert not record.has_gps_key and "meta" not in record.handler_types
        assert isinstance(verdict, Failed)
        assert verdict.reason is FailedReason.METADATA_INDETERMINATE
        assert "+42,0721+077,1292+2189,900/" in (verdict.detail or "")

    def test_the_write_regate_leaves_such_a_clip_byte_identical(
        self, tmp_path, assets, real_transport
    ):
        clip = clone(assets.mp4_atom_without_meta_track, tmp_path / "clip.MP4")
        before = digest(clip)

        result = run_write(real_transport, [planned(clip, "MP4")], tmp_path, keep_original=False)

        assert digest(clip) == before
        assert isinstance(result.outcomes[0], Protected)

    def test_a_positionless_clip_is_still_planned_from_its_own_telemetry(
        self, tmp_path, assets, real_transport
    ):
        outcome = self._classify(assets.mp4, real_transport, tmp_path)

        assert isinstance(outcome, Planned)
        assert outcome.via is Via.CENTROID
        assert outcome.position.lat == pytest.approx(42.335033, abs=1e-6)
        assert outcome.position.lon == pytest.approx(78.226356, abs=1e-6)

    def test_a_positionless_clip_is_written_by_the_extended_regate(
        self, tmp_path, assets, real_transport
    ):
        clip = clone(assets.mp4, tmp_path / "clip.MP4")

        result = run_write(real_transport, [planned(clip, "MP4")], tmp_path, keep_original=False)

        assert isinstance(result.outcomes[0], Geotagged)
        assert coordinates(clip) == pytest.approx([42.5, 78.25, 1500.5])

    def test_a_positionless_still_is_planned_and_written(self, tmp_path, assets, real_transport):
        raf = clone(assets.raf, tmp_path / "a.RAF")

        outcome = self._classify(raf, real_transport, tmp_path)
        result = run_write(real_transport, [planned(raf, "RAF")], tmp_path, keep_original=False)

        assert isinstance(outcome, Planned)
        assert isinstance(result.outcomes[0], Geotagged)


class TestNonFiniteDerivedElevation:
    """A derived elevation the backend cannot store is dropped before the write (real binary)."""

    @pytest.mark.parametrize("midpoint_s", [295, 300, 305], ids=["early", "middle", "late"])
    def test_an_unrepresentable_derived_elevation_is_dropped_before_the_write(
        self, tmp_path, assets, real_transport, midpoint_s
    ):
        raf = clone(assets.raf, tmp_path / "a.RAF")
        index = index_of(
            segment(
                "t.gpx#trk0seg0",
                [(midpoint_s - 5, 42.0, 77.0, -1.7e308), (midpoint_s + 5, 42.002, 77.0, 1.7e308)],
            )
        )
        record = read_records(real_transport, [str(raf)], tmp_path)[str(raf)]

        outcome = classify(record, index, config())

        assert isinstance(outcome, Planned)
        assert outcome.position.ele is None
        result = run_write(real_transport, [outcome], tmp_path, keep_original=False)
        assert isinstance(result.outcomes[0], Geotagged)
        argfile = (tmp_path / "write.args").read_text().lower()
        assert "nan" not in argfile
        assert "inf" not in argfile
        stored = tags(raf, "-GPS:GPSLatitude", "-GPS:GPSLongitude", "-GPS:GPSAltitude")
        assert "GPSLatitude" in stored
        assert "GPSAltitude" not in stored


STILL_MATRIX = [
    pytest.param(
        "-GPSLatitude=",
        "-GPSLatitude=notanumber",
        "WRITE_PARTIAL",
        'Warning: Argument "notanumber" isn\'t numeric in abs in GPS:GPSLatitude (ValueConvInv)',
        id="magnitude-with-suffix",
    ),
    pytest.param(
        "-GPSLatitude=",
        "-GPSLatitude=abc:def",
        "WRITE_PARTIAL",
        'Warning: Argument "abc:def" isn\'t numeric in abs in GPS:GPSLatitude (ValueConvInv)',
        id="magnitude-quoting-a-colon-bearing-value",
    ),
    pytest.param(
        "-GPSLatitude=",
        "-GPSLatitude=inf",
        "WRITE_PARTIAL",
        "Warning: Not a floating point number for GPS:GPSLatitude",
        id="magnitude-no-suffix",
    ),
    pytest.param(
        "-GPSLatitudeRef=",
        "-GPSLatitudeRef=XX",
        "WRITE_PARTIAL",
        "Warning: String too long for GPS:GPSLatitudeRef",
        id="hemisphere-reference-no-suffix",
    ),
    pytest.param(
        "-GPSTimeStamp=",
        "-GPSTimeStamp=abc:def:ghi",
        "WRITE_INCOMPLETE",
        "Warning: Not a floating point number for GPS:GPSTimeStamp",
        id="time-stamp-no-suffix",
    ),
    pytest.param(
        "-GPSAltitude=",
        "-GPSAltitude=notanumber",
        "WRITE_INCOMPLETE",
        "Warning: Error converting value for GPS:GPSAltitude (ValueConvInv)",
        id="altitude-with-suffix",
    ),
]


def write_with_injected_line(
    transport: ExiftoolTransport, target: Planned, tmp_path: Path, prefix: str, replacement: str
):
    """Run the production block with one assignment swapped for a value no formatter can emit."""
    lines = build_write_lines([target], keep_original=False)
    matching = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    assert len(matching) == 1, f"the block carries no single {prefix!r} assignment: {lines}"
    lines[matching[0]] = replacement
    argfile = tmp_path / "injected.args"
    emit_argfile(argfile, lines)
    return attribute([target], transport.write(argfile))


class TestRefusalWordingMatrix:
    """Six measured wordings across five tags, three with no parenthesised suffix at all."""

    @pytest.mark.parametrize(("prefix", "replacement", "expected", "wording"), STILL_MATRIX)
    def test_each_still_refusal_concludes_its_own_reason_quoting_the_measured_wording_verbatim(
        self, tmp_path, assets, real_transport, prefix, replacement, expected, wording
    ):
        raf = clone(assets.raf, tmp_path / "a.RAF")

        result = write_with_injected_line(
            real_transport, planned(raf, "RAF"), tmp_path, prefix, replacement
        )

        outcome = result.outcomes[0]
        assert isinstance(outcome, Failed)
        assert outcome.reason.value == expected
        assert outcome.detail == wording

    def test_a_refused_magnitude_leaves_the_longitude_and_no_latitude_on_disk(
        self, tmp_path, assets, real_transport
    ):
        raf = clone(assets.raf, tmp_path / "a.RAF")

        write_with_injected_line(
            real_transport, planned(raf, "RAF"), tmp_path, "-GPSLatitude=", "-GPSLatitude=inf"
        )

        stored = tags(raf, "-GPS:GPSLatitude", "-GPS:GPSLongitude", "-Composite:GPSPosition")
        assert "GPSLatitude" not in stored
        assert stored["GPSLongitude"] == pytest.approx(78.25)
        assert "GPSPosition" not in stored

    def test_a_refused_quicktime_value_abandons_the_block_and_leaves_the_clip_unchanged(
        self, tmp_path, assets, real_transport
    ):
        clip = clone(assets.mp4, tmp_path / "clip.MP4")
        before = digest(clip)

        result = write_with_injected_line(
            real_transport,
            planned(clip, "MP4"),
            tmp_path,
            "-Keys:GPSCoordinates=",
            "-Keys:GPSCoordinates=42.0721 777.1292 25.0",
        )

        outcome = result.outcomes[0]
        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.WRITE_ERROR
        assert outcome.detail == (
            "Warning: Longitude out of range in Keys:GPSCoordinates (ValueConvInv)"
        )
        assert digest(clip) == before


class TestErroredAndWarningOnlyBlocks:
    """A block with no ``Error:`` line must not be read as clean."""

    def test_a_warning_only_refusal_beside_an_applied_message_is_not_a_success(
        self, tmp_path, assets, real_transport
    ):
        raf = clone(assets.raf, tmp_path / "a.RAF")

        result = write_with_injected_line(
            real_transport,
            planned(raf, "RAF", -33.5, 151.25),
            tmp_path,
            "-GPSLatitudeRef=",
            "-GPSLatitudeRef=XX",
        )

        outcome = result.outcomes[0]
        assert not isinstance(outcome, Geotagged)
        assert isinstance(outcome, Failed)
        assert outcome.reason.value == "WRITE_PARTIAL"
        assert "String too long for GPS:GPSLatitudeRef" in (outcome.detail or "")
        assert tags(raf, "-Composite:GPSPosition")["GPSPosition"].split()[0] == "33.5"

    def test_an_errored_signal_with_no_error_line_is_unattributed(self, tmp_path):
        target = planned(tmp_path / "a.RAF", "RAF")
        path = target.path

        result = attribute(
            [target],
            batch([marked(path, "1 files weren't updated due to errors")], [marked(path)], 1),
        )

        outcome = result.outcomes[0]
        assert isinstance(outcome, Failed)
        assert outcome.reason is FailedReason.WRITE_UNATTRIBUTED
