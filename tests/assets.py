"""Real-container assets for integration and end-to-end tests.

Every asset is built once per session from a *copy* of a file under ``testdata/``; the
originals are never written. ``testdata/`` is committed and pinned by ``.baseline.md5``; tests
that need a real container skip only when it has been removed locally or exiftool is absent.
"""

from __future__ import annotations

import json
import os
import stat
import struct
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support import EXIFTOOL, TESTDATA, clone, exiftool

SOURCE_RAF = TESTDATA / "media" / "day1" / "DSCF8326.RAF"
SOURCE_DNG = TESTDATA / "media" / "drone" / "DJI_20261026094631_0036_D.DNG"
SOURCE_MP4 = TESTDATA / "media" / "drone" / "DJI_20261026094214_0024_D.MP4"
# The ISO 6709 text written into the readable-atom fixture, and the decimal-comma form patched over
# it at identical length: the box sizes stay valid, so the backend sees an atom it cannot parse.
ATOM_ISO6709 = b"+42.0721+077.1292+2189.900/"
ATOM_ISO6709_UNREADABLE = b"+42,0721+077,1292+2189,900/"
# The DateTimeOriginal given to the base RAF: 10:05:00 local at +06:00, i.e. 04:05:00Z, which
# lies inside the synthetic track in tests/conftest.py.
RAF_LOCAL_TIME = "2026:10:26 10:05:00"


@dataclass(frozen=True)
class Assets:
    raf_original: Path
    raf: Path
    raf_gps: Path
    raf_xmp_only: Path
    raf_partial_block: Path
    raf_truncated: Path
    dng_original: Path
    dng_stripped: Path
    dng_positionless: Path
    dng_defective_gps: Path
    dng_xmp_competing_time: Path
    jpeg: Path
    jpeg_gps: Path
    mp4: Path
    mp4_atom: Path
    mp4_atom_unreadable: Path
    mp4_zoneless_rewrap: Path
    mp4_atom_without_meta_track: Path
    mp4_unreadable_atom_without_meta_track: Path
    mov_fujifilm: Path
    mov_fujifilm_uncovered: Path
    mp4_isom_without_position_or_telemetry: Path


_GPS_POINTER_TAG = 0x8825
_PAST_EOF = 0x7FFFFFF0
_IFD_ENTRY_SIZE = 12
_TIFF_LONG = 4
# The source DNG's DateTimeOriginal, and the covered instant (10:05:00 local, 04:05:00Z at +06:00)
# the byte-derived fixture is given instead so that its refusal, not the clock, is what keeps it
# from being written.
_SOURCE_DNG_TIME = b"2026:10:26 09:46:31"
_COVERED_DNG_TIME = RAF_LOCAL_TIME.encode()
# Later than RAF_LOCAL_TIME by several minutes, so the two instants resolve to different
# trackpoints.
XMP_COMPETING_TIME = "2026:10:26 10:09:00"


def _gps_pointer_value_offset(data: bytes) -> int:
    """Return the file offset of the IFD0 GPS-pointer entry's value field.

    Located from the TIFF header rather than hard-coded, so a different fixture layout fails the
    derivation loudly instead of editing the wrong bytes.
    """
    if data[:4] != b"II*\x00":
        raise AssertionError("the DNG fixture is not little-endian TIFF; the derivation assumes it")
    (ifd0,) = struct.unpack_from("<I", data, 4)
    (count,) = struct.unpack_from("<H", data, ifd0)
    for index in range(count):
        entry = ifd0 + 2 + index * _IFD_ENTRY_SIZE
        (tag,) = struct.unpack_from("<H", data, entry)
        if tag == _GPS_POINTER_TAG:
            field_type, value_count = struct.unpack_from("<HI", data, entry + 2)
            if (field_type, value_count) != (_TIFF_LONG, 1):
                raise AssertionError(
                    f"the DNG fixture's GPS pointer is type {field_type} x{value_count}, not one "
                    "LONG; overwriting its value field would not redirect the pointer"
                )
            return entry + 8
    raise AssertionError("the DNG fixture's IFD0 has no GPS pointer entry to corrupt")


def _defective_gps_dng(source: Path, target: Path) -> Path:
    """Copy the DNG with its GPS IFD pointer past EOF and the vendor XMP position renamed.

    The result has a read warning ("Bad GPS directory") alongside the benign maker-notes one, and
    no position in any container: exactly the state in which a single surfaced warning would
    read as "provably absent". Its capture time is moved inside the synthetic track, so that only
    the gate, never the clock, can keep it from being written.
    """
    data = bytearray(source.read_bytes())
    struct.pack_into("<I", data, _gps_pointer_value_offset(bytes(data)), _PAST_EOF)
    payload = bytes(data)
    for old, new in (
        (b"drone-dji:Gps", b"drone-dji:Xps"),
        (_SOURCE_DNG_TIME, _COVERED_DNG_TIME),
    ):
        if payload.count(old) == 0:
            raise AssertionError(f"the DNG fixture does not contain {old!r} to replace")
        payload = payload.replace(old, new)
    target.write_bytes(payload)
    read = json.loads(
        exiftool(
            "-j",
            "-n",
            "-a",
            "-G4",
            "-Composite:GPSPosition",
            "-DateTimeOriginal",
            "-Warning",
            str(target),
        ).stdout
    )[0]
    warnings = " ".join(str(v) for k, v in read.items() if k.endswith("Warning"))
    if "Bad GPS directory" not in warnings:
        raise AssertionError(f"the defective DNG does not read as defective: {read}")
    if any("GPSPosition" in key for key in read):
        raise AssertionError(f"the defective DNG still carries a position: {read}")
    if read.get(":DateTimeOriginal") != RAF_LOCAL_TIME:
        raise AssertionError(f"the defective DNG's time was not moved: {read}")
    return target


def _positionless_clean_dng(source: Path, target: Path) -> Path:
    """Copy the DNG with its position and the two large profile arrays removed.

    Without the arrays exiftool no longer reports "Not decoding some large array(s)", so the file
    reads with only the allowlisted maker-notes warning: the one DNG state the gate lets through
    to a write, which a camera original from this drone never is.
    """
    clone(source, target)
    exiftool(
        "-overwrite_original",
        "-gps:all=",
        "-xmp:gps:all=",
        "-ProfileHueSatMapData1=",
        "-ProfileHueSatMapData2=",
        str(target),
    )
    read = json.loads(
        exiftool("-j", "-n", "-a", "-G4", "-Composite:GPSPosition", "-Warning", str(target)).stdout
    )[0]
    leftovers = {k: v for k, v in read.items() if k != "SourceFile"}
    if any("GPSPosition" in k for k in leftovers) or any(
        "large array" in str(v) for v in leftovers.values()
    ):
        raise AssertionError(f"the positionless DNG fixture was not derived cleanly: {leftovers}")
    return target


def _dng_with_competing_xmp_time(source: Path, target: Path) -> Path:
    """Copy the positionless DNG with an EXIF time and a later XMP time for the same tag.

    Under ``-a -G4`` the non-preferred XMP instance (``Copy1:``) comes first in the key order and
    the preferred EXIF one (``:``) second, which is the shape that exposes a wrong instance
    selection. The two times resolve to different trackpoints on the synthetic track.
    """
    _positionless_clean_dng(source, target)
    exiftool(
        "-overwrite_original",
        f"-DateTimeOriginal={RAF_LOCAL_TIME}",
        f"-XMP:DateTimeOriginal={XMP_COMPETING_TIME}",
        str(target),
    )
    keys = [
        key
        for key in json.loads(
            exiftool("-j", "-n", "-a", "-G4", "-DateTimeOriginal", str(target)).stdout
        )[0]
        if key.endswith("DateTimeOriginal")
    ]
    if keys != ["Copy1:DateTimeOriginal", ":DateTimeOriginal"]:
        raise AssertionError(f"the competing-time DNG does not have its shape: {keys}")
    return target


def gps_keys(path: Path) -> set[str]:
    """Return the ``-a -G4`` keys the position tags and their composite come back under."""
    read = json.loads(
        exiftool(
            "-j",
            "-n",
            "-a",
            "-G4",
            "-GPSLatitudeRef",
            "-GPSLatitude",
            "-GPSLongitude",
            "-Composite:GPSPosition",
            str(path),
        ).stdout
    )[0]
    return {key.rpartition(":")[2] for key in read if key != "SourceFile"}


def _partial_gps_raf(source: Path, target: Path) -> Path:
    """Copy the positionless RAF with only ``GPSLatitudeRef`` written: a GPS block, no position.

    That state must read as not positioned, so a correct reading creates a write target; a copy
    that silently lacks the block would let the test pass without exercising it.
    """
    clone(source, target)
    exiftool("-overwrite_original", "-GPSLatitudeRef=N", str(target))
    keys = gps_keys(target)
    if keys != {"GPSLatitudeRef"}:
        raise AssertionError(f"the partial-block RAF fixture was not derived cleanly: {keys}")
    return target


def _mp4_with_atom(source: Path, readable: Path, unreadable: Path) -> None:
    """Derive the readable ``Keys`` atom clip and its byte-patched unreadable twin.

    The decimal-comma patch keeps the payload length, so no box size changes and ``Composite`` is
    the only thing that notices: it emits no position key at all for the patched file.
    """
    clone(source, readable)
    exiftool("-overwrite_original", f"-Keys:GPSCoordinates={ATOM_ISO6709.decode()}", str(readable))
    payload = readable.read_bytes()
    if payload.count(ATOM_ISO6709) != 1:
        raise AssertionError("the atom payload is not stored verbatim exactly once")
    unreadable.write_bytes(payload.replace(ATOM_ISO6709, ATOM_ISO6709_UNREADABLE))
    read = json.loads(
        exiftool(
            "-j",
            "-n",
            "-a",
            "-G4",
            "-Composite:GPSPosition",
            "-Keys:GPSCoordinates",
            str(unreadable),
        ).stdout
    )[0]
    if any("GPSPosition" in key for key in read) or read.get(":GPSCoordinates") != (
        ATOM_ISO6709_UNREADABLE.decode()
    ):
        raise AssertionError(f"the unreadable-atom clip does not have its shape: {read}")


def _mp4_atom_without_meta_track(atom_clip: Path, target: Path) -> Path:
    """Derive a clip that carries a position atom and no ``meta`` handler track.

    The two ``meta`` handler types are patched to ``subt`` at their fixed offset in each ``hdlr``
    box at identical length, so no box size changes. The clip then takes the container path of the
    discriminator, which is the shape of every Fujifilm MOV and Apple re-wrap, and only the
    container read's atom question can protect it.
    """
    clone(atom_clip, target)
    data = bytearray(target.read_bytes())
    patched, at_index = 0, data.find(b"hdlr")
    while at_index != -1:
        if data[at_index + 12 : at_index + 16] == b"meta":
            data[at_index + 12 : at_index + 16] = b"subt"
            patched += 1
        at_index = data.find(b"hdlr", at_index + 4)
    if patched != 2:
        raise AssertionError(f"expected two meta handler tracks to patch, found {patched}")
    target.write_bytes(bytes(data))
    read = json.loads(exiftool("-j", "-a", "-G4", "-HandlerType", str(target)).stdout)[0]
    if "meta" in {v for k, v in read.items() if k != "SourceFile"}:
        raise AssertionError(f"the clip still has a meta track: {read}")
    return target


def _mp4_zoneless_rewrap(source: Path, target: Path) -> Path:
    """Derive a clip whose major brand is classic QuickTime and which states no time zone.

    The four-byte brand is patched at its fixed offset in the ``ftyp`` box at identical length, so
    no box size changes. That is the shape of an Apple re-wrap the time rule refuses: its creation
    time has no zone this project can verify.
    """
    clone(source, target)
    data = bytearray(target.read_bytes())
    if data[4:12] != b"ftypisom":
        raise AssertionError("the source clip does not open with an isom ftyp box")
    data[8:12] = b"qt  "
    target.write_bytes(bytes(data))
    brand = json.loads(exiftool("-j", "-MajorBrand", str(target)).stdout)[0]["MajorBrand"]
    if "QuickTime" not in brand:
        raise AssertionError(f"the re-wrap clip does not read as a QuickTime brand: {brand}")
    return target


# The capture instant the Fujifilm-shaped MOV states: local 10:05:00 at +06:00 is 04:05:00Z, inside
# the synthetic track. Its container atom trails that start by the clip's duration plus a
# finalisation interval, which is what the camera records there; both figures are the real files'
# shape (atom = start + duration + 23.95 to 25.00 s).
FUJIFILM_LOCAL_TIME = RAF_LOCAL_TIME
FUJIFILM_OFFSET = "+06:00"
FUJIFILM_FINALISATION = timedelta(seconds=25)
# Local 14:05:00 is 08:05:00Z, hours after the synthetic track ends: a clip the dispatch resolves
# correctly and the matcher then cannot place.
FUJIFILM_UNCOVERED_LOCAL_TIME = "2026:10:26 14:05:00"
_QT_EPOCH_TO_UNIX = 2_082_844_800
_MVTG_HEADER = bytes.fromhex("0000000000000001ef1f000001000000")
_TIFF_ASCII, _TIFF_LONG_TYPE = 2, 4
_BOX_HEADER = 8


def _box_at(data: bytes | bytearray, offset: int) -> tuple[int, bytes]:
    size, kind = struct.unpack_from(">I4s", data, offset)
    if size < _BOX_HEADER:
        raise AssertionError(f"the clip has a box of size {size} at {offset}; not a plain box")
    return size, bytes(kind)


def _children(data: bytes | bytearray, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """List ``(type, offset, size)`` for each box between ``start`` and ``end``."""
    found = []
    while start < end:
        size, kind = _box_at(data, start)
        found.append((kind, start, size))
        start += size
    return found


def _only(boxes: list[tuple[bytes, int, int]], kind: bytes) -> tuple[bytes, int, int]:
    matches = [box for box in boxes if box[0] == kind]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {kind!r} box, found {len(matches)}")
    return matches[0]


def _fujifilm_atom_utc(local_time: str) -> str:
    """The container creation time the camera records: the UTC capture plus its finalisation."""
    local = datetime.strptime(local_time, "%Y:%m:%d %H:%M:%S")
    offset = timedelta(hours=int(FUJIFILM_OFFSET[1:3]), minutes=int(FUJIFILM_OFFSET[4:6]))
    return (local - offset + FUJIFILM_FINALISATION).strftime("%Y:%m:%d %H:%M:%S")


def _fujifilm_mvtg(local_time: str) -> bytes:
    """Build the camera's ``MVTG`` atom: a little-endian TIFF directory holding the EXIF block.

    The real atom is a 16-byte header, an IFD0 with ``Make``, ``Model`` and an ``ExifOffset``
    pointer, and an ExifIFD that holds the camera's *local* capture time and the zone it was in.
    Offsets are relative to the start of IFD0, as exiftool reads them for this atom.
    """

    def ascii_value(text: str) -> bytes:
        return text.encode() + b"\x00"

    def directory(entries: list[tuple[int, int, bytes]], base: int) -> tuple[bytes, bytes]:
        """Return the directory bytes and its out-of-line data, offsets given from ``base``."""
        table, data = b"", b""
        data_start = base + 2 + _IFD_ENTRY_SIZE * len(entries) + 4
        for tag, field_type, value in entries:
            if field_type == _TIFF_ASCII:
                count = len(value)
                if count <= 4:
                    slot = value.ljust(4, b"\x00")
                else:
                    slot = struct.pack("<I", data_start + len(data))
                    data += value + (b"\x00" if len(value) % 2 else b"")
            else:
                count, slot = 1, value
            table += struct.pack("<HHI", tag, field_type, count) + slot
        return struct.pack("<H", len(entries)) + table + b"\x00\x00\x00\x00", data

    make, model = ascii_value("FUJIFILM"), ascii_value("X-T5")
    stamp = ascii_value(local_time)
    offset = ascii_value(FUJIFILM_OFFSET)
    exif_entries = [
        (0x9003, _TIFF_ASCII, stamp),
        (0x9004, _TIFF_ASCII, stamp),
        (0x9011, _TIFF_ASCII, offset),
        (0x9012, _TIFF_ASCII, offset),
    ]
    ifd0_count = 3
    ifd0_size = 2 + _IFD_ENTRY_SIZE * ifd0_count + 4
    ifd0_data = len(make) + len(model) + (len(make) % 2) + (len(model) % 2)
    exif_base = ifd0_size + ifd0_data
    exif_dir, exif_data = directory(exif_entries, exif_base)
    ifd0_dir, ifd0_out = directory(
        [
            (0x010F, _TIFF_ASCII, make),
            (0x0110, _TIFF_ASCII, model),
            (0x8769, _TIFF_LONG_TYPE, struct.pack("<I", exif_base)),
        ],
        0,
    )
    if len(ifd0_dir) + len(ifd0_out) != exif_base:
        raise AssertionError("the MVTG layout arithmetic is inconsistent")
    payload = _MVTG_HEADER + ifd0_dir + ifd0_out + exif_dir + exif_data
    return struct.pack(">I4s", _BOX_HEADER + len(payload), b"MVTG") + payload


def _fujifilm_udta_children(local_time: str) -> bytes:
    """The camera's ``udta`` children: format and information strings, stream name, and EXIF."""
    fmt = b"\x00\x0e\x00\x00Digital Camera"
    info = b"\x00\x38\x00\x00FUJIFILM DIGITAL CAMERA X-T5".ljust(60, b"\x00")
    stream = b"FUJIFILM MOV STREAM 0130\x00\x00\x15\x1e\x09\xd8"
    out = b""
    for kind, body in ((b"\xa9fmt", fmt), (b"\xa9inf", info), (b"FFMV", stream)):
        out += struct.pack(">I4s", _BOX_HEADER + len(body), kind) + body
    return out + _fujifilm_mvtg(local_time)


def _stamp_seconds(text: str) -> int:
    """Seconds since 1904-01-01 for an ``YYYY:MM:DD HH:MM:SS`` UTC text."""
    parsed = datetime.strptime(text, "%Y:%m:%d %H:%M:%S").replace(tzinfo=UTC)
    return int(parsed.timestamp()) + _QT_EPOCH_TO_UNIX


def _patch_creation_times(data: bytearray, moov: int, moov_end: int, text: str) -> int:
    """Set creation and modification time in ``mvhd`` and every ``tkhd`` and ``mdhd``."""
    seconds = struct.pack(">I", _stamp_seconds(text))
    patched = 0
    for kind, at, size in _children(data, moov + _BOX_HEADER, moov_end):
        spans = []
        if kind == b"mvhd":
            spans.append(at)
        elif kind == b"trak":
            for sub_kind, sub_at, sub_size in _children(data, at + _BOX_HEADER, at + size):
                if sub_kind == b"tkhd":
                    spans.append(sub_at)
                elif sub_kind == b"mdia":
                    spans += [
                        mdia_at
                        for mdia_kind, mdia_at, _ in _children(
                            data, sub_at + _BOX_HEADER, sub_at + sub_size
                        )
                        if mdia_kind == b"mdhd"
                    ]
        for span in spans:
            if data[span + _BOX_HEADER] != 0:
                raise AssertionError("a version-1 header: its 64-bit times are not patched here")
            body = span + _BOX_HEADER + 4
            data[body : body + 4] = seconds
            data[body + 4 : body + 8] = seconds
            patched += 1
    return patched


def _mov_fujifilm(source: Path, target: Path, local_time: str = FUJIFILM_LOCAL_TIME) -> Path:
    """Derive a clip with the shape of a Fujifilm X-T5 MOV, without the camera's 91.5 MB.

    Length-preserving byte surgery on a copy of the DJI MP4: the ``ftyp`` becomes classic
    QuickTime; ``udta`` is rewritten to the camera's own children, whose ``MVTG`` atom carries the
    EXIF block as a *local* ``DateTimeOriginal`` and ``CreateDate`` with ``OffsetTimeOriginal``; the
    DJI-specific top-level ``meta`` becomes ``free`` and the two telemetry handler tracks stop
    being ``meta``; and every container creation time is set to a UTC instant that trails the
    capture. Nothing is exiftool-written, because exiftool cannot write the ``MVTG`` atom.
    """
    clone(source, target)
    data = bytearray(target.read_bytes())
    if data[4:12] != b"ftypisom" or struct.unpack_from(">I", data, 0)[0] != 28:
        raise AssertionError("the source clip does not open with a 28-byte isom ftyp box")
    data[0:28] = struct.pack(">I4s4sI4s", 20, b"ftyp", b"qt  ", 0, b"qt  ") + struct.pack(
        ">I4s", _BOX_HEADER, b"free"
    )
    top = _children(data, 0, len(data))
    _, moov, moov_size = _only(top, b"moov")
    moov_end = moov + moov_size
    inside = _children(data, moov + _BOX_HEADER, moov_end)
    _, udta, udta_size = _only(inside, b"udta")
    _, meta, _ = _only(inside, b"meta")
    camera = _fujifilm_udta_children(local_time)
    atom_utc = _fujifilm_atom_utc(local_time)
    room = udta_size - _BOX_HEADER
    if len(camera) + _BOX_HEADER > room:
        raise AssertionError("the source udta is too small to hold the camera's atoms")
    filler = struct.pack(">I4s", room - len(camera), b"free")
    data[udta + _BOX_HEADER : udta + udta_size] = camera + filler.ljust(room - len(camera), b"\x00")
    data[meta + 4 : meta + 8] = b"free"
    patched_times = _patch_creation_times(data, moov, moov_end, atom_utc)
    if patched_times != 7:
        raise AssertionError(f"expected seven creation-time headers, patched {patched_times}")
    handlers = 0
    for kind, at, size in _children(data, moov + _BOX_HEADER, moov_end):
        if kind != b"trak":
            continue
        for sub_kind, sub_at, sub_size in _children(data, at + _BOX_HEADER, at + size):
            if sub_kind != b"mdia":
                continue
            for mdia_kind, mdia_at, _ in _children(data, sub_at + _BOX_HEADER, sub_at + sub_size):
                if mdia_kind == b"hdlr" and data[mdia_at + 16 : mdia_at + 20] == b"meta":
                    data[mdia_at + 16 : mdia_at + 20] = b"subt"
                    handlers += 1
    if handlers != 2:
        raise AssertionError(f"expected two meta handler tracks to patch, found {handlers}")
    target.write_bytes(bytes(data))
    _assert_fujifilm_shape(target, local_time)
    return target


def _assert_fujifilm_shape(path: Path, local_time: str) -> None:
    """Fail the derivation unless the clip reads with the Fujifilm MOV's time-evidence shape."""
    read = json.loads(
        exiftool(
            "-j",
            "-a",
            "-G1",
            "-DateTimeOriginal",
            "-OffsetTimeOriginal",
            "-CreateDate",
            "-MajorBrand",
            "-HandlerType",
            "-Make",
            "-Model",
            "-Warning",
            str(path),
        ).stdout
    )[0]
    wanted = {
        "ExifIFD:DateTimeOriginal": local_time,
        "ExifIFD:OffsetTimeOriginal": FUJIFILM_OFFSET,
        "ExifIFD:CreateDate": local_time,
        "QuickTime:CreateDate": _fujifilm_atom_utc(local_time),
    }
    wrong = {key: read.get(key) for key, value in wanted.items() if read.get(key) != value}
    handlers = {v for k, v in read.items() if "HandlerType" in k}
    if wrong or "QuickTime" not in str(read.get("QuickTime:MajorBrand")) or "meta" in handlers:
        raise AssertionError(f"the Fujifilm-shaped MOV does not have its shape: {read}")
    if read.get("ExifIFD:Make", read.get("IFD0:Make")) != "FUJIFILM" or any(
        "Warning" in key for key in read
    ):
        raise AssertionError(f"the Fujifilm-shaped MOV reads with a warning or wrong make: {read}")


def _mp4_isom_without_position_or_telemetry(source: Path, target: Path) -> Path:
    """Derive a generic-ISO-brand clip with no position and no ``meta`` handler track.

    It takes the GPX path with a lone container creation time and no EXIF or ``CreationDate``
    witness: the shape the atom-semantics table admits on the brand alone.
    """
    clone(source, target)
    data = bytearray(target.read_bytes())
    patched, at_index = 0, data.find(b"hdlr")
    while at_index != -1:
        if data[at_index + 12 : at_index + 16] == b"meta":
            data[at_index + 12 : at_index + 16] = b"subt"
            patched += 1
        at_index = data.find(b"hdlr", at_index + 4)
    if patched != 2:
        raise AssertionError(f"expected two meta handler tracks to patch, found {patched}")
    moov = _only(_children(data, 0, len(data)), b"moov")
    seconds = struct.pack(">I", _stamp_seconds(_fujifilm_atom_utc(FUJIFILM_LOCAL_TIME)))
    for kind, at, _ in _children(data, moov[1] + _BOX_HEADER, moov[1] + moov[2]):
        if kind == b"mvhd":
            data[at + 12 : at + 16] = seconds
            data[at + 16 : at + 20] = seconds
    target.write_bytes(bytes(data))
    return target


def require_environment() -> None:
    """Skip the calling test or fixture when exiftool or the corpus is not available."""
    if not Path(EXIFTOOL).exists():
        pytest.skip("exiftool is not installed")
    if not (SOURCE_RAF.exists() and SOURCE_DNG.exists() and SOURCE_MP4.exists()):
        pytest.skip("testdata/ is not present; real RAF, DNG and MP4 fixtures are unavailable")


def build_assets(base: Path) -> Assets:
    """Derive every real-container asset into ``base`` from copies of the corpus."""
    raf_original = clone(SOURCE_RAF, base / "raf_original.RAF")
    raf = clone(SOURCE_RAF, base / "raf.RAF")
    exiftool(
        "-overwrite_original",
        "-gps:all=",
        "-xmp:gps:all=",
        f"-DateTimeOriginal={RAF_LOCAL_TIME}",
        str(raf),
    )
    raf_gps = clone(raf, base / "raf_gps.RAF")
    exiftool(
        "-overwrite_original",
        "-n",
        "-GPSLatitude=42.5",
        "-GPSLatitudeRef=N",
        "-GPSLongitude=78.25",
        "-GPSLongitudeRef=E",
        str(raf_gps),
    )
    raf_xmp = clone(raf, base / "raf_xmp.RAF")
    exiftool(
        "-overwrite_original", "-XMP:GPSLatitude=42.5", "-XMP:GPSLongitude=78.25", str(raf_xmp)
    )
    raf_partial = _partial_gps_raf(raf, base / "raf_partial.RAF")
    raf_truncated = base / "raf_truncated.RAF"
    raf_truncated.write_bytes(raf.read_bytes()[:2_000_000])
    dng_original = clone(SOURCE_DNG, base / "dng_original.DNG")
    dng_stripped = clone(SOURCE_DNG, base / "dng_stripped.DNG")
    exiftool("-overwrite_original", "-gps:all=", "-xmp:gps:all=", str(dng_stripped))
    dng_positionless = _positionless_clean_dng(SOURCE_DNG, base / "dng_positionless.DNG")
    dng_defective_gps = _defective_gps_dng(SOURCE_DNG, base / "dng_defective_gps.DNG")
    dng_xmp_competing_time = _dng_with_competing_xmp_time(
        SOURCE_DNG, base / "dng_xmp_competing_time.DNG"
    )
    # The RAF's embedded preview is a real, warning-free JPEG; give it a time and no position.
    jpeg = base / "jpeg.JPG"
    jpeg.write_bytes(
        subprocess.run(
            [EXIFTOOL, "-b", "-PreviewImage", str(raf)], capture_output=True, check=True
        ).stdout
    )
    exiftool(
        "-overwrite_original",
        "-gps:all=",
        f"-DateTimeOriginal={RAF_LOCAL_TIME}",
        "-OffsetTimeOriginal=+06:00",
        str(jpeg),
    )
    jpeg_gps = clone(jpeg, base / "jpeg_gps.JPG")
    exiftool(
        "-overwrite_original",
        "-n",
        "-GPSLatitude=42.5",
        "-GPSLatitudeRef=N",
        "-GPSLongitude=78.25",
        "-GPSLongitudeRef=E",
        str(jpeg_gps),
    )
    mp4 = clone(SOURCE_MP4, base / "clip.MP4")
    mp4_atom = base / "clip_atom.MP4"
    mp4_atom_unreadable = base / "clip_atom_unreadable.MP4"
    _mp4_with_atom(SOURCE_MP4, mp4_atom, mp4_atom_unreadable)
    mp4_zoneless_rewrap = _mp4_zoneless_rewrap(SOURCE_MP4, base / "clip_rewrap.MP4")
    mp4_atom_without_meta_track = _mp4_atom_without_meta_track(
        mp4_atom, base / "clip_atom_no_meta.MP4"
    )
    mp4_unreadable_atom_without_meta_track = _mp4_atom_without_meta_track(
        mp4_atom_unreadable, base / "clip_atom_unreadable_no_meta.MP4"
    )
    mov_fujifilm = _mov_fujifilm(SOURCE_MP4, base / "clip_fujifilm.MOV")
    mov_fujifilm_uncovered = _mov_fujifilm(
        SOURCE_MP4, base / "clip_fujifilm_uncovered.MOV", FUJIFILM_UNCOVERED_LOCAL_TIME
    )
    mp4_isom_bare = _mp4_isom_without_position_or_telemetry(SOURCE_MP4, base / "clip_isom_bare.MP4")
    for path in base.iterdir():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return Assets(
        raf_original=raf_original,
        raf=raf,
        raf_gps=raf_gps,
        raf_xmp_only=raf_xmp,
        raf_partial_block=raf_partial,
        raf_truncated=raf_truncated,
        dng_original=dng_original,
        dng_stripped=dng_stripped,
        dng_positionless=dng_positionless,
        dng_defective_gps=dng_defective_gps,
        dng_xmp_competing_time=dng_xmp_competing_time,
        jpeg=jpeg,
        jpeg_gps=jpeg_gps,
        mp4=mp4,
        mp4_atom=mp4_atom,
        mp4_atom_unreadable=mp4_atom_unreadable,
        mp4_zoneless_rewrap=mp4_zoneless_rewrap,
        mp4_atom_without_meta_track=mp4_atom_without_meta_track,
        mp4_unreadable_atom_without_meta_track=mp4_unreadable_atom_without_meta_track,
        mov_fujifilm=mov_fujifilm,
        mov_fujifilm_uncovered=mov_fujifilm_uncovered,
        mp4_isom_without_position_or_telemetry=mp4_isom_bare,
    )
