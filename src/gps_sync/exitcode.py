"""The process exit code: a fold over the three verdict inputs."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from gps_sync.model import Outcome, RunLevelReason, reason_key
from gps_sync.registry import spec
from gps_sync.writer import Reconciliation

EXIT_OK = 0
EXIT_FAILED_FILE = 1
EXIT_TOOL_FAILURE = 2
EXIT_ARTIFACT = 3


@dataclass(frozen=True, slots=True)
class ArtifactStatus:
    """Whether every run-level artifact the run undertook to produce was completed."""

    audit_complete: bool = True
    review_complete: bool = True
    stdout_ok: bool = True
    problems: tuple[str, ...] = field(default=())

    @property
    def complete(self) -> bool:
        """True when nothing the run promised is missing or incomplete."""
        return self.audit_complete and self.review_complete and self.stdout_ok


def fold_exit_code(
    outcomes: Iterable[Outcome],
    artifacts: ArtifactStatus,
    reconciliation: Reconciliation | None,
) -> int:
    """Return 0, 1 or 3 for a run that reported per-file outcomes (2 never reaches the fold).

    The code is a maximum over the tally, ordered 0 < 1 < 3: a missing record of an in-place
    write outranks a file the tool declined to write, because only the second can be re-run.
    """
    code = EXIT_OK
    for outcome in outcomes:
        code = max(code, spec(reason_key(outcome)).exit_term)
    if reconciliation is not None and reconciliation.unintended:
        code = max(code, spec(RunLevelReason.WRITE_UNINTENDED_PATH).exit_term)
    if reconciliation is not None and reconciliation.unreconciled:
        code = max(code, spec(RunLevelReason.BLOCK_COUNT_UNRECONCILED).exit_term)
    if not artifacts.complete:
        code = EXIT_ARTIFACT
    return code
