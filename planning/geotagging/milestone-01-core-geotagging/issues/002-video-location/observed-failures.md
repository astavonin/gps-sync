# Observed Failures — 002-video-location

## 2026-11-16 The re-gate overwrites a container position this tool cannot parse
**Observed in:** the run that reproduced it — a byte-patched `Keys:GPSCoordinates` payload (decimal comma, length preserved) on a scratchpad copy of the committed DJI clip, written through the shipped argfile
**Root cause:** `Composite:GPSPosition` is derived, so an unparseable atom emits no key at all; the gate read *provably absent* and the write proceeded. `1 image files updated`, exit 0, the pre-existing atom replaced.
**Status:** covered
**Test:** `tests/integration/test_container_location_pins.py::TestUnparseableAtomThroughTheGate::test_the_regate_leaves_an_unreadable_atom_byte_identical` (integration)
**Evidence:** before fix the file's md5 changed; after fix `1 files failed condition` and the atom is byte-identical. The extended condition still admits a positionless still and a positionless clip, so the shipped stills path is unaffected.

## 2026-11-16 An `abs()` ISO 6709 formatter mirrors a southern-western position
**Observed in:** the run that reproduced it — four quadrants written to `Keys:GPSCoordinates` on scratchpad copies and read back
**Root cause:** the atom has no reference member to carry a hemisphere, and §5 declared only the `abs(v):.9f` form used for the EXIF magnitudes, where `*Ref` carries the sign.
**Status:** covered
**Test:** `tests/integration/test_container_location_pins.py::TestSignedIso6709Emission::test_each_quadrant_round_trips_its_signs` (integration)
**Evidence:** mutating `iso6709` to `abs()` fails exactly three of the four quadrants — NE passes — with a max relative difference of 2.0 on a Sydney-shaped position. Signs round-trip verbatim through both the atom and `Composite:GPSPosition`.

## 2026-11-17 A Fujifilm MOV resolves six hours late
**Observed in:** the run that reproduced it — the derived Fujifilm fixture against a tree with the historical `file_type`-based dispatch restored
**Root cause:** `timeres.container_class` dispatched on `file_type`, so any MOV took the `QUICKTIME_UTC` branch and stamped the *local* `ExifIFD:CreateDate` as UTC.
**Status:** covered
**Test:** `tests/integration/test_real_fujifilm_mov.py::TestTheFujifilmDispatchAgainstTheRealBinary::test_the_resolved_instant_is_the_local_time_minus_its_offset_and_not_the_atom` (integration)
**Evidence:** 7 of 12 fail against the restored defect — the instant lands at 10:05Z where 04:05Z is correct, the clip goes `Unmatchable` instead of `Planned`, and exiftool's own `-geotag` oracle matches a file the tool refused. On the real archive all five planned Fujifilm MOV resolve to local minus `OffsetTimeOriginal`, matching hand computation.

## 2026-11-17 A space-refused target leaves the audit record with no conclusion
**Observed in:** the run that reproduced it — the orchestrator driven with `check_space` refusing one of three planned targets
**Root cause:** `write_complete` received the pre-check `planned` set while `result` covered only `writable`; the free-space stage was inserted between "planned" and "committed" without re-deriving its post-conditions.
**Status:** covered
**Test:** `tests/unit/test_space_stage.py::TestARunWhoseSpaceCheckRefusesSomeTargets::test_every_intended_path_leaves_the_audit_record_with_exactly_one_conclusion` (unit)
**Evidence:** before fix `intended ['A','B','C']` against `conclusions ['A','B']` — indistinguishable from an interrupted write. After fix `set(intended) == set(conclusions)`, and the pre-fix shape is now structurally unreachable because a dropped path raises `InternalConsistencyError`.

## 2026-11-17 An empty write set spawns exiftool, which prints its usage document into the block parser
**Observed in:** the run that reproduced it — `run_write` with an empty target list against the real binary
**Root cause:** the write phase was guarded on `planned` rather than `writable`, so when the space check refused every target exiftool received an empty argfile.
**Status:** covered
**Test:** `tests/unit/test_space_stage.py::TestARunWhoseSpaceCheckRefusesEverything::test_the_writer_is_never_spawned` (unit)
**Evidence:** before fix an empty argfile returned 160,213 bytes over 2,967 lines at exit 0, which `partition_stream` buffered and `_block_anomalies` turned into `BLOCK_COUNT_UNRECONCILED` — a fabricated exit 1 telling the operator to reconcile paths by hand, of which there were none. It forged no marker only because that text holds no line beginning `/`.

## 2026-11-17 The free-space probes abort the run after the intent record is flushed
**Observed in:** the run that reproduced it — `check_space` and `demand` over a `Planned` whose path no longer exists
**Root cause:** `space.py` used bare `os.stat` / `shutil.disk_usage`; `run()` catches only `ToolFailure`, so a vanished target raised `FileNotFoundError` past the handler.
**Status:** covered
**Test:** `tests/unit/test_space_stage.py::TestAVanishedTarget` (unit)
**Evidence:** before fix an uncaught `FileNotFoundError` left the audit record in `intent` phase — the state reserved for an interrupted write — although nothing was written, with a traceback and exit 1 colliding with `EXIT_FAILED_FILE`. After fix it is a per-file refusal that never denies the other targets.

## 2026-11-17 The mutation gate refuses on an unmodified tree
**Observed in:** the run that reproduced it — `scripts/mutants.sh` in a copy whose every file matched the digest manifest, under concurrent load
**Root cause:** the survivor extraction took field 1 of every `gps_sync.` status line and never read the status in field 2, so `mutmut`'s `: timeout` lines counted as unadmitted survivors.
**Status:** covered
**Test:** `tests/unit/test_mutation_tooling.py::TestTheGateReadsTheStatusColumn` (unit)
**Evidence:** `mutants: 1944, not killed: 42` with 14 `FAIL: … not in the baseline` lines for timed-out mutants, and no `OK:` line. Filtering on the status column gives 28 survivors and exit 0 on the same tree. The verdict had been load-dependent, so a figure reported from it was a statement about machine load.

## 2026-11-17 `space_demand` encodes a write run as "nothing planned"
**Observed in:** the run that reproduced it — a write run with four files written, compared against a dry run with nothing planned
**Root cause:** `orchestrator` gated `space_demand` on `not options.write`, and the gate sat on `RunInfo`, which both the report and the JSON record read.
**Status:** covered
**Test:** `tests/unit/test_video_run.py::TestTheSpaceForecastReachesEverySurface::test_a_write_runs_json_record_carries_the_same_forecast_as_the_dry_run` (unit)
**Evidence:** both facts encoded as `space_demand = []`, with a test pinning the second reading, so issue 003 could not distinguish "no targets" from "four targets, 53 MB peak, 174 MB retained". The figure existed on that run in the audit verdict only.

## 2026-11-17 A vanished target is refused pre-match, shrinking the coverage denominator
**Observed in:** the run that reproduced it — one tree in both modes, with a transport that unlinks a matched target during the read
**Root cause:** `check_space` refused it as `METADATA_INDETERMINATE`, which is absent from `FAILED_AFTER_MATCH`, so `_check_match_payload` forced the match payload to be dropped.
**Status:** covered
**Test:** `tests/unit/test_space_stage.py::TestAVanishedTarget::test_the_write_runs_coverage_equals_the_dry_runs` (unit)
**Evidence:** before fix the dry run reported `matched 2, reached_matcher 2, 100.0%` and the write run `matched 1, reached_matcher 1, 100.0%` — so nothing in the report said a matched candidate was lost. After fix, with the declared `WRITE_TARGET_UNEXAMINABLE` reason, the two coverages are equal and the conclusion keeps its `via` and provenance.

## 2026-11-17 An invented top-level record key passes the whole suite
**Observed in:** the run that reproduced it — `"invented": 1` added to `build_run_record`, full suite re-run
**Root cause:** the test named for the census property asserted variant counts and paths, never which renderer outputs moved.
**Status:** covered
**Test:** `tests/e2e/test_video_tree.py::TestTheCensusHoldsOnEveryRunShape` (e2e), with `TestABareRunOverStillsAndVideo`'s pair for the bare shape
**Evidence:** before the frozen key-set the whole suite stayed green on an invented key. The first fix closed the bare dry run only, and seven mutations conditioned on `telemetry_read.clips`, on `via == "CENTROID"` or on `mode != "dry-run"` stayed green — measured twice for the headline pair. Now the same symmetric-difference assertions run on a `--video` dry run and a bare `--write` run against frozen deltas, `CLIP_KEYPATHS` freezes the clip's own 19 key paths instead of excluding the clip, and report lines compare as a `Counter` so a duplicate is visible. Each of the five re-applied mutations fails its own shape's test.

## 2026-11-17 The mutation gate certifies a function with no tests
**Observed in:** the run that reproduced it — an uncovered helper appended to `writeset.py`, which is in `only_mutate`
**Root cause:** `_partition` buckets every status but `survived` into one "undetermined" class, and `gate` reports each as a non-blocking `note:`. Four of those statuses mean the mutant was never evaluated.
**Status:** covered
**Test:** `tests/unit/test_mutation_tooling.py::TestTheGateRefusesAnUndeterminedMutant` (unit)
**Evidence:** before fix, 6 `no tests` mutants produced `OK: no survivor outside the baseline`, exit 0 — under the previous extraction they would have been refused. Blanking a `.meta`'s `exit_code_by_key`, the state a Ctrl-C leaves, produced 185 `not checked` mutants in `gps_sync.telemetry` at exit 0, bypassing the `ZERO_SURVIVOR_MODULES` check NFR8 rests on. After fix the statuses are two declared sets — `TREATED_AS_DETECTED = {timeout}` within `TIMEOUT_BUDGET = 25`, and `NEVER_EVALUATED` holding the other seven — any unnamed status fails, and an undetermined name in a zero-survivor module fails whatever its status. Neutering `_undetermined_refusals` fails 12 tests.

## 2026-11-17 Swapping two arguments in the gate's last line makes it pass unconditionally
**Observed in:** the run that reproduced it — `--survivors` and `--undetermined` exchanged on `scripts/mutants.sh`'s final `exec`
**Root cause:** that line carries the whole decision wiring and no test executes it; its only assertion is `"awk" not in script`, and `shellcheck` cannot see an argument swap.
**Status:** covered
**Test:** `tests/unit/test_mutation_tooling.py::TestTheScriptWiresTheDecisionToItsExitCode` (unit)
**Evidence:** before fix the unit tier stayed green at 1,290 with the arguments exchanged — `gate` received zero survivors, printed every real survivor as a `note:` and exited 0 however many were unadmitted. The new tests execute the real script against canned `mutmut results` text with a stub `mutmut` and `python` on `PATH`, covering an unadmitted survivor, a killed-only run, a `no tests` mutant and an empty run; re-applying the swap fails two of them. The `exec` is also gone, so the `EXIT` trap fires and the temp directory no longer leaks — one test asserts `TMPDIR` is empty after the run.
