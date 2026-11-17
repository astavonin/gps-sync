#!/usr/bin/env python3
"""Decide the mutation gate, propose a candidate baseline and promote one.

An entry's identity is a digest of the mutant's own diff together with a digest of the enclosing
function, alongside the ordinal name, so a renumbered mutant is the same entry and an edited
function is a different one. The committed ``mutation-baseline.txt`` is read, never rewritten, by
``gate`` and ``propose``; only ``promote`` replaces it, and only with a candidate whose admissions
check passed and whose bytes have not changed since.

    mutation_baseline.py partition --results FILE --all-results FILE --survivors FILE
                                   --undetermined FILE
    mutation_baseline.py gate     --survivors FILE [--undetermined FILE]
    mutation_baseline.py propose  --survivors FILE [--undetermined FILE]
    mutation_baseline.py promote

``--results`` is the text of ``mutmut results`` and ``--all-results`` that of ``mutmut results
--all true``; ``partition`` splits them by the status column and checks that every mutant is
accounted for. A survivors file holds one ``survived`` mutant name per line; an undetermined file
holds ``<name><TAB><status>`` for every other non-killed mutant. The gate treats ``timeout`` as a
detection up to ``TIMEOUT_BUDGET`` and refuses every other status. Exit codes: 0 pass, 1 refused,
2 usage.
"""

import argparse
import ast
import hashlib
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "mutation-baseline.txt"
CANDIDATE = ROOT / "mutation-baseline.candidate.txt"
STAMP = ROOT / "mutation-baseline.candidate.txt.checked"
MUTMUT = ROOT / ".venv" / "bin" / "mutmut"
#: What a mutation result is a statement about: the mutated source and the tests run against it.
#: A stamp binds a candidate to their digest, so a candidate checked against other code is refused.
SOURCES = (ROOT / "src" / "gps_sync", ROOT / "tests")
#: Test tiers the gate never runs (pytest_add_cli_args in pyproject.toml deselects them), so an edit
#: there cannot change a mutation result and must not cost a re-propose.
UNRUN_TIERS = ("integration", "e2e")

CATEGORIES = ("GENUINE-GAP", "EQUIVALENT", "BENIGN")
UNCLASSIFIED = "UNCLASSIFIED"
#: Modules in which a surviving mutant is never admitted: a defect there is a wrong coordinate and
#: no prior baseline exists to inherit a justification from.
ZERO_SURVIVOR_MODULES = ("gps_sync.telemetry.",)

_NAME = re.compile(r"^(?P<module>[\w.]+?)\.(?P<token>[^.]+)__mutmut_(?P<ordinal>\d+)$")
_ENTRY = re.compile(
    r"^(?P<name>\S+)(?:\s+(?P<diff>[0-9a-f]{12})\s+(?P<func>[0-9a-f]{12}))?"
    r"\s+#\s*(?P<category>[A-Z-]+)(?::\s*(?P<reason>.*))?$"
)


@dataclass(frozen=True, slots=True)
class Entry:
    """One baseline line; ``diff`` and ``func`` are None for a legacy name-only entry."""

    name: str
    diff: str | None
    func: str | None
    category: str
    reason: str

    def render(self) -> str:
        """Render the line in the current format."""
        identity = f"  {self.diff}  {self.func}" if self.diff and self.func else ""
        return f"{self.name}{identity}  # {self.category}: {self.reason}"


@dataclass(frozen=True, slots=True)
class Survivor:
    """A surviving mutant with its renumber-proof identity."""

    name: str
    diff: str
    func: str
    committed_func: str | None


_RESULT = re.compile(r"^\s+(?P<name>gps_sync\.\S+): (?P<status>[^:]+?)\s*$")
SURVIVED = "survived"
KILLED = "killed"
#: Non-survivor statuses the gate reads as a detection, within TIMEOUT_BUDGET: a mutant that hangs
#: the suite is noticed by it, but only under load, so the count is capped rather than trusted.
TREATED_AS_DETECTED = frozenset({"timeout"})
#: Statuses that decide nothing about the tests: the mutant was not run, was run to no verdict, or
#: was disposed of by something other than a test. Each fails the gate, as does any status not named
#: here or above.
NEVER_EVALUATED = frozenset(
    {
        "no tests",
        "not checked",
        "skipped",
        "check was interrupted by user",
        "suspicious",
        "segfault",
        "caught by type check",
    }
)
#: Timeouts tolerated per run; a quiet machine measured none, and a loaded one fourteen.
TIMEOUT_BUDGET = 25


def parse_results(text: str) -> dict[str, list[str]]:
    """Group the mutant names in ``mutmut results`` output by the status in the second column."""
    grouped: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = _RESULT.match(line)
        if match is not None:
            grouped.setdefault(match["status"], []).append(match["name"])
    return grouped


def parse_baseline(text: str) -> list[Entry]:
    """Parse baseline text, skipping comments and blank lines."""
    entries: list[Entry] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENTRY.match(line)
        if match is None:
            raise ValueError(
                f"baseline line {number} is not '<name> [<diff> <func>]  # CATEGORY: reason'"
            )
        entries.append(
            Entry(
                match["name"],
                match["diff"],
                match["func"],
                match["category"],
                match["reason"] or "",
            )
        )
    return entries


def header_of(text: str) -> list[str]:
    """Return the comment and blank lines that open a baseline, preserved on rewrite."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.strip() and not raw.lstrip().startswith("#"):
            break
        lines.append(raw)
    return lines


def locate(name: str) -> tuple[str, tuple[str, ...]]:
    """Return a mutant's source module path and the qualified function it mutates."""
    match = _NAME.match(name)
    if match is None:
        raise ValueError(f"not a mutant name: {name!r}")
    module = match["module"].split(".", 1)[1]
    token = match["token"]
    if token.startswith("xǁ"):
        _, cls, method = token.split("ǁ")
        return module, (cls, method)
    if not token.startswith("x_"):
        raise ValueError(f"unrecognised mutant token {token!r}")
    return module, (token[2:],)


def function_digest(source: str, qualified: tuple[str, ...]) -> str | None:
    """Digest a function's syntax tree, so formatting and comments never change an identity."""
    body: list[ast.stmt] = ast.parse(source).body
    node: ast.AST | None = None
    for part in qualified:
        node = next(
            (n for n in body if isinstance(n, ast.FunctionDef | ast.ClassDef) and n.name == part),
            None,
        )
        if node is None:
            return None
        body = getattr(node, "body", [])
    return hashlib.sha256(ast.dump(node).encode()).hexdigest()[:12] if node else None


def diff_digest(diff: str) -> str:
    """Digest only a mutant's changed lines, not the header or the numbering around them."""
    changed = [
        line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    return hashlib.sha256("\n".join(changed).encode()).hexdigest()[:12]


def _git_show(path: str) -> str | None:
    done = subprocess.run(
        ["git", "show", f"HEAD:{path}"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else None


def identify(names: Iterable[str]) -> list[Survivor]:
    """Compute each survivor's diff digest, working-tree function digest and committed digest."""
    survivors: list[Survivor] = []
    working: dict[str, str] = {}
    committed: dict[str, str | None] = {}
    for name in names:
        module, qualified = locate(name)
        relative = f"src/gps_sync/{module}.py"
        if relative not in working:
            working[relative] = (ROOT / relative).read_text()
            committed[relative] = _git_show(relative)
        shown = subprocess.run(
            [str(MUTMUT), "show", name], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        head = committed[relative]
        survivors.append(
            Survivor(
                name,
                diff_digest(shown),
                function_digest(working[relative], qualified) or "-" * 12,
                None if head is None else function_digest(head, qualified),
            )
        )
    return survivors


def _legacy_unverifiable(survivor: Survivor, baseline: Iterable[Entry]) -> Entry | None:
    """Return the legacy name-only entry of this name when no committed source can vouch for it."""
    return next(
        (
            entry
            for entry in baseline
            if not (entry.diff and entry.func)
            and entry.name == survivor.name
            and survivor.committed_func is None
        ),
        None,
    )


def admitted_by(survivor: Survivor, baseline: Iterable[Entry]) -> Entry | None:
    """Return the entry that admits a survivor, if any.

    A current-format entry admits by identity alone. A legacy name-only entry admits by name and
    only while the function it names is byte-for-byte what was committed, because a name is
    renumbered by any edit to its function and an inherited justification is then about other code.
    """
    for entry in baseline:
        if entry.diff and entry.func:
            if (entry.diff, entry.func) == (survivor.diff, survivor.func):
                return entry
        elif entry.name == survivor.name and survivor.committed_func == survivor.func:
            return entry
    return None


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of comparing survivors with a baseline."""

    refused: tuple[str, ...]
    stale: tuple[str, ...]


def _undetermined_refusals(undetermined: Mapping[str, str]) -> list[str]:
    refused = [
        f"{name}: status {status!r} means no test evaluated this mutant"
        for name, status in sorted(undetermined.items())
        if status not in TREATED_AS_DETECTED
    ]
    refused += [
        f"{name}: a mutant in this module must be killed by a test, not {status!r}"
        for name, status in sorted(undetermined.items())
        if name.startswith(ZERO_SURVIVOR_MODULES) and status in TREATED_AS_DETECTED
    ]
    timeouts = sum(1 for status in undetermined.values() if status in TREATED_AS_DETECTED)
    if timeouts > TIMEOUT_BUDGET:
        refused.append(
            f"{timeouts} mutants timed out, over the budget of {TIMEOUT_BUDGET}; "
            "rerun on a quiet machine"
        )
    return refused


def gate(
    survivors: list[Survivor], baseline: list[Entry], undetermined: Mapping[str, str] | None = None
) -> Verdict:
    """Fail on an unevaluated mutant, an unadmitted survivor or one in a zero-survivor module."""
    refused = _undetermined_refusals(undetermined or {})
    for survivor in survivors:
        entry = admitted_by(survivor, baseline)
        if survivor.name.startswith(ZERO_SURVIVOR_MODULES):
            refused.append(f"{survivor.name}: no survivor is admitted in this module")
        elif entry is None and _legacy_unverifiable(survivor, baseline) is not None:
            refused.append(
                f"{survivor.name}: its legacy name-only entry can be honoured only against the "
                "committed source, and none is readable here (no git history); run "
                "make mutants-propose to convert the baseline to the identity format"
            )
        elif entry is None:
            refused.append(f"{survivor.name}: not in the baseline")
        elif entry.category not in CATEGORIES:
            refused.append(f"{survivor.name}: category {entry.category!r} is not an admission")
        elif not entry.reason.strip() or entry.reason.strip() == UNCLASSIFIED:
            refused.append(f"{survivor.name}: no justification recorded")
    admitting = {id(admitted_by(s, baseline)) for s in survivors}
    stale = tuple(e.name for e in baseline if id(e) not in admitting)
    return Verdict(tuple(refused), stale)


def propose(
    survivors: list[Survivor],
    committed: list[Entry],
    previous: list[Entry],
    undetermined: Iterable[str] = (),
) -> tuple[list[Entry], list[str]]:
    """Build a candidate and the admissions problems that keep it from being promoted.

    A justification is carried forward from the committed baseline by identity, or from a prior
    candidate a person has edited. A survivor in a function whose digest is unchanged since the
    commit and which no prior entry admits is a regression, never a justification to write.
    """
    candidate: list[Entry] = []
    withheld = set(undetermined)
    problems = [
        f"{name}: status undetermined (neither killed nor survived); rerun on a quiet machine, "
        "no entry is written for it"
        for name in sorted(withheld)
    ]
    for survivor in survivors:
        if survivor.name in withheld:
            continue
        inherited = admitted_by(survivor, committed)
        edited = admitted_by(survivor, previous)
        source = inherited or edited
        if survivor.name.startswith(ZERO_SURVIVOR_MODULES):
            problems.append(f"{survivor.name}: no survivor is admitted in this module; kill it")
        if source is not None and source.category in CATEGORIES:
            candidate.append(
                Entry(survivor.name, survivor.diff, survivor.func, source.category, source.reason)
            )
            continue
        if survivor.committed_func == survivor.func:
            problems.append(
                f"{survivor.name}: a new survivor in a function unchanged since the commit; "
                "a test has regressed, kill it"
            )
        else:
            problems.append(
                f"{survivor.name}: needs a category and a justification written against the "
                "rewritten source, not inherited by name"
            )
        candidate.append(
            Entry(survivor.name, survivor.diff, survivor.func, UNCLASSIFIED, UNCLASSIFIED)
        )
    return candidate, problems


def render(entries: list[Entry], header: list[str]) -> str:
    """Render a baseline file: its header, then entries sorted by name."""
    body = [e.render() for e in sorted(entries, key=lambda e: e.name)]
    return "\n".join([*header, *body]) + "\n"


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _names(path: Path) -> list[str]:
    return sorted({line.strip() for line in path.read_text().splitlines() if line.strip()})


def _undetermined(path: Path) -> dict[str, str]:
    """Read ``<name><TAB><status>`` lines; a line with no status is read as one that fails."""
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        name, _, status = line.strip().partition("\t")
        if name:
            found[name] = status or "unknown"
    return found


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digest() -> str:
    """Digest every Python file the mutation result depends on, by relative path and content."""
    combined = hashlib.sha256()
    for root in SOURCES:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or any(
                part in UNRUN_TIERS for part in path.relative_to(root).parts[:-1]
            ):
                continue
            combined.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return combined.hexdigest()


def _stamp_text() -> str:
    """The stamp: the candidate's bytes and the source the check ran against."""
    return f"{_digest(CANDIDATE)}:{_source_digest()}\n"


def _candidate_problems(text: str) -> list[str]:
    """Return why a candidate may not replace the baseline, whatever its stamp says."""
    try:
        entries = parse_baseline(text)
    except ValueError as error:
        return [str(error)]
    problems = [
        f"{e.name}: category {e.category!r} is not an admission or has no justification"
        for e in entries
        if e.category not in CATEGORIES or not e.reason.strip() or e.reason.strip() == UNCLASSIFIED
    ]
    problems += [
        f"{e.name}: a legacy name-only entry cannot be promoted" for e in entries if not e.diff
    ]
    return problems


def main(argv: list[str], identifier: Callable[[Iterable[str]], list[Survivor]] = identify) -> int:
    """Run one mode; ``identifier`` is a seam so the decision logic is testable without mutmut."""
    parser = argparse.ArgumentParser(prog="mutation_baseline.py", description=__doc__)
    parser.add_argument("mode", choices=("partition", "gate", "propose", "promote"))
    parser.add_argument("--survivors", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--all-results", type=Path)
    parser.add_argument("--undetermined", type=Path)
    arguments = parser.parse_args(argv)
    committed_text = _read(BASELINE)
    if arguments.mode == "promote":
        return _promote()
    if arguments.mode == "partition":
        return _partition(
            arguments.results, arguments.all_results, arguments.survivors, arguments.undetermined
        )
    if arguments.survivors is None:
        print("error: --survivors is required", file=sys.stderr)
        return 2
    survivors = identifier(_names(arguments.survivors))
    undetermined = _undetermined(arguments.undetermined) if arguments.undetermined else {}
    committed = parse_baseline(committed_text)
    if arguments.mode == "gate":
        verdict = gate(survivors, committed, undetermined)
        for name, status in sorted(undetermined.items()):
            print(f"note: {status}, counted as detected within the timeout budget: {name}")
        for name in verdict.stale:
            print(f"note: baseline entry no longer survives: {name}")
        for line in verdict.refused:
            print(f"FAIL: {line}", file=sys.stderr)
        print("OK: no survivor outside the baseline" if not verdict.refused else "")
        return 1 if verdict.refused else 0
    previous = parse_baseline(_read(CANDIDATE))
    candidate, problems = propose(survivors, committed, previous, sorted(undetermined))
    CANDIDATE.write_text(render(candidate, header_of(committed_text)))
    STAMP.unlink(missing_ok=True)
    print(f"candidate written: {CANDIDATE.name} (the committed baseline is untouched)")
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        return 1
    STAMP.write_text(_stamp_text())
    print("admissions check passed; promote with: make mutants-promote")
    return 0


def _partition(
    results: Path | None,
    all_results: Path | None,
    survivors: Path | None,
    undetermined: Path | None,
) -> int:
    """Split ``mutmut results`` by status, and refuse a run that leaves a mutant unaccounted for."""
    if results is None or all_results is None or survivors is None or undetermined is None:
        print(
            "error: partition needs --results, --all-results, --survivors and --undetermined",
            file=sys.stderr,
        )
        return 2
    grouped = parse_results(results.read_text())
    everything = parse_results(all_results.read_text())
    survived = sorted(grouped.get(SURVIVED, []))
    other = sorted(
        (name, status) for status, names in grouped.items() if status != SURVIVED for name in names
    )
    survivors.write_text("".join(f"{name}\n" for name in survived))
    undetermined.write_text("".join(f"{name}\t{status}\n" for name, status in other))
    total = sum(len(names) for names in everything.values())
    killed = len(everything.get(KILLED, []))
    print(f"mutants: {total}")
    print(f"killed: {killed}")
    print(f"survived: {len(survived)}")
    for status in sorted(set(grouped) - {SURVIVED}):
        print(f"{status}: {len(grouped[status])}")
    if total == 0 or len(survived) + len(other) + killed != total:
        print(
            f"error: {len(survived)} survived + {len(other)} undetermined + {killed} killed "
            f"does not account for {total} mutants; the run is truncated or unreadable",
            file=sys.stderr,
        )
        return 1
    return 0


def _promote() -> int:
    if not CANDIDATE.exists() or not STAMP.exists():
        print(
            "error: no candidate has passed the admissions check; run mutants-propose",
            file=sys.stderr,
        )
        return 1
    if STAMP.read_text() != _stamp_text():
        print(
            "error: the candidate or the code it was checked against changed after its "
            "admissions check; run mutants-propose",
            file=sys.stderr,
        )
        return 1
    problems = _candidate_problems(CANDIDATE.read_text())
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    print(f"promoting {CANDIDATE.name} over {BASELINE.name}")
    diff = subprocess.run(
        ["diff", "-u", str(BASELINE), str(CANDIDATE)], capture_output=True, text=True, check=False
    )
    print(diff.stdout)
    BASELINE.write_text(CANDIDATE.read_text())
    CANDIDATE.unlink()
    STAMP.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
