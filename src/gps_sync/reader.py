"""The batched metadata reader: one tag record per candidate, never one process per file."""

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from gps_sync.argfile import emit_argfile, path_line
from gps_sync.errors import ToolFailure
from gps_sync.records import TagRecord, TelemetryRecord
from gps_sync.transport import TELEMETRY_CHUNK_SIZE, Transport

#: Bounded chunking above the reference envelope; at or below it one process reads everything.
READ_CHUNK_SIZE = 5000


def read_records(
    transport: Transport,
    paths: Sequence[str],
    temp_dir: Path,
    *,
    trace: Callable[[str], None] = lambda _message: None,
    chunk_size: int = READ_CHUNK_SIZE,
) -> dict[str, TagRecord]:
    """Read every path in as few batches as ``chunk_size`` allows and reconcile the counts.

    Count in equals count out: a candidate absent from the result is a tool-level failure,
    because treating a missing record as empty would read as "no position", the unsafe direction.
    """
    records: dict[str, TagRecord] = {}
    for number, start in enumerate(range(0, len(paths), chunk_size)):
        chunk = paths[start : start + chunk_size]
        argfile = temp_dir / f"read-{number:03d}.args"
        emit_argfile(argfile, [path_line(p) for p in chunk])
        trace(f"read argfile: {argfile} ({len(chunk)} paths)")
        result = transport.read(argfile)
        requested, returned = set(chunk), set(result.records)
        if requested != returned:
            missing = sorted(requested - returned)
            extra = sorted(returned - requested)
            raise ToolFailure(
                "exiftool read did not return exactly the requested records "
                f"(missing {len(missing)}: {missing[:3]}, unexpected {len(extra)}: {extra[:3]}); "
                "the media tree changed during the run or a file cannot be opened. "
                f"exiftool said: {result.stderr.strip()[:300] or 'nothing'}"
            )
        for path, raw in result.records.items():
            try:
                records[path] = TagRecord.from_json(raw)
            except ToolFailure as failure:
                # The version is fetched only here: it is what a user needs after an upgrade.
                raise ToolFailure(
                    f"{failure} (running exiftool {transport.version().strip()})"
                ) from failure
    return records


def read_telemetry(
    transport: Transport,
    paths: Sequence[str],
    temp_dir: Path,
    *,
    trace: Callable[[str], None] = lambda _message: None,
    chunk_size: int = TELEMETRY_CHUNK_SIZE,
) -> Iterator[dict[str, TelemetryRecord]]:
    """Yield the telemetry of ``paths`` one chunk at a time, reconciling each chunk's counts.

    A telemetry record can run to megabytes, so what a run retains is bounded by one chunk and
    not by the archive: the generator holds nothing it has already yielded. A dropped record would
    read as "no samples", so a chunk that does not return exactly what it was asked for aborts.
    """
    for number, start in enumerate(range(0, len(paths), chunk_size)):
        chunk = paths[start : start + chunk_size]
        argfile = temp_dir / f"telemetry-{number:03d}.args"
        emit_argfile(argfile, [path_line(p) for p in chunk])
        trace(f"telemetry argfile: {argfile} ({len(chunk)} paths)")
        result = transport.read_telemetry(argfile)
        requested, returned = set(chunk), set(result.records)
        if requested != returned:
            raise ToolFailure(
                "exiftool telemetry read did not return exactly the requested records "
                f"(missing {len(requested - returned)}, unexpected {len(returned - requested)}); "
                f"exiftool said: {result.stderr.strip()[:300] or 'nothing'}"
            )
        yield {path: TelemetryRecord.from_json(raw) for path, raw in result.records.items()}
