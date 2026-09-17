"""跨任务盲测装置 CLI 入口。

用法示例：
  python -m evaluation.blind_pilot.run_blind_matrix --smoke           # 确定性冒烟（零 API）
  python -m evaluation.blind_pilot.run_blind_matrix --tasks T01 --comparators adaptive --runs 1 --confirm-mode none
  python -m evaluation.blind_pilot.run_blind_matrix --full            # 全量 36 轨迹（需 API/GPU）
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popper.core import file_hash, read_json, write_json
from popper.research.evaluation_service import scoring_code_hash

from evaluation.blind_pilot.matrix import build_matrix, run_matrix
from evaluation.blind_pilot.report import build_report, write_report
from evaluation.blind_pilot.tasks.registry import all_task_ids, task_spec


SMOKE_CELLS = [
    ("T01-wdbc-positive", "fixed_registered_search"),
    ("T03-synth-near-zero", "fixed_registered_search"),
    ("T05-ts-positive", "same_model_same_tools_fixed_plan"),
    ("T06-ts-negative", "same_model_same_tools_fixed_plan"),
    ("T09-vision-positive", "fixed_registered_search"),
    ("T02-synth-negative", "same_model_same_tools_fixed_plan"),
]


def _smoke_cells():
    return [{"task_id": task, "comparator": comp, "run_index": 1}
            for task, comp in SMOKE_CELLS]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="确定性冒烟（零 API）")
    parser.add_argument("--full", action="store_true", help="全量 36 轨迹")
    parser.add_argument("--tasks", help="逗号分隔 task id 列表")
    parser.add_argument("--comparators", help="逗号分隔 comparator 列表")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--confirm-mode", default="local",
                        choices=("external", "local", "none"))
    parser.add_argument("--model-url", default="https://api.deepseek.com")
    parser.add_argument("--model-name",
                        default=os.environ.get("POPPER_MODEL", "deepseek-flash"))
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--out", default=None, help="trial 根目录（缺省自动命名）")
    args = parser.parse_args(argv)

    if args.smoke:
        cells = _smoke_cells()
        confirm_mode = "local"
    elif args.full:
        cells = build_matrix(all_task_ids(), _comparators(args.comparators), args.runs)
        confirm_mode = args.confirm_mode
    else:
        tasks = all_task_ids() if not args.tasks else [t.strip() for t in args.tasks.split(",")]
        cells = build_matrix(tasks, _comparators(args.comparators), args.runs)
        confirm_mode = args.confirm_mode

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    # 目录名保持短小：cell 目录还会再嵌套 .popper/runs/<rid>/…，过长会撞
    # Windows MAX_PATH(260) 导致写文件失败。
    trial_root = Path(args.out) if args.out else (
        ROOT / "evaluation/runs" / f"bp-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:4]}")
    trial_root.mkdir(parents=True, exist_ok=True)

    has_key = bool(os.environ.get("POPPER_API_KEY"))
    summaries = run_matrix(
        cells, trial_root=trial_root, confirm_mode=confirm_mode,
        model_url=args.model_url, model_name=args.model_name,
        resume=not args.no_resume)
    for summary in summaries:
        if summary.get("status") != "completed":
            summary["error_stage"] = summary.get("error_stage", "run")

    report = build_report(
        summaries, protocol_sha256=file_hash(ROOT / "evaluation/autonomy-v2-protocol.json"),
        run_config={"stamp": stamp, "confirm_mode": confirm_mode,
                    "model": args.model_name, "has_api_key": has_key,
                    "model_call_limits": {"logical": 32, "http": 64}},
        planned_trajectories=len(cells))
    report_path = write_report(report, trial_root)

    valid = report["valid_loops"]
    non_blocked = [s for s in summaries if s.get("status") == "completed"]
    ok = len(non_blocked) == len(cells) and valid == len(cells)
    print(f"cells={len(cells)} valid={valid} completed={len(non_blocked)} "
          f"report={report_path}")
    for summary in summaries:
        print(f"  {summary['cell']}: status={summary.get('status')} "
              f"phase={summary.get('phase')} valid={summary.get('valid_loop')} "
              f"err={summary.get('error_type')}")
    return 0 if ok else 2


def _comparators(value):
    from evaluation.blind_pilot.policies import COMPARATORS
    if not value:
        return list(COMPARATORS)
    items = [v.strip() for v in value.split(",")]
    for item in items:
        if item not in COMPARATORS:
            raise SystemExit(f"未知对照: {item}，可选 {COMPARATORS}")
    return items


if __name__ == "__main__":
    raise SystemExit(main())
