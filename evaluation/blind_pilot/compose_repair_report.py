"""Compose an audited repair report while preserving every source trial.

The result is explicitly a post-fix composite, not a new frozen benchmark run.
Each replacement must name a cell that exists in the base trial. When multiple
repair trials claim the same cell, the later one in argument order supersedes
the earlier one (mirroring the documented fix history); every superseded
intermediate is preserved on the cell, and all source trials remain on disk.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from popper.core import ProtocolError, digest, file_hash, read_json, write_json
from evaluation.blind_pilot.audit import audited_summary
from evaluation.blind_pilot.report import build_report, is_valid_loop


def cell_key(summary):
    cell = summary.get("cell")
    if not isinstance(cell, dict):
        raise ProtocolError("cell summary is missing its registered cell identity")
    required = ("task_id", "comparator", "run_index")
    if any(name not in cell for name in required):
        raise ProtocolError("cell identity is incomplete")
    return tuple(cell[name] for name in required)


def _audit_trial(trial):
    trial = Path(trial).resolve()
    cells = {}
    sources = {}
    for path in sorted(trial.glob("*/cell-summary.json")):
        original = read_json(path)
        key = cell_key(original)
        if key in cells:
            raise ProtocolError(f"duplicate cell in trial: {key!r}")
        gold = trial / "tasks" / original["task_id"] / "gold"
        revised = audited_summary(path.parent, gold, original)
        revised["valid_loop"] = is_valid_loop(revised)
        cells[key] = revised
        sources[key] = {
            "trial": str(trial),
            "cell_summary": str(path),
            "cell_summary_sha256": file_hash(path),
            "audited_summary_sha256": digest(revised),
        }
    if not cells:
        raise ProtocolError(f"trial has no cell summaries: {trial}")
    report_path = trial / "blind-report.json"
    return cells, sources, ({
        "path": str(report_path),
        "sha256": file_hash(report_path),
        "report": read_json(report_path),
    } if report_path.is_file() else None)


def compose(base_trial, replacement_trials):
    base_cells, base_sources, base_report = _audit_trial(base_trial)
    if base_report is None:
        raise ProtocolError("base trial must contain blind-report.json")
    selected = dict(base_cells)
    selected_sources = dict(base_sources)
    replacements = []
    claims = {}  # cell key -> per-trial override metadata (later trials win)
    for replacement_trial in replacement_trials:
        repair_cells, repair_sources, repair_report = _audit_trial(replacement_trial)
        trial_path = str(Path(replacement_trial).resolve())
        for key, repaired in repair_cells.items():
            if key not in base_cells:
                raise ProtocolError(f"replacement cell is absent from base trial: {key!r}")
            original = base_cells[key]
            claim = claims.setdefault(key, {
                "trials": [],
                "winner": None,
                "super_sources": [],
            })
            claim["trials"].append(trial_path)
            # A later repair trial for the same cell supersedes earlier ones,
            # mirroring the documented fix history (e.g. v3 re-run of v2 cells).
            if claim["winner"] is not None:
                claim["super_sources"].append(claim["winner_source"])
            claim["winner"] = {
                "cell": {"task_id": key[0], "comparator": key[1], "run_index": key[2]},
                "source": repair_sources[key],
                "superseded": base_sources[key],
                "before": {
                    "status": original.get("status"),
                    "valid_loop": is_valid_loop(original),
                    "conclusion_matched": original.get("conclusion_matched"),
                    "error_type": original.get("error_type"),
                    "error_message": original.get("error_message"),
                },
                "after": {
                    "status": repaired.get("status"),
                    "valid_loop": is_valid_loop(repaired),
                    "conclusion_matched": repaired.get("conclusion_matched"),
                    "error_type": repaired.get("error_type"),
                    "error_message": repaired.get("error_message"),
                },
                "repair_report": ({"path": repair_report["path"],
                                   "sha256": repair_report["sha256"]}
                                  if repair_report is not None else None),
            }
            claim["winner_source"] = repair_sources[key]
            selected[key] = repaired
            selected_sources[key] = repair_sources[key]

    for key, claim in claims.items():
        if len(claim["trials"]) > 1:
            claim["winner"]["supersedes"] = claim["super_sources"]
        replacements.append(claim["winner"])

    ordered = [selected[key] for key in sorted(selected, key=lambda item: (item[0], item[1], item[2]))]
    original_report = base_report["report"]
    report = build_report(
        ordered,
        protocol_sha256=original_report.get("protocol_sha256"),
        run_config={
            "kind": "post_fix_composite",
            "base_run_config": original_report.get("run_config"),
            "source_trials": [str(Path(base_trial).resolve()),
                              *[str(Path(path).resolve()) for path in replacement_trials]],
        },
        planned_trajectories=original_report.get("planned_trajectories", len(base_cells)),
    )
    report["composite"] = {
        "is_single_frozen_run": False,
        "interpretation": "Post-fix composite; source trials and original failures remain preserved.",
        "base_report": {"path": base_report["path"], "sha256": base_report["sha256"]},
        "replacement_count": len(replacements),
        "replacements": replacements,
        "original_failures": original_report.get("failures", []),
        "selected_cell_sources_sha256": digest([
            {"cell": {"task_id": key[0], "comparator": key[1], "run_index": key[2]},
             "source": selected_sources[key]}
            for key in sorted(selected_sources, key=lambda item: (item[0], item[1], item[2]))
        ]),
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("replacements", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = compose(args.base, args.replacements)
    target = args.output or args.base / "blind-repair-report.json"
    write_json(target, report)
    print(f"replacements={report['composite']['replacement_count']} "
          f"evidence_valid={report['valid_loops']} "
          f"scientifically_valid={report['scientifically_valid_loops']} report={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
