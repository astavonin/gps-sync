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
from pathlib import Path

import pytest

from tests.support import EXIFTOOL, TESTDATA, clone, exiftool

SOURCE_RAF = TESTDATA / "media" / "day1" / "DSCF8326.RAF"
SOURCE_DNG = TESTDATA / "media" / "drone" / "DJI_20261026094631_0036_D.DNG"
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


def require_environment() -> None:
    """Skip the calling test or fixture when exiftool or the corpus is not available."""
    if not Path(EXIFTOOL).exists():
        pytest.skip("exiftool is not installed")
    if not SOURCE_RAF.exists() or not SOURCE_DNG.exists():
        pytest.skip("testdata/ is not present; real RAF and DNG fixtures are unavailable")


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
    )
