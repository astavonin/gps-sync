# gps-sync

`gps-sync` writes GPS positions into still photographs, and with `--video` into MP4 and MOV clips, that carry none. A still's position comes from a GPX track; a clip that carries its own per-sample telemetry (a DJI drone clip) takes the centroid of that telemetry instead. It is built around one rule: **a position that is already present is never overwritten.**

It reads and writes photographs through [exiftool](https://exiftool.org) used strictly as a batched tag reader and writer. All matching (GPX parsing, merging, interpolation, gap limits) is done by `gps-sync` itself, which is what makes a dry run exact: the coordinates a dry run reports are the coordinates a write run applies.

Stills are the default. Video is opt-in with `--video`; without it no video extension is a candidate.

## Safety guarantees

- **Dry-run is the default.** Nothing in the media tree is modified unless `--write` is given.
- **Never overwrite a position.** The decision is made from the file's own metadata (`Composite:GPSPosition`), never from camera make, model, container or directory. A present position, `(0,0)` included, is protected. A file whose metadata could not be read cleanly is *refused*, not assumed empty. The check is repeated by exiftool immediately before each file is modified.
- **Never fabricate a position.** Interpolation happens only between two trackpoints of the same track segment, independently of `--max-gap`. Two activities recorded close together are never averaged into a place the camera never was.
- **Every write is recorded.** A write run writes an audit record under `runs/` before the first file is touched and completes it after the last, so an interrupted run is detectable.
- **Only containers verified byte for byte are written:** RAF and DNG with the EXIF GPS tags, and MP4 and MOV with one `Keys:GPSCoordinates` value, decided from the file's content and not its extension. Everything else is refused with an explanation. A positionless DNG from a DJI drone is refused in practice, so no such file can be written; see [A DJI DNG is refused whether or not exiftool has rewritten it](#a-dji-dng-is-refused-whether-or-not-exiftool-has-rewritten-it).

## Requirements

- Python 3.15 or newer, standard library only.
- exiftool 13.55 or newer (`brew install exiftool`). The binary is taken from `GPS_SYNC_EXIFTOOL`, then `PATH`, then `/opt/homebrew/bin/exiftool`. Message wordings and flag behaviour the tool parses were measured on 13.55, so the version is checked at start-up.

## Usage

```bash
# dry run (the default): reports everything, writes nothing
gps-sync --media <media-root> --tracks <gpx-root>

# machine-readable record instead of the human report
gps-sync --media <media-root> --tracks <gpx-root> --json > run.json

# recover near-misses by clamping to the nearest track endpoint, then apply
gps-sync --media <media-root> --tracks <gpx-root> --extend 90m
gps-sync --media <media-root> --tracks <gpx-root> --write --review-dir <dir>

# keep exiftool's _original copy beside every written file
gps-sync --media <media-root> --tracks <gpx-root> --write --keep-original

# also admit video: clips with telemetry need no tracks root at all
gps-sync --media <media-root> --video
gps-sync --media <media-root> --tracks <gpx-root> --video --write --review-dir <dir>
```

Install the `gps-sync` command onto your `PATH` with `pipx`, which gives it its own isolated environment:

```bash
make install            # pipx install --force .
make install-editable   # the installed command follows the working tree, for trying a change
make uninstall          # pipx uninstall gps-sync
```

Without installing, run `PYTHONPATH=src python -m gps_sync ...`.

### Options

| Flag | Argument | Default | Effect |
|---|---|---|---|
| `--media` | directory | required | Root of the media tree. Resolved once; a symlinked root is followed, symlinks found *inside* the walk are skipped and reported. |
| `--tracks` | directory | required without `--video` | Root of the GPX tree. Resolved the same way. Optional with `--video`, where omitting it declares an empty index: nothing is matched against a track. A root that is supplied and holds no GPX file is still a fatal error, so a mistyped path cannot pass for "no tracks". |
| `--video` | none | off | Admit `.mp4` and `.mov` as candidates. Absent means no video extension is a candidate and the run is a stills run. |
| `--write` | none | off | Apply writes. Absent means dry-run. |
| `--max-gap` | duration, zero or more | `120s` | Maximum width of a bracketing interval *within one segment*, inclusive. |
| `--extend` | duration, zero or more | `0s` | Clamp window for a gap interpolation refused. `0s` disables extension. |
| `--time-shift` | signed duration | `+0s` | Added to each resolved UTC instant before matching. Never inferred. |
| `--utc-offset` | `±HH:MM` | none | Offset for local-time stills that carry no `OffsetTimeOriginal`, and nothing else. It is never consulted for a video container. |
| `--review-dir` | directory | `~/gps-sync/<media>-<digest>` | Parent of `_unprocessed/` and `runs/`. May not be, or contain, the media root; may lie inside it. |
| `--keep-original` | none | off | Retain exiftool's `_original` copy. |
| `--json` | none | off | Emit the JSON record on stdout *instead of* the report. |
| `--verbose` | none | off | Diagnostic trace on stderr; retains the argfiles and prints their paths. |

**Durations** are a decimal number followed by `s`, `m` or `h`; a bare number is seconds. `90m`, `5400s` and `5400` are the same value. `1h30m`, `120 s` and `2M` are errors. A leading `+` or `-` is accepted only by `--time-shift` (write a negative value as `--time-shift=-60s` or `--time-shift -60s`).

### The three parameters that are gps-sync's own

These differ in kind from exiftool's similarly named options. A user comparing output against `exiftool -geotag` must not treat them as the same quantity.

- **`--max-gap` is the width of the bracketing interval within one segment**: the elapsed time between the two trackpoints surrounding the photo, not the distance from the photo to the nearer point. A bracket exactly equal to `--max-gap` is accepted. It is not exiftool's `GeoMaxIntSecs`.
- **`--extend D` clamps to the nearer endpoint of a refused gap** (the earlier endpoint on an exact tie), within `D` of that endpoint. It applies to any gap interpolation refused for a distance reason: an over-wide bracket, a segment boundary, or an instant outside the track's outer ends. It never extrapolates heading or speed, and an extended result is reported as `via EXTENDED` and counted separately from interpolated ones. It is not exiftool's `GeoMaxExtSecs`.
- **`--time-shift` is added to the file's resolved UTC instant to obtain the instant used for matching**: `matching_instant = resolved_instant + shift`. A camera whose clock ran *slow* (it stamped `12:00:00` for an exposure made at true `12:01:00`) is corrected with `--time-shift +60s`; a fast clock with a negative value. An inverted sign does not fail, it doubles the error, so check it. It is not exiftool's `-geosync`, and it is never auto-detected.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every file reached a correct terminal outcome (`Unmatchable` and `Protected` included), the backend concluded about nothing outside the intended write set, and every artifact the run promised was completed. A re-run in which everything is already tagged exits 0. |
| 1 | At least one file terminated `Failed`, or the backend concluded about a path the run did not intend to write. |
| 2 | A tool-level failure before any per-file outcome: bad arguments, exiftool missing or too old, unusable destination, unparseable GPX, unreadable roots, a candidate the backend returned no record for. |
| 3 | Per-file work completed and was reported, but a run-level artifact is missing or incomplete (audit record, review folder, or stdout write). |

When several apply the highest of 0, 1, 3 wins. A dry run that exits 1 tells you the write run will too; the converse does not hold, because the backend can still fail on a file, be interrupted, or find a position that appeared since the read.

### Streams

stdout carries the primary output: the human report by default, pure JSON under `--json`. stderr carries diagnostics in both modes: warnings (such as overlapping track segments), errors and the `--verbose` trace.

## What the report tells you

Counts per outcome and per reason, a coverage rate, the value of the parameter that would change each refusal, and a clock-sensitivity line.

- `SEGMENT_BOUNDARY` (the camera was used between two activities) and `MAX_GAP_EXCEEDED` (a dropout inside one activity) are never summed, because they need different responses. Raising `--max-gap` does not help a boundary file.
- **Coverage** is matched files over files that reached the matcher. Already-tagged files and files with no usable time are outside both terms. When nothing reached the matcher (an idempotent re-run) the rate is reported as not applicable, which is the healthy reading.
- **Clock sensitivity** is the median speed at the matched photo instants and the displacement a 60 s clock error would cause at that speed. It is re-measured on every run because the exposure depends entirely on how you moved: a vehicle at 60 km/h is roughly 40 times a walking pace.
- **Free space** (a dry run only) prints `Free space a write needs on volume N: P bytes at peak ...; with --keep-original R bytes accumulate`, one line per filesystem holding a planned target. `N` is the operating system's device number for that filesystem (`st_dev`), not a mount name. `P` is the largest single planned target plus 1 MiB, the figure a write checks per file; `R` is the sum of every planned target on that volume, the figure `--keep-original` turns the requirement into. Both are in bytes: divide by 1,000,000,000 for the decimal GB the sections above use. The same figures are the `space_demand` list of the JSON record, which a write run carries too, and of the audit record's verdict.
- **Telemetry read** (`Telemetry read: N clip(s) in C chunk(s), S s`) appears only when a clip was read for its own telemetry, and is the JSON record's `telemetry_read` object (`clips`, `chunks`, `elapsed_s`, all zero on a run that read none).
- **Altitude not available** carries a breakout over `NONE_USABLE`, `NOT_CO_INDEXED`, `NOT_REPRESENTABLE` and `GPX_PATH_NO_ELEVATION`; the JSON record's `summary.altitude_unavailable` is an object with `total` and `breakdown`.
- **Telemetry centroid dispersion** gives the count and the minimum, median and maximum distance of the farthest used fix from the written position, over the clips positioned from their own telemetry. The JSON record always carries it as `summary.centroid_dispersion`, with `null` figures when there were none.
- **Run-level diagnostics** (`Run-level diagnostic: ...`) name a condition that belongs to the run rather than to a file: no tracks root was given, an elevation was dropped as unrepresentable, the backend concluded about a path the run did not intend, or the block counts did not reconcile. The JSON record's `run_level` object carries one count per condition that fired and is `{}` otherwise, so a consumer that reads only JSON sees them.

Files that were not geotagged are linked under `_unprocessed/<reason>/<path relative to the media root>` so they open directly in Capture One. The folder is rebuilt on every run, including dry runs, and only over entries gps-sync itself created: it will refuse a folder that holds anything else, a marker naming a different media root, or a marker version it does not recognise.

### A DJI DNG is refused whether or not exiftool has rewritten it

A DNG from a DJI drone carries two minor warnings on every read: `Possibly incorrect maker notes offsets`, which is on the allowlist, and `Not decoding some large array(s)`, which is not. The second comes from the `ProfileHueSatMapData` arrays every camera original from this drone carries, so it is present in the file as it left the camera and no rewrite is needed to reach it. A positionless DJI DNG is therefore refused as `UNRECOGNISED_MINOR_WARNING` and linked under `_unprocessed/unrecognised-warning/`, even though its metadata read correctly, and no option and no action on the file changes that outcome. The refusal is deliberate: the allowlist grows only on measured evidence, and widening it would let an unrelated minor warning stand in front of a file with no position.

The consequence is that no DJI DNG from this drone can be written at all. The DNG write path is verified byte for byte, but it is reachable only for a DNG that does not carry those arrays, which in this project means a synthetic test fixture; the guarantee under [Safety guarantees](#safety-guarantees) that RAF and DNG containers are written is qualified accordingly.

## Run artifacts

Everything gps-sync writes lives under one review root (`--review-dir`, by default `~/gps-sync/<media-basename>-<digest>/`):

- `_unprocessed/` is rebuilt per run.
- `runs/<UTC timestamp>-<pid>.json` is the audit record, written on write runs only and never deleted by the tool. A record still in its `intent` phase means a write process was started and did not finish: some of the paths it lists may now carry new positions, and the next run names it before anything else.

A dry run writes no audit record. The review root may lie inside the media tree; discovery skips `_unprocessed/` and `runs/` wherever they sit. A review root that is, or contains, the media root, and `/` or `$HOME` themselves, are refused.

## What a write changes

Only the EXIF GPS tags are written: `GPSLatitude`, `GPSLatitudeRef`, `GPSLongitude`, `GPSLongitudeRef`, `GPSAltitude`, `GPSAltitudeRef`, `GPSDateStamp`, `GPSTimeStamp`. When elevation cannot be derived (a GPX without `<ele>`, or a clamp onto a point without one) the two altitude tags are omitted rather than written as zero. exiftool itself adds `GPSVersionID` when it creates a GPS block. No XMP GPS properties are written, so a tool that reads only XMP will not see the position.

### Byte-level changes per container

Measured on the test fixtures with exiftool 13.55:

- **RAF**: the file shrinks by about 52 KB (52,968 bytes on the design's reference file, 52,448 bytes on this repository's test fixture) because exiftool drops the padding inside the embedded preview's EXIF block. The RAW payload at the end of the file (49,103,287 bytes on the fixture), the preview's compressed scan and every pre-existing tag are preserved, and the 8 GPS tags are added.
- **DNG**: only the header and metadata region changes (-2,674 bytes on the design's reference file); the trailing payload, the embedded preview and every pre-existing tag are preserved.

### `-overwrite_original` and filesystem attributes

By default gps-sync writes with `-overwrite_original`: exiftool assembles a new file beside the original and renames it into place. An interruption therefore leaves either the old file or the new one, never a half-written photograph. The cost is that the new file is a *different file* with the same name. Measured on macOS:

- Preserved: POSIX mode and ownership.
- **Not preserved:** extended attributes (including Finder tags), the creation date, and the inode (also `kMDItemWhereFroms`). This holds for a video write exactly as for a still: measured on a written clip, only `com.apple.provenance` remained. It is a property of the rename-over write, not of video, and it is not restored afterwards.

`--keep-original` keeps the prior file as `<name>_original`. That copy keeps its own inode and everything attached to it, which makes it the mitigation if your attributes matter. It doubles the footprint of a RAW tree. The `_original` name is never a candidate on a later run.

The alternative, `-overwrite_original_in_place`, preserves attributes by copying a temporary file's contents over the original, so an interruption during the copy damages the photograph itself. gps-sync declines it: losing a photograph is worse than losing a Finder tag.

### A read-only file is not protected

Making a file read-only does **not** stop a write. exiftool writes a temporary file in the containing directory and renames it over the target, so only the *directory's* writability matters, never the file's mode bits (measured: a `chmod 444` target was written and kept its mode). The never-overwrite guarantee rests entirely on gps-sync's metadata check; there is no filesystem backstop beneath it. `--keep-original` is the only mechanism that retains the prior bytes.

### Hard links

Candidates are deduplicated by device and inode, so two names for one file are processed once and the second is reported as a duplicate. A rename-based write replaces the written name with a new file, so **every other hard link continues to point at the pre-write content**. The report says so whenever a duplicate was skipped.

### Exclusive access

gps-sync assumes nothing else modifies the media tree during a run. Classification finishes before any byte is written, so a decision is acted on tens of seconds after it was made. If another program (Capture One, a second gps-sync) gives a file a position in that window, exiftool's per-file re-check refuses the write and the file is reported as `appeared-mid-run`. That bounds the damage; it is not coordination. Do not run two instances over one tree.

### A write is not undoable

The audit record names what changed, which makes a run reversible by hand, but nothing in the tool performs the reversal.

## Existing positions are preserved, even bad ones

`Protected` makes no claim about a position's *quality*. gps-sync preserves existing positions unconditionally, including ones measurably worse than the track would compute. On the reference archive 603 camera-geotagged frames carry positions at a median fix age of 329 s (p90 2,625 s, maximum 2.0 h), and 80 of them share one coordinate across 4.3 hours. They all land in `already-tagged/` and nothing distinguishes them from well-tagged files. The tool is not licensed to decide an existing position is wrong; use the fix age or the absence of `GPSDateTime` in the file itself if you need to judge.

## Video

`--video` admits MP4 and MOV containers to the same run, the same report, the same audit record and the same exit code as stills. Each clip takes one of two sources, chosen by the file's own structure: a clip with a `meta` handler track (all 109 DJI MP4 in the reference archive) takes the **centroid of its own telemetry**, and is never matched against the GPX track; every other clip is matched against the track at its start instant, as a still is. The track is not a fallback: a clip whose telemetry yields no usable fix is refused, because the pilot's position is a median 1,035 m and up to 4,620 m from the drone's.

- **A video write rewrites the whole file.** The media payload is relocated and a full-size temporary file is created in the target's own directory, for a change of 99 to 164 bytes. A write therefore needs free space equal to the clip's own size, plus 1 MiB, on the clip's own volume. A clip the volume cannot hold is refused as `WRITE_INSUFFICIENT_SPACE` and the others proceed. A dry run reports the peak a single write needs per volume; `--keep-original` keeps every `_original`, so across the DJI set it would add 78.80 GB.
- **The position is written to `Keys:GPSCoordinates` as an ISO 6709 value**, signed, with nine decimals and an altitude of four. Nothing is written to `UserData`, `ItemList` or XMP, and the instant is reported and audited but not written into the clip.
- **A written altitude on a drone clip is the aircraft's altitude**, not the scene's.
- **A container whose time semantics this project has not verified is refused rather than guessed at** (`TIME_SEMANTICS_UNVERIFIED`). A time that carries an EXIF offset outranks any container atom; a DJI or Android atom is read as UTC only under the `isom` and `mp42` brands; a classic QuickTime atom is not, because the one file that can rule on it is local. `--utc-offset` never stands in.
- **A clip whose telemetry scan covers less than its own duration is refused** (`TELEMETRY_INCONSISTENT`) rather than positioned from the part that was read. The ceiling is one second.
- **An elevation the file format cannot hold is dropped, and the position is written without it.** The backend neither refuses nor warns above 4,294,967,295 m or for a non-finite value, so the tool decides before the write; the run reports how many elevations were dropped.
- **A video already carrying a container position this tool cannot read is refused, not written.** A position written by another tool in a form this one does not parse is never overwritten: its presence is established from the atom itself, not from the derived `Composite:GPSPosition`, which is simply absent for such a payload.
- **When the backend refuses one assignment of a multi-assignment write, the target has still been rewritten.** `WRITE_PARTIAL` means the position itself was refused and the file does not carry what the run planned (a refused hemisphere reference leaves a position that reads as northern or eastern, which the next run reports as already positioned and never retries). `WRITE_INCOMPLETE` means the position was applied and another assignment was refused. Neither is reachable from user data; each is a defect in this tool and the quoted diagnostic is what to report.
- **A hidden entry is skipped on every run, including a bare one.** A file or a directory whose name begins with a dot, with its whole subtree, is reported as `HIDDEN_ENTRY` and never walked, so a deliberately hidden photograph is no longer geotagged. A hidden candidate that 001 classified is now an `Ignored` row.
- **Stated limits.** A clip matched against the track is matched at one instant, which is sound for a clip shot standing still and not for one that moves; the displacement on a moving clip is its own travel and nothing here bounds it. A clip with both telemetry and an EXIF GPS position cannot be told apart from its samples by the second read, which is why the container read, which does see it, runs first.

## Time

A still's instant comes from `DateTimeOriginal` with `OffsetTimeOriginal`. `CreateDate` is never used for a still, and `GPSDateTime` is never read as an instant source (it records when a paired phone got a fix, not when the shutter fired). A local-time still with no offset tag needs `--utc-offset`; without it the file fails loudly rather than being assigned a zone.

## Development

```bash
make lint         # ruff check + ruff format --check
make typecheck    # mypy .
make test         # unit, then integration, then e2e
make all          # lint -> typecheck -> test
make mutants      # mutation gate over the read/classify/match/write path (not part of `make all`)
```

All commands go through `.venv/bin/*`. The suite has three levels:

- **unit** (`tests/unit`): no exiftool process. Pure matcher and classifier tests, the orchestrator against a substituted transport, and filesystem-level discovery and review-folder tests.
- **integration** (`tests/integration`, `-m integration`): the real exiftool and real RAF/DNG containers.
- **e2e** (`tests/e2e`, `-m e2e`): the whole CLI as a subprocess over a copied, reference-shaped tree.

The fixture corpus is committed and pinned: `testdata/` holds real camera originals (353 MB across 19 files), because the byte-level, tag-set, preview-digest and attribute assertions are statements about a real container and do not hold on a truncated stand-in (design §6's fixture rule). Tests copy files from `testdata/` into their own temporary directories (and never write to `testdata/`), building every GPS-stripped, truncated, renamed or otherwise derived fixture from those copies. The suite skips only when `testdata/` has been removed locally or exiftool is absent; a drifted or missing file is restored from the repository, not skipped past. `testdata/` is pinned by `testdata/.baseline.md5`: any test that derives files from it fails when a fixture has drifted, gone missing, or when an unrecorded file has appeared, naming each one. Set `GPS_SYNC_CORPUS_BASELINE=ignore` to run against a deliberately modified corpus. The coverage regression over the real photo archive runs only when `GPS_SYNC_REFERENCE_ARCHIVE=1` is set and the volume is mounted.

### Mutation gate

`make mutants` is the check that the suite itself can fail. It runs [mutmut](https://github.com/boxed/mutmut) (declared as the `dev` extra in `pyproject.toml`; install with `.venv/bin/pip install mutmut==3.8.1`, since the project itself is deliberately not installed into the venv; the package has no runtime dependencies) over the path where a defect becomes a wrong coordinate or an overwritten position: `records.py`, `classifier.py`, `matcher.py`, `timeres.py`, `writer.py`, `exitcode.py`, `telemetry.py` and `writeset.py`. The reporting and serialisation modules (`report.py`, `serialise.py`, `runrecord.py`, `summary.py`) are deliberately out of scope: a wrong label is not a wrong coordinate. The scope and its rationale live in `[tool.mutmut]` in `pyproject.toml`.

- **What it does.** `scripts/mutants.sh` starts from a clean `mutants/` working copy, generates about 1,900 mutants (one small source change each) and runs the **unit selector only** against each, with mutmut choosing just the tests that cover the mutated function. The integration and e2e tiers are not part of the gate because they spawn real exiftool and would multiply the run by the mutant count. It takes about 45 seconds of wall time on a 14-core machine.
- **Pass condition.** Every mutant `mutmut results` lists is placed in one of three classes by its status, and the gate fails on the first or the third. *Survived*: no test notices the change; it must be admitted by `mutation-baseline.txt` (below), and none is admitted in `telemetry.py`. *Treated as detected*: `timeout`, a mutant that hangs the suite; it passes with a `note:` line per name, up to a budget of 25 per run (`TIMEOUT_BUDGET`), because a timeout is load-dependent and a mutant killed on a quiet machine can time out on a busy one; one in `telemetry.py` fails whatever the budget. *Never evaluated*: `no tests`, `not checked`, `skipped`, `check was interrupted by user`, `suspicious`, `segfault`, `caught by type check`, and any status the script does not know; each fails with a `FAIL:` line naming the mutant, because no test ever judged it (an uncovered helper in `writeset.py`, or an interrupted run, produces exactly these). The run also fails unless `survived + undetermined + killed` equals the total `mutmut` reports and the total is above zero, so a truncated run is loud. A survivor is admitted by a baseline entry carrying a category and a reason: `GENUINE-GAP` (a behaviour no test pins; write the test, then delete the line), `EQUIVALENT` (the mutant behaves identically) or `BENIGN` (only a report label or a fail-safe edge changes). An entry whose category is missing or `UNCLASSIFIED` fails the gate. Baseline entries that no longer survive are listed as stale and do not fail the run.
- **Entry identity.** An entry is `<name>  <diff digest>  <function digest>  # CATEGORY: reason`. The digests make an entry renumber-proof and make an edit visible: a justification is inherited only by an identical mutant of an identical function, never by name. A legacy name-only entry is honoured only while its function is byte for byte what was committed.
- **Reading a failure.** The run prints the new survivors by name. `.venv/bin/mutmut show <name>` prints the exact diff, and `.venv/bin/mutmut tests-for-mutant <name>` lists the tests that ran against it. Either write a test that fails on the mutant, or accept it with a reason (next point).
- **Accepting a survivor.** Run `make mutants-propose`. It writes a *candidate* baseline to `mutation-baseline.candidate.txt` and never touches `mutation-baseline.txt`; it carries justifications forward by identity, marks the rest `UNCLASSIFIED`, and exits non-zero while any entry lacks a justification written against the current source, or any survivor sits in a function unchanged since the commit (which is a regression, not something to justify). Edit the candidate, re-run `make mutants-propose` until it passes, then `make mutants-promote` replaces the committed baseline and prints the diff. Promotion is refused unless the candidate passed the check and has not changed since. There is deliberately no target that rewrites the baseline in place.
- **Noise.** A mutant that only rarely survives can flip between killed and survived from run to run; re-run before concluding anything about a single line.
- **Safety.** The run works in `mutants/` (git-ignored), links `testdata/` into it rather than copying 353 MB, and fails if any file under `testdata/` changes during the run.

