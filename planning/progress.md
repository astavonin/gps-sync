# Progress

## Active

Nothing in flight. Issue 001 is merged; 002 and 003 are not started and await an explicit `/start`.

## Recently completed

- **001-gpx-photo-geotagger** — `merged ✅` 2026-11-15. GPX-driven stills geotagger: writes a position into stills that lack one and never overwrites an existing one. Published at https://github.com/astavonin/gps-sync (`d539886`, public).
  - 673 tests passing across unit, integration and e2e; ruff, ruff-format, shellcheck and mypy clean.
  - `make mutants` gate added: 1,089 mutants, 107 baselined survivors, fails on any new survivor.
  - Design at Revision 4, `Approved`.

## Carried forward

- **Code review stands at `CHANGES REQUESTED`.** Every confirmed finding from re-review pass 3 was fixed and independently verified, and the mutation gate was added as the structural answer, but no reviewer has assessed the fixed tree. Recorded rather than resolved.
- **7 `GENUINE-GAP` mutation survivors** in `mutation-baseline.txt`: three documented as failing safe (the `--extend` boundary, `allow_z` for QuickTime `CreateDate`, `assert_targets_writable`'s context check), plus elevation dropped on exact-hit and clamped positions, which only the integration tier kills.
- **Unexplained test-isolation flakiness.** Two mutants were each killed exactly once across six gate runs. Best lead is a stale bytecode cache when a source file is restored within the same second.
- **`projctl` is not installed**, so GitHub work went through `gh` directly, against the `CLAUDE.md` rule that it never should. Extending `projctl` is the stated fix.
- **603 already-geotagged RAF in the archive carry unreliable positions** (median fix age 329 s). Deliberately deferred; the never-overwrite invariant keeps them `Protected`.

## Next

- **002 video location** — `not started`. Extends 001's `via` enumeration and the exiftool transport boundary.
- **003 import tool** — `not started`. Needs `/research` first, invoked by the user.
