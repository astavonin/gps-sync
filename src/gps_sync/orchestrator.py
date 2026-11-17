"""The run orchestrator: sequences the phases, applies the mode branch, derives the exit code.

The classifier never sees the write flag. Dry-run is "the orchestrator never invokes the
writer", not a conditional inside classification. Every tool-level abort condition (exit 2)
fires before the first per-file outcome exists, which is why track parsing precedes the media
walk.
"""

import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from gps_sync.argfile import is_representable
from gps_sync.audit import AuditWriter, RecordContext, find_incomplete_records
from gps_sync.classifier import (
    ClassifierConfig,
    GpxPath,
    TelemetryPath,
    decide_path,
    derive_outcome,
)
from gps_sync.discovery import discover
from gps_sync.errors import InternalConsistencyError, ToolFailure
from gps_sync.exitcode import ArtifactStatus, fold_exit_code
from gps_sync.gpx import TracksParse, parse_tracks
from gps_sync.index import build_index
from gps_sync.model import Failed, FailedReason, Outcome, Planned
from gps_sync.options import Options
from gps_sync.reader import read_records, read_telemetry
from gps_sync.report import render_report
from gps_sync.review import (
    check_precondition,
    prepare_destinations,
    rebuild,
    resolve_layout,
)
from gps_sync.runrecord import render_json, space_demand_to_list
from gps_sync.space import SpaceCheck, check_space, demand
from gps_sync.summary import RunInfo, TelemetryRead, run_level_figures, summarise
from gps_sync.transport import ExiftoolTransport, Transport, check_version, locate_exiftool
from gps_sync.writer import (
    Reconciliation,
    WriteResult,
    assert_targets_writable,
    conclusions_by_path,
    merge_conclusions,
    run_write,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _make_temp_dir(media_root: Path, review_root: Path, base: Path | None) -> Path:
    try:
        if base is not None:
            base.mkdir(parents=True, exist_ok=True)
        created = tempfile.mkdtemp(prefix="gps-sync-", dir=base)
    except OSError as error:
        where = base if base is not None else tempfile.gettempdir()
        raise ToolFailure(f"cannot create a temporary directory in {where}: {error}") from None
    temp_dir = Path(created).resolve()
    for forbidden in (media_root, review_root):
        if temp_dir.is_relative_to(forbidden):
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ToolFailure(
                f"the temporary directory {temp_dir} would lie inside {forbidden}; set TMPDIR "
                "to a location outside the media tree and the run-artifact root"
            )
    return temp_dir


def run(
    options: Options,
    *,
    transport: Transport | None = None,
    clock: Callable[[], datetime] = _utcnow,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    home: Path | None = None,
    temp_base: Path | None = None,
    space_check: SpaceCheck = check_space,
) -> int:
    """Run the tool and return the process exit code (0, 1, 2 or 3)."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    def diagnostic(message: str) -> None:
        print(message, file=err)

    def trace(message: str) -> None:
        if options.verbose:
            diagnostic(f"trace: {message}")

    try:
        return _run(
            options,
            transport,
            clock,
            out,
            diagnostic,
            trace,
            home or Path.home(),
            temp_base,
            space_check,
        )
    except ToolFailure as failure:
        diagnostic(f"gps-sync: error: {failure}")
        return 2


def _run(
    options: Options,
    transport: Transport | None,
    clock: Callable[[], datetime],
    out: TextIO,
    diagnostic: Callable[[str], None],
    trace: Callable[[str], None],
    home: Path,
    temp_base: Path | None,
    space_check: SpaceCheck,
) -> int:
    backend = transport or ExiftoolTransport(locate_exiftool(), trace)
    version_text = backend.version()
    check_version(version_text)
    media_root = options.media.resolve()
    layout = resolve_layout(media_root, options.review_dir, home)
    check_precondition(layout.unprocessed, media_root)
    prepare_destinations(layout)
    temp_dir = _make_temp_dir(media_root, layout.review_root, temp_base)
    try:
        trace(f"temporary directory: {temp_dir}")
        incomplete_prior = find_incomplete_records(layout.runs)
        tracks_root = options.tracks.resolve() if options.tracks is not None else None
        # A supplied root that holds no GPX file stays fatal; an omitted one is a declared empty
        # index, so a mistyped path cannot pass for "no tracks".
        parsed = parse_tracks(tracks_root) if tracks_root is not None else TracksParse((), ())
        index = build_index(s for f in parsed.files for s in f.segments)
        for overlap in index.overlaps:
            diagnostic(
                f"warning: segments {overlap.segment_a} and {overlap.segment_b} overlap in time; "
                "brackets inside the overlap are refused as SEGMENT_BOUNDARY"
            )
        found = discover(media_root, layout.prune_set, video=options.video)
        for ignored in found.ignored:
            trace(f"ignored {ignored.reason.value}: {ignored.path}")
        for pruned in found.pruned:
            trace(f"pruned run-artifact path: {pruned}")
        outcomes: list[Outcome] = list(found.ignored)
        readable = [p for p in found.candidates if is_representable(p)]
        outcomes += [
            Failed(
                p,
                FailedReason.METADATA_INDETERMINATE,
                "the path contains a line break and cannot be passed to exiftool literally",
            )
            for p in found.candidates
            if not is_representable(p)
        ]
        config = ClassifierConfig(
            options.max_gap, options.extend, options.time_shift, options.utc_offset
        )
        records = read_records(backend, readable, temp_dir, trace=trace) if readable else {}
        verdicts = {path: decide_path(records[path], config) for path in readable}
        telemetry_paths = [p for p, v in verdicts.items() if isinstance(v, TelemetryPath)]
        for path, verdict in verdicts.items():
            if not isinstance(verdict, TelemetryPath):
                outcomes.append(
                    derive_outcome(records[path], verdict, None, index, config)
                    if isinstance(verdict, GpxPath)
                    else verdict
                )
        telemetry_started = clock()
        chunks = 0
        for chunk in read_telemetry(backend, telemetry_paths, temp_dir, trace=trace):
            chunks += 1
            for path, telemetry in chunk.items():
                verdict = verdicts[path]
                if not isinstance(verdict, TelemetryPath):
                    raise InternalConsistencyError(
                        f"{path} was read for telemetry without a verdict"
                    )
                outcomes.append(derive_outcome(records[path], verdict, telemetry, index, config))
        # A run that read no telemetry reports zero, so two identical stills-only runs yield the
        # same record rather than differing by the time of an empty loop.
        telemetry_read = TelemetryRead(
            len(telemetry_paths),
            chunks,
            (clock() - telemetry_started).total_seconds() if telemetry_paths else 0.0,
        )
        outcomes.sort(key=lambda o: o.path)

        record_context = RecordContext(
            options.as_record(),
            str(media_root),
            None if tracks_root is None else str(tracks_root),
            version_text,
        )
        problems: list[str] = []
        audit_ok = True
        audit_path: str | None = None
        reconciliation: Reconciliation | None = None
        audit = AuditWriter(layout.runs, clock) if options.write else None
        planned = [o for o in outcomes if isinstance(o, Planned)]
        forecast = demand(planned)
        result: WriteResult | None = None
        if audit is not None:
            audit_path = str(audit.path)
            if planned:
                assert_targets_writable(planned)
                try:
                    audit.write_intent(record_context, planned)
                except OSError as error:
                    raise ToolFailure(
                        f"cannot write the intent record {audit.path}: {error}"
                    ) from None
                writable, refused = space_check(planned, keep_original=options.keep_original)
                # An empty write set spawns nothing: an empty argfile makes exiftool print its
                # whole usage document, which the block parser would ingest as a trailing run.
                written = (
                    run_write(
                        backend,
                        writable,
                        temp_dir,
                        keep_original=options.keep_original,
                        trace=trace,
                    )
                    if writable
                    else None
                )
                result = merge_conclusions(planned, written, refused)
                reconciliation = result.reconciliation
                by_path = conclusions_by_path(result)
                outcomes = [by_path[o.path] if isinstance(o, Planned) else o for o in outcomes]
            else:
                reconciliation = Reconciliation()
        summary = summarise(outcomes)
        if audit is not None and reconciliation is not None:
            completion = {
                "variant_counts": summary.variant_counts,
                "unintended_paths": len(reconciliation.unintended),
                "write_returncode": reconciliation.returncode,
                "space_demand": space_demand_to_list(forecast),
                "telemetry_read": telemetry_read.as_record(),
                "run_level": run_level_figures(
                    outcomes, tracks_given=tracks_root is not None, reconciliation=reconciliation
                ),
            }
            try:
                audit.write_complete(record_context, planned, result, completion)
            except OSError as error:
                audit_ok = False
                problems.append(
                    f"audit record {audit.path} could not be completed ({error}); "
                    + (
                        "the intent-phase record remains on disk and is the floor of what may have "
                        "changed"
                        if planned
                        else "no file was written"
                    )
                )
        rebuilt = rebuild(layout.unprocessed, media_root, outcomes)
        problems += [f"review folder: entry left untouched: {entry}" for entry in rebuilt.refused]
        problems += [f"review folder: {error}" for error in rebuilt.errors]
        artifacts = ArtifactStatus(audit_ok, not rebuilt.incomplete, True, tuple(problems))
        info = RunInfo(
            mode="write" if options.write else "dry-run",
            media_root=str(media_root),
            tracks_root=None if tracks_root is None else str(tracks_root),
            exiftool_version=version_text,
            options=options.as_record(),
            review_root=str(layout.review_root),
            unprocessed=str(layout.unprocessed),
            runs=str(layout.runs),
            audit_record=audit_path,
            pruned=tuple(found.pruned),
            gpx_files=parsed.files,
            duplicate_instants=index.duplicates_dropped,
            skipped_track_symlinks=parsed.skipped_symlinks,
            overlaps=index.overlaps,
            incomplete_prior_records=tuple(
                (r.name, r.created, r.intended_paths) for r in incomplete_prior
            ),
            artifacts=artifacts,
            reconciliation=reconciliation,
            run_level=run_level_figures(
                outcomes, tracks_given=tracks_root is not None, reconciliation=reconciliation
            ),
            space_demand=forecast,
            telemetry_read=telemetry_read,
        )
        provisional = fold_exit_code(outcomes, artifacts, reconciliation)
        text = (
            render_json(outcomes, summary, info, provisional)
            if options.json
            else render_report(outcomes, summary, info)
        )
        try:
            out.write(text)
            out.flush()
        except (OSError, ValueError) as error:
            diagnostic(f"gps-sync: error: cannot write the primary output: {error}")
            artifacts = ArtifactStatus(audit_ok, not rebuilt.incomplete, False, tuple(problems))
        return fold_exit_code(outcomes, artifacts, reconciliation)
    finally:
        if not options.verbose:
            shutil.rmtree(temp_dir, ignore_errors=True)
