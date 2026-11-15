# Goal — Geotagging

Give the photo and video archive accurate, auditable GPS metadata derived from recorded GPX tracks, without ever altering a position a camera already wrote.

## Milestones

| Milestone | Scope | State |
|---|---|---|
| `milestone-01-core-geotagging` | Stills, then video, then import with automated destination selection | in progress — 1 of 3 issues merged |

## Why this shape

The archive's positions come from three sources that cannot be treated alike. Fujifilm stills carry no position at all and are the geotagging target. DJI stills and video carry their own telemetry and must keep it. iPhone files carry a position from the phone's own fix. The one invariant that makes a single tool safe across all three is that presence of a position is decided from the file's own metadata and a present position is never overwritten — never from camera make, model, container or directory.

## Standing constraints

- **Never overwrite an existing position.** Enforced at the metadata gate, which is three-valued: present, provably absent, or indeterminate-and-refused. There is no filesystem-level backstop beneath it, because `-overwrite_original` writes through a read-only file.
- **Never fabricate a position.** Interpolation is refused across a track-segment boundary independently of `--max-gap`, after a prototype demonstrated max-gap alone producing a position 25 km from either track.
- **Dry run by default.** Writes happen only under an explicit flag, and classification is identical in both modes.
- **exiftool is a batched transport only.** gps-sync owns matching; `-geotag`, `-geosync` and the `Geo*` API options are prohibited in shipped code, with one permitted cross-implementation test.

## Deferred

Correcting the 603 already-geotagged RAF whose phone-derived positions are unreliable (median fix age 329 s, p90 2,625 s). The never-overwrite invariant keeps them `Protected`, so no milestone-01 issue may re-tag them; it needs an issue of its own with an explicit opt-in.
