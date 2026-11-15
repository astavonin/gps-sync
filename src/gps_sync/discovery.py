"""Discovery: a filesystem walk of our own, never ``exiftool -r``.

The walk skips symlinks (counting them), prunes the run-artifact root wherever it sits,
deduplicates candidates by device and inode, and emits only absolute real paths. Candidacy is
decided by the filename's own final extension, which is what keeps exiftool's ``_original``
backup out of the candidate set.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from gps_sync.errors import ToolFailure
from gps_sync.model import Ignored, IgnoredReason

#: Recognised still extensions (lowercase). A recognised still always becomes a candidate.
STILL_EXTENSIONS = frozenset({".raf", ".dng", ".jpg", ".jpeg", ".tif", ".tiff", ".heic"})


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Candidates to read, entries that never became candidates, and the pruned paths."""

    candidates: tuple[str, ...]
    ignored: tuple[Ignored, ...]
    pruned: tuple[str, ...]


@dataclass(slots=True)
class _Walk:
    root: Path
    prune: frozenset[Path]
    candidates: list[str] = field(default_factory=list)
    ignored: list[Ignored] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    inodes: dict[tuple[int, int], str] = field(default_factory=dict)

    def visit(self, directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as error:
            raise ToolFailure(f"cannot read media directory {directory}: {error}") from None
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                self.ignored.append(Ignored(entry.path, IgnoredReason.SYMLINK, self._target(path)))
            elif entry.is_dir(follow_symlinks=False):
                if path in self.prune:
                    self.pruned.append(entry.path)
                else:
                    self.visit(path)
            elif not entry.is_file(follow_symlinks=False) or (
                os.path.splitext(entry.name)[1].lower() not in STILL_EXTENSIONS
            ):
                self.ignored.append(Ignored(entry.path, IgnoredReason.NOT_A_STILL))
            else:
                self._candidate(entry)

    def _target(self, link: Path) -> str | None:
        """Name the real target when it lies inside the media root."""
        try:
            target = link.resolve(strict=True)
        except OSError:
            return None
        return str(target) if target.is_relative_to(self.root) else None

    def _candidate(self, entry: os.DirEntry[str]) -> None:
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise ToolFailure(f"cannot stat {entry.path}: {error}") from None
        key = (stat.st_dev, stat.st_ino)
        retained = self.inodes.get(key)
        if retained is None:
            self.inodes[key] = entry.path
            self.candidates.append(entry.path)
        else:
            self.ignored.append(Ignored(entry.path, IgnoredReason.DUPLICATE_INODE, retained))


def discover(media_root: Path, prune: frozenset[Path]) -> DiscoveryResult:
    """Walk ``media_root`` (already resolved) and classify every entry.

    ``prune`` holds resolved absolute directories, the run-artifact subtrees, that are skipped
    wherever they sit.
    """
    if not media_root.is_dir():
        raise ToolFailure(f"media root {media_root} is not a readable directory")
    walk = _Walk(media_root, prune)
    walk.visit(media_root)
    return DiscoveryResult(tuple(walk.candidates), tuple(walk.ignored), tuple(walk.pruned))
