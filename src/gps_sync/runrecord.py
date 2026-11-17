"""The machine-readable run record emitted under ``--json``.

It carries everything the human report carries that is not formatting, rendered from the same
``RunSummary`` and the same outcome serialisation, so the two cannot disagree.
"""

import json
from collections.abc import Sequence
from typing import Any

from gps_sync.model import Outcome
from gps_sync.registry import REGISTRY
from gps_sync.serialise import instant_text, outcome_to_dict
from gps_sync.space import VolumeDemand
from gps_sync.summary import RunInfo, RunSummary


def summary_to_dict(summary: RunSummary) -> dict[str, Any]:
    """Render the run-level figures."""
    coverage = summary.coverage
    sensitivity = summary.sensitivity
    window = summary.clamp_window
    return {
        "variant_counts": summary.variant_counts,
        "reason_counts": summary.reason_counts,
        "success_by_via": summary.success_by_via,
        "coverage": {
            "matched": coverage.matched,
            "reached_matcher": coverage.reached_matcher,
            "rate_percent": coverage.rate,
            "applicable": coverage.rate is not None,
            "by_via": dict(coverage.by_via),
        },
        "remedies": [
            {
                "reason": r.reason,
                "parameter": r.parameter,
                "files": r.files,
                "smallest_to_recover_any_s": r.smallest_to_recover_any_s,
                "smallest_to_recover_all_s": r.smallest_to_recover_all_s,
            }
            for r in summary.remedies
        ],
        "sensitivity": {
            "contributors": sensitivity.contributors,
            "excluded": sensitivity.excluded,
            "excluded_total": sensitivity.excluded_total,
            "median_speed_mps": sensitivity.median_speed_mps,
            "displacement_60s_m": sensitivity.displacement_60s_m,
            "applicable": sensitivity.median_speed_mps is not None,
        },
        "clamp_window": {
            "count": window.count,
            "smallest_s": window.smallest_s,
            "median_s": window.median_s,
            "largest_s": window.largest_s,
        },
        "altitude_unavailable": {
            "total": summary.altitude_unavailable,
            "breakdown": summary.altitude_breakdown,
        },
        "centroid_dispersion": {
            "count": summary.dispersion.count,
            "minimum_m": summary.dispersion.minimum_m,
            "median_m": summary.dispersion.median_m,
            "maximum_m": summary.dispersion.maximum_m,
        },
    }


def space_demand_to_list(demand: Sequence[VolumeDemand]) -> list[dict[str, int]]:
    """Render the free-space forecast, one entry per volume, for every surface that carries it."""
    return [
        {"volume": d.volume, "peak_bytes": d.peak_bytes, "retained_bytes": d.retained_bytes}
        for d in demand
    ]


def _reconciliation_to_dict(info: RunInfo) -> dict[str, Any] | None:
    reconciliation = info.reconciliation
    if reconciliation is None:
        return None
    data: dict[str, Any] = {
        "returncode": reconciliation.returncode,
        "last_marker": reconciliation.last_marker,
        "process_failure": reconciliation.process_failure,
        "unintended_conclusions": [
            {"path": u.path, "lines": list(u.lines)} for u in reconciliation.unintended
        ],
        "unreconciled_blocks": list(reconciliation.unreconciled),
        "unreconciled_paths": list(reconciliation.unreconciled_paths),
    }
    return data


def build_run_record(
    outcomes: Sequence[Outcome], summary: RunSummary, info: RunInfo, exit_code: int | None = None
) -> dict[str, Any]:
    """Assemble the JSON-serialisable run record."""
    return {
        "tool": "gps-sync",
        "mode": info.mode,
        "media_root": info.media_root,
        "tracks_root": info.tracks_root,
        "exiftool_version": info.exiftool_version,
        "options": info.options,
        "artifacts": {
            "review_root": info.review_root,
            "review_folder": info.unprocessed,
            "audit_directory": info.runs,
            "audit_record": info.audit_record,
            "pruned": list(info.pruned),
            "audit_complete": info.artifacts.audit_complete,
            "review_complete": info.artifacts.review_complete,
            "problems": list(info.artifacts.problems),
        },
        "incomplete_prior_records": [
            {"name": n, "created": c, "intended_paths": k}
            for n, c, k in info.incomplete_prior_records
        ],
        "tracks": [
            {
                "path": g.relative_path,
                "points_kept": g.kept,
                "dropped": {k.value: v for k, v in g.dropped.items()},
                "duplicate_instants_collapsed": info.duplicate_instants.get(g.relative_path, 0),
            }
            for g in info.gpx_files
        ],
        "skipped_track_symlinks": list(info.skipped_track_symlinks),
        "overlaps": [
            {
                "segment_a": o.segment_a,
                "segment_b": o.segment_b,
                "start": instant_text(o.start),
                "end": instant_text(o.end),
            }
            for o in info.overlaps
        ],
        "summary": summary_to_dict(summary),
        "reconciliation": _reconciliation_to_dict(info),
        "run_level": dict(info.run_level),
        "space_demand": space_demand_to_list(info.space_demand),
        "telemetry_read": info.telemetry_read.as_record(),
        "registry": {
            key.value: {
                "variant": row.variant,
                "subfolder": row.subfolder,
                "label": row.label,
                "remedy": row.remedy,
                "exit_term": row.exit_term,
                "per_file_lines": row.per_file_lines,
            }
            for key, row in REGISTRY.items()
        },
        "exit_code": exit_code,
        "files": [outcome_to_dict(o) for o in outcomes],
    }


def render_json(
    outcomes: Sequence[Outcome], summary: RunSummary, info: RunInfo, exit_code: int | None = None
) -> str:
    """Render the run record as JSON text."""
    return json.dumps(build_run_record(outcomes, summary, info, exit_code), indent=2) + "\n"
