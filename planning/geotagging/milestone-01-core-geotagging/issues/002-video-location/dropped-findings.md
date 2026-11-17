# Dropped Findings — 002-video-location

## 2026-11-17 Three integration pins assert interpreter behaviour, and seven exercise no 002 code
**Round:** /review-code-fix-loop, re-review 1
**Dropped by:** Codex
**Why:** Refuted by a peer reviewer, not by the orchestrator. The `statistics.fmean` pin at `tests/integration/test_real_video.py` is an *independent oracle* against production's `sum/len` in `telemetry._mean`, compared at `abs=1e-9` — which is what an oracle is for, not an assertion about the interpreter. `tests/unit/test_nonfinite_elevation.py` pins production functions throughout (`_mean`, `is_representable_elevation`, `match`). No pin asserting only interpreter behaviour could be found. The "seven exercise no 002 code" half was never actionable: the list lived in one reviewer's private numbering and the fix pass could not identify the members. Recorded as dropped although the fix pass had already replaced three pins on its strength; the replacements pin production code and are harmless, so they were kept. (Codex has never run on this issue — standing user direction — so this entry's attribution is the nearest category, the finding having been retracted rather than orchestrator-dropped.)

## 2026-11-17 §6 1183 and the EXIF-GPS-IFD-in-MOV half of §6 1185
**Round:** /review-code-fix-loop, re-review 1
**Dropped by:** orchestrator
**Why:** Both need two small real archive files (2.6 MB Android, 3.1 MB Apple re-wrap) committed as fixtures; neither can be derived by byte patch. 1183 is `Keys` precedence over a pre-existing `UserData` value; 1185's remaining half is the never-overwrite invariant over the EXIF GPS IFD storage place, where only `Keys` is represented today. Still believed live, and the derived Fujifilm fixture closed 1180, 1212 and most of 1186 without them.

## 2026-11-17 No real Apple re-wrap, Android or Canon fixture
**Round:** /review-code-fix-loop, re-review 1
**Dropped by:** orchestrator
**Why:** The derived stand-ins are all byte-patched from the committed DJI clip, so the "refused" and "admitted" clips in the brand-split tests are not real Apple or Android files. Measured mitigation: the production reader and classifier were run over all 200 real archive videos with no misclassification, and 34 DJI `.mov` re-wraps were correctly refused `TIME_SEMANTICS_UNVERIFIED` where guessing would have been wrong by 8 h 47 m. Still believed live as a regression risk — nothing in the suite would catch a dispatch change against those producers.

## 2026-11-17 The sample-population figures were not re-measured after the archive shrank
**Round:** /review-code-fix-loop, re-review 1
**Dropped by:** orchestrator
**Why:** The user deleted 18 clips, so every figure derived from the telemetry sample population is stale: 378,507 samples, the container-read and `-ee` timings, and every dispersion and altitude figure taken over the samples. Re-deriving them needs a ~377 s `-ee` sweep of the whole archive. The container-level counts were re-measured (200 / 96 / 115 / 42 / 41) and the sample-derived figures were instead annotated in `design.md` with their 2026-11-15 vintage and the population they were taken over. Recorded as dropped rather than closed: the annotations make the staleness visible but do not remove it.

## 2026-11-17 Advisory-only Low findings across three review rounds
**Round:** /review-code-fix-loop, initial review and re-review 1
**Dropped by:** orchestrator
**Why:** The loop's own rule fixes Lows carrying a concrete fix direction and skips advisory-only entries. Skipped under it: the three structurally-unreachable zero terms `_altitude_lines` prints on a bare stills run (accepted as totality over the enumeration); `_TOKEN_PUNCTUATION`'s five unexercised characters; `_resolve_members`' documented "in line order" being unobservable; `run_level_figures` computed twice with identical arguments (pure, so duplication only); the `"dry-run"` mode literal lacking a declared vocabulary; three remedies ending by telling the operator to report a diagnostic without naming where; and the telemetry chunk size being budgeted against memory where the real constraint is time and blast radius — measured 379 MB peak against a 256 MB text budget, 18.9 minutes per chunk, ~40 minutes of silence across the archive, all-or-nothing on a reconciliation failure. The last of these is the one most worth revisiting; it is a performance and observability gap, not a correctness one, and the phase precedes the intent record so a kill there writes nothing.
