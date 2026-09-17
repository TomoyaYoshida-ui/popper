"""运行一条可复核的 Research Controller 工程闭环；不把它计为新科学发现。"""
from __future__ import annotations

import json
import runpy
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from popper.core import file_hash, initialize, write_json
from popper.research import ResearchController


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trial = ROOT / "evaluation" / "runs" / f"research-controller-{stamp}"
    project, run_dir = trial / "project", trial / "research"
    source = ROOT / "examples" / "quadratic"
    project.mkdir(parents=True)
    for name in ("experiment.json", "model.py"):
        shutil.copyfile(source / name, project / name)
    runpy.run_path(str(source / "generate_data.py"))["generate"](project)
    initialization = initialize(project)
    ResearchController.initialize(project, run_dir)
    controller = ResearchController(run_dir)
    try:
        result = controller.run(trusted_local=True, auto_confirm=True)
    finally:
        controller.close()
    summary = {
        "schema_version": "1.0", "classification": "engineering_demonstration",
        "claim_limit": "真实执行与自适应控制闭环；不证明新颖性或跨任务自主科研能力",
        "trial": str(trial), "phase": result["phase"],
        "capability_mode": result["capability_mode"],
        "evaluation_trust": result["evaluation_trust"],
        "observations": len(result["observations"]),
        "decisions": len(result["decisions"]), "budget": result["budget"],
        "integrity": result["integrity"],
        "input_hashes": initialization["input_hashes"],
        "research_manifest_sha256": file_hash(run_dir / "research.json"),
        "research_db_sha256": file_hash(run_dir / "research.sqlite"),
    }
    write_json(trial / "trial-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["phase"] == "concluded" and result["integrity"]["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
