# Progress

**Last Updated:** 2026-11-17

## Active

- **002-video-location** — `implemented, reviews open 🔄` 2026-11-17. Committed and pushed to `feature/002-video-location` (70 files, +12,262/−436). `design.md` Revision 5, 1,341 lines, status `Draft`. 1,459 tests passing (1,312 unit + 106 integration + 41 e2e), 16 skipped; ruff, ruff-format, mypy and shellcheck clean; `make mutants` 1,944 mutants / 27 baselined survivors / exit 0. The issue folder holds six artefacts — `observed-failures.md` 12 entries all `covered`, `dropped-findings.md` 5 entries.
  - **Both review verdicts predate the current tree.** `design-review.md` is re-review 3's CHANGES REQUESTED against Revision 4 and has never seen Revision 5; `code-review.md` is re-review 1's and has never seen the fixes made after it. Every Critical, High and Medium finding in both was fixed and independently verified by measurement; no reviewer reassessed the result, so both markers stand as written. `design-review.md` carries a `Provenance of this file's status` section recording exactly what changed after its verdict.
  - Real-archive dry run: 486 `Protected` unchanged with and without `--video`; 50 planned targets (45 `.MP4` via `CENTROID`, 5 `.MOV` via `INTERPOLATED`), and all 50 independently confirmed to carry no existing position. No write run against the archive has been made.

## Recently completed

- **001-gpx-photo-geotagger** — `merged ✅` 2026-11-15. GPX-driven stills geotagger: writes a position into stills that lack one and never overwrites an existing one. Published at https://github.com/astavonin/gps-sync (`d539886`, public).
  - 673 tests passing across unit, integration and e2e; ruff, ruff-format, shellcheck and mypy clean.
  - Design at Revision 4, `Approved`. Code review stands at CHANGES REQUESTED — re-review pass 3's findings were all fixed and verified, but no reviewer assessed the fixed tree.

## Carried forward

- **`mutation-baseline.txt` was promoted to the identity format** during 002: 27 entries (17 BENIGN, 9 EQUIVALENT, 1 GENUINE-GAP, 0 telemetry), replacing 107 legacy name-only rows. Six of 001's seven genuine gaps are now killed by tests; `_refuse_or_clamp_outer__mutmut_15` survives and fails safe.
- **Unexplained test-isolation flakiness.** Two mutants were each killed exactly once across six gate runs. Best lead is a stale bytecode cache when a source file is restored within the same second.
- **`-overwrite_original` loses Finder tags, `kMDItemWhereFroms` and birth time** — it renames a temp file into place, so the inode changes. `-overwrite_original_in_place` preserves them but copies over the original, so an interruption damages the media. Declined for atomicity; disclosed in the README.
- **`projctl` is configured and working** (`~/.local/bin/projctl`, `projctl.yaml` → `platform: github`, repo `astavonin/gps-sync`). 001's GitHub work went through `gh` directly, against the `CLAUDE.md` rule, because a forced `PATH` made projctl read as absent. No cleanup needed; future GitHub work goes through projctl.
- **001 was run for real against the archive on 2026-11-15**, writing 323 RAF + 98 XMP in `2026/Issyk Kul`. Spot-checked: `DSCF8524.RAF` reads `42°05'29.76"N, 77°07'45.27"E`. No `_original` backups exist — `--keep-original` is opt-in, so those writes have no undo.
- **603 already-geotagged RAF in the archive carry unreliable positions** (median fix age 329 s). Deferred by decision; the never-overwrite invariant keeps them `Protected`.

## Next

- **002 video location** — implementation is pushed and no reviewer has seen it. The next action is the user's call: a fresh review round, a PR via `/mr`, or `/complete`. Not authorized to advance.
- **003 import tool** — `not started`. Needs `/research` first, invoked by the user.
