"""The CLI contract: the flag inventory, its defaults and the shared duration grammar."""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, NoReturn

from gps_sync.durations import DurationError, parse_duration, parse_utc_offset
from gps_sync.errors import ToolFailure

DEFAULT_MAX_GAP = timedelta(seconds=120)


@dataclass(frozen=True, slots=True)
class Options:
    """Validated options."""

    media: Path
    tracks: Path | None
    write: bool = False
    max_gap: timedelta = DEFAULT_MAX_GAP
    extend: timedelta = timedelta(0)
    time_shift: timedelta = timedelta(0)
    utc_offset: timedelta | None = None
    review_dir: Path | None = None
    keep_original: bool = False
    json: bool = False
    verbose: bool = False
    video: bool = False

    def as_record(self) -> dict[str, Any]:
        """Render the options for the audit record and the JSON record."""
        return {
            "media": str(self.media),
            "tracks": None if self.tracks is None else str(self.tracks),
            "write": self.write,
            "max_gap_s": self.max_gap.total_seconds(),
            "extend_s": self.extend.total_seconds(),
            "time_shift_s": self.time_shift.total_seconds(),
            "utc_offset_s": None if self.utc_offset is None else self.utc_offset.total_seconds(),
            "review_dir": None if self.review_dir is None else str(self.review_dir),
            "keep_original": self.keep_original,
            "json": self.json,
            "verbose": self.verbose,
            "video": self.video,
        }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ToolFailure(message)


def _duration(text: str) -> timedelta:
    try:
        return parse_duration(text)
    except DurationError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _signed_duration(text: str) -> timedelta:
    try:
        return parse_duration(text, signed=True)
    except DurationError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _offset(text: str) -> timedelta:
    try:
        return parse_utc_offset(text)
    except DurationError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser; parse errors raise ``ToolFailure`` (exit code 2)."""
    parser = _Parser(
        prog="gps-sync",
        description="Write GPS positions into stills, and with --video into video containers, "
        "that carry none: from GPX tracks, or from a clip's own telemetry. "
        "Dry-run is the default; an existing position is never overwritten.",
        allow_abbrev=False,
    )
    parser.add_argument("--media", required=True, type=Path, help="root of the media tree to scan")
    parser.add_argument(
        "--tracks",
        type=Path,
        default=None,
        help="root of the GPX tree to parse (required unless --video is given; omitted under "
        "--video, nothing is matched against a track)",
    )
    parser.add_argument(
        "--video",
        action="store_true",
        help="also admit MP4 and MOV video containers as candidates",
    )
    parser.add_argument("--write", action="store_true", help="apply writes (default is dry-run)")
    parser.add_argument(
        "--max-gap",
        type=_duration,
        default=DEFAULT_MAX_GAP,
        metavar="DURATION",
        help="maximum bracket width within one segment, inclusive (default 120s)",
    )
    parser.add_argument(
        "--extend",
        type=_duration,
        default=timedelta(0),
        metavar="DURATION",
        help="clamp to the nearer endpoint of a refused gap within this window (default 0s)",
    )
    parser.add_argument(
        "--time-shift",
        type=_signed_duration,
        default=timedelta(0),
        metavar="SIGNED_DURATION",
        help="added to each resolved UTC instant before matching (default +0s)",
    )
    parser.add_argument(
        "--utc-offset",
        type=_offset,
        default=None,
        metavar="±HH:MM",
        help="offset for local-time stills that carry no OffsetTimeOriginal; never consulted for a "
        "video container",
    )
    parser.add_argument(
        "--review-dir",
        type=Path,
        default=None,
        help="parent of _unprocessed/ and runs/ (default ~/gps-sync/<media>-<digest>)",
    )
    parser.add_argument(
        "--keep-original", action="store_true", help="retain exiftool's _original copy"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the report")
    parser.add_argument("--verbose", action="store_true", help="diagnostic trace on stderr")
    return parser


def _glue_signed_values(argv: Sequence[str]) -> list[str]:
    """Let ``--time-shift -60s`` work: argparse would otherwise read ``-60s`` as an option."""
    glued: list[str] = []
    items = list(argv)
    i = 0
    while i < len(items):
        if items[i] == "--time-shift" and i + 1 < len(items) and items[i + 1][:1] in ("+", "-"):
            glued.append(f"--time-shift={items[i + 1]}")
            i += 2
        else:
            glued.append(items[i])
            i += 1
    return glued


def parse_options(argv: Sequence[str]) -> Options:
    """Parse and validate ``argv``."""
    namespace = build_parser().parse_args(_glue_signed_values(argv))
    if namespace.tracks is None and not namespace.video:
        raise ToolFailure(
            "the following arguments are required: --tracks (unless --video is given)"
        )
    return Options(
        media=namespace.media,
        tracks=namespace.tracks,
        write=namespace.write,
        max_gap=namespace.max_gap,
        extend=namespace.extend,
        time_shift=namespace.time_shift,
        utc_offset=namespace.utc_offset,
        review_dir=namespace.review_dir,
        keep_original=namespace.keep_original,
        json=namespace.json,
        verbose=namespace.verbose,
        video=namespace.video,
    )
