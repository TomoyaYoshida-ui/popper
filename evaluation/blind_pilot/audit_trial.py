"""Audit an existing blind-pilot run without overwriting original cell summaries."""
from __future__ import annotations

import argparse
from pathlib import Path

from popper.core import read_json, write_json
from evaluation.blind_pilot.audit import audited_summary
from evaluation.blind_pilot.report import build_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    args = parser.parse_args(argv)
    trial = args.trial.resolve()
    output = trial / "audit"
    output.mkdir(exist_ok=True)
    audited = []
    for path in sorted(trial.glob("*/cell-summary.json")):
        summary = read_json(path)
        gold = trial / "tasks" / summary["task_id"] / "gold"
        revised = audited_summary(path.parent, gold, summary)
        from evaluation.blind_pilot.report import is_valid_loop
        revised["valid_loop"] = is_valid_loop(revised)
        write_json(output / f"{path.parent.name}.json", revised)
        audited.append(revised)
    original = read_json(trial / "blind-report.json") if (trial / "blind-report.json").is_file() else {}
    report = build_report(audited, protocol_sha256=original.get("protocol_sha256"),
                          run_config=original.get("run_config"),
                          planned_trajectories=original.get("planned_trajectories", len(audited)))
    report["audit_of"] = str(trial)
    report["original_report_preserved"] = str(trial / "blind-report.json")
    target = trial / "blind-audit-report.json"
    write_json(target, report)
    print(f"audited={len(audited)} evidence_valid={report['valid_loops']} "
          f"conclusion_matched={report['conclusion_matched']} report={target}")
    return 0 if len(audited) == report["planned_trajectories"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
