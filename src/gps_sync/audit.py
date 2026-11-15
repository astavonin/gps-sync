"""The two-phase audit record.

The intended write set is flushed and synced before the writer process is spawned, and the
per-path conclusions are written after it returns by an atomic rewrite. A record still in its
``intent`` phase is the incomplete-run marker. The tool never deletes a record.
"""

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gps_sync.model import Planned
from gps_sync.serialise import instant_text, outcome_to_dict, position_dict, provenance_dict
from gps_sync.writer import Reconciliation, WriteResult

RECORD_VERSION = 1
INTENT = "intent"
COMPLETE = "complete"

Clock = Callable[[], datetime]


def record_name(moment: datetime, pid: int) -> str:
    """Name a record ``<UTC basic ISO-8601 to the second>-<pid>.json``.

    Lexicographic order is chronological, which makes a directory listing a history.
    """
    return f"{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}-{pid}.json"


@dataclass(frozen=True, slots=True)
class RecordContext:
    """Run facts every record carries."""

    options: Mapping[str, Any]
    media_root: str
    tracks_root: str
    exiftool_version: str


@dataclass(frozen=True, slots=True)
class IncompleteRecord:
    """A prior record found still in its intent phase."""

    name: str
    created: str
    intended_paths: int


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _intended_entry(target: Planned) -> dict[str, Any]:
    return {
        "path": target.path,
        "instant": instant_text(target.instant),
        "position": position_dict(target.position),
        "altitude_included": target.position.ele is not None,
        "via": target.via.value,
        "provenance": provenance_dict(target.provenance),
    }


def _reconciliation_dict(reconciliation: Reconciliation) -> dict[str, Any]:
    data: dict[str, Any] = {
        "unintended_conclusions": [
            {"path": u.path, "lines": list(u.lines)} for u in reconciliation.unintended
        ],
        "returncode": reconciliation.returncode,
    }
    if reconciliation.last_marker is not None:
        data["last_marker"] = reconciliation.last_marker
    if reconciliation.process_failure is not None:
        data["process_failure"] = reconciliation.process_failure
    return data


class AuditWriter:
    """Writes one run's record under ``runs/``."""

    def __init__(self, runs_dir: Path, clock: Clock, pid: int | None = None) -> None:
        self._runs_dir = runs_dir
        self._clock = clock
        self._pid = os.getpid() if pid is None else pid
        self._moment = clock()
        self.path = runs_dir / record_name(self._moment, self._pid)

    def _document(
        self, phase: str, context: RecordContext, intended: Sequence[Planned]
    ) -> dict[str, Any]:
        return {
            "record_version": RECORD_VERSION,
            "phase": phase,
            "created": instant_text(self._moment),
            "pid": self._pid,
            "media_root": context.media_root,
            "tracks_root": context.tracks_root,
            "exiftool_version": context.exiftool_version,
            "options": dict(context.options),
            "intended": [_intended_entry(t) for t in intended],
        }

    def write_intent(self, context: RecordContext, intended: Sequence[Planned]) -> None:
        """Create the record in intent phase; it is flushed and synced before returning."""
        payload = json.dumps(self._document(INTENT, context, intended), indent=2).encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self._runs_dir)

    def write_complete(
        self,
        context: RecordContext,
        intended: Sequence[Planned],
        result: WriteResult | None,
        verdict: Mapping[str, Any],
    ) -> None:
        """Atomically rewrite the record in complete phase.

        A write run that changed nothing passes ``result=None`` and an empty intended list, so
        "nothing to do" stays distinguishable from "something went wrong".
        """
        document = self._document(COMPLETE, context, intended)
        document["conclusions"] = (
            [outcome_to_dict(o) for o in result.outcomes] if result is not None else []
        )
        document["reconciliation"] = (
            _reconciliation_dict(result.reconciliation) if result is not None else {}
        )
        document["verdict"] = dict(verdict)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(document, indent=2).encode()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.path)
        _fsync_directory(self._runs_dir)


def find_incomplete_records(runs_dir: Path) -> list[IncompleteRecord]:
    """Return prior records still in their intent phase, oldest first."""
    found: list[IncompleteRecord] = []
    if not runs_dir.is_dir():
        return found
    for entry in sorted(runs_dir.glob("*.json")):
        try:
            document = json.loads(entry.read_text())
        except OSError, ValueError:
            continue
        if isinstance(document, dict) and document.get("phase") == INTENT:
            intended = document.get("intended", [])
            found.append(
                IncompleteRecord(entry.name, str(document.get("created", "")), len(intended))
            )
    return found
