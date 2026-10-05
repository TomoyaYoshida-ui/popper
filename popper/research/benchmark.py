"""Offline cross-domain kernel benchmark with retained, verifiable evidence.

This measures the installed engine on bundled non-ML tasks, not an LLM or a
scientific superiority claim. Wall time is descriptive and machine-dependent.
"""
from __future__ import annotations

import platform
import time
from pathlib import Path

from ..core import ProtocolError, file_hash, read_json, write_json
from ..demo import CASES, FILES, initialize_demo
from .controller import ResearchController
from .evidence_binding import export_research_evidence, verify_research_evidence
from .readout import read_research_state
from .store import ResearchStore


def _case_summary(root, name):
    project = root / name / "project"
    run = root / name / "research"
    evidence = root / name / "evidence"
    state = read_research_state(run)
    if not state.get("ok") or not state["experiment_graph"]["integrity"]["ok"]:
        raise ProtocolError(f"{name} 的研究投影或来源图不完整")
    if state["manifest"].get("capability_mode") != "registered_hypothesis_baseline":
        raise ProtocolError(f"{name} 不是固定预注册基线策略")
    if state["study"]["status"] not in {"concluded", "budget_exhausted"}:
        raise ProtocolError(f"{name} 的 Study 尚未终结")
    store = ResearchStore(run / "research.sqlite")
    try:
        audit = store.verify()
        if not audit["ok"]:
            raise ProtocolError(f"{name} 的研究事件链不完整: {audit['reason']}")
        cursor = store.event_cursor()
    finally:
        store.close()
    if state["claims"]:
        appendix = verify_research_evidence(project, run, evidence)
        evidence_count = appendix["claim_count"]
    else:
        if evidence.exists():
            raise ProtocolError(f"{name} 无 Claim 却存在证据附录")
        evidence_count = 0
    return {
        "case": name,
        "metric": state["manifest"]["metric"]["name"],
        "repeat_unit": state["manifest"]["repeat_unit"],
        "study_id": state["manifest"]["study_id"],
        "study_status": state["study"]["status"],
        "event_cursor": cursor,
        "research_manifest_sha256": file_hash(run / "research.json"),
        "input_sha256": {item: file_hash(project / item) for item in FILES},
        "budget_spent": state["budget"]["spent"],
        "claims": [{"claim_id": row["entity_id"], "status": row["status"],
                    "oriented_delta": row["oriented_delta"], "scope": row["scope"]}
                   for row in sorted(state["claims"], key=lambda item: item["entity_id"])],
        "evidence_claim_count": evidence_count,
        "evidence_available": bool(state["claims"]),
    }


def run_cross_domain_benchmark(out_dir, cases=None, *, trusted_local=False):
    if not trusted_local:
        raise ProtocolError("基准将执行内置候选代码，须显式指定 --trusted-local")
    names = tuple(CASES if cases is None else cases)
    if not names or len(set(names)) != len(names) or any(name not in CASES for name in names):
        raise ProtocolError("基准案例必须是不重复的内置非 ML 案例")
    root = Path(out_dir).absolute()
    if root.exists():
        raise ProtocolError("基准输出目录已存在，拒绝覆盖")
    root.mkdir(parents=True)
    report = {"schema_version": "1.0", "kind": "cross_domain_kernel_benchmark",
              "policy": "registered_hypothesis_baseline", "model_used": False,
              "trust": "trusted_local_same_account",
              "limitations": ["not_a_model_capability_benchmark",
                              "not_an_independent_scientific_replication",
                              "wall_time_not_comparable_across_machines"],
              "environment": {"python": platform.python_version(),
                              "platform": platform.platform()},
              "requested_cases": list(names), "cases": [], "status": "running"}
    try:
        for name in names:
            started = time.perf_counter()
            project, run, evidence = (root / name / "project",
                                      root / name / "research", root / name / "evidence")
            row = {"case": name, "status": "failed"}
            try:
                initialize_demo(project, name)
                ResearchController.initialize(project, run)
                controller = ResearchController(run)
                try:
                    development = controller.run(trusted_local=True)
                    if development["phase"] == "ready_for_confirmation":
                        final = controller.confirm(trusted_local=True)
                    elif development["phase"] in {"concluded", "budget_exhausted"}:
                        final = development
                    else:
                        raise ProtocolError(f"{name} 未进入终态: {development['phase']}")
                    if final["phase"] not in {"concluded", "budget_exhausted"}:
                        raise ProtocolError(f"{name} 未进入终态: {final['phase']}")
                finally:
                    controller.close()
                if final["claims"]:
                    export_research_evidence(project, run, evidence)
                row = {"status": "passed", "summary": _case_summary(root, name)}
            except Exception as error:
                row = {"status": "failed", "error_type": type(error).__name__,
                       "error": str(error)}
            row["case"] = name
            row["wall_seconds"] = round(time.perf_counter() - started, 6)
            report["cases"].append(row)
    finally:
        report["status"] = ("passed" if len(report["cases"]) == len(names)
                            and all(row["status"] == "passed" for row in report["cases"])
                            else "failed")
        write_json(root / "benchmark.json", report)
    return {"status": report["status"], "output_dir": str(root),
            "case_count": len(report["cases"]),
            "passed": sum(row["status"] == "passed" for row in report["cases"])}


def verify_cross_domain_benchmark(out_dir):
    root = Path(out_dir).resolve()
    report = read_json(root / "benchmark.json")
    if (not isinstance(report, dict) or report.get("schema_version") != "1.0"
            or report.get("kind") != "cross_domain_kernel_benchmark"
            or report.get("status") != "passed"
            or report.get("policy") != "registered_hypothesis_baseline"
            or report.get("model_used") is not False
            or not isinstance(report.get("requested_cases"), list)
            or not isinstance(report.get("cases"), list)
            or not report["cases"]
            or [row.get("case") for row in report["cases"]
                if isinstance(row, dict)] != report["requested_cases"]):
        raise ProtocolError("基准报告结构或状态不合法")
    names = []
    for row in report["cases"]:
        name = row.get("case") if isinstance(row, dict) else None
        if (name not in CASES or name in names or row.get("status") != "passed"
                or type(row.get("wall_seconds")) not in (int, float)
                or row["wall_seconds"] < 0):
            raise ProtocolError("基准案例记录不合法")
        names.append(name)
        if row.get("summary") != _case_summary(root, name):
            raise ProtocolError(f"{name} 的基准报告与当前权威研究状态不一致")
    return {"ok": True, "case_count": len(names),
            "verification_level": "local_research_store_and_source_artifacts"}
