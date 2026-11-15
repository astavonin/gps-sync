"""Shape B prototype for gps-sync — a design-validation harness, not the deliverable.

Exists to prove the Phase 2 design on real data before Phase 4 implementation. It owns
discovery, the GPS gate, GPX parsing, matching and reporting; exiftool is used only as a
batched tag read/write transport.

The safety-critical rule this harness exists to exercise: a position is written ONLY to a
file that has no position already, and only when both bracketing trackpoints belong to the
same track segment. Interpolating between two different segments would average two unrelated
activities into a coordinate that was never occupied.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree as ET

GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}
UTC = timezone.utc

# Only containers whose in-place write safety was verified byte-for-byte in research.
WRITE_ALLOWLIST = {".raf", ".dng"}
# Stills are in scope for issue 001; video defers to issue 002.
MEDIA_ALLOWLIST = {".raf", ".dng"}

READ_TAGS = (
    "-Composite:GPSPosition",
    "-EXIF:DateTimeOriginal",
    "-EXIF:OffsetTimeOriginal",
    "-QuickTime:CreateDate",
    "-EXIF:Make",
    "-EXIF:Model",
    "-File:FileType",
)


# --------------------------------------------------------------------------- outcomes


@dataclass(frozen=True)
class Position:
    lat: float
    lon: float
    ele: float | None = None


@dataclass(frozen=True)
class Ignored:
    path: Path
    reason: str


@dataclass(frozen=True)
class Protected:
    path: Path
    existing: str


@dataclass(frozen=True)
class Failed:
    path: Path
    reason: str


@dataclass(frozen=True)
class Unmatchable:
    path: Path
    utc: datetime
    reason: str
    nearest_seg: str | None
    delta_s: float | None


@dataclass(frozen=True)
class Matched:
    """Planned (dry-run) or Geotagged (written) — `written` discriminates."""

    path: Path
    utc: datetime
    pos: Position
    seg_id: str
    bracket_s: float
    via: str  # INTERPOLATED | EXTENDED
    written: bool = False


Outcome = Ignored | Protected | Failed | Unmatchable | Matched


# --------------------------------------------------------------------------- tracks


@dataclass
class TrackIndex:
    times: list[datetime] = field(default_factory=list)
    lats: list[float] = field(default_factory=list)
    lons: list[float] = field(default_factory=list)
    eles: list[float | None] = field(default_factory=list)
    segs: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.times)


def parse_tracks(tracks_dir: Path) -> TrackIndex:
    """Parse every GPX under tracks_dir into one time-ordered index.

    Each point keeps its segment identity (file + trk index + trkseg index) so the matcher
    can refuse to interpolate across a segment boundary.
    """
    rows: list[tuple[datetime, float, float, float | None, str]] = []
    for gpx in sorted(tracks_dir.rglob("*.gpx")):
        root = ET.parse(gpx).getroot()
        for ti, trk in enumerate(root.findall(".//g:trk", GPX_NS)):
            for si, seg in enumerate(trk.findall("g:trkseg", GPX_NS)):
                seg_id = f"{gpx.name}#trk{ti}seg{si}"
                for tp in seg.findall("g:trkpt", GPX_NS):
                    tnode = tp.find("g:time", GPX_NS)
                    if tnode is None or not tnode.text:
                        continue
                    when = datetime.fromisoformat(tnode.text.strip().replace("Z", "+00:00"))
                    ele_node = tp.find("g:ele", GPX_NS)
                    rows.append(
                        (
                            when.astimezone(UTC),
                            float(tp.get("lat")),
                            float(tp.get("lon")),
                            float(ele_node.text) if ele_node is not None and ele_node.text else None,
                            seg_id,
                        )
                    )
    rows.sort(key=lambda r: r[0])
    idx = TrackIndex()
    for when, lat, lon, ele, seg in rows:
        idx.times.append(when)
        idx.lats.append(lat)
        idx.lons.append(lon)
        idx.eles.append(ele)
        idx.segs.append(seg)
    return idx


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------- matching


def match(idx: TrackIndex, when: datetime, max_gap_s: float, extend_s: float):
    """Resolve an instant to a position. Returns (Position, seg_id, bracket_s, via) or (None, reason, nearest_seg, delta_s)."""
    if not len(idx):
        return None, "no trackpoints", None, None

    i = bisect.bisect_left(idx.times, when)

    # Exact hit on a stored point.
    if i < len(idx) and idx.times[i] == when:
        return (
            Position(idx.lats[i], idx.lons[i], idx.eles[i]),
            idx.segs[i],
            0.0,
            "INTERPOLATED",
        )

    # Before the first point / after the last: only --extend can rescue these.
    if i == 0 or i >= len(idx):
        edge = 0 if i == 0 else len(idx) - 1
        delta = abs((idx.times[edge] - when).total_seconds())
        if extend_s > 0 and delta <= extend_s:
            return (
                Position(idx.lats[edge], idx.lons[edge], idx.eles[edge]),
                idx.segs[edge],
                delta,
                "EXTENDED",
            )
        where = "before first trackpoint" if i == 0 else "after last trackpoint"
        return None, where, idx.segs[edge], delta

    lo, hi = i - 1, i

    # THE GUARD: never interpolate across a segment boundary. Two different activities
    # bracketing an instant means the camera was between activities, not on a track.
    if idx.segs[lo] != idx.segs[hi]:
        d_lo = (when - idx.times[lo]).total_seconds()
        d_hi = (idx.times[hi] - when).total_seconds()
        near_i, delta = (lo, d_lo) if d_lo <= d_hi else (hi, d_hi)
        if extend_s > 0 and delta <= extend_s:
            return (
                Position(idx.lats[near_i], idx.lons[near_i], idx.eles[near_i]),
                idx.segs[near_i],
                delta,
                "EXTENDED",
            )
        return None, "between track segments", idx.segs[near_i], delta

    bracket = (idx.times[hi] - idx.times[lo]).total_seconds()
    if bracket > max_gap_s:
        d_lo = (when - idx.times[lo]).total_seconds()
        d_hi = (idx.times[hi] - when).total_seconds()
        near_i, delta = (lo, d_lo) if d_lo <= d_hi else (hi, d_hi)
        if extend_s > 0 and delta <= extend_s:
            return (
                Position(idx.lats[near_i], idx.lons[near_i], idx.eles[near_i]),
                idx.segs[near_i],
                delta,
                "EXTENDED",
            )
        return None, f"bracket {bracket:.0f}s exceeds max-gap", idx.segs[near_i], delta

    frac = (when - idx.times[lo]).total_seconds() / bracket if bracket else 0.0
    lat = idx.lats[lo] + (idx.lats[hi] - idx.lats[lo]) * frac
    lon = idx.lons[lo] + (idx.lons[hi] - idx.lons[lo]) * frac
    ele = None
    if idx.eles[lo] is not None and idx.eles[hi] is not None:
        ele = idx.eles[lo] + (idx.eles[hi] - idx.eles[lo]) * frac
    return Position(lat, lon, ele), idx.segs[lo], bracket, "INTERPOLATED"


# --------------------------------------------------------------------------- time


def resolve_utc(tags: dict, fallback_offset: str | None):
    """Per-container time resolution. Returns (datetime, rule) or (None, error)."""
    ftype = (tags.get("File:FileType") or "").upper()

    if ftype in {"MP4", "M4V"}:
        raw = tags.get("QuickTime:CreateDate")
        if not raw:
            return None, "no QuickTime:CreateDate"
        return _parse_naive(raw).replace(tzinfo=UTC), "quicktime-utc"

    raw = tags.get("EXIF:DateTimeOriginal")
    if not raw:
        return None, "no EXIF:DateTimeOriginal"
    naive = _parse_naive(raw)

    offset = tags.get("EXIF:OffsetTimeOriginal")
    if offset:
        return naive.replace(tzinfo=_parse_offset(offset)).astimezone(UTC), "exif-offset"

    # Local-time container with no offset tag: the flag is required, never guessed.
    if fallback_offset:
        return naive.replace(tzinfo=_parse_offset(fallback_offset)).astimezone(UTC), "cli-offset"
    return None, "local time with no OffsetTimeOriginal and no --utc-offset given"


def _parse_naive(raw: str) -> datetime:
    return datetime.strptime(raw.strip()[:19], "%Y:%m:%d %H:%M:%S")


def _parse_offset(text: str) -> timezone:
    text = text.strip()
    sign = -1 if text[0] == "-" else 1
    hh, mm = int(text[1:3]), int(text[4:6])
    return timezone(sign * timedelta(hours=hh, minutes=mm))


# --------------------------------------------------------------------------- exiftool


def _run_argfile(args: Sequence[str], capture_json: bool) -> str:
    """Run one exiftool process driven by an argfile.

    An argfile is used for every invocation because its lines are taken literally, which is
    what makes paths containing spaces safe without any quoting.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False) as fh:
        fh.write("\n".join(args) + "\n")
        argfile = fh.name
    try:
        proc = subprocess.run(
            ["exiftool", "-@", argfile],
            capture_output=True,
            text=True,
            check=False,
        )
        if capture_json:
            return proc.stdout
        return proc.stdout + proc.stderr
    finally:
        os.unlink(argfile)


def read_metadata(paths: Sequence[Path]) -> dict[Path, dict]:
    """One exiftool process for the whole batch."""
    if not paths:
        return {}
    args = ["-j", "-n", "-G", *READ_TAGS, *[str(p) for p in paths]]
    out = _run_argfile(args, capture_json=True)
    try:
        records = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return {}
    result: dict[Path, dict] = {}
    for rec in records:
        src = rec.get("SourceFile")
        if src:
            result[Path(src).resolve()] = rec
    return result


def write_positions(matches: Sequence[Matched], keep_original: bool) -> str:
    """One exiftool process for the whole batch, one -execute block per file."""
    if not matches:
        return "nothing to write"
    args: list[str] = []
    for m in matches:
        # Resolve the real path: writing to a symlink would replace the link and leave the
        # original untouched.
        real = m.path.resolve()
        block = [
            "-overwrite_original" if not keep_original else "-P",
            f"-GPSLatitude={abs(m.pos.lat):.8f}",
            f"-GPSLatitudeRef={'N' if m.pos.lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(m.pos.lon):.8f}",
            f"-GPSLongitudeRef={'E' if m.pos.lon >= 0 else 'W'}",
            f"-GPSDateStamp={m.utc.strftime('%Y:%m:%d')}",
            f"-GPSTimeStamp={m.utc.strftime('%H:%M:%S')}",
        ]
        if m.pos.ele is not None:
            block += [
                f"-GPSAltitude={abs(m.pos.ele):.3f}",
                f"-GPSAltitudeRef={'above' if m.pos.ele >= 0 else 'below'}",
            ]
        block += [str(real), "-execute"]
        args += block
    return _run_argfile(args, capture_json=False)


# --------------------------------------------------------------------------- discovery


def discover(media_root: Path, exclude: Iterable[Path]) -> tuple[list[Path], list[Ignored]]:
    """Walk media_root without following symlinks; classify by extension allowlist."""
    excluded = {p.resolve() for p in exclude}
    targets: list[Path] = []
    ignored: list[Ignored] = []
    for dirpath, dirnames, filenames in os.walk(media_root, followlinks=False):
        here = Path(dirpath).resolve()
        if any(here == ex or ex in here.parents for ex in excluded):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if (here / d).resolve() not in excluded]
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                ignored.append(Ignored(full, "symlink"))
                continue
            if full.suffix.lower() not in MEDIA_ALLOWLIST:
                ignored.append(Ignored(full, f"not in media allowlist ({full.suffix or 'no ext'})"))
                continue
            targets.append(full)
    return targets, ignored


# --------------------------------------------------------------------------- pipeline


def run(
    media_root: Path,
    tracks_dir: Path,
    *,
    write: bool,
    max_gap_s: float,
    extend_s: float,
    fallback_offset: str | None,
    review_dir: Path | None,
    keep_original: bool = False,
) -> list[Outcome]:
    review = review_dir.resolve() if review_dir else None
    targets, outcomes_ignored = discover(media_root, exclude=[review] if review else [])
    outcomes: list[Outcome] = list(outcomes_ignored)

    idx = parse_tracks(tracks_dir)
    meta = read_metadata(targets)

    pending: list[Matched] = []
    for p in targets:
        tags = meta.get(p.resolve(), {})

        existing = tags.get("Composite:GPSPosition")
        if existing not in (None, ""):
            # THE INVARIANT. Any present position, (0,0) included, is protected.
            outcomes.append(Protected(p, str(existing)))
            continue

        if p.suffix.lower() not in WRITE_ALLOWLIST:
            outcomes.append(Failed(p, f"write safety unverified for {p.suffix}"))
            continue

        when, rule = resolve_utc(tags, fallback_offset)
        if when is None:
            outcomes.append(Failed(p, rule))
            continue

        pos, seg_or_reason, bracket_or_seg, via_or_delta = match(idx, when, max_gap_s, extend_s)
        if pos is None:
            outcomes.append(
                Unmatchable(p, when, seg_or_reason, bracket_or_seg, via_or_delta)
            )
            continue
        pending.append(
            Matched(p, when, pos, seg_or_reason, bracket_or_seg, via_or_delta, written=False)
        )

    if write and pending:
        write_positions(pending, keep_original)
        pending = [
            Matched(m.path, m.utc, m.pos, m.seg_id, m.bracket_s, m.via, written=True)
            for m in pending
        ]
    outcomes.extend(pending)

    if review:
        build_review_folder(outcomes, review)
    return outcomes


def build_review_folder(outcomes: Sequence[Outcome], review: Path) -> None:
    """Symlink every non-geotagged media file under a reason subfolder.

    Symlinks rather than hard links: exiftool's write changes the inode, so a hard link
    would freeze as a stale full-size copy of the pre-write file.
    """
    buckets = {
        "outside-track": [o.path for o in outcomes if isinstance(o, Unmatchable)],
        "already-tagged": [o.path for o in outcomes if isinstance(o, Protected)],
        "no-timestamp": [o.path for o in outcomes if isinstance(o, Failed)],
    }
    for name, paths in buckets.items():
        sub = review / name
        sub.mkdir(parents=True, exist_ok=True)
        for p in paths:
            link = sub / p.name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(p.resolve())


# --------------------------------------------------------------------------- reporting


def summarise(outcomes: Sequence[Outcome]) -> dict:
    counts: dict[str, int] = {}
    for o in outcomes:
        key = type(o).__name__
        if isinstance(o, Matched):
            key = "Geotagged" if o.written else "Planned"
        counts[key] = counts.get(key, 0) + 1
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="gps-sync Shape B prototype (design validation)")
    ap.add_argument("--media", required=True, type=Path)
    ap.add_argument("--tracks", required=True, type=Path)
    ap.add_argument("--write", action="store_true", help="apply changes (default is dry-run)")
    ap.add_argument("--max-gap", type=float, default=120.0)
    ap.add_argument("--extend", type=float, default=0.0, help="seconds; 0 disables extension")
    ap.add_argument("--utc-offset", default=None, help="fallback offset, e.g. +06:00")
    ap.add_argument("--review-dir", type=Path, default=None)
    ap.add_argument("--keep-original", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    ns = ap.parse_args(argv)

    outcomes = run(
        ns.media,
        ns.tracks,
        write=ns.write,
        max_gap_s=ns.max_gap,
        extend_s=ns.extend,
        fallback_offset=ns.utc_offset,
        review_dir=ns.review_dir,
        keep_original=ns.keep_original,
    )

    mode = "WRITE" if ns.write else "DRY-RUN"
    print(f"mode={mode}  max-gap={ns.max_gap:g}s  extend={ns.extend:g}s")
    for o in sorted(outcomes, key=lambda x: str(x.path)):
        rel = o.path
        if isinstance(o, Matched):
            tag = "GEOTAGGED" if o.written else "PLANNED"
            print(
                f"  {tag:11s} {rel.name:28s} {o.pos.lat:.7f},{o.pos.lon:.7f}"
                f"  bracket={o.bracket_s:.0f}s via={o.via} seg={o.seg_id}"
            )
        elif isinstance(o, Protected):
            print(f"  {'PROTECTED':11s} {rel.name:28s} existing={o.existing}")
        elif isinstance(o, Unmatchable):
            d = f"{o.delta_s/60:.1f}min" if o.delta_s is not None else "?"
            print(f"  {'UNMATCHABLE':11s} {rel.name:28s} {o.reason} (nearest {d})")
        elif isinstance(o, Failed):
            print(f"  {'FAILED':11s} {rel.name:28s} {o.reason}")
        else:
            print(f"  {'IGNORED':11s} {rel.name:28s} {o.reason}")

    counts = summarise(outcomes)
    print("\nsummary: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    if ns.json:
        payload = []
        for o in outcomes:
            rec = {"path": str(o.path), "kind": type(o).__name__}
            if isinstance(o, Matched):
                rec.update(
                    kind="Geotagged" if o.written else "Planned",
                    lat=o.pos.lat,
                    lon=o.pos.lon,
                    ele=o.pos.ele,
                    seg_id=o.seg_id,
                    bracket_s=o.bracket_s,
                    via=o.via,
                    utc=o.utc.isoformat(),
                )
            elif isinstance(o, Protected):
                rec["existing"] = str(o.existing)
            elif isinstance(o, Unmatchable):
                rec.update(reason=o.reason, nearest_seg=o.nearest_seg, delta_s=o.delta_s,
                           utc=o.utc.isoformat())
            elif isinstance(o, (Failed, Ignored)):
                rec["reason"] = o.reason
            payload.append(rec)
        ns.json.write_text(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
