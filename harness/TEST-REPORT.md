# Harness test report — gps-sync photo geotagging

Run 2026-11-14 against real copied data. Purpose: validate the Phase 2 design before Phase 4 implementation, with particular attention to the cases where a defect destroys data rather than merely failing.

## Safety posture

Every test ran on copies under `testdata/`. The 26 source files in `/Volumes/Data SSD 2TB/PhotoArchive/2026/Issyk Kul` and `/Users/astavonin/Downloads/tracks` were MD5-hashed before the fixture was built and re-hashed after every test phase. All 26 are byte-identical to the pre-test snapshot.

## Fixture

`testdata/media` — 23 files in four nested subdirectories, exercising recursion. `testdata/tracks` — all 7 Garmin GPX files. Total 1.0 GB, untracked by git; `testdata/MANIFEST.json` records the expected outcome for every file and is reproducible from the data.

| Case | Files | Purpose |
|---|---|---|
| Multi-track, different images | 8 RAF across 4 tracks (2 each) | several tracks covering different images |
| Single track, multiple images | 4 RAF, all Shatyly Panorama | one track covering many images |
| Already tagged | 3 DJI DNG | must never be overwritten |
| Outside coverage | 2 RAF (50.6 min before, 62.5 min after) | legitimate skip |
| Non-media / out of scope | 2 `.DS_Store`, 1 `.xmp`, 1 `.txt`, 1 MP4, 1 MOV | must be ignored silently |

Expected totals, derived independently from track time-window containment rather than from the matcher: Matched 12, Protected 3, Unmatchable 2, Ignored 6.

## Results

| # | Check | Result |
|---|---|---|
| 1 | Dry-run outcomes versus the independent manifest | 23/23 correct outcome kind, correct owning track, position inside that track's bounding box |
| 2 | Independent oracle — exiftool's own `-geotag` | agreement on all 12 matched files to **0.00 cm**; both refused the same 2 out-of-track files |
| 3 | Dry-run is non-destructive | all 30 fixture files byte-identical |
| 4 | Write touches exactly the intended files | 12 changed; 3 already-tagged DNG, 6 ignored, 2 unmatchable and all 7 GPX byte-identical |
| 5 | Written positions equal the dry-run plan | max delta 0.060 cm (coordinate formatting only) |
| 6 | RAW sensor payload survives the write | 88.7–92.2 % of each file byte-identical as a trailing run; differences confined to the header/preview region |
| 7 | Idempotency | second write run: Protected=15, zero bytes changed — the invariant makes the tool self-protecting |
| 8 | Review folder | correct reason buckets; symlinks resolve and read through to real files |
| 9 | Review folder inside the scanned tree | identical counts across two passes. For contrast, `exiftool -r` reported **16 RAF for 14 real files**, having followed the symlinks |
| 10 | `--extend` | both near-misses recovered, tagged `EXTENDED` and so distinguishable from `INTERPOLATED` |
| 11 | Originals untouched | all 26 byte-identical to the pre-test snapshot |

## The defect this testing found

The design relied on `--max-gap` alone to reject photos outside track coverage. That is insufficient for multi-track input.

Merging several GPX files into one time-ordered index means a photo falling between two tracks is bracketed by the last point of one and the first point of the next. If that inter-track gap is shorter than max-gap, interpolation averages two unrelated activities.

Demonstrated with two synthetic tracks 60 s apart in time and 50.0 km apart in space, and a photo at the centre of the gap. The 60 s bracket passes the 120 s default, so max-gap accepts it. The resulting position was 42.2250050, 77.0000000 — **25.0 km from the end of one track and 25.0 km from the start of the other**, a fabricated location that satisfies every constraint the original design specified.

Fix, implemented and verified: every trackpoint carries a segment identity (file + `<trk>` index + `<trkseg>` index), and interpolation is refused when the bracketing points belong to different segments, independently of max-gap. The photo is then correctly reported `Unmatchable`, reason "between track segments".

This was invisible to the Phase 1 measurements because on this archive the maximum intra-track gap is 65 s and the minimum inter-track gap is 1.29 h — a 71x separation, so the distributions never overlap and max-gap alone is accidentally sufficient. It breaks as soon as a watch is paused and restarted, or two activities are recorded close together in one day.

## Reproducing

```bash
# dry-run (default; writes nothing)
python3 harness/gps_sync_proto.py --media testdata/media --tracks testdata/tracks

# apply, with a review folder for everything not geotagged
python3 harness/gps_sync_proto.py --media testdata/media --tracks testdata/tracks \
  --write --review-dir testdata/_unprocessed

# recover near-misses by snapping to the nearest track endpoint
python3 harness/gps_sync_proto.py --media testdata/media --tracks testdata/tracks --extend 5400
```

Fixture media is modified in place by `--write`. To reset, re-copy from the source archive using `testdata/MANIFEST.json` as the file list.

## Status

`harness/gps_sync_proto.py` is a design-validation prototype, not the deliverable. The production implementation belongs to Phase 4, after design review. The prototype's value is the evidence above and the segment-guard defect it surfaced.
