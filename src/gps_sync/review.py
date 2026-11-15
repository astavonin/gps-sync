"""Run-artifact layout, the review-folder rebuild precondition and the bounded rebuild.

Rebuilding ``_unprocessed/`` is the one filesystem write in this tool that could destroy data it
did not create, and it is reachable from dry-run. It is therefore a bounded operation: only
entries the tool itself would have created are ever removed, and that rule is evaluated again,
per entry, at the moment of removal.
"""

import contextlib
import hashlib
import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from gps_sync import __version__
from gps_sync.errors import ToolFailure
from gps_sync.model import Outcome, reason_key
from gps_sync.registry import review_subfolders, spec

MARKER_NAME = ".gps-sync-review-marker.json"
MARKER_VERSION = 1
RECOGNISED_MARKER_VERSIONS = frozenset({1})
UNPROCESSED = "_unprocessed"
RUNS = "runs"


@dataclass(frozen=True, slots=True)
class ArtifactLayout:
    """The resolved run-artifact root and its two subtrees."""

    review_root: Path

    @property
    def unprocessed(self) -> Path:
        """The rebuilt per-run review folder."""
        return self.review_root / UNPROCESSED

    @property
    def runs(self) -> Path:
        """The append-only audit-record directory, a sibling of the review folder."""
        return self.review_root / RUNS

    @property
    def prune_set(self) -> frozenset[Path]:
        """The subtrees discovery must skip wherever they sit."""
        return frozenset({self.unprocessed, self.runs})


def derive_default_review_root(media_root: Path, home: Path) -> Path:
    """Return ``~/gps-sync/<basename>-<digest>``, one root per media tree."""
    digest = hashlib.sha256(str(media_root).encode()).hexdigest()[:8]
    return home / "gps-sync" / f"{media_root.name or 'root'}-{digest}"


def check_review_root(review_root: Path, media_root: Path, home: Path) -> None:
    """Refuse a review root that is ``/``, ``$HOME``, or is or contains the media root.

    A root *inside* the media tree is permitted: discovery prunes it. A root that is or contains
    the media root is refused because that same prune would swallow the media tree.
    """
    if review_root in (Path("/"), home):
        raise ToolFailure(
            f"review root {review_root} is not acceptable: choose another --review-dir"
        )
    if review_root == media_root or review_root in media_root.parents:
        raise ToolFailure(
            f"review root {review_root} is, or contains, the media root {media_root}; "
            "choose another --review-dir"
        )


def resolve_layout(media_root: Path, review_dir: Path | None, home: Path) -> ArtifactLayout:
    """Resolve the run-artifact root, by derivation or by flag, and apply the containment rule."""
    root = (
        derive_default_review_root(media_root, home)
        if review_dir is None
        else review_dir.expanduser()
    )
    resolved = root.resolve()
    check_review_root(resolved, media_root, home.resolve())
    return ArtifactLayout(resolved)


def _is_symlink(path: Path) -> bool:
    return stat.S_ISLNK(os.lstat(path).st_mode)


def _foreign_entry(unprocessed: Path) -> Path | None:
    """Return the first entry the tool would not have created, or None."""
    buckets = review_subfolders()
    for entry in sorted(unprocessed.iterdir()):
        mode = os.lstat(entry).st_mode
        if entry.name == MARKER_NAME and stat.S_ISREG(mode):
            continue
        if not stat.S_ISDIR(mode) or entry.name not in buckets:
            return entry
        found = _foreign_in_bucket(entry)
        if found is not None:
            return found
    return None


def _foreign_in_bucket(directory: Path) -> Path | None:
    for entry in sorted(directory.iterdir()):
        mode = os.lstat(entry).st_mode
        if stat.S_ISLNK(mode):
            continue
        if not stat.S_ISDIR(mode):
            return entry
        found = _foreign_in_bucket(entry)
        if found is not None:
            return found
    return None


def check_precondition(unprocessed: Path, media_root: Path) -> None:
    """Require the review folder to be absent, empty, or marked by this tool for this media root.

    An unreadable review folder is a tool-level failure (exit 2), not a crash: the run has not
    started, so no per-file outcome may be reported for it.
    """
    try:
        _check_precondition(unprocessed, media_root)
    except OSError as error:
        raise ToolFailure(f"cannot inspect the review folder {unprocessed}: {error}") from None


def _check_precondition(unprocessed: Path, media_root: Path) -> None:
    if not os.path.lexists(unprocessed):
        return
    if _is_symlink(unprocessed) or not unprocessed.is_dir():
        raise ToolFailure(f"{unprocessed} exists and is not a directory gps-sync created")
    entries = list(unprocessed.iterdir())
    if not entries:
        return
    marker = unprocessed / MARKER_NAME
    if not marker.is_file() or _is_symlink(marker):
        raise ToolFailure(
            f"{unprocessed} is not empty and carries no gps-sync marker; gps-sync will not remove "
            "content it did not create. Point --review-dir elsewhere or clear the folder by hand."
        )
    try:
        declared = json.loads(marker.read_text())
        version = declared["marker_version"]
        marked_root = declared["media_root"]
    except OSError, ValueError, KeyError, TypeError:
        raise ToolFailure(f"{marker} is unreadable or malformed; refusing to rebuild") from None
    if version not in RECOGNISED_MARKER_VERSIONS:
        raise ToolFailure(
            f"{marker} carries marker version {version!r}, which this gps-sync does not "
            "recognise; point --review-dir elsewhere or clear the folder by hand"
        )
    if marked_root != str(media_root):
        raise ToolFailure(
            f"review folder {unprocessed} belongs to media root {marked_root}, not {media_root}; "
            "point --review-dir elsewhere or clear the folder by hand"
        )
    foreign = _foreign_entry(unprocessed)
    if foreign is not None:
        raise ToolFailure(
            f"{foreign} inside the review folder was not created by gps-sync; refusing to "
            "rebuild. Point --review-dir elsewhere or remove it by hand."
        )


def prepare_destinations(layout: ArtifactLayout) -> None:
    """Create the run-artifact root and ``runs/`` when absent and prove both writable."""
    for directory in (layout.review_root, layout.runs):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".gps-sync-write-probe"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as error:
            raise ToolFailure(
                f"run-artifact directory {directory} is not usable: {error}"
            ) from None


@dataclass(slots=True)
class RebuildReport:
    """What the rebuild did and, when it was incomplete, exactly why."""

    links_created: int = 0
    refused: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        """True when any entry was refused or any creation failed."""
        return bool(self.refused or self.errors)


def _remove_bucket_entries(directory: Path, report: RebuildReport) -> None:
    for entry in sorted(directory.iterdir()):
        mode = os.lstat(entry).st_mode
        if stat.S_ISLNK(mode):
            entry.unlink()
        elif stat.S_ISDIR(mode):
            _remove_bucket_entries(entry, report)
            with contextlib.suppress(OSError):
                entry.rmdir()
        else:
            report.refused.append(str(entry))


def _remove_prior(unprocessed: Path, report: RebuildReport) -> None:
    buckets = review_subfolders()
    for entry in sorted(unprocessed.iterdir()):
        mode = os.lstat(entry).st_mode
        if entry.name == MARKER_NAME and stat.S_ISREG(mode):
            continue
        if stat.S_ISDIR(mode) and entry.name in buckets:
            _remove_bucket_entries(entry, report)
            with contextlib.suppress(OSError):
                entry.rmdir()
        else:
            report.refused.append(str(entry))


def _write_marker(unprocessed: Path, media_root: Path) -> None:
    payload = {
        "tool": "gps-sync",
        "tool_version": __version__,
        "marker_version": MARKER_VERSION,
        "media_root": str(media_root),
    }
    temporary = unprocessed / (MARKER_NAME + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2))
    os.replace(temporary, unprocessed / MARKER_NAME)


def _ensure_real_directory(path: Path, report: RebuildReport) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        try:
            path.mkdir()
        except OSError as error:
            report.errors.append(f"{path}: {error}")
            return False
        return True
    if stat.S_ISDIR(mode):
        return True
    report.refused.append(str(path))
    return False


def _link(
    unprocessed: Path, subfolder: str, relative: Path, target: str, report: RebuildReport
) -> None:
    current = unprocessed
    for component in (subfolder, *relative.parent.parts):
        current = current / component
        if not _ensure_real_directory(current, report):
            return
    link = current / relative.name
    if os.path.lexists(link):
        report.refused.append(str(link))
        return
    try:
        os.symlink(target, link)
    except OSError as error:
        report.errors.append(f"{link}: {error}")
        return
    report.links_created += 1


def rebuild(unprocessed: Path, media_root: Path, outcomes: Iterable[Outcome]) -> RebuildReport:
    """Rebuild ``_unprocessed/<reason>/<path relative to the media root>`` symlinks.

    Never raises for a refusal: an entry the bounded-removal rule refuses at removal time, a
    mirrored component that is not a real directory, or a failed creation is recorded and the
    review folder reported incomplete.
    """
    report = RebuildReport()
    try:
        unprocessed.mkdir(parents=True, exist_ok=True)
        _remove_prior(unprocessed, report)
        _write_marker(unprocessed, media_root)
    except OSError as error:
        report.errors.append(f"{unprocessed}: {error}")
        return report
    for outcome in outcomes:
        subfolder = spec(reason_key(outcome)).subfolder
        if subfolder is None:
            continue
        relative = Path(outcome.path).relative_to(media_root)
        _link(unprocessed, subfolder, relative, outcome.path, report)
    return report
