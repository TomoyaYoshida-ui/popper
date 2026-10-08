"""Loopback-only workstation for one explicitly selected experiment project."""
import datetime
import hashlib
import json
import math
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import Experiment, ProtocolError, canonical, digest, file_hash, read_json, write_json
from .api_contracts import ErrorCode, failure as api_failure, new_trace_id, success as api_success
from .identity import EntityKind, parse_id
from .reproduction import (EVENT_DB, ReproductionTask, STATE_DIR,
                           reproduction_research_projection)
from .review import GATE_STATUS_LABELS, MODE7_NAMES, RISK_NAMES
from .workspace import (
    DISCLOSURE_LABELS,
    REGISTRY_REL,
    TEMPLATE_LABELS,
    VALID_DISCLOSURES,
    VALID_TEMPLATES,
    WorkspaceHub,
    Workspace,
    WorkspaceIdempotencyConflict,
    WorkspaceVersionConflict,
)
from .workspace_files import ProjectFileConflict, ProjectFileUncertain, ProjectFiles
from .materialize import Materializer
from .orchestrator import Orchestrator
from .research.readout import controller_routing_readiness, read_research_state
from .research.evidence_binding import render_research_evidence
from .research.workbench_drafts import create_workbench_bound_draft
from .research.workbench_drafts import (bound_draft_paths, list_workbench_bound_drafts,
                                        preview_agent_bound_draft,
                                        read_workbench_bound_draft)
from .manuscript_flow import ManuscriptFlow
from .research.revisions import RevisionDiffArchive, RevisionStore
from .research.revision_drafts import (preview_initial_revision,
                                       submit_initial_revision)
from . import revision_tools
from .research import agent_revisions
from .research.oscillator_worker import controller_for_run
from .research.worktrees import RevisionWorktreeManager
from .research.execution_jobs import RevisionExecutionJobStore
from .research.design_jobs import DesignExecutionJobStore
from .research.study_jobs import StudyJobStore, preview_study_action
from .research.project_prep import (preview_project_preparation,
                                    commit_project_preparation)
from .research.benchmark import (run_cross_domain_benchmark,
                                 verify_cross_domain_benchmark)
from .research.benchmark_jobs import BenchmarkJobStore
from .research.followup import create_followup_study, resume_followup_study
from .research.contracts import (TransitionError, validate_design_proposal,
                                 validate_hypothesis_proposal)
from .research.store import (ResearchIdempotencyConflict, ResearchStore,
                             ResearchVersionConflict)
from .chat import (CHAT_LABELS, CHAT_REL, CHAT_TOOLS, MODEL_MAX_TOKENS, TEXT_LIMIT,
                   make_chat_client)
from .harness.conversation import adapt_conversation_harness
from .sessions import (SESSIONS_REL, AgentSessionStore, SessionIdempotencyConflict,
                       read_session_summaries, SessionMetadataConflict,
                       SessionBudgetExceeded, SessionNotFound, TurnNotFound,
                       ToolCallConflict, ToolCallNotFound)
from .terminal import TerminalManager
from . import task_context as task_memory
from .workstation_lease import WorkstationLease


# 研究实体状态 / 假设修订来源 / 决策与反思动作→中文标签：后端单一定义，前端不再硬编码或回退。
RESEARCH_STATUS_LABELS = {
    "study": {
        "active": "进行中", "concluded": "已结论", "budget_exhausted": "预算耗尽",
        "blocked_external": "外部受阻", "integrity_failed": "完整性失败",
    },
    "hypothesis": {
        "untested": "未实验", "supported_in_scope": "注册预测达到阈值",
        "contradicted_in_scope": "观察方向与预测相反", "inconclusive": "证据不足", "retired": "已退役",
    },
    "design": {"draft": "草稿", "frozen": "已冻结"},
    "run": {
        "queued": "排队", "running": "执行中", "succeeded": "成功",
        "infrastructure_failed": "基础设施失败", "implementation_failed": "实现失败",
        "cancelled": "已取消",
    },
    "action": {
        "run_experiment": "执行实验", "add_control": "安排对照",
        "request_confirmation": "请求确认", "stop": "停止",
        "request_scope_boundary_confirmation": "请求边界确认",
        "conclude_scope_boundary": "边界结论", "implement_revision": "实现修订",
        "repair_implementation": "修复实现", "freeze_external_confirmation": "冻结外部确认",
    },
    "origin": {"created": "预注册", "revised": "证据后修订"},
}

# Arbor 假设节点状态→中文标签：后端单一定义，前端不再硬编码或回退。
ARBOR_NODE_STATUS_LABELS = {
    "root": "根节点",
    "pending": "待验证",
    "executed": "已执行",
    "support": "支持",
    "contradict": "证伪",
    "pruned": "已剪枝",
    "merged": "已合并",
    "validated": "已验证",
}

# Agent Runtime tool registry. Risk is server-owned: callers cannot downgrade a
# write into a read tool to bypass approval. The first slice intentionally stays
# narrow and delegates to Workspace's versioned/idempotent command owner.
AGENT_TOOLS = {
    "context.tool_result": "disclose",
    "context.source": "disclose",
    "research.create_design": "write",
    "research.create_hypothesis": "write",
    "research.freeze_design": "write",
    "research.execute_design": "write",
    "research.study_preview": "disclose",
    "research.study_action": "write",
    "research.study_job": "disclose",
    "research.draft_preview": "disclose",
    "research.draft_create": "write",
    "research.status": "disclose",
    "research.inspect_design": "disclose",
    "research.inspect_claim": "disclose",
    "research.inspect_observation": "disclose",
    "workspace.task_create": "write",
    "workspace.task_toggle": "write",
    "workspace.file_list": "disclose",
    "workspace.file_read": "disclose",
    "workspace.file_replace": "write",
}
MODEL_TOOL_ALIASES = {
    "context_tool_result": "context.tool_result",
    "context_source": "context.source",
    "research_create_design": "research.create_design",
    "research_create_hypothesis": "research.create_hypothesis",
    "research_freeze_design": "research.freeze_design",
    "research_execute_design": "research.execute_design",
    "research_study_preview": "research.study_preview",
    "research_study_action": "research.study_action",
    "research_study_job": "research.study_job",
    "research_draft_preview": "research.draft_preview",
    "research_draft_create": "research.draft_create",
    "research_status": "research.status",
    "research_inspect_design": "research.inspect_design",
    "research_inspect_claim": "research.inspect_claim",
    "research_inspect_observation": "research.inspect_observation",
    "workspace_task_create": "workspace.task_create",
    "workspace_task_toggle": "workspace.task_toggle",
    "workspace_file_list": "workspace.file_list",
    "workspace_file_read": "workspace.file_read",
    "workspace_file_replace": "workspace.file_replace",
}


AGENT_TOOLS.update(revision_tools.REVISION_RISKS)
MODEL_TOOL_ALIASES.update(revision_tools.ALIASES)


def validate_agent_tool_arguments(tool_name, arguments):
    if tool_name in revision_tools.REVISION_RISKS:
        return revision_tools.validate(tool_name, arguments)
    if not isinstance(arguments, dict):
        raise ProtocolError("Agent tool arguments 必须是对象")
    if tool_name == "context.tool_result":
        if (not {"source_call_id", "field_path"}.issubset(arguments)
                or set(arguments) - {"source_call_id", "field_path", "offset"}
                or not isinstance(arguments["field_path"], str)
                or type(arguments.get("offset", 0)) is not int
                or arguments.get("offset", 0) < 0):
            raise ProtocolError("context.tool_result 参数不合法")
        parse_id(arguments["source_call_id"], EntityKind.TOOL_CALL)
        return arguments
    if tool_name == "context.source":
        if (not {"source_session_id", "expected_version"}.issubset(arguments)
                or set(arguments) - {"source_session_id", "expected_version", "turn_id"}
                or not all(isinstance(value, str) and value for value in arguments.values())):
            raise ProtocolError("context.source 参数不合法")
        parse_id(arguments["source_session_id"], EntityKind.SESSION)
        if arguments.get("turn_id"):
            parse_id(arguments["turn_id"], EntityKind.TURN)
        return arguments
    if tool_name == "research.create_design":
        if (set(arguments) != {"study_id", "expected_version", "hypothesis_id",
                               "expected_hypothesis_version", "interventions",
                               "control", "baseline", "metric", "scorer_id",
                               "metric_direction", "analysis_unit", "splits",
                               "repeat_values", "min_meaningful_effect"}
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1):
            raise ProtocolError("research.create_design arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
        parse_id(arguments["hypothesis_id"], EntityKind.HYPOTHESIS)
        validate_design_proposal({key: arguments[key] for key in (
            "hypothesis_id", "expected_hypothesis_version", "interventions",
            "control", "baseline", "metric", "scorer_id", "metric_direction",
            "analysis_unit", "splits", "repeat_values", "min_meaningful_effect")})
    elif tool_name == "research.create_hypothesis":
        if (set(arguments) != {"study_id", "expected_version", "mechanism",
                               "applicability", "predictions", "falsification",
                               "alternatives"}
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1):
            raise ProtocolError("research.create_hypothesis arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
        validate_hypothesis_proposal({key: arguments[key] for key in (
            "mechanism", "applicability", "predictions", "falsification",
            "alternatives")})
    elif tool_name == "research.freeze_design":
        if (set(arguments) != {"study_id", "design_id", "expected_version"}
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1):
            raise ProtocolError("research.freeze_design arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
        parse_id(arguments["design_id"], EntityKind.DESIGN)
    elif tool_name == "research.execute_design":
        if (set(arguments) != {"study_id", "design_id", "expected_version",
                               "design_sha256"}
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1
                or not isinstance(arguments["design_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", arguments["design_sha256"])):
            raise ProtocolError("research.execute_design arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
        parse_id(arguments["design_id"], EntityKind.DESIGN)
    elif tool_name == "research.status":
        if set(arguments) != {"study_id"}:
            raise ProtocolError("research.status arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
    elif tool_name in {"research.study_preview", "research.study_action"}:
        required = {"study_id", "action", "max_steps"}
        if tool_name == "research.study_action":
            required.add("approval_sha256")
        if (set(arguments) != required or arguments.get("action") not in {"run", "confirm"}
                or type(arguments.get("max_steps")) is not int
                or not 1 <= arguments["max_steps"] <= 100
                or (tool_name == "research.study_action" and
                    (not isinstance(arguments["approval_sha256"], str) or
                     not re.fullmatch(r"[0-9a-f]{64}", arguments["approval_sha256"])))):
            raise ProtocolError(f"{tool_name} arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
    elif tool_name == "research.study_job":
        if (set(arguments) != {"study_id", "job_id"}
                or not isinstance(arguments["job_id"], str)
                or not re.fullmatch(r"JOBS-[0-9a-f]{24}", arguments["job_id"])):
            raise ProtocolError("research.study_job arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
    elif tool_name in {"research.draft_preview", "research.draft_create"}:
        required = {"study_id", "expected_state_version", "title", "author_note"}
        if tool_name == "research.draft_create":
            required.add("approval_sha256")
        if (set(arguments) != required
                or type(arguments.get("expected_state_version")) is not int
                or arguments["expected_state_version"] < 1
                or not isinstance(arguments.get("title"), str)
                or not isinstance(arguments.get("author_note"), str)
                or (tool_name == "research.draft_create" and
                    (not isinstance(arguments["approval_sha256"], str) or
                     not re.fullmatch(r"[0-9a-f]{64}", arguments["approval_sha256"])))):
            raise ProtocolError(f"{tool_name} arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
    elif tool_name == "research.inspect_design":
        if (set(arguments) != {"study_id", "design_id", "expected_version"}
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1):
            raise ProtocolError("research.inspect_design arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
        parse_id(arguments["design_id"], EntityKind.DESIGN)
    elif tool_name == "research.inspect_claim":
        if (set(arguments) != {"study_id", "claim_id", "expected_version"}
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1):
            raise ProtocolError("research.inspect_claim arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
        parse_id(arguments["claim_id"], EntityKind.CLAIM)
    elif tool_name == "research.inspect_observation":
        if (set(arguments) != {"study_id", "observation_id", "expected_version"}
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1):
            raise ProtocolError("research.inspect_observation arguments 不合法")
        parse_id(arguments["study_id"], EntityKind.STUDY)
        parse_id(arguments["observation_id"], EntityKind.OBSERVATION)
    elif tool_name == "workspace.task_create":
        allowed = {"expected_version", "fid", "title"}
        if (set(arguments) - allowed or not {"expected_version", "fid"} <= set(arguments)
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1
                or not isinstance(arguments["fid"], str) or not arguments["fid"]
                or ("title" in arguments and (not isinstance(arguments["title"], str)
                                                or not arguments["title"].strip()))):
            raise ProtocolError("workspace.task_create arguments 不合法")
    elif tool_name == "workspace.task_toggle":
        if (set(arguments) != {"expected_version", "fid", "quid", "done"}
                or type(arguments["expected_version"]) is not int
                or arguments["expected_version"] < 1
                or not isinstance(arguments["fid"], str) or not arguments["fid"]
                or not isinstance(arguments["quid"], str) or not arguments["quid"]
                or type(arguments["done"]) is not bool):
            raise ProtocolError("workspace.task_toggle arguments 不合法")
    elif tool_name == "workspace.file_list":
        if arguments:
            raise ProtocolError("workspace.file_list arguments 不合法")
    elif tool_name == "workspace.file_read":
        if (set(arguments) != {"path"} or not isinstance(arguments["path"], str)
                or not 0 < len(arguments["path"]) <= 512):
            raise ProtocolError("workspace.file_read arguments 不合法")
    elif tool_name == "workspace.file_replace":
        if (set(arguments) != {"path", "expected_sha256", "old_text", "new_text"}
                or not isinstance(arguments["path"], str)
                or not 0 < len(arguments["path"]) <= 512
                or not isinstance(arguments["expected_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", arguments["expected_sha256"])
                or not isinstance(arguments["old_text"], str)
                or not 0 < len(arguments["old_text"]) <= 16_384
                or not isinstance(arguments["new_text"], str)
                or len(arguments["new_text"]) > 16_384):
            raise ProtocolError("workspace.file_replace arguments 不合法")
    else:
        raise ProtocolError(f"未登记 Agent tool: {tool_name}")
    return arguments


def _agent_development_observation(scope):
    """Never route confirmation/holdout feedback through the dev-only Agent tool."""
    return scope == "dev" or (isinstance(scope, str)
                              and scope.startswith("dev:slice:")
                              and len(scope) > len("dev:slice:"))


def agent_workspace_command(tool_name, arguments):
    """Translate one validated Agent tool into the Workspace command owner."""
    validate_agent_tool_arguments(tool_name, arguments)
    if tool_name == "workspace.task_create":
        command = {"op": "task_create", "fid": arguments["fid"]}
        if "title" in arguments:
            command["title"] = arguments["title"]
    elif tool_name == "workspace.task_toggle":
        command = {"op": "task_toggle", "fid": arguments["fid"],
                   "quid": arguments["quid"], "done": arguments["done"]}
    else:
        raise ProtocolError("Agent tool executor 未登记")
    return command, arguments["expected_version"]


def agent_research_command(tool_name, arguments):
    """Map approved Agent proposals to the Research Store command authority."""
    validate_agent_tool_arguments(tool_name, arguments)
    if tool_name == "research.create_design":
        payload = {key: arguments[key] for key in (
            "hypothesis_id", "expected_hypothesis_version", "interventions",
            "control", "baseline", "metric", "scorer_id", "metric_direction",
            "analysis_unit", "splits", "repeat_values", "min_meaningful_effect")}
        action = "create_design"
    elif tool_name == "research.create_hypothesis":
        payload = {key: arguments[key] for key in (
            "mechanism", "applicability", "predictions", "falsification",
            "alternatives")}
        action = "create_hypothesis"
    elif tool_name == "research.freeze_design":
        payload = {"design_id": arguments["design_id"]}
        action = "freeze_design"
    else:
        raise ProtocolError("Agent research tool 未登记")
    return arguments["study_id"], action, payload, arguments["expected_version"]


def load_literature(run_dir):
    """读取真实 paper-search 检索产物（只读 vendor run）。

    结构：<工作区根>/integrations/runs/<run>/literature/search-*.json
    返回 papers 列表；无则空。
    """
    run_dir = Path(run_dir).resolve()
    papers = []
    searchfiles = list((run_dir / "literature").glob("search-*.json")) \
        if (run_dir / "literature").is_dir() else []
    for path in searchfiles:
        try:
            if not path.resolve().is_relative_to(run_dir):
                continue
            data = read_json(path)
            for p in data.get("papers", []) or []:
                papers.append(p)
        except Exception:
            continue
    return papers


def load_arbor(run_dir):
    """读取真实 Arbor 假设树产物（只读 vendor run）。

    结构：<工作区根>/integrations/runs/<run>/.arbor/tree.json + run.json
    返回 {run, nodes, frontier, evidence}（投影，与 vendor arbor-state 对齐）。
    """
    run_dir = Path(run_dir).resolve()
    arbor = run_dir / ".arbor"
    tree_path = arbor / "tree.json"
    if not tree_path.is_file() or not tree_path.resolve().is_relative_to(run_dir):
        return None
    try:
        tree = read_json(tree_path)
        run_path = arbor / "run.json"
        run = (read_json(run_path) if run_path.is_file()
               and run_path.resolve().is_relative_to(run_dir) else {})
        raw_nodes = tree.get("nodes") or []
        # nodes 可能是 dict（id→node）或 list；统一成 list
        nodes = list(raw_nodes.values()) if isinstance(raw_nodes, dict) else list(raw_nodes)
        root = tree.get("root", {})
        if isinstance(root, dict) and root and not any(n.get("id") == "n0" for n in nodes):
            nodes.insert(0, root)
        frontier = [{"id": n["id"]} for n in nodes if n.get("status") in {"pending", "executed"}]
        evidence = [{"node_id": n.get("id"),
                     "dev_score": (n.get("metadata") or {}).get("dev_score"),
                     "result": (n.get("metadata") or {}).get("result", "")}
                    for n in nodes if n.get("status") in {"executed", "support", "contradict", "merged"}]
        return {"run": run, "root": root, "nodes": nodes, "frontier": frontier, "evidence": evidence}
    except Exception:
        return None


def _vendor_runs_base(workspace_root):
    return Path(workspace_root).resolve() / "integrations" / "runs"


def list_vendor_runs(workspace_root):
    """只列出当前工作区内的 vendor runs；不扫描 Popper 安装目录。"""
    base = _vendor_runs_base(workspace_root)
    root = Path(workspace_root).resolve()
    if not base.is_dir() or not base.resolve().is_relative_to(root):
        return []
    resolved = base.resolve()
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and p.resolve().is_relative_to(resolved))


def load_all_literature(workspace_root):
    """读取当前工作区内全部真实 paper-search 检索产物。"""
    base = _vendor_runs_base(workspace_root)
    papers = []
    for run in list_vendor_runs(workspace_root):
        papers.extend(load_literature(base / run))
    return papers


def load_all_arbor(workspace_root):
    """读取当前工作区内全部真实 Arbor 假设树产物。"""
    base = _vendor_runs_base(workspace_root)
    arbor_runs = []
    for run in list_vendor_runs(workspace_root):
        data = load_arbor(base / run)
        if data is not None:
            arbor_runs.append({"run_dir": run, **data})
    return arbor_runs


def build_review_payload(state):
    """从实验真实状态派生拒稿风险预检（R1-R7 + 7-mode）。

    全部字段派生自已登记状态，不伪造实验数字：delta/阈值来自 claim，
    预注册/密封测试集等标记来自协议是否已冻结并消费最终测试。
    """
    try:
        from .review import RejectionReview
    except Exception:
        return None
    claim = state.get("claim")
    spec = state.get("spec", {})
    report = {
        # R2：真实增量与预注册阈值
        "delta": claim.get("delta") if claim else None,
        "min_improvement": spec.get("min_improvement"),
        # R4：overclaim 只看方向一致性 —— 由 controller 派生。
        "evidence_consistent": bool(claim),
        "contradiction": False,
        # R3/R6/R7 为机器产证据 + 用户裁决，缺省判 provisional/待裁决。
        "mechanisms": [],
        "heldout_falsifiable": False,
        "heldout_verified": bool(claim and claim.get("status") == "supports_threshold"),
        "importance": None,
        # R5：预注册语义状态真实标志。
        "preregistered": state.get("phase") in {"searching", "frozen", "confirming", "completed"},
        "resampled": False,
        "sealed_test": state.get("phase") in {"confirming", "completed"},
    }
    review = RejectionReview().assess(report)
    review["adjudications"] = state.get("adjudications", {})
    return review


# campaign 阶段顺序：与"LangGraph 8 阶段流水线"对齐，对应 Idea→检索→假设→Popper 开发→冻结→确认→写作。
CAMPAIGN_STAGES = (
    "idea", "literature", "dataset", "baseline", "arbor",
    "dev", "freeze", "confirm", "materialize",
)
PHASE_ORDINAL = {"searching": 1, "frozen": 2, "confirming": 3,
                 "completed": 4, "confirmation_failed": 3}
PHASES = {"searching": "开发集探索", "frozen": "候选已冻结",
          "confirming": "最终测试中", "completed": "实验完成",
          "confirmation_failed": "最终测试失败"}
# 事件 kind→中文标签：后端单一定义，前端不再硬编码或回退。
EVENT_NAMES = {
    "initialized": "登记实验协议", "run_started": "开始执行",
    "run_completed": "完成并登记证据", "candidate_proposed": "提出候选",
    "frozen": "冻结候选", "test_consumed": "消费最终测试",
    "confirmed": "生成结论", "run_failed": "执行失败",
    "confirmation_failed": "最终测试失败", "recovered": "恢复中断状态",
}
# campaign 阶段状态→中文标签：后端单一定义，前端不再硬编码或回退。
STAGE_STATUS_LABELS = {
    "done": "已完成", "active": "进行中", "todo": "待推进", "warn": "待补充",
}


def build_campaign_payload(state, results, papers, arbor, deliverables):
    """从真实状态派生统一切磋进度（不做假数据）。

    每个阶段 status ∈ done/active/todo/warn，附真实证据摘要。
    """
    spec = state.get("spec", {})
    claim = state.get("claim")
    phase = state.get("phase", "searching")
    ordinal = PHASE_ORDINAL.get(phase, 1)
    dev_runs = [r for r in results if r.get("split") == "dev"]
    basis = [r for r in dev_runs if r.get("config") == spec.get("baseline")]
    n_papers = len(papers)
    arbor_nodes = sum(len(r.get("nodes", [])) for r in arbor)
    arbor_frontier = sum(len(r.get("frontier", [])) for r in arbor)
    done = {  # 每阶段的实际门槛
        "idea": 1 <= ordinal,
        "literature": n_papers > 0,
        "dataset": 1 <= ordinal,
        "baseline": bool(basis),
        "arbor": arbor_nodes > 0,
        "dev": bool(dev_runs),
        "freeze": 2 <= ordinal,
        "confirm": 3 <= ordinal and bool(claim),
        "materialize": len(deliverables) > 0,
    }
    steps = {
        "idea": {
            "label": "选题创意", "src": "objective",
            "evidence": [("研究目标", spec.get("objective") or "—"),
                         ("指标", f"{spec.get('metric', {}).get('name')} · {spec.get('metric', {}).get('direction')}"),
                         ("预注册阈值", spec.get("min_improvement"))],
        },
        "literature": {
            "label": "文献检索", "src": "vendor paper_search",
            "evidence": [("检索入库", f"{n_papers} 篇（跨 run 去重后）")],
        },
        "dataset": {
            "label": "数据就位", "src": "controller 契约",
            "evidence": [("种子", f"{len(spec.get('seeds') or [])} 个"),
                         ("预算", f"{spec.get('budget')} 次"),
                         ("评估器", state.get("evaluator_id") or spec.get("metric", {}).get("name"))],
        },
        "baseline": {
            "label": "基线", "src": "controller 契约",
            "evidence": [(f"基线 {json.dumps(spec.get('baseline'), ensure_ascii=False)}",
                          f"{basis[0]['mean']:.4f}" if basis else "待运行")],
        },
        "arbor": {
            "label": "假设搜索环", "src": "vendor arbor",
            "evidence": [(f"假设节点", f"{arbor_nodes}"),
                         ("frontier", str(arbor_frontier))],
        },
        "dev": {
            "label": "候选开发", "src": "controller 契约",
            "evidence": [("成功开发集 runs", str(len(dev_runs)))],
        },
        "freeze": {
            "label": "冻结候选", "src": "state machine",
            "evidence": [("当前阶段", phase),
                         (f"冻结 " + json.dumps(spec.get("candidates") or [], ensure_ascii=False)[:80], "")],
        },
        "confirm": {
            "label": "最终确认", "src": "controller 契约",
            "evidence": [(f"结论 δ", f"{claim['delta']:.4f}" if claim else "—"),
                         ("阈值", str(spec.get("min_improvement"))),
                         ("结论状态", claim.get("status") if claim else "未生成")],
        },
        "materialize": {
            "label": "成果物化", "src": "workspace",
            "evidence": [("可交付稿件", f"{len(deliverables)} 份")],
        },
    }
    stages = []
    for idx, key in enumerate(CAMPAIGN_STAGES):
        st = steps[key]
        if done[key]:
            status = "done"
        elif key == "dev" and ordinal == 1:
            status = "active"
        elif key == "freeze" and ordinal == 1 and bool(dev_runs):
            status = "active"
        elif key == "arbor" and arbor_nodes == 0:
            status = "warn"
        elif key == "literature" and n_papers == 0:
            status = "warn"
        else:
            status = "todo" if idx > 0 else "active"
        stages.append({"key": key, "label": st["label"], "src": st["src"],
                       "status": status,
                       "evidence": [{"k": k, "v": v} for k, v in st["evidence"] if v != ""]})
    return {"objective": spec.get("name") or state.get("mode", ""),
            "phase": phase, "phase_label": PHASES.get(phase, phase),
            "stages": stages}


def campaign_pending_payload(campaign_dir, campaign_job=None):
    """从真实 campaign.json 派生挂起审批区块（无则 None，不做假数据）。

    仅当该运行目录确有 `waiting_approval` 时返回审批卡片所需信息；证据（proposal
    及其 diff）读自运行目录真实产物并只截取头部，全部只读。
    """
    run_dir = Path(campaign_dir)
    manifest_path = run_dir / "campaign.json"
    if not manifest_path.is_file():
        return None
    try:
        data = read_json(manifest_path)
    except Exception:
        return None
    pending = data.get("waiting_approval")
    if not pending:
        return None
    proposal_dir = run_dir / "proposal"
    evidence = {}
    diff_path = proposal_dir / "proposal.diff"
    if diff_path.is_file():
        text = diff_path.read_text(encoding="utf-8", errors="replace")
        evidence["diff"] = text[:4000]
    pjson = proposal_dir / "proposal.json"
    if pjson.is_file():
        try:
            evidence["proposal"] = {"bytes": pjson.stat().st_size,
                                    "summary": read_json(pjson)}
        except Exception:
            evidence["proposal"] = {"bytes": pjson.stat().st_size}
    return {
        "run_dir": str(run_dir),
        "status": data.get("status"),
        "step": pending.get("step"),
        "required": pending.get("required"),
        "reason": pending.get("reason"),
        "history": [{"step": h.get("step"), "outcome": h.get("outcome")}
                    for h in data.get("history", [])],
        "evidence": evidence,
        "job": campaign_job or {"status": "idle"},
    }


def project_mode(project):
    root = Path(project).resolve()
    if ((root / STATE_DIR / "state.json").is_file()
            or (root / STATE_DIR / EVENT_DB).is_file()):
        return "reproduction"
    if (root / ".popper" / "state.db").is_file():
        return "experiment"
    raise ProtocolError("项目既不是已初始化实验，也不是已注册复现任务")


class Workstation(ThreadingHTTPServer):
    def _service(self, name):
        from importlib import import_module
        cache = getattr(self, "_service_instances", None)
        if cache is None:
            cache = {}
            self._service_instances = cache
        if name not in cache:
            module = import_module("popper.services." + name)
            service_type = getattr(module, "".join(part.title() for part in name.split("_")) + "Service")
            cache[name] = service_type(self)
        return cache[name]

    daemon_threads = True

    def __init__(self, address, project, trusted_local=False, campaign_dir=None,
                 research_runs=(), workspace_roots=(),
                 chat_base_url=None, chat_model=None, chat_llm=None,
                 evaluation_budget_path=None):
        self._workspace_leases = []
        self.evaluation_budget_path = (Path(evaluation_budget_path).resolve()
                                       if evaluation_budget_path else None)
        if self.evaluation_budget_path is not None:
            # Opt-in campaign ledger; normal workstations retain their existing
            # session budgets. Validate persisted policy before queue recovery.
            from .research.agent_evaluation_budget import AgentEvaluationBudget
            ledger = AgentEvaluationBudget(self.evaluation_budget_path)
            ledger.close()
        project_mode(project)
        roots = tuple(workspace_roots)
        try:
            project_root = Path(project).resolve()
            self._workspace_leases.append(WorkstationLease(project_root))
            lease_roots = {project_root, *(Path(root).resolve() for root in roots)}
            registry_path = project_root / REGISTRY_REL
            if registry_path.is_file():
                registry = read_json(registry_path)
                if not isinstance(registry, dict) or not isinstance(registry.get("roots"), list):
                    raise ProtocolError("工作区根注册表结构不合法")
                for entry in registry["roots"]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("root"), str):
                        raise ProtocolError("工作区根注册表条目不合法")
                    lease_roots.add(Path(entry["root"]).resolve())
            for root in sorted(lease_roots - {project_root}, key=str):
                self._workspace_leases.append(WorkstationLease(root))
            self._initialize(address, project, trusted_local, campaign_dir,
                             research_runs, roots, chat_base_url, chat_model, chat_llm)
        except BaseException:
            if hasattr(self, "_monitor_stop"):
                self._monitor_stop.set()
            if hasattr(self, "_monitor_thread"):
                self._monitor_thread.join(timeout=2)
            if hasattr(self, "terminals"):
                self.terminals.close_all()
            if hasattr(self, "socket"):
                super().server_close()
            if hasattr(self, "job_threads_lock"):
                self._release_leases_after_jobs()
            else:
                self._release_workspace_leases()
            raise

    def _initialize(self, address, project, trusted_local, campaign_dir,
                    research_runs, workspace_roots, chat_base_url, chat_model, chat_llm):
        self.project = Path(project).resolve()
        self.primary_project = self.project
        self.mode = project_mode(self.project)
        if self.mode == "reproduction":
            # A process cannot legitimately remain inside an execution phase
            # across workstation restarts. Record that recovery in the same
            # authoritative event stream before serving any state.
            ReproductionTask(self.project).recover_interrupted()
        # 研究运行目录只能显式登记（--research-run，可重复）：不做磁盘扫描，
        # 假设环只展示用户点名的研究 run；每个目录只读投影其 research.sqlite。
        self._research_run_registry_path = self.project / ".popper" / "research-runs.json"
        self.research_runs_lock = threading.Lock()
        self.followup_create_lock = threading.Lock()
        self.study_init_lock = threading.Lock()
        self.bound_draft_lock = threading.Lock()
        self.benchmark_enqueue_lock = threading.Lock()
        saved_runs = []
        if self._research_run_registry_path.is_file():
            registry = read_json(self._research_run_registry_path)
            if (not isinstance(registry, dict)
                    or set(registry) != {"schema_version", "runs"}
                    or registry["schema_version"] != "1.0"
                    or not isinstance(registry["runs"], list)
                    or any(not isinstance(item, str) or not item
                           for item in registry["runs"])):
                raise ProtocolError("研究运行目录注册表结构不合法")
            saved_runs = registry["runs"]
        self._explicit_research_runs = tuple(Path(run_dir).resolve() for run_dir in research_runs)
        self.research_runs = list(dict.fromkeys(
            Path(run_dir).resolve() for run_dir in [*research_runs, *saved_runs]))
        self.token = secrets.token_urlsafe(32)
        self.trusted_local = trusted_local
        self.job_lock = threading.Lock()
        self.job = {"status": "idle"}
        self.campaign_dir = Path(campaign_dir) if campaign_dir else None
        if self.campaign_dir is not None and self.mode != "experiment":
            raise ProtocolError("--campaign 仅对实验项目生效（当前为论文复现模式）")
        self.campaign_lock = threading.Lock()
        self.campaign_job = {"status": "idle"}
        # 后台作业线程登记表：这些是 daemon 线程，进程退出时会被内核直接回收。
        # 如果没有地方能等它们收尾，「服务器已关闭 = 副作用已落盘」就是个假命题：
        # 实际表现为服务器关闭后仍有线程在写 run_dir / .popper（测试里是临时目录
        # 删不掉、Linux 上报 Directory not empty；状态文件还停在 idle）。监控线程
        # 是无限循环，刻意不登记，否则 wait_for_jobs 永远回不来。
        self.job_threads_lock = threading.Lock()
        self._job_threads: list[threading.Thread] = []
        # 审批恢复所用内置节点；测试可注入 fake 以便离线验证调用链。
        self._campaign_nodes = None
        self.ws_lock = threading.Lock()
        self.materialize_lock = threading.Lock()
        # 多工作区根：主根 = 项目目录；额外根仅经 CLI --workspace-root 显式登记。
        # 服务器内一律经 .workspace 属性取「当前根」的 Workspace，切换即全局生效。
        self.workspaces = WorkspaceHub(self.project, self.ws_lock)
        self.workspaces.register_cli_roots(workspace_roots)
        self._activate_workspace_project()
        # 自由对话：BYOK 端点必须成对提供（--chat-base-url/--chat-model），key 走
        # POPPER_API_KEY（make_chat_client 构造即校验，fail fast）。未配置时如实
        # configured=false 并拒绝发送；chat_llm 供测试注入 fake，绕过真实端点。
        self.chat_base_url = chat_base_url
        if bool(chat_base_url) != bool(chat_model):
            raise ProtocolError("--chat-base-url 和 --chat-model 必须同时提供")
        self.chat_model = chat_model
        self.chat_llm = chat_llm if chat_llm is not None else (
            make_chat_client(chat_base_url, chat_model,
                             enable_file_tools=trusted_local) if chat_base_url and os.environ.get("POPPER_API_KEY") else None)
        self.chat_harness = adapt_conversation_harness(self.chat_llm)
        # RLock：send_chat 持锁调用模型并把锁传给 ChatStore，读-算-写全程同锁互斥。
        self.chat_lock = threading.RLock()
        self._provider_cli_base, self._provider_cli_model = chat_base_url, chat_model
        self._provider_injected = chat_llm is not None
        self._runtime_provider_keys = {}
        self._turn_harnesses = {}
        self._service("providers").dispatch("refresh")
        self.turn_jobs_lock = threading.Lock()
        self._turn_jobs = {}
        self.agent_worker_id = "local-" + secrets.token_hex(8)
        self.agent_slots = threading.BoundedSemaphore(2)
        self.revision_job_events_lock = threading.Lock()
        self._revision_job_cancel_events = {}
        self.research_execution_lock = threading.RLock()
        # 启动恢复：任何上次进程退出时仍为 running 的模型 turn 都显式标记为
        # interrupted；旧 chat.json 只导入一次，之后 sessions.sqlite 是唯一权威。
        for root in self.workspaces.roots_payload():
            store = AgentSessionStore(root["root"])
            try:
                store.import_legacy_chat(Path(root["root"]) / CHAT_REL, chat_model)
                self._reconcile_agent_tool_receipts(root["root"], store)
                store.recover_interrupted()
            finally:
                store.close()
        # 真终端是高权限本地能力：只有 --trusted-local 才能创建 ConPTY，会话根固定为项目目录。
        self.terminals = TerminalManager(
            self.project, enabled=trusted_local,
            allowed_roots=(row["root"] for row in self.workspaces.roots_payload()))
        # SSE 订阅者：monitor 线程按资源指纹做 diff，只广播变化资源的完整补丁。
        self._subscribers: set[queue.Queue] = set()
        self._monitor_stop = threading.Event()
        super().__init__(address, Handler)
        self._start_monitor()
        self._resume_queued_agent_jobs()
        self._recover_revision_worktrees()
        self._recover_revision_execution_jobs()
        self._recover_design_execution_jobs()
        self._recover_benchmark_jobs()
        self._recover_study_jobs()

    def server_close(self):
        self._monitor_stop.set()
        if hasattr(self, "_monitor_thread"):
            self._monitor_thread.join(timeout=2)
        try:
            self.terminals.close_all()
        finally:
            super().server_close()
        self._release_leases_after_jobs()

    def _release_leases_after_jobs(self):
        return self._service('workspace').dispatch('_release_leases_after_jobs')

    def _release_workspace_leases(self):
        return self._service('workspace').dispatch('_release_workspace_leases')

    # ── 资源 payload：HTTP 端点与 SSE monitor 共用同一份构造逻辑 ──────────────

    @property
    def workspace(self):
        return self._service('workspace').dispatch('workspace')

    def _activate_workspace_project(self):
        return self._service('workspace').dispatch('_activate_workspace_project')

    def preview_project_setup(self, project_path, protocol=None, files=None):
        return self._service('workspace').dispatch('preview_project_setup', project_path, protocol, files)

    def prepare_project(self, project_path, protocol, files, approval_sha256):
        return self._service('workspace').dispatch('prepare_project', project_path, protocol, files, approval_sha256)

    def vendor_workspace_root(self):
        return self._service('workspace').dispatch('vendor_workspace_root')

    def _reconcile_agent_tool_receipts(self, root, store):
        return self._service('tools').dispatch('_reconcile_agent_tool_receipts', root, store)

    def research_run_for_study(self, study_id):
        return self._service('research').dispatch('research_run_for_study', study_id)

    def state_payload(self):
        return self._service('research').dispatch('state_payload')

    def workspace_payload(self):
        return self._service('workspace').dispatch('workspace_payload')

    def research_payload(self):
        return self._service('research').dispatch('research_payload')

    def _visible_research_runs(self, workspace_root=None):
        return self._service('research').dispatch('_visible_research_runs', workspace_root)

    def study_jobs_payload(self, study_id):
        return self._service('research').dispatch('study_jobs_payload', study_id)

    def enqueue_study_action(self, study_id, action, max_steps, approval_sha256, idempotency_key, envelope=None):
        return self._service('research').dispatch('enqueue_study_action', study_id, action, max_steps, approval_sha256, idempotency_key, envelope)

    def _run_study_job(self, run_dir, job_id):
        return self._service('research').dispatch('_run_study_job', run_dir, job_id)

    def _recover_study_jobs(self):
        return self._service('research').dispatch('_recover_study_jobs')

    def cancel_study_job(self, study_id, job_id):
        return self._service('research').dispatch('cancel_study_job', study_id, job_id)

    def preview_study_initialization(self, run_name, budget_cap):
        return self._service('research').dispatch('preview_study_initialization', run_name, budget_cap)

    def initialize_study_from_workbench(self, run_name, budget_cap, proposal_sha256):
        return self._service('research').dispatch('initialize_study_from_workbench', run_name, budget_cap, proposal_sha256)

    @staticmethod
    def _benchmark_job_store(root):
        return BenchmarkJobStore(Path(root) / ".popper" / "benchmark-jobs.sqlite")

    def _recover_benchmark_jobs(self):
        return self._service('research').dispatch('_recover_benchmark_jobs')

    def _run_benchmark_job(self, root, job_id):
        return self._service('research').dispatch('_run_benchmark_job', root, job_id)

    def enqueue_benchmark(self, run_name, idempotency_key):
        return self._service('research').dispatch('enqueue_benchmark', run_name, idempotency_key)

    def benchmark_jobs_payload(self):
        return self._service('research').dispatch('benchmark_jobs_payload')

    def benchmark_job_detail(self, job_id):
        return self._service('research').dispatch('benchmark_job_detail', job_id)

    def research_evidence_preview(self, study_id):
        return self._service('deliverables').dispatch('research_evidence_preview', study_id)

    def create_bound_draft_from_workbench(self, study_id, spec):
        return self._service('deliverables').dispatch('create_bound_draft_from_workbench', study_id, spec)

    def agent_bound_draft_preview(self, study_id, expected_state_version, title, author_note):
        return self._service('deliverables').dispatch('agent_bound_draft_preview', study_id, expected_state_version, title, author_note)

    def agent_create_bound_draft(self, study_id, expected_state_version, title, author_note, approval_sha256):
        return self._service('deliverables').dispatch('agent_create_bound_draft', study_id, expected_state_version, title, author_note, approval_sha256)

    def bound_drafts_payload(self, study_id):
        return self._service('deliverables').dispatch('bound_drafts_payload', study_id)

    def bound_draft_detail(self, study_id, draft_id):
        return self._service('deliverables').dispatch('bound_draft_detail', study_id, draft_id)

    def review_bound_draft_from_workbench(self, study_id, draft_id, reason):
        return self._service('deliverables').dispatch('review_bound_draft_from_workbench', study_id, draft_id, reason)

    def followup_preview(self, parent_study_id):
        return self._service('research').dispatch('followup_preview', parent_study_id)

    def create_followup_from_workbench(self, parent_study_id, new_project_name, new_run_name, new_test_path, candidates, budget, objective, expected_parent_manifest_sha256):
        return self._service('research').dispatch('create_followup_from_workbench', parent_study_id, new_project_name, new_run_name, new_test_path, candidates, budget, objective, expected_parent_manifest_sha256)

    def _verified_research_state_for_agent(self, study_id, development_observation_id=None):
        return self._service('research').dispatch('_verified_research_state_for_agent', study_id, development_observation_id)

    def research_status_for_agent(self, study_id):
        return self._service('research').dispatch('research_status_for_agent', study_id)

    def research_design_for_agent(self, study_id, design_id):
        return self._service('research').dispatch('research_design_for_agent', study_id, design_id)

    def research_claim_for_agent(self, study_id, claim_id):
        return self._service('research').dispatch('research_claim_for_agent', study_id, claim_id)

    def research_observation_for_agent(self, study_id, observation_id):
        return self._service('research').dispatch('research_observation_for_agent', study_id, observation_id)

    def revision_payload(self, revision_id):
        return self._service('research').dispatch('revision_payload', revision_id)

    def migrate_revision_diff(self, revision_id):
        return self._service('research').dispatch('migrate_revision_diff', revision_id)

    def _revision_worktree_manager(self, run_dir):
        run_dir = Path(run_dir).resolve()
        namespace = hashlib.sha256(str(run_dir).encode("utf-8")).hexdigest()[:20]
        root = Path(tempfile.gettempdir()) / "popper-revision-worktrees" / namespace
        return RevisionWorktreeManager(
            run_dir / "revision-worktrees.sqlite", root, self.project,
            run_dir / "revisions")

    @staticmethod
    def _revision_job_store(run_dir):
        return RevisionExecutionJobStore(Path(run_dir) / "revision-execution-jobs.sqlite")

    @staticmethod
    def _design_job_store(run_dir):
        return DesignExecutionJobStore(Path(run_dir) / "design-execution-jobs.sqlite")

    def _recover_design_execution_jobs(self):
        return self._service('research').dispatch('_recover_design_execution_jobs')

    def _run_design_job(self, run_dir, job_id):
        return self._service('research').dispatch('_run_design_job', run_dir, job_id)

    def _run_design_job_locked(self, run_dir, job_id):
        return self._service('research').dispatch('_run_design_job_locked', run_dir, job_id)

    def _start_design_job(self, run_dir, job_id):
        return self._service('research').dispatch('_start_design_job', run_dir, job_id)

    def enqueue_design_execution(self, study_id, design_id, expected_version, design_sha256):
        return self._service('research').dispatch('enqueue_design_execution', study_id, design_id, expected_version, design_sha256)

    def design_job_payload(self, job_id):
        return self._service('research').dispatch('design_job_payload', job_id)

    def cancel_design_job(self, job_id, reason):
        return self._service('research').dispatch('cancel_design_job', job_id, reason)

    def retry_design_job(self, job_id, expected_attempt, design_sha256):
        return self._service('research').dispatch('retry_design_job', job_id, expected_attempt, design_sha256)

    def design_jobs_payload(self):
        return self._service('research').dispatch('design_jobs_payload')

    def _recover_revision_worktrees(self):
        return self._service('research').dispatch('_recover_revision_worktrees')

    def _recover_revision_execution_jobs(self):
        return self._service('research').dispatch('_recover_revision_execution_jobs')

    @staticmethod
    def _public_worktree(record):
        return {key: value for key, value in record.items() if key != "proposal"}

    def revision_worktrees_payload(self):
        return self._service('research').dispatch('revision_worktrees_payload')

    def revision_worktree_events_payload(self, worktree_id):
        return self._service('research').dispatch('revision_worktree_events_payload', worktree_id)

    def create_revision_worktree(self, revision_id, idempotency_key):
        return self._service('research').dispatch('create_revision_worktree', revision_id, idempotency_key)

    def remove_revision_worktree(self, worktree_id):
        return self._service('research').dispatch('remove_revision_worktree', worktree_id)

    def _worktree_operation(self, worktree_id, operation):
        return self._service('research').dispatch('_worktree_operation', worktree_id, operation)

    def propose_worktree_revision(self, worktree_id):
        return self._service('research').dispatch('propose_worktree_revision', worktree_id)

    def submit_worktree_revision(self, worktree_id, proposal_sha256):
        return self._service('research').dispatch('submit_worktree_revision', worktree_id, proposal_sha256)

    def _run_revision_job(self, run_dir, job_id):
        return self._service('research').dispatch('_run_revision_job', run_dir, job_id)

    def _run_revision_job_locked(self, run_dir, job_id):
        return self._service('research').dispatch('_run_revision_job_locked', run_dir, job_id)

    def _start_revision_job(self, run_dir, job_id):
        return self._service('research').dispatch('_start_revision_job', run_dir, job_id)

    def execute_worktree_revision(self, worktree_id):
        return self._service('research').dispatch('execute_worktree_revision', worktree_id)

    def execute_initial_revision(self, revision_id, manifest_sha256):
        return self._service('research').dispatch('execute_initial_revision', revision_id, manifest_sha256)

    def execute_agent_revision(self, revision_id, manifest_sha256):
        return self._service('research').dispatch('execute_agent_revision', revision_id, manifest_sha256)

    def _execute_approved_revision(self, revision_id, manifest_sha256, *, allow_repair):
        return self._service('research').dispatch('_execute_approved_revision', revision_id, manifest_sha256, allow_repair=allow_repair)

    def revision_job_payload(self, job_id):
        return self._service('research').dispatch('revision_job_payload', job_id)

    def cancel_revision_job(self, job_id, reason):
        return self._service('research').dispatch('cancel_revision_job', job_id, reason)

    def campaign_payload(self):
        return self._service('research').dispatch('campaign_payload')

    # ── 自由对话：BYOK 通用聊天，独立于事件溯源链 ────────────────────────────

    def _chat_system_prompt(self, workspace_root=None):
        return self._service('sessions').dispatch('_chat_system_prompt', workspace_root)

    def chat_payload(self):
        return self._service('sessions').dispatch('chat_payload')

    def _chat_payload(self, store):
        return self._service('sessions').dispatch('_chat_payload', store)

    def _agent_private_paths(self, files):
        return self._service('tools').dispatch('_agent_private_paths', files)

    def _require_agent_file_disclosure(self, files, path):
        return self._service('tools').dispatch('_require_agent_file_disclosure', files, path)

    def _agent_file_list(self, files):
        return self._service('tools').dispatch('_agent_file_list', files)

    def _agent_tool_public(self, call):
        return self._service('tools').dispatch('_agent_tool_public', call)

    def sessions_payload(self, scope=None):
        return self._service('sessions').dispatch('sessions_payload', scope)

    def _context_root(self, workspace_id):
        return self._service('sessions').dispatch('_context_root', workspace_id)

    def _context_facts(self, root):
        return self._service('sessions').dispatch('_context_facts', root)

    def task_context_payload(self, workspace_id, session_id=None):
        return self._service('sessions').dispatch('task_context_payload', workspace_id, session_id)

    def task_context_preview(self, workspace_id, session_id, task_id=None, source_session_id=None, study_ids=None):
        return self._service('sessions').dispatch('task_context_preview', workspace_id, session_id, task_id, source_session_id, study_ids)

    def commit_task_context(self, workspace_id, session_id, request):
        return self._service('sessions').dispatch('commit_task_context', workspace_id, session_id, request)

    def _assemble_session_context(self, store, session_id, system, user_text=None, feedback_source_turn_id=None):
        return Workstation._service(self, 'sessions').dispatch('_assemble_session_context', store, session_id, system, user_text, feedback_source_turn_id)

    def _task_source_for_call(self, root, call):
        return self._service('sessions').dispatch('_task_source_for_call', root, call)

    def session_events_payload(self, session_id, *, after=0, limit=200):
        return self._service('sessions').dispatch('session_events_payload', session_id, after=after, limit=limit)

    def session_tool_events_payload(self, session_id, *, after=0, limit=200):
        return self._service('sessions').dispatch('session_tool_events_payload', session_id, after=after, limit=limit)

    def project_files_payload(self, *, relative=None, workspace_id=None):
        return self._service('workspace').dispatch('project_files_payload', relative=relative, workspace_id=workspace_id)

    def save_project_file(self, *, workspace_id, relative, content, expected_sha256):
        return self._service('workspace').dispatch('save_project_file', workspace_id=workspace_id, relative=relative, content=content, expected_sha256=expected_sha256)

    def preview_initial_revision_from_workbench(self, spec):
        return self._service('sessions').dispatch('preview_initial_revision_from_workbench', spec)

    def submit_initial_revision_from_workbench(self, spec, proposal_sha256):
        return self._service('sessions').dispatch('submit_initial_revision_from_workbench', spec, proposal_sha256)

    def preview_project_file(self, *, workspace_id, relative, content, expected_sha256):
        return self._service('workspace').dispatch('preview_project_file', workspace_id=workspace_id, relative=relative, content=content, expected_sha256=expected_sha256)

    def create_agent_session(self, title, harness='chat', workspace_id=None):
        return self._service('sessions').dispatch('create_agent_session', title, harness, workspace_id)

    def _check_session_workspace(self, workspace_id):
        return self._service('sessions').dispatch('_check_session_workspace', workspace_id)

    def update_agent_session(self, workspace_id, session_id, expected_updated, **changes):
        return self._service('sessions').dispatch('update_agent_session', workspace_id, session_id, expected_updated, **changes)

    def select_agent_session(self, session_id, workspace_id=None):
        return self._service('sessions').dispatch('select_agent_session', session_id, workspace_id)

    def set_session_token_budget(self, session_id, token_budget):
        return self._service('sessions').dispatch('set_session_token_budget', session_id, token_budget)

    def register_pricing_catalog(self, name, currency, prices):
        return self._service('sessions').dispatch('register_pricing_catalog', name, currency, prices)

    def set_session_price_catalog(self, session_id, catalog_id):
        return self._service('sessions').dispatch('set_session_price_catalog', session_id, catalog_id)

    def set_session_cost_budget(self, session_id, cost_budget_micros):
        return self._service('sessions').dispatch('set_session_cost_budget', session_id, cost_budget_micros)

    @staticmethod
    def _token_reservation(system_prompt, messages):
        # OpenAI-compatible tokenizers operate over bytes/subwords; UTF-8 bytes
        # are a conservative upper bound for prompt tokens without coupling the
        # kernel to a provider tokenizer. Output is bounded by max_tokens.
        return len(canonical({"system": system_prompt, "messages": messages,
                              "tools": CHAT_TOOLS}).encode("utf-8")) + 1024 + MODEL_MAX_TOKENS

    @staticmethod
    def _reserve_context(value):
        keys = ("system", "messages", "tools", "request_limits") if "request_limits" in value else ("system", "messages", "tools")
        return len(canonical({key: value[key] for key in keys}).encode("utf-8")) + 1024 + MODEL_MAX_TOKENS

    def _require_session_harness(self, store, session_id):
        return self._service('sessions').dispatch('_require_session_harness', store, session_id)

    def _run_session_turn(self, root, session_id, turn_id, trace_id, system_prompt, cancel_event=None):
        return self._service('sessions').dispatch('_run_session_turn', root, session_id, turn_id, trace_id, system_prompt, cancel_event)

    def _invoke_budgeted_harness(self, frozen, turn_id, system_prompt, history, cancel_event, on_delta, on_progress=None):
        return self._service('sessions').dispatch('_invoke_budgeted_harness', frozen, turn_id, system_prompt, history, cancel_event, on_delta, on_progress)

    def _validate_turn_input(self, text):
        return self._service('sessions').dispatch('_validate_turn_input', text)

    def send_session_turn(self, session_id, text, idempotency_key, trace_id):
        return self._service('sessions').dispatch('send_session_turn', session_id, text, idempotency_key, trace_id)

    def launch_session_turn(self, session_id, text, idempotency_key, trace_id, workspace_id=None):
        return self._service('sessions').dispatch('launch_session_turn', session_id, text, idempotency_key, trace_id, workspace_id)

    def _schedule_agent_job(self, root, job, turn, system_prompt):
        return self._service('sessions').dispatch('_schedule_agent_job', root, job, turn, system_prompt)

    def retry_session_turn(self, turn_id, idempotency_key, trace_id):
        return self._service('sessions').dispatch('retry_session_turn', turn_id, idempotency_key, trace_id)

    def steer_session_turn(self, turn_id, text, idempotency_key, trace_id):
        return self._service('sessions').dispatch('steer_session_turn', turn_id, text, idempotency_key, trace_id)

    def _execute_agent_job(self, root, job, turn, system_prompt, cancel_event):
        return self._service('sessions').dispatch('_execute_agent_job', root, job, turn, system_prompt, cancel_event)

    def _resume_queued_agent_jobs(self):
        return self._service('sessions').dispatch('_resume_queued_agent_jobs')

    def cancel_session_turn(self, turn_id, reason):
        return self._service('sessions').dispatch('cancel_session_turn', turn_id, reason)

    def propose_agent_tool(self, session_id, turn_id, tool_name, arguments, idempotency_key, trace_id):
        return self._service('tools').dispatch('propose_agent_tool', session_id, turn_id, tool_name, arguments, idempotency_key, trace_id)

    def decide_agent_tool(self, call_id, decision, reason):
        return self._service('tools').dispatch('decide_agent_tool', call_id, decision, reason)

    def cancel_agent_tool(self, call_id, reason):
        return self._service('tools').dispatch('cancel_agent_tool', call_id, reason)

    def continue_session_after_tools(self, source_turn_id, workspace_root=None):
        return self._service('sessions').dispatch('continue_session_after_tools', source_turn_id, workspace_root)

    def _maybe_continue_after_tool(self, call, workspace_root=None):
        return self._service('sessions').dispatch('_maybe_continue_after_tool', call, workspace_root)

    def _wake_agent_research_job(self, run_dir, job_id):
        return self._service('tools').dispatch('_wake_agent_research_job', run_dir, job_id)

    def _tool_result_for_call(self, root, call):
        return self._service('tools').dispatch('_tool_result_for_call', root, call)

    def execute_agent_tool(self, call_id):
        return self._service('tools').dispatch('execute_agent_tool', call_id)

    def send_chat(self, text):
        return self._service('sessions').dispatch('send_chat', text)

    def terminal_command(self, payload):
        return self._service('workspace').dispatch('terminal_command', payload)

    # ── SSE：资源级 diff 广播 ────────────────────────────────────────────────

    def subscribe(self):
        return self._service('events').dispatch('subscribe')

    def unsubscribe(self, q):
        return self._service('events').dispatch('unsubscribe', q)

    def _broadcast(self, patch):
        return self._service('events').dispatch('_broadcast', patch)

    @staticmethod
    def _path_sig(path):
        """文件/目录的轻量指纹（mtime_ns + size），缺失返回 None。"""
        if not path.exists():
            return None
        if path.is_file():
            st = path.stat()
            return "f", st.st_mtime_ns, st.st_size
        items = []
        for child in sorted(path.rglob("*")):
            try:
                st = child.stat()
            except OSError:
                continue
            if child.is_file():
                items.append((str(child.relative_to(path)), st.st_mtime_ns, st.st_size))
        return "d", tuple(items)

    @staticmethod
    def _job_sig(job):
        result = job.get("result")
        result_sig = (hashlib.sha256(canonical(result).encode("utf-8")).hexdigest()
                      if result is not None else None)
        return job.get("status"), job.get("action"), job.get("error"), result_sig

    @staticmethod
    def _vendor_sig():
        """integrations/runs 下 vendor 产物（检索/假设树）的目录级指纹。"""
        base = Path(__file__).resolve().parents[1] / "integrations" / "runs"
        if not base.is_dir():
            return None
        sig = []
        for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            for sub in (run_dir, run_dir / "literature", run_dir / ".arbor"):
                try:
                    sig.append((run_dir.name, sub.name, sub.stat().st_mtime_ns))
                except OSError:
                    continue
        return tuple(sig)

    def _fingerprints(self):
        return self._service('events').dispatch('_fingerprints')

    def _start_monitor(self):
        return self._service('events').dispatch('_start_monitor')


    def launch(self, action):
        return self._service('research').dispatch('launch', action)

    def _start_job(self, target):
        return self._service('research').dispatch('_start_job', target)

    def wait_for_jobs(self, timeout=120):
        return self._service('research').dispatch('wait_for_jobs', timeout)

    def approve_campaign(self, decision, reason='', materialize=None):
        return self._service('research').dispatch('approve_campaign', decision, reason, materialize)


class Handler(BaseHTTPRequestHandler):
    def _handle_provider_config(self, method, route):
        return self._service("provider_routes").dispatch("handle", method, route)

    def do_PUT(self):
        route = urlparse(self.path).path
        if not self.host_ok():
            return self.send(403, {"error": "Invalid Host"})
        if not self._handle_provider_config("PUT", route):
            self.send(404, {"error": "Not found"})

    def _service(self, name):
        from importlib import import_module
        cache = getattr(self, "_service_instances", None)
        if cache is None:
            cache = {}
            self._service_instances = cache
        if name not in cache:
            module = import_module("popper.services." + name)
            service_type = getattr(module, "".join(part.title() for part in name.split("_")) + "Service")
            cache[name] = service_type(self)
        return cache[name]

    def log_message(self, *_):
        pass

    def host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    @staticmethod
    def _frontend_dist():
        source_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
        if (source_dist / "index.html").is_file():
            return source_dist
        return Path(__file__).resolve().parent / "web"

    _MIME = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".mjs": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".tex": "text/plain; charset=utf-8",
        ".map": "application/json; charset=utf-8",
        ".ico": "image/x-icon",
        ".png": "image/png",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }

    def _serve_static(self):
        dist = self._frontend_dist()
        if not (dist / "index.html").is_file():
            return self.send(503, {"error": "前端资源缺失：请重新安装完整的 Popper 包"})
        rel = (self.path or "/").lstrip("/").split("?", 1)[0] or "index.html"
        if rel.split("/")[0] == "api":
            return self.send(404, {"error": "Not found"})
        target = (dist / rel).resolve()
        if not target.is_relative_to(dist.resolve()):
            return self.send(403, {"error": "Forbidden"})
        if target.is_file():
            mime = self._MIME.get(target.suffix.lower(), "application/octet-stream")
            return self.send(200, target.read_bytes(), mime)
        # SPA 回退
        return self.send(200, (dist / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")

    def _drain_rejected_post(self):
        # Windows can reset a connection carrying an unread request body before
        # the client receives our 403. Never parse or act on rejected input.
        if self.command != "POST":
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if 0 < size <= 65_536:
                previous_timeout = self.connection.gettimeout()
                try:
                    self.connection.settimeout(0.5)
                    self.rfile.read(size)
                finally:
                    self.connection.settimeout(previous_timeout)
        except (OSError, ValueError):
            pass

    def authorized(self):
        allowed = self.host_ok() and secrets.compare_digest(
            self.headers.get("X-Popper-Token", ""), self.server.token)
        if not allowed:
            self._drain_rejected_post()
        return allowed

    def origin_ok(self):
        allowed = self.headers.get("Origin") in {None, f"http://{self.headers.get('Host')}"}
        if not allowed:
            self._drain_rejected_post()
        return allowed

    def send(self, status, content, mime="application/json; charset=utf-8", extra_headers=None):
        if isinstance(content, bytes):
            body = content
        else:
            body = (canonical(content) if mime.startswith("application/json") else content).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _handle_task_context(self, method, route, parts, query):
        return self._service('context_routes').dispatch('_handle_task_context', method, route, parts, query)

    def do_GET(self):
        return self._service('http_routes').dispatch('do_GET')

    def _serve_events(self, token):
        return self._service('http_routes').dispatch('_serve_events', token)

    def do_POST(self):
        return self._service('http_routes').dispatch('do_POST')

    def _post_adjudicate(self, payload):
        return self._service('campaign_routes').dispatch('_post_adjudicate', payload)

    def _post_campaign(self, payload):
        return self._service('campaign_routes').dispatch('_post_campaign', payload)

    def _post_file(self, payload):
        return self._service('workspace_routes').dispatch('_post_file', payload)

    def _post_workspace(self, payload):
        return self._service('workspace_routes').dispatch('_post_workspace', payload)

    def _post_materialize(self, payload):
        return self._service('delivery_routes').dispatch('_post_materialize', payload)


def serve(project, port, trusted_local=False, campaign_dir=None, research_runs=(),
          workspace_roots=(), chat_base_url=None, chat_model=None):
    project_mode(project)
    server = Workstation(("127.0.0.1", port), project, trusted_local, campaign_dir,
                         research_runs=research_runs, workspace_roots=workspace_roots,
                         chat_base_url=chat_base_url, chat_model=chat_model)
    print(f"Popper workstation: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
