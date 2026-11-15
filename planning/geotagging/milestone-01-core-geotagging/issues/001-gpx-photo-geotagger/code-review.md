# Code Review

**Status:** APPROVED

**Subject:** gps-sync issue `001-gpx-photo-geotagger` — 28 modules in `src/gps_sync/`, against `design.md` Revision 4 (Approved)
**Assessment:** ✅ Approve — by user decision after the fixes, not by reviewer verdict

## Provenance of this status

**All four reviewers returned CHANGES REQUESTED.** That verdict was reached on the tree as it stood at 627 tests and is recorded unaltered below. This file reads `APPROVED` because of what followed, in this order:

1. Every confirmed finding was fixed — H8 and H9 and M33–M41 — each with a pinning test verified by applying the named mutation and watching it fail.
2. The structural cause behind three consecutive passes of High findings was closed at the root: `TagRecord` now has exactly one construction site, inside `from_json`, and an AST scan over `tests/` forbids any other. Seven pre-existing tests became load-bearing as a side effect.
3. A mutation gate was added as the standing answer to the class every pass had been finding by hand. On its first run it found two further defects no reviewer had — unpinned hemisphere refs for sub-unit coordinates, and `--keep-original` silently discardable — both since closed, along with four more in the attribution guard, the segment guard and the antimeridian threshold.
4. Verification at the time of approval: 673 tests passing across unit, integration and e2e; ruff, ruff-format, shellcheck and mypy clean; `make mutants` reporting no survivor outside its 107-entry baseline.
5. **No fifth review pass has assessed the fixed tree.** The user approved it with the findings and the remaining register in view.

So this status records a user decision taken on the evidence, not an independent re-assessment. Anyone reading this file for assurance should treat the Resolution section and the 7 `GENUINE-GAP` entries in `mutation-baseline.txt` as the live register of what is known to be untested.
**Codex:** ✗ not run — codex-flow is not installed on this machine
**Pass:** re-review 3 of `/review-code-fix-loop` (the fourth review of this code). Four reviewers: safety/security/performance, testability/correctness/standards, observability/maintainability/extendability/supportability, and the test-coverage pass. All four returned CHANGES REQUESTED independently.

**Per-reviewer outcome:** safety 0 High / 5 Medium / 6 Low · correctness 0 High / 6 Medium / 13 Low · supportability 1 High / 2 Medium / 9 Low · test-coverage 1 High / 2 Medium / 6 Low.

**Verification at review time:** ruff, `ruff format --check`, `mypy .` all clean; `pytest -q` → 627 passed, 1 skipped. Corpus 353 MB / 21 files, verified against `.baseline.md5` after every run.

**Method.** All four reviewers verified by mutation rather than by reading — 38, 23, 16 and 4 mutations respectively, each applied to an isolated copy with the full suite re-run. That is the method note that matters most in this file: **every High across all four review passes of this code was found by mutation, and none by reading.**

## The structural result

**The `records.py` derivation class is closed.** Three consecutive passes had each found exactly one unpinned derivation rule in that module, all invisible for one reason: the shared test builder constructed `TagRecord` with *derived* fields as parameters, so the entire unit pass bypassed `TagRecord.from_json`. Routing the builder through the parser closed it, and two reviewers confirmed independently:

- the `has_gps_key=bool(positions)` mutation now fails **15** tests where it previously failed none — and **7 of those are pre-existing classifier tests armed retroactively**, without being touched;
- all 60 builder call sites audited: none passes a derived field;
- `grep 'TagRecord('` across `tests/` finds **no direct construction anywhere**. One reviewer's phrasing: *"the class is closed by signature, not by accident."*

The safety reviewer attacked that module from four directions — presence flag, parseable selection, instance rank, blank primary — and every rule failed loudly. First pass of the four in which `records.py` came through clean.

## Findings (confirmed set)

### Critical
None. No reviewer found a path that writes a wrong position today, and the write path held under every mutation constructed against it.

### High

- **H8** [safety] **A blank `OffsetTimeOriginal` was silently replaced by `--utc-offset`, writing a position ~8 km wrong.** A regression introduced by the previous pass's `_first` change. `_text` → `_first` returned `None` for a blank primary, and `None` fell through to `utc_offset`, while a *malformed* offset was correctly refused — so a blank tag was treated as absent rather than unreadable. Measured: `:OffsetTimeOriginal=""` with `Copy1:OffsetTimeOriginal="+06:00"` under `--utc-offset +00:00` over a 7-hour track yielded `Planned` at lat 42.0721 where the true offset gives 42.0001 — a 6-hour instant error, and the file would be written. Reachable: on 13.55 a RAF accepts `OffsetTimeOriginal` in ExifIFD, IFD1, IFD0, SubIFD and MakerNotes, and with two instances **IFD1 becomes the unprefixed primary while ExifIFD becomes `Copy1:`**. Five of `_first`'s six consumers took the safe branch; this was the sixth, and the one that decides a written coordinate. *Raised Medium by its finder; recorded High under this review's rubric — silent wrong behaviour in a reachable case.*

- **H9** [safety / extendability] **The strict key-shape guard was one-sided.** `_INSTANCE_KEY`'s `(?P<name>.+)` matched colons, so a group name behind a copy prefix was swallowed into the tag name: `Copy1:Composite:GPSPosition` and `Copy1:Doc1:GPSPosition` were **accepted** with `has_gps_key=False` — a present position read as absent, the exact direction the function's own docstring says it refuses — while `Doc1:GPSPosition` was correctly refused. Executed against shipped code. Not reachable under `-G4` on 13.55, but `Copy1:ExifTool:Warning` is a shape exiftool **really emits** under `-G4:0`, and the five existing parametrised cases were all on the side of the guard that worked.

### Medium

- **M33** The corpus gate's empty-baseline guard had no discriminating test: deleting `if not rows: raise` left all 627 green, because its own `empty`/`blank` parametrisations reached their raise through the **extras walk** instead. Probed directly: an empty baseline in a directory holding only that baseline **passed clean** with the guard removed. *3 of 4 reviewers.*
- **M34** `README.md` stated "Real containers are not committed" — false since `.gitignore` was rewritten to track `testdata/` deliberately, and contradicting both `.gitignore`'s own header and §6. One reviewer traced the harm: this is the sentence a maintainer reads when the tripwire fires, and believing the containers absent leaves `GPS_SYNC_CORPUS_BASELINE=ignore` looking like the only move — which is how 12 drifted RAFs went unnoticed for a day. *3 of 4.*
- **M35** `design.md:815` described a committed corpus that never existed — "one HEIC, one JPEG … roughly 100 MB" against 6 RAF, 1 DNG, no HEIC, no JPEG, 353 MB. *4 of 4.*
- **M36** The e2e `.DS_Store` planting had no pinned effect: deleting `plant_ignored_fixtures(template)` left all 15 e2e tests green, because the corpus committed files at exactly the planted paths — 4-byte placeholders containing the literal text `junk`. So the test named "*are planted in every tree*" verified presence, never provenance. *2 of 4.*
- **M37** `MANIFEST.json`'s `files` array — the oracle §6 names as the independent justification for the e2e counts — had **zero read-sites**, was name-excluded from the baseline, and duplicated `EXPECTED` with nothing asserting agreement. *2 of 4.*
- **M38** The `raf_partial` fixture derivation was vacuous: deleting it entirely, making the fixture a byte copy of `assets.raf`, left all 67 integration and e2e tests green — on the state §6 calls "the one whose correct reading *creates* a write target". Its three sibling fixtures all self-verify; this one was the asymmetry.
- **M39** §5 and §6 recorded neither of the two read-contract rules added that session (the key-shape refusal and the blank-primary rule), so both were tested only incidentally — the same asymmetry a prior pass filed about instance selection.
- **M40** §6 asserted two incompatible things about HEIC: a committed HEIC in the corpus, and two derived HEIC fixtures, where **no real HEIC container is read by any test**.
- **M41** `RunInfo.review_problems` dead — no read-site, write-site or test. Raised in the first review, dropped as single-reviewer, re-raised here.

### Low
Thirteen items, each confirmed by two or more reviewers or mutation-verified: the key-shape abort message lacking a path, version and remedy; `Copy0:Tag` tying with the primary at rank 0 with dict order breaking the tie; `re.DOTALL` admitting `":Make\n"` as a distinct tag name; `test_the_numeric_rank_also_holds…` unable to fail under a lexical rank; the diagnostics-collection fixture using ranks where lexical and numeric agree; the blank-primary test asserting the field but not the outcome; `hashlib.md5` without `usedforsecurity=False` at one of three call sites; a no-op `dataclasses.replace`; a docstring saying "17 files" for a 19-file template; and §6:821's `Ignored` figure not saying which tree it describes.

## Resolution

**All confirmed findings above were fixed after this review, and independently verified.** No fifth review pass has assessed the result — the status marker above records pass 3's verdict, not an assessment of the current tree.

- **647 passed, 1 skipped** (from 627). ruff, `ruff format --check`, `mypy .` all clean.
- **H8** — `records.py` gained `_primary()`, preserving `""` as present-but-blank where `None` means absent; `timeres` refuses blank with *"OffsetTimeOriginal is present but blank; refusing to assume an offset"* **before** the `--utc-offset` fallback. Confirmed by probe: the field reads `''` and resolution returns `TimeFailure`. All five other `_first` consumers audited and shown safe.
- **H9** — the regex is now `(?:Copy(?P<copy>[1-9]\d*))?:(?P<name>\w+)` under `fullmatch` with no `DOTALL`. Confirmed by probe: `Copy1:Composite:GPSPosition`, `Copy1:ExifTool:Warning`, `Copy0:GPSPosition` and `":Make\n"` all refused, `Copy2:GPSPosition` still accepted. `\w+` rather than `[^:]+`, because `[^:]+` still admitted the trailing newline.
- **The class closed properly, not by practice.** An AST scan over `tests/` for `TagRecord(` and for `dataclasses.replace` touching a derived field found **one real violation** — `test_classifier.py:303` — which was rewritten to build through the parser. The scan was confirmed to fire on a planted probe.
- **M33–M41** all fixed, each with a mutation-verified pinning test. The `.DS_Store` pair was **deleted** from `testdata/` and `MANIFEST.json` so planting is now their only origin; `MANIFEST.json` was brought *into* the baseline, so the declared oracle is no longer the one artefact whose drift goes undetected; `EXPECTED` is now derived from the manifest.
- **Corpus:** 19 files, 353 MB, baseline 18 rows. A bare `testdata/` run yields `Ignored 3`; an e2e tree yields 5, because each plants its own pair — two trees that differ by construction, now stated as such.
- **Design:** Revision 4, 1160 lines, 21 sites across both amendments. §5 and §6 now carry both read-contract rules with test requirements; §6's corpus composition and weight are restated as measured; the HEIC gap is recorded open with the JPEG pair named as the substitute; four universally-quantified exit-2 absolutes were scoped to the "before any per-file outcome is *reported*" form that already existed at the contract's definition sites — three of which were **already false** for pre-existing conditions; and §3 is now dated as design-time context rather than rewritten, preserving the record of what the implementation started from.
- **A transient defect was caught in flight:** the key-shape refusal message briefly interpolated an undefined `SUPPORTED_VERSION`, a `NameError` that would have masked the intended `ToolFailure` and so the exit-2 abort itself. Fixed before it could be filed.

## Recommendation

The loop's own data is the recommendation. Across four passes the Highs went **5 → 1 → 1 → 2**, and every one was the previous fix's own unpinned surface — three of them in `records.py` for a single structural reason, now closed and verified closed. The remaining two were in that session's newest additions, and the fix passes had themselves become a defect source: H8 was introduced by a Low approved one pass earlier.

The common thread across all four passes is the actionable finding: **every High was found by mutation and none by reading.** Four reviewers independently adopted mutation testing because it is the only method that finds this class. So the structural answer is not a fifth review pass — it is to make mutation testing a gate the suite runs itself, scoped to the read/classify/match/write path where all four defects lived, with a recorded baseline so the pass condition is "no new survivor". That work is tracked separately from this review.
