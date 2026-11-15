"""Builders shared by the unit, integration and end-to-end suites."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gps_sync.classifier import ClassifierConfig
from gps_sync.gpx import TrackPoint, TrackSegment
from gps_sync.index import TrackIndex, build_index
from gps_sync.records import TagRecord
from gps_sync.transport import MarkedBlock, ReadResult, WriteBatchResult

T0 = datetime(2026, 10, 26, 4, 0, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parent.parent
TESTDATA = REPO / "testdata"
EXIFTOOL = shutil.which("exiftool") or "/opt/homebrew/bin/exiftool"

Spec = tuple[float, float, float, float | None]


def at(seconds: float) -> datetime:
    """Return the instant ``seconds`` after ``T0``."""
    return T0 + timedelta(seconds=seconds)


def segment(ident: str, points: Iterable[Spec], source: str | None = None) -> TrackSegment:
    """Build a segment from ``(t_seconds, lat, lon, ele)`` tuples."""
    pts = tuple(TrackPoint(at(t), lat, lon, ele, ident) for t, lat, lon, ele in points)
    return TrackSegment(ident, source or ident.split("#")[0], pts)


def index_of(*segments: TrackSegment) -> TrackIndex:
    """Merge segments into an index."""
    return build_index(segments)


def tag_record(
    path: str = "/media/a.RAF",
    *,
    gps: object = None,
    date_time_original: str | None = "2026:10:26 10:00:30",
    offset: str | None = "+06:00",
    create_date: str | None = None,
    file_type: str | None = "RAF",
    make: str | None = "FUJIFILM",
    model: str | None = "X-T5",
    warnings: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
    extra: Mapping[str, object] | None = None,
) -> TagRecord:
    """Build a record by parsing the ``-j -n -a -G4`` object exiftool would have printed.

    Every derived field (presence of the position, the parsed position, diagnostics, the primary
    instance of each scalar) is computed by ``TagRecord.from_json``, so a synthetic record can
    never hold a combination the parser cannot produce. ``gps`` is the raw ``GPSPosition`` value:
    ``None`` leaves the tag absent, while ``""`` or a list is present and malformed. Several
    warnings or errors become ``:Warning``, ``Copy1:Warning``... exactly as ``-a`` emits them.
    """
    raw: dict[str, object] = {"SourceFile": path}
    scalars = {
        "DateTimeOriginal": date_time_original,
        "OffsetTimeOriginal": offset,
        "CreateDate": create_date,
        "FileType": file_type,
        "Make": make,
        "Model": model,
    }
    for name, value in scalars.items():
        if value is not None:
            raw[f":{name}"] = value
    if gps is not None:
        raw[":GPSPosition"] = gps
    for name, values in (("Warning", warnings), ("Error", errors)):
        for rank, value in enumerate(values):
            raw[f"{'' if rank == 0 else f'Copy{rank}'}:{name}"] = value
    raw.update(extra or {})
    return TagRecord.from_json(raw)


def config(
    max_gap: float = 120,
    extend: float = 0,
    time_shift: float = 0,
    utc_offset: timedelta | None = None,
) -> ClassifierConfig:
    """Build a classifier configuration from plain seconds."""
    return ClassifierConfig(
        timedelta(seconds=max_gap),
        timedelta(seconds=extend),
        timedelta(seconds=time_shift),
        utc_offset,
    )


def block_text(*blocks: tuple[Sequence[str], str]) -> str:
    """Render ``(result_lines, marker)`` pairs as exiftool prints them."""
    return "".join(
        "".join(f"{line}\n" for line in lines) + f"{marker}\n" for lines, marker in blocks
    )


def marked(marker: str, *lines: str) -> MarkedBlock:
    """Build a marker-terminated block."""
    return MarkedBlock(marker, tuple(lines))


def batch(
    stdout: Sequence[MarkedBlock],
    stderr: Sequence[MarkedBlock],
    returncode: int = 0,
) -> WriteBatchResult:
    """Build a write-batch result."""
    return WriteBatchResult(tuple(stdout), tuple(stderr), returncode)


class FakeTransport:
    """A substituted transport: records come from a callable, writes from a handler."""

    def __init__(
        self,
        records: Callable[[str], Mapping[str, object]] | None = None,
        *,
        version: str = "13.55",
        on_write: Callable[[Path], WriteBatchResult] | None = None,
    ) -> None:
        self._records = records or (lambda path: {})
        self._version = version
        self._on_write = on_write
        self.read_calls: list[list[str]] = []
        self.write_calls: list[list[str]] = []

    def version(self) -> str:
        return self._version

    def read(self, argfile: Path) -> ReadResult:
        paths = argfile.read_text().splitlines()
        self.read_calls.append(paths)
        return ReadResult({p: {"SourceFile": p, **self._records(p)} for p in paths})

    def write(self, argfile: Path) -> WriteBatchResult:
        self.write_calls.append(argfile.read_text().splitlines())
        if self._on_write is None:
            raise AssertionError("write was not expected")
        return self._on_write(argfile)


def raf_json(
    gps: str | None = None,
    dto: str = "2026:10:26 10:00:30",
    offset: str | None = "+06:00",
    file_type: str = "RAF",
    **extra: object,
) -> dict[str, object]:
    """Build the ``-j -n -a -G4`` object exiftool would return for a RAF.

    Tag names are given bare and emitted as the ``:Tag`` keys the real invocation produces, so the
    reader's instance-prefix handling is on the path of every test that uses this builder. A name
    already carrying a prefix (``Copy1:Warning``) is passed through unchanged.
    """
    data: dict[str, object] = {
        ":DateTimeOriginal": dto,
        ":FileType": file_type,
        ":Make": "FUJIFILM",
        ":Model": "X-T5",
    }
    if offset is not None:
        data[":OffsetTimeOriginal"] = offset
    if gps is not None:
        data[":GPSPosition"] = gps
    for name, value in extra.items():
        data[name if ":" in name else f":{name}"] = value
    return data


def gpx_text(points: Iterable[tuple[str | None, str, str, str | None]], extra: str = "") -> str:
    """Build a one-segment GPX 1.1 document from ``(time, lat, lon, ele)`` tuples."""
    body = []
    for time_text, lat, lon, ele in points:
        inner = ""
        if ele is not None:
            inner += f"<ele>{ele}</ele>"
        if time_text is not None:
            inner += f"<time>{time_text}</time>"
        body.append(f'<trkpt lat="{lat}" lon="{lon}">{inner}</trkpt>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">{extra}'
        f"<trk><trkseg>{''.join(body)}</trkseg></trk></gpx>"
    )


def clone(source: Path, destination: Path) -> Path:
    """Copy a (large) file using an APFS clone where possible; never touches the source."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["cp", "-c", str(source), str(destination)], capture_output=True)
    if result.returncode != 0:
        shutil.copyfile(source, destination)
    os.chmod(destination, 0o644)
    return destination


def exiftool(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the real exiftool on test-owned files."""
    return subprocess.run([EXIFTOOL, *args], capture_output=True, text=True, check=False)


def run_info(**overrides: object):  # type: ignore[no-untyped-def]
    """Build a ``RunInfo`` with neutral defaults for reporter and JSON tests."""
    from gps_sync.exitcode import ArtifactStatus
    from gps_sync.summary import RunInfo

    values: dict[str, object] = {
        "mode": "dry-run",
        "media_root": "/media",
        "tracks_root": "/tracks",
        "exiftool_version": "13.55",
        "options": {},
        "review_root": "/review",
        "unprocessed": "/review/_unprocessed",
        "runs": "/review/runs",
        "audit_record": None,
        "pruned": (),
        "gpx_files": (),
        "duplicate_instants": {},
        "skipped_track_symlinks": (),
        "overlaps": (),
        "incomplete_prior_records": (),
        "artifacts": ArtifactStatus(),
        "reconciliation": None,
    }
    values.update(overrides)
    return RunInfo(**values)  # type: ignore[arg-type]


BASELINE = TESTDATA / ".baseline.md5"
# Set to "ignore" to run against a deliberately modified corpus; any other value, or none, enforces.
CORPUS_BASELINE_ENV = "GPS_SYNC_CORPUS_BASELINE"
# Finder creates and rewrites .DS_Store on a folder view, so whether one exists under testdata/ is
# not a property the suite may rest on. MANIFEST.json is deliberately pinned: it is the declared
# oracle for the e2e counts, and an oracle whose own drift went unnoticed would vouch for anything.
_NOT_FIXTURES = frozenset({".DS_Store"})
_BASELINE_LINE = re.compile(r"^(?P<md5>[0-9a-f]{32})  (?P<path>\S.*)$")


class CorpusError(AssertionError):
    """The fixture corpus cannot be vouched for: it drifted, or its baseline is unusable."""


def _read_baseline(baseline: Path) -> dict[str, str]:
    try:
        lines = baseline.read_text().splitlines()
    except OSError as error:
        raise CorpusError(f"cannot read the corpus baseline {baseline}: {error}") from error
    rows: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        match = _BASELINE_LINE.match(line)
        if match is None:
            raise CorpusError(f"{baseline.name}:{number} is not '<md5>  <path>': {line!r}")
        rows[match["path"]] = match["md5"]
    if not rows:
        raise CorpusError(f"{baseline.name} lists no files")
    return rows


def corpus_mismatches(baseline: Path | None = None, root: Path = REPO) -> list[str]:
    """Name every fixture that drifted, vanished or was added since the baseline was recorded.

    Baseline lines are ``<md5>  <path relative to root>`` as ``md5sum`` writes them, and the
    corpus is everything under the baseline's directory. A baseline that is absent, empty or has
    a line that does not parse raises ``CorpusError``: a check that cannot read its reference
    must not read as a clean corpus.
    """
    baseline = baseline or BASELINE
    recorded = {
        path: md5 for path, md5 in _read_baseline(baseline).items() if _is_fixture(Path(path))
    }
    problems: list[str] = []
    for relative, expected in recorded.items():
        path = root / relative
        if not path.is_file():
            problems.append(f"{relative}: missing")
            continue
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, lambda: hashlib.md5(usedforsecurity=False))
        if digest.hexdigest() != expected:
            problems.append(f"{relative}: md5 differs from the baseline")
    known = {os.path.abspath(root / relative) for relative in recorded}
    for current in baseline.parent.rglob("*"):
        if current.is_dir() and not current.is_symlink():
            continue
        if _is_fixture(current) and current != baseline and os.path.abspath(current) not in known:
            shown = current.relative_to(root) if current.is_relative_to(root) else current
            problems.append(f"{shown.as_posix()}: not in the baseline")
    return problems


def _is_fixture(path: Path) -> bool:
    return path.name not in _NOT_FIXTURES


def check_corpus(baseline: Path | None = None, root: Path = REPO) -> None:
    """Raise ``CorpusError`` unless ``testdata/`` matches its baseline, naming every mismatch.

    Called by every fixture that derives files from the corpus, so a drifted fixture fails the run
    instead of being silently relied on. ``GPS_SYNC_CORPUS_BASELINE=ignore`` opts out.
    """
    if os.environ.get(CORPUS_BASELINE_ENV) == "ignore":
        return
    problems = corpus_mismatches(baseline, root)
    if problems:
        raise CorpusError(
            f"testdata/ no longer matches its baseline ({len(problems)} file(s)): "
            + "; ".join(problems)
            + f". Restore the corpus, or set {CORPUS_BASELINE_ENV}=ignore to proceed anyway."
        )
