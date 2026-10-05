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
        "untested": "未实验", "supported_in_scope": "范围内支持",
        "contradicted_in_scope": "范围内证伪", "inconclusive": "证据不足", "retired": "已退役",
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
        if bool(chat_base_url) != bool(chat_model):
            raise ProtocolError("--chat-base-url 和 --chat-model 必须同时提供")
        self.chat_model = chat_model
        self.chat_llm = chat_llm if chat_llm is not None else (
            make_chat_client(chat_base_url, chat_model,
                             enable_file_tools=trusted_local) if chat_base_url else None)
        self.chat_harness = adapt_conversation_harness(self.chat_llm)
        # RLock：send_chat 持锁调用模型并把锁传给 ChatStore，读-算-写全程同锁互斥。
        self.chat_lock = threading.RLock()
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
        if self.wait_for_jobs(timeout=0):
            # Detached jobs may still write project state after the listener
            # closes. Keep ownership until their final side effects land.
            def release_after_jobs():
                while self.wait_for_jobs(timeout=1):
                    pass
                self._release_workspace_leases()
            self._lease_reaper = threading.Thread(target=release_after_jobs, daemon=True)
            self._lease_reaper.start()
        else:
            self._release_workspace_leases()

    def _release_workspace_leases(self):
        for lease in reversed(self._workspace_leases):
            lease.close()

    # ── 资源 payload：HTTP 端点与 SSE monitor 共用同一份构造逻辑 ──────────────

    @property
    def workspace(self):
        """当前工作区根的 Workspace：多根切换后所有 Quest/产物读写都落在当前根。"""
        return self.workspaces.current()

    def _activate_workspace_project(self):
        """Use an initialized selected root as the active experiment project."""
        self.navigation_version = getattr(self, "navigation_version", 0) + 1
        root = self.workspace.root
        try:
            mode = project_mode(root)
        except ProtocolError:
            root = self.primary_project
            mode = project_mode(root)
        self.project = root
        self.mode = mode
        registry_path = root / ".popper" / "research-runs.json"
        self._research_run_registry_path = registry_path
        if registry_path.is_file():
            registry = read_json(registry_path)
            if (not isinstance(registry, dict) or registry.get("schema_version") != "1.0"
                    or not isinstance(registry.get("runs"), list)):
                raise ProtocolError("研究运行目录注册表结构不合法")
            with self.research_runs_lock:
                for item in registry["runs"]:
                    if not isinstance(item, str) or not item:
                        raise ProtocolError("研究运行目录注册表条目不合法")
                    run = Path(item).resolve()
                    if run not in self.research_runs:
                        self.research_runs.append(run)

    def preview_project_setup(self, project_path, protocol=None, files=None):
        if not self.trusted_local:
            raise ProtocolError("项目准备需要可信本机模式")
        return preview_project_preparation(project_path, protocol, files)

    def prepare_project(self, project_path, protocol, files, approval_sha256):
        if not self.trusted_local:
            raise ProtocolError("项目准备需要可信本机模式")
        with self.study_init_lock:
            result = commit_project_preparation(project_path, protocol, files, approval_sha256)
            root = Path(result["project_path"])
            with self.ws_lock:
                if root not in {Path(row["root"]).resolve()
                                for row in self.workspaces.roots_payload()}:
                    lease = WorkstationLease(root)
                    try:
                        self.workspaces.register_cli_roots([root])
                    except Exception:
                        lease.close()
                        raise
                    self._workspace_leases.append(lease)
                wid = next(row["wid"] for row in self.workspaces.roots_payload()
                           if Path(row["root"]).resolve() == root)
                self.workspaces.switch(wid)
                self._activate_workspace_project()
                with self.terminals.lock:
                    self.terminals.allowed_roots.add(root)
            return {**result, "workspace_id": wid}

    def vendor_workspace_root(self):
        """Capture one authorized workspace root for a read-only vendor projection."""
        with self.ws_lock:
            return self.workspace.root

    def _reconcile_agent_tool_receipts(self, root, store):
        """Close the write-committed/tool-result-not-committed crash window.

        Only a matching durable Workspace, Research, or file-edit receipt proves completion.
        Missing or conflicting receipts remain uncertain and are marked
        interrupted by the normal recovery path; no command is replayed here.
        """
        workspace = Workspace(root)
        for call in store.running_tool_calls():
            try:
                key = "tool-" + call["call_id"]
                if call["tool_name"] in revision_tools.REVISION_RISKS:
                    result = agent_revisions.reconcile(self, call, root)
                elif call["tool_name"] == "workspace.file_replace":
                    validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                    result = ProjectFiles(root).edit_receipt(
                        call["call_id"], call["arguments"])
                elif call["tool_name"] == "research.execute_design":
                    args = validate_agent_tool_arguments(
                        call["tool_name"], call["arguments"])
                    run_dir = self.research_run_for_study(args["study_id"])
                    if not (run_dir / "design-execution-jobs.sqlite").is_file():
                        continue
                    jobs = self._design_job_store(run_dir)
                    try:
                        integrity = jobs.verify()
                        job = jobs.for_design(args["design_id"]) if integrity["ok"] else None
                        result = ({"research_run": str(run_dir), "job": job}
                                  if job is not None
                                  and job["design_sha256"] == args["design_sha256"] else None)
                    finally:
                        jobs.close()
                elif call["tool_name"] == "research.study_action":
                    args = validate_agent_tool_arguments(
                        call["tool_name"], call["arguments"])
                    run_dir = self.research_run_for_study(args["study_id"])
                    if not (run_dir / "study-jobs.sqlite").is_file():
                        continue
                    jobs = StudyJobStore(run_dir)
                    try:
                        jobs.verify()
                        job = jobs.by_key(key)
                        request = {**args}
                        result = ({"research_run": str(run_dir), "job": job}
                                  if job is not None and job["request"] == request else None)
                    finally:
                        jobs.close()
                elif call["tool_name"] in {"research.create_design",
                                         "research.create_hypothesis",
                                         "research.freeze_design"}:
                    args = call["arguments"]
                    study_id, action, payload, expected = agent_research_command(
                        call["tool_name"], args)
                    run_dir = self.research_run_for_study(study_id)
                    if not (run_dir / "research.sqlite").is_file():
                        continue
                    research_store = ResearchStore(run_dir / "research.sqlite")
                    try:
                        receipt = research_store.command_receipt(key)
                        integrity = research_store.verify() if receipt is not None else None
                        result = (research_store.execute_command(
                            study_id, action, payload,
                            idempotency_key=key,
                            expected_version=expected,
                            actor="agent", trace_id=call["trace_id"])
                            if receipt is not None and integrity["ok"]
                            and receipt["study_id"] == study_id
                            and receipt["actor"] == "agent" else None)
                    finally:
                        research_store.close()
                else:
                    command, _ = agent_workspace_command(
                        call["tool_name"], call["arguments"])
                    result = workspace.command_receipt(command, idempotency_key=key)
                if result is not None:
                    store.finish_tool_call(call["call_id"], result=result)
            except Exception:
                # A malformed or conflicting receipt is not proof of success.
                continue

    def research_run_for_study(self, study_id):
        """Resolve a study only within explicitly registered research runs."""
        if not isinstance(study_id, str) or not study_id:
            raise ProtocolError("study_id 必须是非空字符串")
        matches = []
        for run_dir in self.research_runs:
            manifest_path = run_dir / "research.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = read_json(manifest_path)
            except Exception:
                continue
            if manifest.get("study_id") == study_id:
                matches.append(run_dir)
        if not matches:
            raise LookupError(f"未登记 study: {study_id}")
        if len(matches) != 1:
            raise ProtocolError(f"study 登记冲突: {study_id}")
        return matches[0]

    def state_payload(self):
        """构造 GET /api/state 的响应（含后端单一定义的阶段标签 phases）。"""
        if self.mode == "experiment":
            exp = Experiment(self.project)
            try:
                events = [{"seq": r["seq"], "kind": r["kind"], "payload": json.loads(r["payload"])}
                          for r in exp.db.execute("SELECT seq,kind,payload FROM events ORDER BY seq DESC LIMIT 50")]
                runs = [dict(row) for row in exp.db.execute("SELECT id,split,status FROM runs ORDER BY rowid")]
                payload = {"mode": "experiment", "state": exp.state(), "results": exp.results(),
                           "runs": runs, "events": events,
                           "review": build_review_payload(exp.state())}
            finally:
                exp.close()
        else:
            task = ReproductionTask(self.project)
            state = task.audit()
            history = task.event_history()
            manifest = state["manifest"]
            outputs = state.get("run", {}).get("output_hashes", {}) if state.get("run") else {}
            artifacts = [{"path": name, "sha256": value,
                          "bytes": (self.project / name).stat().st_size}
                         for name, value in outputs.items()]
            result_path = next((name for name in outputs if name.endswith("results.json")), None)
            report_path = next((name for name in outputs if name.lower().endswith("report.md")), None)
            payload = {"mode": "reproduction", "state": state,
                       "paper": read_json(self.project / manifest["paper_record"]),
                       "protocol": read_json(self.project / manifest["protocol"]),
                       "result": read_json(self.project / result_path) if result_path else None,
                       "report": (self.project / report_path).read_text(encoding="utf-8")[:100000]
                                 if report_path else None,
                       "artifacts": artifacts,
                       "events": [{"seq": event["seq"], "kind": event["event_type"],
                                   "payload": {key: value for key, value in event["payload"].items()
                                               if key != "state"}}
                                  for event in reversed(history["events"][-50:])],
                       "reproduction_integrity": history["integrity"],
                       "research_projection": reproduction_research_projection(
                           self.project, state)}
        payload.update({"job": self.job, "trusted_local": self.trusted_local,
                        "terminal_capability": self.terminals.capability(),
                        # 实验状态机阶段标签：后端单一定义，前端不得再硬编码。
                        "phases": PHASES,
                        # 事件 kind 与阶段状态中文标签：后端单一下发，前端不再硬编码或回退。
                        "event_names": EVENT_NAMES,
                        "stage_status_labels": STAGE_STATUS_LABELS,
                        # 评审门禁 R1-R7/状态/7-mode 中文标签：后端单一下发，前端不再硬编码或回退。
                        "risk_names": RISK_NAMES,
                        "gate_status_labels": GATE_STATUS_LABELS,
                        "mode7_names": MODE7_NAMES})
        return payload

    def workspace_payload(self):
        """构造 GET /api/workspace 的响应（当前根的 Quest 状态 + 根注册表）。"""
        with self.ws_lock:
            ws = self.workspaces.current()
            if ws.is_empty():
                ws.seed_if_empty(ws.project_spec(), ws.current_phase())
            data = ws.read()
            roots = self.workspaces.roots_payload()
            current_root = self.workspaces.current_wid()
            for root in roots:
                root["entity_ref"] = parse_id(
                    root["wid"], EntityKind.WORKSPACE).as_dict()
            for folder in data["folders"]:
                for task in folder["tasks"]:
                    task["entity_ref"] = parse_id(
                        task["quid"], EntityKind.QUEST).as_dict()
        public_meta = {key: value for key, value in data["meta"].items()
                       if not key.startswith("_")}
        return {"schema_version": data["schema_version"], "meta": public_meta,
                "folders": data["folders"],
                "deliverables": ws.list_deliverables(data),
                "roots": roots, "current_root": current_root,
                "environment_label": getattr(self, "environment_label", None),
                # 稿件模板与 AI 披露口径中文标签：后端单一下发，前端不再硬编码或回退。
                "template_labels": TEMPLATE_LABELS,
                "disclosure_labels": DISCLOSURE_LABELS,
                # 合法模板与披露口径取值清单：后端单一下发，前端不再硬编码。
                "valid_templates": sorted(VALID_TEMPLATES),
                "valid_disclosures": sorted(d for d in VALID_DISCLOSURES if d)}

    def research_payload(self):
        """构造 GET /api/research 的响应：已登记研究 run 的只读投影。

        每个 run 目录独立投影，失败以 ok:false + reason 原样呈现，不吞错误。
        """
        runs = [{"run_dir": str(run_dir), **read_research_state(run_dir)}
                for run_dir in self._visible_research_runs()]
        return {"runs": runs, "status_labels": RESEARCH_STATUS_LABELS,
                "trusted_local": self.trusted_local}

    def _visible_research_runs(self, workspace_root=None):
        """Show only runs registered to the selected workspace, plus CLI roots on the primary."""
        if workspace_root is None:
            with self.ws_lock:
                root = self.workspace.root.resolve()
        else:
            root = Path(workspace_root).resolve()
        registry_path = root / ".popper" / "research-runs.json"
        registered = []
        if registry_path.is_file():
            registry = read_json(registry_path)
            if (not isinstance(registry, dict) or registry.get("schema_version") != "1.0"
                    or not isinstance(registry.get("runs"), list)):
                raise ProtocolError("研究运行目录注册表结构不合法")
            registered = [Path(item).resolve() for item in registry["runs"]
                          if isinstance(item, str) and item]
        if root == self.primary_project:
            registered = [*self._explicit_research_runs, *registered]
        allowed = set(registered)
        with self.research_runs_lock:
            return [run_dir for run_dir in self.research_runs if run_dir in allowed]

    def study_jobs_payload(self, study_id):
        run_dir = self.research_run_for_study(study_id)
        if not (run_dir / "study-jobs.sqlite").exists():
            return {"jobs": []}
        store = StudyJobStore(run_dir)
        try:
            store.verify()
            return {"jobs": store.list()}
        finally:
            store.close()

    def enqueue_study_action(self, study_id, action, max_steps, approval_sha256, idempotency_key):
        if not self.trusted_local:
            raise ProtocolError("Study 执行需要可信本机模式")
        run_dir = self.research_run_for_study(study_id)
        request = {"study_id": study_id, "action": action, "max_steps": max_steps,
                   "approval_sha256": approval_sha256}
        store = StudyJobStore(run_dir)
        try:
            store.verify()
            prior = store.by_key(idempotency_key)
            if prior:
                if prior["request"] != request:
                    raise ProtocolError("幂等键已绑定不同的 Study 请求")
                return prior
            preview = preview_study_action(run_dir, action, max_steps)
            if preview["approval_sha256"] != approval_sha256:
                raise ProtocolError("研究状态已变化，请重新预览")
            job = store.enqueue(idempotency_key, request)
        finally:
            store.close()
        self._start_job(lambda: self._run_study_job(run_dir, job["job_id"]))
        return job

    def _run_study_job(self, run_dir, job_id):
        with self.research_execution_lock:
            store = StudyJobStore(run_dir)
            controller = None
            try:
                if store.get(job_id)["status"] != "queued":
                    return
                job = store.claim(job_id)
                request = job["request"]
                try:
                    preview = preview_study_action(run_dir, request["action"], request["max_steps"])
                    if preview["approval_sha256"] != request["approval_sha256"]:
                        raise ProtocolError("排队期间研究状态已变化，请重新预览")
                    from .research.controller import ResearchController
                    controller = controller_for_run(run_dir)
                    cancelled = lambda: store.get(job_id)["cancel_requested"]
                    result = (controller.run(sandboxed=True, max_steps=request["max_steps"],
                                             auto_confirm=False, should_cancel=cancelled)
                              if request["action"] == "run" else
                              controller.confirm(sandboxed=True, should_cancel=cancelled))
                    store.finish(job_id, "succeeded", result={"phase": result["phase"],
                        "observation_ids": [row["observation_id"] for row in result["observations"]],
                        "claim_ids": [row["claim_id"] for row in result["claims"]]})
                except Exception as error:
                    store.finish(job_id, "cancelled" if store.get(job_id)["cancel_requested"] else "failed",
                                 error=f"{type(error).__name__}: {error}")
            finally:
                if controller is not None:
                    controller.close()
                store.close()
                self._wake_agent_research_job(run_dir, job_id)

    def _recover_study_jobs(self):
        if not self.trusted_local:
            return
        for run_dir in self.research_runs:
            if not (run_dir / "study-jobs.sqlite").exists():
                continue
            store = StudyJobStore(run_dir)
            try:
                store.verify()
                for job in store.list():
                    if job["status"] == "running":
                        store.finish(job["job_id"], "interrupted",
                                     error="服务重启，执行中断；请审查研究记录，确认资源不会自动重试")
                    elif job["status"] == "queued":
                        self._start_job(lambda root=run_dir, key=job["job_id"]: self._run_study_job(root, key))
            finally:
                store.close()

    def cancel_study_job(self, study_id, job_id):
        if not self.trusted_local:
            raise ProtocolError("取消 Study 作业需要可信本机模式")
        store = StudyJobStore(self.research_run_for_study(study_id))
        try:
            return store.cancel(job_id)
        finally:
            store.close()

    def preview_study_initialization(self, run_name, budget_cap):
        if not self.trusted_local:
            raise ProtocolError("创建 Study 需要以 --trusted-local 启动工作台")
        if (not isinstance(run_name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_name)):
            raise ProtocolError("研究运行名称只能使用字母、数字、点、横线或下划线")
        if (isinstance(budget_cap, bool) or not isinstance(budget_cap, (int, float))
                or not math.isfinite(budget_cap) or not 4 <= budget_cap <= 10000):
            raise ProtocolError("Study 预算上限必须是 4 到 10000 的有限数")
        with self.ws_lock:
            root = self.workspace.root.resolve()
        parent = (root / ".popper" / "research-runs").resolve()
        run_dir = (parent / run_name).resolve()
        if not parent.is_relative_to(root) or run_dir.parent != parent:
            raise ProtocolError("研究运行目录越出当前工作区")
        exp = Experiment(root, readonly=True)
        try:
            state = exp.verify_inputs()
        finally:
            exp.close()
        if state["phase"] != "searching":
            raise ProtocolError("只有 searching 阶段的预注册实验可以创建 Study")
        if read_json(root / "experiment.json") != state["spec"]:
            raise ProtocolError("预注册协议文件与实验状态不一致")
        existing = None
        if run_dir.exists():
            if not (run_dir / "research.json").is_file():
                raise ProtocolError("研究运行目录已存在但未完成初始化，需先审计")
            existing = read_research_state(run_dir)
            if (not existing.get("ok") or not existing.get("experiment_graph", {}).get(
                    "integrity", {}).get("ok")
                    or Path(existing["manifest"]["project"]).resolve() != root):
                raise ProtocolError("已有研究运行与当前项目不匹配或完整性失败")
            if existing["budget"]["cap"] != float(budget_cap):
                raise ProtocolError("已有 Study 的预算上限与本次请求不同")
        proposal = {"project": str(root), "run_dir": str(run_dir),
                    "budget_cap": float(budget_cap),
                    "input_hashes": state["input_hashes"],
                    "spec_sha256": digest(state["spec"]),
                    "evaluator_id": state["evaluator_id"],
                    "evaluator_hash": state["evaluator_hash"],
                    "objective": state["spec"]["objective"]}
        return {"project": str(root), "run_dir": str(run_dir),
                "run_name": run_name, "budget_cap": float(budget_cap),
                "objective": state["spec"]["objective"],
                "metric": state["spec"]["metric"],
                "candidate_count": len(state["spec"]["candidates"]),
                "already_initialized": existing is not None,
                "study_id": existing["manifest"]["study_id"] if existing else None,
                "proposal_sha256": digest(proposal)}

    def initialize_study_from_workbench(self, run_name, budget_cap, proposal_sha256):
        if (not isinstance(proposal_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", proposal_sha256) is None):
            raise ProtocolError("Study 初始化确认摘要不合法")
        with self.study_init_lock:
            preview = self.preview_study_initialization(run_name, budget_cap)
            if preview["proposal_sha256"] != proposal_sha256:
                raise ProtocolError("预注册实验或初始化参数已变化，请重新预览")
            run_dir = Path(preview["run_dir"])
            if not preview["already_initialized"]:
                from .research.controller import ResearchController
                ResearchController.initialize(
                    Path(preview["project"]), run_dir, budget_cap=budget_cap)
            state = read_research_state(run_dir)
            if (not state.get("ok") or not state.get("experiment_graph", {}).get(
                    "integrity", {}).get("ok")):
                raise ProtocolError("Study 已初始化但研究状态完整性失败，未登记")
            with self.research_runs_lock:
                if run_dir not in self.research_runs:
                    write_json(self._research_run_registry_path,
                               {"schema_version": "1.0", "runs": [
                                   *[str(path) for path in self.research_runs], str(run_dir)]})
                    self.research_runs.append(run_dir)
            return {"study_id": state["manifest"]["study_id"],
                    "run_dir": str(run_dir), "already_initialized": preview["already_initialized"],
                    "state_version": state["state_version"]}

    @staticmethod
    def _benchmark_job_store(root):
        return BenchmarkJobStore(Path(root) / ".popper" / "benchmark-jobs.sqlite")

    def _recover_benchmark_jobs(self):
        for root_info in self.workspaces.roots_payload():
            root = Path(root_info["root"])
            db = root / ".popper" / "benchmark-jobs.sqlite"
            if not db.is_file():
                continue
            jobs = self._benchmark_job_store(root)
            try:
                integrity = jobs.verify()
                if not integrity["ok"]:
                    raise ProtocolError(f"基准 Job 队列完整性失败: {integrity['reason']}")
                for job in jobs.list():
                    if job["status"] != "running":
                        continue
                    try:
                        verify_cross_domain_benchmark(job["output_dir"])
                    except Exception:
                        continue
                    jobs.finish(job["job_id"])
                jobs.interrupt_running()
                queued = [row["job_id"] for row in jobs.list()
                          if row["status"] == "queued"]
            finally:
                jobs.close()
            if self.trusted_local:
                for job_id in queued:
                    self._start_job(lambda root=root, job_id=job_id:
                                    self._run_benchmark_job(root, job_id))

    def _run_benchmark_job(self, root, job_id):
        jobs = self._benchmark_job_store(root)
        try:
            integrity = jobs.verify()
            if not integrity["ok"]:
                raise ProtocolError(f"基准 Job 队列完整性失败: {integrity['reason']}")
            job = jobs.claim(job_id)
        except ProtocolError:
            jobs.close()
            return
        jobs.close()
        error = None
        try:
            result = run_cross_domain_benchmark(job["output_dir"], trusted_local=True)
            if result["status"] != "passed":
                error = "跨领域基准含失败案例；详情见 benchmark.json"
            else:
                verify_cross_domain_benchmark(job["output_dir"])
        except Exception as exc:
            error = str(exc)
        jobs = self._benchmark_job_store(root)
        try:
            jobs.finish(job_id, error=error)
        finally:
            jobs.close()

    def enqueue_benchmark(self, run_name, idempotency_key):
        if not self.trusted_local:
            raise ProtocolError("基准执行需要以 --trusted-local 启动工作台")
        if not isinstance(run_name, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_name):
            raise ProtocolError("基准名称只允许字母、数字、点、横线或下划线")
        with self.ws_lock:
            root = self.workspace.root
        parent = root / ".popper" / "benchmarks"
        if not parent.resolve().is_relative_to(root):
            raise ProtocolError("基准输出目录越出工作区")
        output = parent / run_name
        with self.benchmark_enqueue_lock:
            jobs = self._benchmark_job_store(root)
            try:
                integrity = jobs.verify()
                if not integrity["ok"]:
                    raise ProtocolError(f"基准 Job 队列完整性失败: {integrity['reason']}")
                job, created = jobs.enqueue(idempotency_key, output)
            finally:
                jobs.close()
            if created:
                self._start_job(lambda: self._run_benchmark_job(root, job["job_id"]))
        return job

    def benchmark_jobs_payload(self):
        with self.ws_lock:
            root = self.workspace.root
        db = root / ".popper" / "benchmark-jobs.sqlite"
        if not db.is_file():
            return {"jobs": [], "integrity": {"ok": True}}
        jobs = self._benchmark_job_store(root)
        try:
            integrity = jobs.verify()
            rows = jobs.list() if integrity["ok"] else []
        finally:
            jobs.close()
        return {"jobs": rows, "integrity": integrity}

    def benchmark_job_detail(self, job_id):
        with self.ws_lock:
            root = self.workspace.root
        if not (root / ".popper" / "benchmark-jobs.sqlite").is_file():
            raise LookupError("基准 Job 不存在")
        jobs = self._benchmark_job_store(root)
        try:
            integrity = jobs.verify()
            if not integrity["ok"]:
                raise ProtocolError(f"基准 Job 队列完整性失败: {integrity['reason']}")
            job = jobs.get(job_id)
        finally:
            jobs.close()
        report_path = Path(job["output_dir"]) / "benchmark.json"
        report = read_json(report_path) if report_path.is_file() else None
        verification = None
        if job["status"] == "succeeded":
            verification = verify_cross_domain_benchmark(job["output_dir"])
        return {"job": job, "report": report, "verification": verification}

    def research_evidence_preview(self, study_id):
        run_dir = self.research_run_for_study(study_id)
        manifest = read_json(run_dir / "research.json")
        return render_research_evidence(manifest["project"], run_dir)

    def create_bound_draft_from_workbench(self, study_id, spec):
        if not self.trusted_local:
            raise ProtocolError("证据绑定草稿创建需要以 --trusted-local 启动工作台")
        run_dir = self.research_run_for_study(study_id)
        manifest = read_json(run_dir / "research.json")
        with self.bound_draft_lock:
            return create_workbench_bound_draft(manifest["project"], run_dir, spec)

    def agent_bound_draft_preview(self, study_id, expected_state_version,
                                  title, author_note):
        run_dir = self.research_run_for_study(study_id)
        manifest = read_json(run_dir / "research.json")
        return preview_agent_bound_draft(manifest["project"], run_dir, title,
                                         author_note, expected_state_version)

    def agent_create_bound_draft(self, study_id, expected_state_version,
                                 title, author_note, approval_sha256):
        if not self.trusted_local:
            raise ProtocolError("Agent 稿件生成需要可信本机模式")
        with self.research_execution_lock, self.bound_draft_lock:
            preview = self.agent_bound_draft_preview(
                study_id, expected_state_version, title, author_note)
            if preview["approval_sha256"] != approval_sha256:
                raise ProtocolError("证据或稿件方案已变化，请重新预览并审批")
            run_dir = self.research_run_for_study(study_id)
            manifest = read_json(run_dir / "research.json")
            result = create_workbench_bound_draft(manifest["project"], run_dir,
                                                  preview["spec"])
            return {key: result[key] for key in ("status", "replayed", "draft_id",
                "study_id", "claim_count", "manuscript_path", "publication_attested")}

    def bound_drafts_payload(self, study_id):
        run_dir = self.research_run_for_study(study_id)
        project = read_json(run_dir / "research.json")["project"]
        return {"drafts": list_workbench_bound_drafts(project, run_dir, study_id)}

    def bound_draft_detail(self, study_id, draft_id):
        run_dir = self.research_run_for_study(study_id)
        project = read_json(run_dir / "research.json")["project"]
        result = read_workbench_bound_draft(project, run_dir, draft_id)
        experiment = Experiment(project)
        try:
            flow = ManuscriptFlow(experiment)
            flow._verify_projection(result["manuscript_path"])
            result["flow_state"] = flow.current(result["manuscript_path"])
        finally:
            experiment.close()
        return result

    def review_bound_draft_from_workbench(self, study_id, draft_id, reason):
        if not self.trusted_local:
            raise ProtocolError("人工审校登记需要以 --trusted-local 启动工作台")
        run_dir = self.research_run_for_study(study_id)
        project = Path(read_json(run_dir / "research.json")["project"]).resolve()
        with self.bound_draft_lock:
            _, evidence, draft, spec_path = bound_draft_paths(project, draft_id)
            experiment = Experiment(project)
            try:
                flow = ManuscriptFlow(experiment)
                return flow.review_bound(draft / "manuscript.md", run_dir,
                                         evidence, spec_path, reason)
            finally:
                experiment.close()

    def followup_preview(self, parent_study_id):
        parent_run = self.research_run_for_study(parent_study_id)
        manifest_path = parent_run / "research.json"
        manifest = read_json(manifest_path)
        parent_project = Path(manifest["project"]).resolve()
        return {"parent_study_id": parent_study_id,
                "parent_manifest_sha256": file_hash(manifest_path),
                "allowed_test_root": str(parent_project.parent),
                "project_parent": str(parent_project.parent),
                "run_parent": str(parent_run.parent),
                "existing_candidates": [manifest["configs"][key] for key in
                                        manifest["candidate_hypothesis_ids"]]}

    def create_followup_from_workbench(self, parent_study_id, new_project_name,
                                       new_run_name, new_test_path, candidates,
                                       budget, objective, expected_parent_manifest_sha256):
        if not self.trusted_local:
            raise ProtocolError("后继研究创建需要以 --trusted-local 启动工作台")
        name_pattern = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
        if (not isinstance(new_project_name, str)
                or not re.fullmatch(name_pattern, new_project_name)
                or not isinstance(new_run_name, str)
                or not re.fullmatch(name_pattern, new_run_name)):
            raise ProtocolError("新项目和研究运行目录名称只能使用字母、数字、点、横线或下划线")
        if type(budget) is not int or budget < 1:
            raise ProtocolError("后继研究预算必须是正整数")
        parent_run = self.research_run_for_study(parent_study_id)
        parent_manifest_path = parent_run / "research.json"
        if (not isinstance(expected_parent_manifest_sha256, str)
                or expected_parent_manifest_sha256 != file_hash(parent_manifest_path)):
            raise ProtocolError("父研究清单已变化，请重新核对")
        parent_project = Path(read_json(parent_manifest_path)["project"]).resolve()
        new_project = (parent_project.parent / new_project_name).resolve()
        new_run = (parent_run.parent / new_run_name).resolve()
        if (new_project.parent != parent_project.parent
                or new_run.parent != parent_run.parent):
            raise ProtocolError("后继项目或研究运行目录越界")
        test_path = Path(new_test_path).resolve() if isinstance(new_test_path, str) else None
        if (test_path is None or not test_path.is_file()
                or not test_path.is_relative_to(parent_project.parent)):
            raise ProtocolError("新确认集必须是父项目同级目录树内已有的文件")
        with self.followup_create_lock:
            if new_project.exists() or new_run.exists():
                result = resume_followup_study(
                    parent_run, new_project, new_run, candidates, test_path,
                    budget=budget, objective=objective)
            else:
                result = create_followup_study(
                    parent_run, new_project, new_run, candidates, test_path,
                    budget=budget, objective=objective)
            already_leased = any(
                lease.path.parent.parent == new_project
                for lease in self._workspace_leases)
            lease = None if already_leased else WorkstationLease(new_project)
            try:
                with self.research_runs_lock:
                    persisted = [str(path) for path in self.research_runs]
                    if new_run not in self.research_runs:
                        write_json(self._research_run_registry_path,
                                   {"schema_version": "1.0",
                                    "runs": [*persisted, str(new_run)]})
                        self.research_runs.append(new_run)
                with self.ws_lock:
                    self.workspaces.register_cli_roots([new_project])
                with self.terminals.lock:
                    self.terminals.allowed_roots.add(new_project)
                if lease is not None:
                    self._workspace_leases.append(lease)
            except Exception:
                if lease is not None:
                    lease.close()
                raise
            return result

    def _verified_research_state_for_agent(self, study_id,
                                           development_observation_id=None):
        """Replay-check a registered Study before any Agent research disclosure."""
        run_dir = self.research_run_for_study(study_id)
        before = read_research_state(run_dir)
        if (not before.get("ok")
                or not before["experiment_graph"]["integrity"]["ok"]):
            raise ProtocolError(f"research 状态或来源图不可读取: {before.get('reason')}")
        if development_observation_id is not None:
            candidate = next((item for item in before["observations"]
                              if item["entity_id"] == development_observation_id
                              and item.get("study_id") == study_id), None)
            if candidate is None:
                raise LookupError("Observation 不属于已登记 Study 或不存在")
            if not _agent_development_observation(candidate.get("scope")):
                raise ProtocolError(
                    "Agent 仅允许读取开发评估 Observation；确认集结果须通过终结 Claim 查看")
        manifest_path = run_dir / "research.json"
        manifest_sha = file_hash(manifest_path)
        try:
            frozen_scope = json.loads(before["study"]["scope"])
        except (KeyError, TypeError, ValueError):
            raise ProtocolError("Study 缺少冻结研究范围") from None
        if (not isinstance(frozen_scope, dict)
                or frozen_scope.get("manifest_sha256") != manifest_sha):
            raise ProtocolError("Research manifest 与 Study 冻结登记不一致")
        store = ResearchStore(run_dir / "research.sqlite", readonly=True)
        try:
            audit = store.verify()
        finally:
            store.close()
        if not audit["ok"]:
            raise ProtocolError(f"research 证据完整性校验失败: {audit['reason']}")
        after = read_research_state(run_dir)
        if (not after.get("ok")
                or not after["experiment_graph"]["integrity"]["ok"]
                or after["state_version"] != before["state_version"]
                or after["events_total"] != before["events_total"]
                or file_hash(manifest_path) != manifest_sha):
            raise ProtocolError("证据读取期间 Study 状态变化或来源图失效，请重新提议读取")
        return run_dir, after

    def research_status_for_agent(self, study_id):
        """Bounded, replay-checked Study summary from a registered run."""
        _, state = self._verified_research_state_for_agent(study_id)
        graph = state.get("experiment_graph") or {}
        draft_designs = [
            {"design_id": design.get("design_id"),
             "hypothesis_id": design.get("hypothesis_id"),
             "hypothesis_version": design.get("hypothesis_version")}
            for design in state.get("designs") or [] if design.get("status") == "draft"
        ]
        hypotheses = [
            {"hypothesis_id": item.get("hypothesis_id"),
             "version": item.get("version"), "status": item.get("status"),
             "mechanism_excerpt": str(item.get("mechanism") or "")[:120]}
            for item in state.get("hypotheses") or []
        ]
        designs = [
            {"design_id": item.get("design_id"),
             "hypothesis_id": item.get("hypothesis_id"),
             "hypothesis_version": item.get("hypothesis_version"),
             "status": item.get("status")}
            for item in state.get("designs") or []
        ]
        observations = [
            {"observation_id": item.get("entity_id"),
             "run_id": item.get("run_id"), "scope": item.get("scope"),
             "status": item.get("status")}
            for item in state.get("observations") or []
            if _agent_development_observation(item.get("scope"))
        ]
        return {
            "ok": True, "study_id": study_id,
            "state_version": state.get("state_version"),
            "study": state.get("study"),
            "manifest": state.get("manifest"),
            "budget": state.get("budget"),
            "entity_counts": {name: len(state.get(name) or []) for name in (
                "hypotheses", "designs", "runs", "observations", "decisions",
                "reflections")},
            "draft_designs": draft_designs[:20],
            "draft_designs_total": len(draft_designs),
            "hypotheses": hypotheses[:8],
            "hypotheses_total": len(hypotheses),
            "designs": designs[:8],
            "designs_total": len(designs),
            "observations": observations[:20],
            "observations_total": len(observations),
            "experiment_graph_integrity": graph.get("integrity"),
            "events_total": state.get("events_total"),
        }

    def research_design_for_agent(self, study_id, design_id):
        """Disclose one registered Study's design; never accept filesystem paths."""
        parse_id(study_id, EntityKind.STUDY)
        parse_id(design_id, EntityKind.DESIGN)
        run_dir, state = self._verified_research_state_for_agent(study_id)
        design = next((item for item in state.get("designs") or []
                       if item.get("design_id") == design_id
                       and item.get("study_id") == study_id), None)
        if design is None:
            raise LookupError("design 不属于已登记 study 或不存在")
        hypothesis = next((item for item in state.get("hypotheses") or []
                           if item.get("hypothesis_id") == design.get("hypothesis_id")
                           and item.get("study_id") == study_id), None)
        if hypothesis is None:
            raise ProtocolError("design 引用的 hypothesis 不存在于该 Study")
        readiness = controller_routing_readiness(run_dir, state, design)
        return {"ok": True, "study_id": study_id, "design_id": design_id,
                "state_version": state["state_version"], "design": design,
                "design_sha256": digest(design), "hypothesis": hypothesis,
                "hypothesis_version_matches": (
                    hypothesis.get("version") == design.get("hypothesis_version")),
                "execution_readiness": readiness}

    def research_claim_for_agent(self, study_id, claim_id):
        """Disclose one checked Claim, never raw artifacts or arbitrary paths."""
        parse_id(study_id, EntityKind.STUDY)
        parse_id(claim_id, EntityKind.CLAIM)
        run_dir = self.research_run_for_study(study_id)
        state = read_research_state(run_dir)
        if not state.get("ok") or not state["experiment_graph"]["integrity"]["ok"]:
            raise ProtocolError(f"research 状态或来源图不可读取: {state.get('reason')}")
        manifest = read_json(run_dir / "research.json")
        evidence = render_research_evidence(manifest["project"], run_dir)["bundle"]
        after = read_research_state(run_dir)
        if (not after.get("ok") or not after["experiment_graph"]["integrity"]["ok"]
                or after["state_version"] != state["state_version"]
                or evidence["study_state_version"] != state["state_version"]):
            raise ProtocolError("证据读取期间 Study 状态变化或来源图失效，请重新提议读取")
        if evidence["study_id"] != study_id:
            raise ProtocolError("证据附录与请求 Study 不一致")
        binding = next((item for item in evidence["bindings"]
                        if item["claim_id"] == claim_id), None)
        if binding is None:
            raise LookupError("Claim 不属于已登记 Study 或不存在")
        return {"ok": True, "study_id": study_id, "claim_id": claim_id,
                "state_version": state["state_version"], "binding": binding,
                "verification_level": evidence["verification_level"],
                "limitations": evidence["limitations"]}

    def research_observation_for_agent(self, study_id, observation_id):
        """Recheck one active or concluded Observation without exposing local paths."""
        parse_id(study_id, EntityKind.STUDY)
        parse_id(observation_id, EntityKind.OBSERVATION)
        _, after = self._verified_research_state_for_agent(
            study_id, development_observation_id=observation_id)
        observation = next((item for item in after["observations"]
                            if item["entity_id"] == observation_id
                            and item.get("study_id") == study_id), None)
        if observation is None:
            raise LookupError("Observation 不属于已登记 Study 或不存在")
        if not _agent_development_observation(observation.get("scope")):
            raise ProtocolError("Agent 仅允许读取开发评估 Observation；确认集结果须通过终结 Claim 查看")
        if not all(observation.get(key) for key in (
                "artifact_id", "artifact_sha256", "selector")):
            raise ProtocolError("Observation 缺少可重验的评分制品绑定")
        run = next((item for item in after["runs"]
                    if item["entity_id"] == observation["run_id"]), None)
        if run is None:
            raise ProtocolError("Observation 来源 Run 不存在")
        fields = ("entity_id", "status", "run_id", "design_id", "hypothesis_id",
                  "hypothesis_version", "scorer_id", "value", "unit",
                  "uncertainty", "scope", "artifact_sha256", "selector",
                  "evaluator_service_hash", "trust")
        public_observation = {key: observation.get(key) for key in fields}
        return {"ok": True, "study_id": study_id,
                "observation_id": observation_id,
                "state_version": after["state_version"],
                "observation": public_observation,
                "run": {"run_id": run["entity_id"], "status": run["status"],
                        "design_id": run["design_id"],
                        "revision_id": run.get("revision_id")},
                "verification_level": "local_research_store_and_scoring_artifact",
                "limitations": ["development_observation_not_final_claim",
                                "not_an_independent_reproduction"]}

    def revision_payload(self, revision_id):
        """Return one verified immutable revision and its identity-bound diff."""
        if (not isinstance(revision_id, str) or len(revision_id) != 28
                or not revision_id.startswith("REV-")
                or any(ch not in "0123456789abcdef" for ch in revision_id[4:])):
            raise ProtocolError("revision_id 格式不合法")
        matches = []
        for run_dir in self.research_runs:
            manifest_path = run_dir / "revisions" / revision_id / "revision.json"
            if not manifest_path.is_file():
                continue
            store = RevisionStore(run_dir / "revisions")
            manifest = store.verify(revision_id)
            matches.append((str(run_dir), manifest, file_hash(manifest_path)))
        if not matches:
            raise FileNotFoundError("CodeRevision 不存在")
        manifest_hashes = {item[2] for item in matches}
        if len(manifest_hashes) != 1:
            raise ProtocolError("相同 revision_id 在多个研究目录中内容冲突")
        manifest = matches[0][1]
        identity = manifest["identity"]
        diff_record = RevisionDiffArchive(
            Path(matches[0][0]) / "revisions").resolve(revision_id)
        return {
            "revision_id": revision_id,
            "schema_version": manifest["schema_version"],
            "status": manifest["status"],
            "manifest_sha256": matches[0][2],
            "research_runs": [item[0] for item in matches],
            "hypothesis_id": identity.get("hypothesis_id"),
            "design_id": identity.get("design_id"),
            "parent_revision_id": identity.get("parent_revision_id"),
            "actor": identity.get("actor"),
            "source_control": identity.get("source_control", {"kind": "legacy_unbound"}),
            "files": [{"path": name, "sha256": sha}
                      for name, sha in sorted(manifest["files"].items())],
            "diff": diff_record["diff"],
            "diff_sha256": diff_record["diff_sha256"],
            "diff_source": diff_record["source"],
            "diff_attestation_id": diff_record["attestation_id"],
        }

    def migrate_revision_diff(self, revision_id):
        if not self.trusted_local:
            raise ProtocolError("迁移旧 revision Diff 需要以 --trusted-local 启动")
        matches = [run_dir for run_dir in self.research_runs
                   if (run_dir / "revisions" / revision_id / "revision.json").is_file()]
        if not matches:
            raise FileNotFoundError("CodeRevision 不存在")
        if len(matches) != 1:
            raise ProtocolError("revision 位于多个研究目录；请用 CLI 指定 run_dir 迁移")
        RevisionDiffArchive(matches[0] / "revisions").migrate(revision_id)
        return self.revision_payload(revision_id)

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
        queued = []
        for run_dir in self.research_runs:
            if not (run_dir / "design-execution-jobs.sqlite").is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                integrity = jobs.verify()
                if not integrity["ok"]:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                running = [row for row in jobs.list() if row["status"] == "running"]
                if running:
                    try:
                        projection = read_research_state(run_dir)
                    except Exception:
                        projection = {"ok": False}
                    verified = False
                    if (projection.get("ok") and projection.get("experiment_graph", {}).get(
                            "integrity", {}).get("ok")):
                        research_store = ResearchStore(run_dir / "research.sqlite")
                        try:
                            verified = research_store.verify()["ok"]
                        finally:
                            research_store.close()
                    if verified:
                        designs = {row["design_id"]: row
                                   for row in projection["designs"]}
                        runs = {row["run_id"]: row for row in projection["runs"]}
                        for job in running:
                            design = designs.get(job["design_id"])
                            if design is None or digest(design) != job["design_sha256"]:
                                continue
                            matches = [obs for obs in projection["observations"]
                                       if obs.get("scope") == "dev"
                                       and runs.get(obs.get("run_id"), {}).get(
                                           "design_id") == job["design_id"]
                                       and runs[obs["run_id"]]["status"] == "succeeded"]
                            if len(matches) == 1:
                                jobs.finish(job["job_id"],
                                            observation_id=matches[0]["observation_id"])
                jobs.interrupt_running()
                queued.extend((run_dir, row["job_id"]) for row in jobs.list()
                              if row["status"] == "queued")
            finally:
                jobs.close()
        if self.trusted_local:
            for run_dir, job_id in queued:
                self._start_design_job(run_dir, job_id)

    def _run_design_job(self, run_dir, job_id):
        with self.research_execution_lock:
            return self._run_design_job_locked(run_dir, job_id)

    def _run_design_job_locked(self, run_dir, job_id):
        jobs = self._design_job_store(run_dir)
        controller = None
        try:
            try:
                job = jobs.claim(job_id)
            except ProtocolError:
                return
            try:
                from .research.controller import ResearchController
                controller = controller_for_run(run_dir)
                current = self.research_design_for_agent(
                    controller.manifest["study_id"], job["design_id"])
                if current["design_sha256"] != job["design_sha256"]:
                    raise ProtocolError("冻结设计快照已变化，拒绝执行")
                observation = controller.execute_registered_design(
                    job["design_id"],
                    should_cancel=lambda: bool(jobs.get(job_id)["cancel_requested"]))
                jobs.finish(job_id, observation_id=observation["observation_id"])
            except Exception as error:
                current = jobs.get(job_id)
                # A cancellation can race the final Observation commit. The
                # research ledger, not the transport exception, decides success.
                observation_id = None
                try:
                    projection = read_research_state(run_dir)
                    if (projection.get("ok") and projection.get("experiment_graph", {}).get(
                            "integrity", {}).get("ok")):
                        store = ResearchStore(run_dir / "research.sqlite")
                        try:
                            verified = store.verify()["ok"]
                        finally:
                            store.close()
                        if verified:
                            runs = {row["run_id"]: row for row in projection["runs"]}
                            matches = [obs for obs in projection["observations"]
                                       if obs.get("scope") == "dev"
                                       and runs.get(obs.get("run_id"), {}).get(
                                           "design_id") == current["design_id"]
                                       and runs[obs["run_id"]]["status"] == "succeeded"]
                            if len(matches) == 1:
                                observation_id = matches[0]["observation_id"]
                except Exception:
                    pass
                if observation_id is not None:
                    jobs.finish(job_id, observation_id=observation_id)
                elif current["cancel_requested"]:
                    jobs.finish_cancelled(job_id)
                else:
                    jobs.finish(job_id, error=f"{type(error).__name__}: {error}")
        finally:
            if controller is not None:
                controller.close()
            jobs.close()
            self._wake_agent_research_job(run_dir, job_id)

    def _start_design_job(self, run_dir, job_id):
        self._start_job(lambda: self._run_design_job(Path(run_dir), job_id))

    def enqueue_design_execution(self, study_id, design_id, expected_version,
                                 design_sha256):
        if not self.trusted_local:
            raise ProtocolError("设计执行需要以 --trusted-local 启动工作台")
        if type(expected_version) is not int or expected_version < 1:
            raise ProtocolError("study 版本不合法")
        current = self.research_design_for_agent(study_id, design_id)
        if current["design_sha256"] != design_sha256:
            raise ProtocolError("冻结设计摘要不一致，请重新审查设计")
        run_dir = self.research_run_for_study(study_id)
        jobs = self._design_job_store(run_dir)
        try:
            integrity = jobs.verify()
            if not integrity["ok"]:
                raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
            prior = jobs.for_design(design_id)
            if prior is not None:
                if prior["design_sha256"] != design_sha256:
                    raise ProtocolError("同一设计已有不同冻结快照的执行 Job")
                return {"research_run": str(run_dir), "job": prior}
        finally:
            jobs.close()
        if current["state_version"] != expected_version:
            raise ProtocolError("study 版本已变化，请重新审查设计")
        if (current["design"]["status"] != "frozen"
                or not current["hypothesis_version_matches"]
                or not current["execution_readiness"]["registered_design_route_ready"]):
            raise ProtocolError("设计不满足已注册候选执行入口: " + ", ".join(
                current["execution_readiness"]["registered_design_reasons"]))
        jobs = self._design_job_store(run_dir)
        try:
            integrity = jobs.verify()
            if not integrity["ok"]:
                raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
            job = jobs.enqueue(design_id, design_sha256)
        finally:
            jobs.close()
        if job["status"] == "queued":
            self._start_design_job(run_dir, job["job_id"])
        return {"research_run": str(run_dir), "job": job}

    def design_job_payload(self, job_id):
        for run_dir in self.research_runs:
            if not (run_dir / "design-execution-jobs.sqlite").is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                try:
                    job = jobs.get(job_id)
                except ProtocolError:
                    continue
                integrity = jobs.verify()
                if not integrity["ok"]:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                return {"research_run": str(run_dir), "job": job}
            finally:
                jobs.close()
        raise FileNotFoundError("设计执行 Job 不存在")

    def cancel_design_job(self, job_id, reason):
        if not self.trusted_local:
            raise ProtocolError("设计执行取消需要以 --trusted-local 启动工作台")
        for run_dir in self.research_runs:
            if not (run_dir / "design-execution-jobs.sqlite").is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                try:
                    jobs.get(job_id)
                except ProtocolError:
                    continue
                integrity = jobs.verify()
                if not integrity["ok"]:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                return {"research_run": str(run_dir), "job": jobs.cancel(job_id, reason)}
            finally:
                jobs.close()
        raise FileNotFoundError("设计执行 Job 不存在")

    def retry_design_job(self, job_id, expected_attempt, design_sha256):
        if not self.trusted_local:
            raise ProtocolError("设计执行重试需要以 --trusted-local 启动工作台")
        if type(expected_attempt) is not int or expected_attempt < 1:
            raise ProtocolError("设计执行重试次数不合法")
        for run_dir in self.research_runs:
            if not (run_dir / "design-execution-jobs.sqlite").is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                try:
                    job = jobs.get(job_id)
                except ProtocolError:
                    continue
                integrity = jobs.verify()
                if not integrity["ok"]:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                if job["design_sha256"] != design_sha256:
                    raise ProtocolError("冻结设计摘要不一致，请重新审查设计")
                if job["attempt"] == expected_attempt + 1:
                    return {"research_run": str(run_dir), "job": job}
                projection = read_research_state(run_dir)
                if not projection.get("ok"):
                    raise ProtocolError("研究状态不可验证，拒绝重试设计执行")
                study_id = projection["manifest"]["study_id"]
                current = self.research_design_for_agent(study_id, job["design_id"])
                if (current["design_sha256"] != design_sha256
                        or not current["execution_readiness"]["registered_design_route_ready"]):
                    raise ProtocolError("设计或执行条件已变化，请重新审查")
                retried, applied = jobs.retry_cancelled(job_id, expected_attempt)
            finally:
                jobs.close()
            if applied and retried["status"] == "queued":
                self._start_design_job(run_dir, job_id)
            return {"research_run": str(run_dir), "job": retried}
        raise FileNotFoundError("设计执行 Job 不存在")

    def design_jobs_payload(self):
        records = []
        for run_dir in self.research_runs:
            if not (run_dir / "design-execution-jobs.sqlite").is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                integrity = jobs.verify()
                if not integrity["ok"]:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                records.extend({"research_run": str(run_dir), **job} for job in jobs.list())
            finally:
                jobs.close()
        return {"jobs": records}

    def _recover_revision_worktrees(self):
        if not self.trusted_local:
            return
        for run_dir in self.research_runs:
            if not (run_dir / "revision-worktrees.sqlite").is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                manager.recover_provisioning()
            finally:
                manager.close()

    def _recover_revision_execution_jobs(self):
        """Expose interrupted work honestly and resume durable queued work."""
        if not self.trusted_local:
            return
        queued = []
        for run_dir in self.research_runs:
            path = run_dir / "revision-execution-jobs.sqlite"
            if not path.is_file():
                continue
            jobs = self._revision_job_store(run_dir)
            try:
                integrity = jobs.verify()
                if not integrity["ok"]:
                    raise ProtocolError(f"Revision 执行队列完整性失败: {integrity['reason']}")
                running = [row for row in jobs.list() if row["status"] == "running"]
                jobs.interrupt_running()
                if running and (run_dir / "revision-worktrees.sqlite").is_file():
                    manager = self._revision_worktree_manager(run_dir)
                    try:
                        for job in running:
                            if job["worktree_id"] == f'initial:{job["revision_id"]}':
                                continue
                            try:
                                manager.finish_execution(
                                    job["worktree_id"],
                                    error="Interrupted: workstation restarted during execution")
                            except ProtocolError:
                                pass
                    finally:
                        manager.close()
                queued.extend((run_dir, row["job_id"]) for row in jobs.list()
                              if row["status"] == "queued")
            finally:
                jobs.close()
        for run_dir, job_id in queued:
            self._start_revision_job(run_dir, job_id)

    @staticmethod
    def _public_worktree(record):
        return {key: value for key, value in record.items() if key != "proposal"}

    def revision_worktrees_payload(self):
        records = []
        registries = []
        jobs_payload = []
        job_registries = []
        for run_dir in self.research_runs:
            registry = run_dir / "revision-worktrees.sqlite"
            if registry.is_file():
                manager = self._revision_worktree_manager(run_dir)
                try:
                    records.extend({"research_run": str(run_dir),
                                    **self._public_worktree(row)}
                                   for row in manager.list())
                    registries.append({"research_run": str(run_dir),
                                       "integrity": manager.verify()})
                finally:
                    manager.close()
            job_path = run_dir / "revision-execution-jobs.sqlite"
            if job_path.is_file():
                jobs = self._revision_job_store(run_dir)
                try:
                    jobs_payload.extend({"research_run": str(run_dir), **row}
                                        for row in jobs.list())
                    job_registries.append({"research_run": str(run_dir),
                                           "integrity": jobs.verify()})
                finally:
                    jobs.close()
        records.sort(key=lambda row: (row["created"], row["worktree_id"]))
        jobs_payload.sort(key=lambda row: (row["created"], row["job_id"]))
        return {"worktrees": records, "jobs": jobs_payload, "registries": registries,
                "job_registries": job_registries,
                "trusted_local": self.trusted_local}

    def revision_worktree_events_payload(self, worktree_id):
        for run_dir in self.research_runs:
            if not (run_dir / "revision-worktrees.sqlite").is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                try:
                    record = manager.get(worktree_id)
                except ProtocolError:
                    continue
                return {"research_run": str(run_dir),
                        "worktree": self._public_worktree(record),
                        "events": manager.history(worktree_id),
                        "integrity": manager.verify()}
            finally:
                manager.close()
        raise FileNotFoundError("worktree 不存在")

    def create_revision_worktree(self, revision_id, idempotency_key):
        if not self.trusted_local:
            raise ProtocolError("创建 Git worktree 需要以 --trusted-local 启动")
        detail = self.revision_payload(revision_id)
        run_dir = Path(detail["research_runs"][0])
        manager = self._revision_worktree_manager(run_dir)
        try:
            return {"research_run": str(run_dir),
                    **self._public_worktree(
                        manager.create(revision_id, idempotency_key))}
        finally:
            manager.close()

    def remove_revision_worktree(self, worktree_id):
        if not self.trusted_local:
            raise ProtocolError("移除 Git worktree 需要以 --trusted-local 启动")
        for run_dir in self.research_runs:
            if not (run_dir / "revision-worktrees.sqlite").is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                try:
                    manager.get(worktree_id)
                except ProtocolError:
                    continue
                return {"research_run": str(run_dir),
                        **self._public_worktree(manager.remove(worktree_id))}
            finally:
                manager.close()
        raise FileNotFoundError("worktree 不存在")

    def _worktree_operation(self, worktree_id, operation):
        if not self.trusted_local:
            raise ProtocolError("worktree 操作需要以 --trusted-local 启动")
        for run_dir in self.research_runs:
            if not (run_dir / "revision-worktrees.sqlite").is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                try:
                    manager.get(worktree_id)
                except ProtocolError:
                    continue
                record = operation(manager)
                return {"research_run": str(run_dir),
                        **self._public_worktree(record)}
            finally:
                manager.close()
        raise FileNotFoundError("worktree 不存在")

    def propose_worktree_revision(self, worktree_id):
        return self._worktree_operation(
            worktree_id, lambda manager: manager.propose(worktree_id, "local_user"))

    def submit_worktree_revision(self, worktree_id, proposal_sha256):
        return self._worktree_operation(
            worktree_id, lambda manager: manager.submit(
                worktree_id, proposal_sha256, "local_user"))

    def _run_revision_job(self, run_dir, job_id):
        with self.research_execution_lock:
            return self._run_revision_job_locked(run_dir, job_id)

    def _run_revision_job_locked(self, run_dir, job_id):
        jobs = self._revision_job_store(run_dir)
        manager = None
        controller = None
        try:
            try:
                job = jobs.claim(job_id)
            except ProtocolError:
                return
            if job["cancel_requested"]:
                return
            initial = job["worktree_id"] in {f'initial:{job["revision_id"]}', f'approved:{job["revision_id"]}'}
            if initial:
                revision_id = job["revision_id"]
            else:
                manager = self._revision_worktree_manager(run_dir)
                record = manager.begin_execution(job["worktree_id"])
                revision_id = record["successor_revision_id"]
            jobs.progress(job_id, "executing_revision", 35)
            try:
                from .research.controller import ResearchController
                controller = controller_for_run(run_dir)
                with self.revision_job_events_lock:
                    cancel_event = self._revision_job_cancel_events.setdefault(
                        job_id, threading.Event())
                result = controller.execute_revision(
                    revision_id, should_cancel=cancel_event.is_set)
                jobs.progress(job_id, "recording_result", 90)
                run_id = (result.get("observation") or {}).get("run_id")
                if run_id is None:
                    run_id = "RUN-" + digest({
                        "revision_id": revision_id, "split": "dev"})[:24]
                if manager is not None:
                    manager.finish_execution(job["worktree_id"], run_id=run_id)
                jobs.finish(job_id, run_id=run_id)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                current = jobs.get(job_id)
                if current["cancel_requested"]:
                    if manager is not None:
                        manager.finish_execution(
                            job["worktree_id"], error=message, cancelled=True)
                    jobs.finish_cancelled(job_id, current["error"] or "user requested cancellation")
                else:
                    if manager is not None:
                        manager.finish_execution(job["worktree_id"], error=message)
                    jobs.finish(job_id, error=message)
        finally:
            if controller is not None:
                controller.close()
            if manager is not None:
                manager.close()
            jobs.close()
            with self.revision_job_events_lock:
                self._revision_job_cancel_events.pop(job_id, None)
            self._wake_agent_research_job(run_dir, job_id)

    def _start_revision_job(self, run_dir, job_id):
        self._start_job(lambda: self._run_revision_job(Path(run_dir), job_id))

    def execute_worktree_revision(self, worktree_id):
        if not self.trusted_local:
            raise ProtocolError("执行后继 revision 需要以 --trusted-local 启动")
        for run_dir in self.research_runs:
            if not (run_dir / "revision-worktrees.sqlite").is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                try:
                    record = manager.get(worktree_id)
                except ProtocolError:
                    continue
                jobs = self._revision_job_store(run_dir)
                try:
                    job = jobs.for_worktree(worktree_id)
                    if job is None:
                        if record["status"] != "submitted":
                            raise ProtocolError("只有已提交的 worktree 可以创建执行 Job")
                        job = jobs.enqueue(worktree_id, record["successor_revision_id"])
                finally:
                    jobs.close()
                if job["status"] == "queued":
                    self._start_revision_job(run_dir, job["job_id"])
                return {"research_run": str(run_dir), "job": job,
                        "worktree": self._public_worktree(record)}
            finally:
                manager.close()
        raise FileNotFoundError("worktree 不存在")

    def execute_initial_revision(self, revision_id, manifest_sha256):
        return self._execute_approved_revision(revision_id, manifest_sha256, allow_repair=False)

    def execute_agent_revision(self, revision_id, manifest_sha256):
        return self._execute_approved_revision(revision_id, manifest_sha256, allow_repair=True)

    def _execute_approved_revision(self, revision_id, manifest_sha256, *, allow_repair):
        if not self.trusted_local:
            raise ProtocolError("执行初始 revision 需要以 --trusted-local 启动")
        detail = self.revision_payload(revision_id)
        if detail["parent_revision_id"] is not None and not allow_repair:
            raise ProtocolError("此入口只执行没有父版本的初始 revision")
        if detail["manifest_sha256"] != manifest_sha256:
            raise ProtocolError("Revision manifest 已变化，请重新审查")
        if len(detail["research_runs"]) != 1:
            raise ProtocolError("Revision 研究目录不唯一")
        run_dir = Path(detail["research_runs"][0])
        manifest = read_json(run_dir / "research.json")
        if Path(manifest["project"]).resolve() != self.workspace.root.resolve():
            raise ProtocolError("Revision 不属于当前登记工作区")
        state = read_research_state(run_dir)
        if (not state.get("ok") or not state.get("experiment_graph", {}).get(
                "integrity", {}).get("ok")):
            raise ProtocolError("研究状态或来源图未通过完整性检查")
        if detail["parent_revision_id"] is not None:
            from .research.revision_drafts import failed_repair_parent
            revision = RevisionStore(run_dir / "revisions").verify(revision_id)
            failed_repair_parent(run_dir, state, detail["parent_revision_id"],
                                revision["identity"]["hypothesis_id"], revision["identity"]["design_id"])
        jobs = self._revision_job_store(run_dir)
        try:
            integrity = jobs.verify()
            if not integrity["ok"]:
                raise ProtocolError(f"Revision 执行队列完整性失败: {integrity['reason']}")
            policy_path = self.workspace.root / "evaluation-policy.json"
            if self.evaluation_budget_path is not None and policy_path.is_file():
                exp = Experiment(self.workspace.root, readonly=True)
                try:
                    core = exp.verify_inputs()
                finally:
                    exp.close()
                if "evaluation-policy.json" not in core["spec"].get("provenance_files", []):
                    raise ProtocolError("评测开发限制必须是冻结的预注册输入")
                policy = read_json(policy_path)
                limit = policy.get("candidate_development_limit")
                if type(limit) is not int or not 1 <= limit <= 6:
                    raise ProtocolError("评测开发执行限制不合法")
                candidate_revisions = set()
                for previous_job in jobs.list():
                    previous = RevisionStore(run_dir / "revisions").verify(previous_job["revision_id"])
                    if previous["identity"]["hypothesis_id"] != manifest["control_hypothesis_id"]:
                        candidate_revisions.add(previous_job["revision_id"])
                if revision_id not in candidate_revisions and len(candidate_revisions) >= limit:
                    raise ProtocolError("预注册候选开发次数已耗尽；失败和取消均计入，不自动追加")
            key = "approved" if detail["parent_revision_id"] is not None else "initial"
            job = jobs.enqueue(f"{key}:{revision_id}", revision_id)
        finally:
            jobs.close()
        if job["status"] == "queued":
            self._start_revision_job(run_dir, job["job_id"])
        return {"research_run": str(run_dir), "job": job}

    def revision_job_payload(self, job_id):
        for run_dir in self._visible_research_runs(self.workspace.root):
            path = run_dir / "revision-execution-jobs.sqlite"
            if not path.is_file():
                continue
            jobs = RevisionExecutionJobStore.readonly(path)
            try:
                try:
                    job = jobs.get(job_id)
                except ProtocolError:
                    continue
                result = {"research_run": str(run_dir), "job": job,
                          "events": jobs.history(job_id), "integrity": jobs.verify()}
            finally:
                jobs.close()
            manifest = read_json(run_dir / "research.json")
            if (result["integrity"]["ok"] and job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}
                    and manifest.get("scorer_id") == "oscillator-trajectory-v1"
                    and (run_dir / "oscillator-worker-binding.json").is_file()):
                from .research.development_evidence import build_development_evidence
                try:
                    result["development_diagnostics"] = build_development_evidence(
                        manifest["project"], run_dir, job_id)
                except (ProtocolError, OSError, ValueError) as error:
                    result["development_diagnostics_error"] = task_memory.redact({
                        "reason": str(error), "verified": False, "scope": "dev"})
            return result
        raise FileNotFoundError("revision execution job 不存在")

    def cancel_revision_job(self, job_id, reason):
        if not self.trusted_local:
            raise ProtocolError("取消 revision job 需要以 --trusted-local 启动")
        for run_dir in self.research_runs:
            path = run_dir / "revision-execution-jobs.sqlite"
            if not path.is_file():
                continue
            jobs = self._revision_job_store(run_dir)
            try:
                try:
                    job = jobs.cancel(job_id, reason)
                except ProtocolError as error:
                    if "不存在" in str(error):
                        continue
                    raise
                if job["status"] == "running":
                    with self.revision_job_events_lock:
                        self._revision_job_cancel_events.setdefault(
                            job_id, threading.Event()).set()
                return {"research_run": str(run_dir), "job": job}
            finally:
                jobs.close()
        raise FileNotFoundError("revision execution job 不存在")

    def campaign_payload(self):
        """构造 GET /api/campaign 的响应（9 阶段统一切磋进度）。"""
        if self.mode != "experiment":
            return {"objective": None, "phase": "reproduction",
                    "phase_label": "论文复现", "stages": [],
                    "campaign": None}
        exp = Experiment(self.project)
        try:
            state = exp.state()
            results = exp.results()
        finally:
            exp.close()
        papers = load_all_literature(self.project)
        arbor = load_all_arbor(self.project)
        with self.ws_lock:
            ws = self.workspace
            if ws.is_empty():
                ws.seed_if_empty(ws.project_spec(), ws.current_phase())
            data = ws.read()
            deliverables = ws.list_deliverables(data)
        payload = build_campaign_payload(state, results or [], papers, arbor,
                                         deliverables)
        payload["campaign"] = (campaign_pending_payload(self.campaign_dir,
                                                        self.campaign_job)
                               if self.campaign_dir else None)
        return payload

    # ── 自由对话：BYOK 通用聊天，独立于事件溯源链 ────────────────────────────

    def _chat_system_prompt(self, workspace_root=None):
        """从真实项目状态构造对话 system prompt：只注入事实，不编造上下文。"""
        if self.mode == "experiment":
            exp = Experiment(self.project, readonly=True)
            try:
                exp.replay()
                state = exp.state()
                results = exp.results(split="dev")
            finally:
                exp.close()
            spec = state.get("spec", {})
            metric = spec.get("metric", {})
            lines = ["你是 Popper 科研工作台的对话助手。",
                     f"当前实验：{spec.get('name')}",
                     f"研究目标：{spec.get('objective')}",
                     f"指标：{metric.get('name')}（方向 {metric.get('direction')}）",
                     f"阶段：{PHASES.get(state.get('phase'), state.get('phase'))}",
                     f"预注册改善阈值：{spec.get('min_improvement')}"]
            claim = state.get("claim")
            if claim:
                lines.append(f"结论：oriented delta={claim.get('delta')}（{claim.get('status')}）")
                lines.append("结论证据 run_id：" + json.dumps(
                    claim.get("evidence_ids", []), ensure_ascii=False))
            lines.append("已完成运行（每条都是控制器保存的真实结果；run_id 是证据定位符）：")
            if results:
                for result in results:
                    summary = {
                        "run_id": result.get("run_id"),
                        "split": result.get("split"),
                        "config": result.get("config"),
                        "metric": result.get("metric"),
                        "mean": result.get("mean"),
                        "std": result.get("std"),
                        "n_seeds": result.get("n_seeds"),
                        "per_seed": result.get("per_seed"),
                        "evaluator_id": result.get("evaluator_id"),
                        "trust": result.get("trust"),
                    }
                    lines.append("- " + json.dumps(summary, ensure_ascii=False,
                                                    sort_keys=True, separators=(",", ":")))
            else:
                lines.append("- 无已完成运行")
            registered_studies = []
            for run_dir in self._visible_research_runs(workspace_root):
                try:
                    study_id = read_json(run_dir / "research.json").get("study_id")
                    parse_id(study_id, EntityKind.STUDY)
                    registered_studies.append(study_id)
                except Exception:
                    continue
            lines.append("已显式登记的 Research Study ID（research.status、"
                         "research.study_preview、research.study_job 与证据读取需用户批准；"
                         "不得用不存在的 ID 或文件路径替代）：")
            lines.append(json.dumps(sorted(set(registered_studies)), ensure_ascii=False))
            if workspace_root is None:
                workspace = self.workspace_payload()
            else:
                workspace = Workspace(workspace_root).read() or {
                    "meta": {"state_version": 0}, "folders": []}
            workspace_context = {
                "state_version": workspace["meta"]["state_version"],
                "folders": [{"fid": folder["fid"], "name": folder["name"],
                             "tasks": [{"quid": task["quid"], "title": task["title"],
                                        "done": task["done"]}
                                       for task in folder["tasks"]]}
                            for folder in workspace["folders"]],
            }
            lines.append("当前 Workspace（工具参数必须使用这里的真实 ID 与 state_version）：")
            lines.append(json.dumps(workspace_context, ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")))
            lines.append("回答数值问题时优先直接引用上述 split/config/mean，并同时给出 run_id。"
                         "只基于以上真实状态回答；不得编造未发生的实验结果、证据位置或评测数字。"
                         "无法回答时如实说明。")
            lines.append("推进 Study 时先调用 research_study_preview 获取当前审批摘要；"
                         "再提交 research_study_action，经用户批准后只会返回 Job。"
                         "用 research_study_job 查询终态；仅在作业成功且真实 Claim 可核验后"
                         "表述实验结论。独立确认资源一次性消费，不要求自动重试。")
            lines.append("需要生成研究稿件时，先读取真实 Claim 和 Study 版本，"
                         "调用 research_draft_preview 获取包含全部 Claim 的稿件摘要，"
                         "再调用 research_draft_create 并等待用户批准。作者说明未验证；"
                         "生成结果仅是待审草稿，不得称为已投稿或已发表。")
            if self.trusted_local:
                lines.append("需要改代码时先用 workspace_file_list 查路径，再用 workspace_file_read "
                             "取得源码与 SHA-256，最后用 workspace_file_replace 提交唯一文本替换。"
                             "每步须用户审批；冻结实验输入不能原地改写，须走 Revision。")
            return "\n".join(lines)
        return ("你是 Popper 科研工作台（论文复现模式）的对话助手。基于用户提供的上下文"
                "回答，不得编造实验结果。" +
                ("代码修改须先列文件、审批读取，再提交带摘要的唯一文本替换并等待用户审查差异；"
                 "冻结输入不可原地修改。" if self.trusted_local else ""))

    def chat_payload(self):
        """构造 GET /api/chat 的响应：当前工作区根的对话 + 配置状态 + 界面文案。"""
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                store.import_legacy_chat(self.workspace.root / CHAT_REL, self.chat_model)
                return self._chat_payload(store)
            finally:
                store.close()

    def _chat_payload(self, store):
        session = store.default_session(create=False)
        continuation = None
        if session:
            # Never surface a scheduling receipt from an earlier Turn after
            # its child has already consumed those results and completed.
            for item in store.turns(session["session_id"])[-1:]:
                state = store.get_continuation_state(item["turn_id"])
                if state:
                    reason = state.get("reason")
                    continuation = {"state": state["status"],
                        "reason": reason.get("message") if isinstance(reason, dict) else reason,
                        "source_turn_id": state["source_turn_id"],
                        "root_turn_id": state["root_turn_id"],
                        "correction_attempts": state.get("correction_count", 0), "max_corrections": 2}
                    break
        descriptor = self.chat_harness.descriptor if self.chat_harness else None
        workspace_id = next((item["wid"] for item in self.workspaces.roots_payload()
                             if Path(item["root"]).resolve() == store.root), None)
        return {"workspace_id": workspace_id, "navigation_version": self.navigation_version,
                "configured": self.chat_harness is not None, "model": self.chat_model,
                "continuation": continuation,
                "harnesses": [descriptor.as_dict()] if descriptor else [],
                "session_id": session["session_id"] if session else None,
                "sessions": store.list_sessions(),
                "turns": store.turns(session["session_id"]) if session else [],
                "jobs": store.agent_jobs(session["session_id"]) if session else [],
                "usage": store.usage_summary(session["session_id"]) if session else {
                    "jobs": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "reserved_tokens": 0,
                    "token_budget": None, "remaining_tokens": None,
                    "cost_micros": 0, "reserved_cost_micros": 0,
                    "cost_budget_micros": None, "remaining_cost_micros": None,
                    "currency": None},
                "messages": store.chat_messages(session["session_id"]) if session else [],
                "tool_calls": [self._agent_tool_public(call) for call in
                               store.tool_calls(session["session_id"])] if session else [],
                "pricing_catalogs": store.list_pricing_catalogs(),
                "event_integrity": store.verify_turn_events(),
                "tool_event_integrity": store.verify_tool_events(),
                "labels": CHAT_LABELS}

    def _agent_private_paths(self, files):
        if self.evaluation_budget_path is None:
            return set()
        experiment = Experiment(files.root, readonly=True)
        try:
            spec = experiment.verify_inputs()["spec"]
            # The host/Kernel can read frozen datasets; the campaign model must
            # only read separately prepared reference-free initial materials.
            return {(files.root / name).resolve() for name in (
                spec["train"], spec["dev"], spec["test"], *spec.get("provenance_files", []))}
        finally:
            experiment.close()

    def _require_agent_file_disclosure(self, files, path):
        if (files.root / path).resolve() in self._agent_private_paths(files):
            raise ProtocolError("评测参考/确认数据不允许向模型披露；请读取无参考解的初始材料")

    def _agent_file_list(self, files):
        listing = files.list_files()
        private = self._agent_private_paths(files)
        visible = [row for row in listing["files"] if (files.root / row["path"]).resolve() not in private]
        return {**listing, "files": visible[:100],
                "truncated": listing["truncated"] or len(visible) > 100}

    def _agent_tool_public(self, call):
        if call.get("status") == "invalid":
            error = call.get("error") or {}
            return {**call, "executed": False, "validation_error": call.get("error"),
                    "original_tool_name": error.get("original_tool_name"),
                    "raw_arguments": error.get("raw_arguments", call.get("arguments")),
                    "preview": {"ok": False, "reason": "参数无效，未执行"}}
        if call["tool_name"] == "context.tool_result":
            try:
                self._tool_result_for_call(self.workspace.root, call)
                return {**call, "preview": {"ok": True, "scope": "当前会话已完成工具的脱敏详情"}}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] == "context.source":
            try:
                result = self._task_source_for_call(self.workspace.root, call)
                return {**call, "preview": {"ok": True, "context_source": result}}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"].startswith("research.") and isinstance(call.get("arguments"), dict):
            study_id = call["arguments"].get("study_id")
            if isinstance(study_id, str):
                try:
                    if self.research_run_for_study(study_id) not in self._visible_research_runs():
                        raise ProtocolError("Study 不属于当前工作区")
                except Exception as error:
                    return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] in revision_tools.REVISION_RISKS:
            try:
                return {**call, "preview": agent_revisions.preview(
                    self, call["tool_name"], call["arguments"], self.workspace.root, call=call)}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] in {"research.draft_preview", "research.draft_create"}:
            try:
                args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                preview = self.agent_bound_draft_preview(
                    args["study_id"], args["expected_state_version"],
                    args["title"], args["author_note"])
                matches = (call["tool_name"] == "research.draft_preview" or
                           self.trusted_local and
                           preview["approval_sha256"] == args["approval_sha256"])
                return {**call, "preview": {"ok": matches, "bound_draft": preview,
                    "reason": None if matches else "需要可信本地会话和当前有效的稿件审批摘要"}}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] in {"research.study_preview", "research.study_action",
                                 "research.study_job"}:
            try:
                args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                run_dir = self.research_run_for_study(args["study_id"])
                if call["tool_name"] == "research.study_job":
                    jobs = StudyJobStore(run_dir)
                    try:
                        jobs.verify()
                        job = jobs.get(args["job_id"])
                        if job["request"]["study_id"] != args["study_id"]:
                            raise ProtocolError("作业不属于该 Study")
                    finally:
                        jobs.close()
                    return {**call, "preview": {"ok": True, "job": job}}
                preview = preview_study_action(run_dir, args["action"], args["max_steps"])
                match = (call["tool_name"] == "research.study_preview" or
                         self.trusted_local and preview["approval_sha256"] == args["approval_sha256"])
                return {**call, "preview": {"ok": match, "study_action": preview,
                    "approval_matches": match if call["tool_name"] == "research.study_action" else None,
                    "reason": None if match else "需要可信本地会话和当前有效的审批摘要"}}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] in {"workspace.file_list", "workspace.file_read",
                                 "workspace.file_replace"}:
            try:
                if not self.trusted_local:
                    raise ProtocolError("项目文件工具仅在可信本机模式可用")
                args = validate_agent_tool_arguments(
                    call["tool_name"], call["arguments"])
                files = ProjectFiles(self.workspace.root)
                if call["tool_name"] == "workspace.file_list":
                    result = self._agent_file_list(files)
                    return {**call, "preview": {"ok": True, "file_list": result}}
                if call["tool_name"] == "workspace.file_read":
                    self._require_agent_file_disclosure(files, args["path"])
                    result = files.read(args["path"])
                    if result["bytes"] > 65_536:
                        raise ProtocolError("向模型披露的文件不得超过 64 KiB")
                    return {**call, "preview": {"ok": True, "file_read": result}}
                result, _updated = files.preview_replace(
                    args["path"], args["old_text"], args["new_text"],
                    args["expected_sha256"])
                return {**call, "preview": {"ok": result["changed"],
                                           "file_edit": result,
                                           "reason": None if result["changed"]
                                           else "补丁没有产生修改"}}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] == "research.status":
            try:
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                result = self.research_status_for_agent(args["study_id"])
                return {**call, "preview": {"ok": True,
                                           "research_status": result}}
            except Exception as error:
                return {**call, "preview": {"ok": False,
                                           "reason": str(error)}}
        if call["tool_name"] == "research.create_design":
            try:
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                projection = read_research_state(
                    self.research_run_for_study(args["study_id"]))
                if not projection.get("ok"):
                    return {**call, "preview": {"ok": False,
                                               "reason": projection.get("reason")}}
                hypothesis = next((item for item in projection["hypotheses"]
                                   if item.get("hypothesis_id") == args["hypothesis_id"]
                                   and item.get("study_id") == args["study_id"]), None)
                valid = (hypothesis is not None and hypothesis.get("status") == "untested"
                         and projection["study"].get("status") == "active")
                proposal = {key: args[key] for key in (
                    "interventions", "control", "baseline", "metric", "scorer_id",
                    "metric_direction", "analysis_unit", "splits", "repeat_values",
                    "min_meaningful_effect")}
                return {**call, "preview": {
                    "ok": valid,
                    "reason": (None if valid else
                               "假设不属于该 Study、非 untested 或 Study 非 active"),
                    "study_id": args["study_id"],
                    "state_version": projection["state_version"],
                    "version_matches": projection["state_version"] == args["expected_version"],
                    "hypothesis": hypothesis,
                    "hypothesis_version_matches": (
                        hypothesis is not None and
                        hypothesis.get("version") == args["expected_hypothesis_version"]),
                    "proposal": proposal, "proposal_sha256": digest(proposal),
                    "execution_ready": False,
                }}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] == "research.create_hypothesis":
            try:
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                projection = read_research_state(
                    self.research_run_for_study(args["study_id"]))
                if not projection.get("ok"):
                    return {**call, "preview": {"ok": False,
                                               "reason": projection.get("reason")}}
                proposal = {key: args[key] for key in (
                    "mechanism", "applicability", "predictions",
                    "falsification", "alternatives")}
                return {**call, "preview": {
                    "ok": projection["study"].get("status") == "active",
                    "reason": (None if projection["study"].get("status") == "active"
                               else "Study 不在 active 状态"),
                    "study_id": args["study_id"],
                    "study": projection["study"],
                    "state_version": projection["state_version"],
                    "version_matches": projection["state_version"] == args["expected_version"],
                    "proposal": proposal, "proposal_sha256": digest(proposal),
                }}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] == "research.inspect_design":
            try:
                args = call["arguments"]
                result = self.research_design_for_agent(
                    args["study_id"], args["design_id"])
                return {**call, "preview": {
                    "ok": True, "study_id": result["study_id"],
                    "state_version": result["state_version"],
                    "version_matches": result["state_version"] == args["expected_version"],
                    "design": result["design"],
                    "design_sha256": result["design_sha256"],
                    "hypothesis": result["hypothesis"],
                    "hypothesis_version_matches": result["hypothesis_version_matches"],
                    "execution_readiness": result["execution_readiness"],
                }}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] == "research.inspect_claim":
            try:
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                result = self.research_claim_for_agent(
                    args["study_id"], args["claim_id"])
                return {**call, "preview": {
                    "ok": True, **result,
                    "version_matches": result["state_version"] == args["expected_version"],
                }}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] == "research.inspect_observation":
            try:
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                result = self.research_observation_for_agent(
                    args["study_id"], args["observation_id"])
                return {**call, "preview": {
                    "ok": True, **result,
                    "version_matches": result["state_version"] == args["expected_version"],
                }}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] == "research.execute_design":
            try:
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                result = self.research_design_for_agent(
                    args["study_id"], args["design_id"])
                readiness = result["execution_readiness"]
                valid = (self.trusted_local and readiness["registered_design_route_ready"])
                return {**call, "preview": {
                    "ok": valid,
                    "reason": (None if valid else "需要可信本地会话及已注册候选执行入口资格: "
                               + ", ".join(readiness["registered_design_reasons"])),
                    "study_id": args["study_id"],
                    "state_version": result["state_version"],
                    "version_matches": result["state_version"] == args["expected_version"],
                    "design": result["design"],
                    "design_sha256": result["design_sha256"],
                    "design_sha256_matches": result["design_sha256"] == args["design_sha256"],
                    "hypothesis": result["hypothesis"],
                    "hypothesis_version_matches": result["hypothesis_version_matches"],
                    "execution_readiness": readiness,
                    "budget_effect": "开发预算 1；失败或中断也可能计费",
                }}
            except Exception as error:
                return {**call, "preview": {"ok": False, "reason": str(error)}}
        if call["tool_name"] != "research.freeze_design":
            return call
        try:
            args = call["arguments"]
            run_dir = self.research_run_for_study(args["study_id"])
            projection = read_research_state(run_dir)
            if not projection.get("ok"):
                return {**call, "preview": {"ok": False,
                                          "reason": projection.get("reason")}}
            design = next((item for item in projection["designs"]
                           if item.get("design_id") == args["design_id"]), None)
            return {**call, "preview": {
                "ok": design is not None,
                "study_id": args["study_id"],
                "state_version": projection["state_version"],
                "version_matches": projection["state_version"] == args["expected_version"],
                "design": design,
                "design_sha256": digest(design) if design is not None else None,
                "reason": None if design is not None else "设计不属于该 Study 或不存在",
            }}
        except Exception as error:
            return {**call, "preview": {"ok": False, "reason": str(error)}}

    def sessions_payload(self, scope=None):
        if scope == "all":
            with self.ws_lock:
                roots = self.workspaces.roots_payload()
            projects = []
            for root in roots:
                try:
                    sessions = [{**session, "workspace_id": root["wid"]}
                                for session in read_session_summaries(root["root"])]
                    projects.append({"workspace_id": root["wid"], "name": root["name"],
                                     "sessions": sessions, "error": None})
                except Exception:
                    projects.append({"workspace_id": root["wid"], "name": root["name"],
                                     "sessions": [], "error": "会话读取失败，请重试"})
            return {"projects": projects}
        if scope not in (None, "current"):
            raise ProtocolError("不支持的会话查询范围")
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                return {"sessions": store.list_sessions()}
            finally:
                store.close()

    def _context_root(self, workspace_id):
        entry = next((item for item in self.workspaces.roots_payload() if item["wid"] == workspace_id), None)
        if not entry:
            raise SessionNotFound("项目不存在")
        return Path(entry["root"]).resolve()

    def _context_facts(self, root):
        facts = []
        for run_dir in self._visible_research_runs(root):
            state = read_research_state(run_dir)
            if not state.get("ok"):
                continue
            if not state.get("experiment_graph", {}).get("integrity", {}).get("ok", True):
                raise ProtocolError("研究证据完整性校验失败，不能将此研究作为交接事实；请在研究面板核验")
            study = state.get("study") or {}
            jobs = []
            if (run_dir / "study-jobs.sqlite").is_file():
                job_store = StudyJobStore.readonly(run_dir)
                try:
                    job_store.verify()
                    jobs = [{"job_id": job["job_id"], "status": job["status"], "error": job.get("error"), "updated": job.get("updated")}
                            for job in job_store.list() if job.get("request", {}).get("study_id") == state["manifest"]["study_id"]]
                finally:
                    job_store.close()
            facts.append({"study_id": state["manifest"]["study_id"],
                          "name": study.get("question") or state["manifest"].get("objective") or run_dir.name,
                          "state_version": state["state_version"], "status": study.get("status"),
                          "budget": state.get("budget"), "jobs": jobs, "observations": len(state.get("observations", [])),
                          "drafts": [{"draft_id": draft["draft_id"], "verified": draft["ok"]} for draft in list_workbench_bound_drafts(root, run_dir, state["manifest"]["study_id"])],
                          "claims": [{"claim_id": item.get("claim_id"), "status": item.get("status")} for item in state.get("claims", [])]})
        return task_memory.redact(facts)

    def task_context_payload(self, workspace_id, session_id=None):
        root = self._context_root(workspace_id)
        try:
            store = AgentSessionStore.readonly(root)
        except FileNotFoundError:
            if session_id:
                raise SessionNotFound("会话不存在")
            return {"workspace_id": workspace_id, "tasks": [], "sessions": [], "binding": None, "selected": None, "facts": self._context_facts(root)}
        try:
            result = task_memory.list_context(store, session_id)
            selected_body = result["selected"]["body"] if result["selected"] else {}
            source_id = selected_body.get("source_session_id")
            source_stale = bool(source_id and store.get_session(source_id)["updated"] != selected_body.get("source_version"))
        finally:
            store.close()
        result.update(workspace_id=workspace_id, facts=self._context_facts(root))
        if result["selected"]:
            current = {item["study_id"]: digest(item) for item in result["facts"]}
            old = result["selected"]["body"].get("fact_fingerprints", {})
            result["stale"] = (source_stale or result["selected"]["revision"] != result["selected"]["version"]
                               or any(current.get(key) != value for key, value in old.items()))
        return result

    def task_context_preview(self, workspace_id, session_id, task_id=None, source_session_id=None, study_ids=None):
        root = self._context_root(workspace_id)
        store = AgentSessionStore.readonly(root)
        try:
            candidate = task_memory.preview(store, session_id, task_id, source_session_id)
        finally:
            store.close()
        facts = self._context_facts(root)
        selected = study_ids if study_ids is not None else candidate.get("study_ids", [])
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected) or set(selected) - {item["study_id"] for item in facts}:
            raise ProtocolError("研究对象不属于当前项目")
        candidate.update(workspace_id=workspace_id, session_id=session_id, study_ids=selected,
                         facts=[item for item in facts if item["study_id"] in selected],
                         fact_fingerprints={item["study_id"]: digest(item) for item in facts if item["study_id"] in selected})
        candidate.pop("preview_sha256", None)
        candidate["preview_sha256"] = digest(candidate)
        return candidate

    def commit_task_context(self, workspace_id, session_id, request):
        with self.chat_lock, self.ws_lock:
            self._check_session_workspace(workspace_id)
            root = self._context_root(workspace_id)
            store = AgentSessionStore(root)
            try:
                prior = task_memory.replay_receipt(store, session_id, request)
                if prior is not None:
                    return prior
                candidate = request.get("preview")
                refreshed = None
                if request.get("op") != "detach":
                    if not isinstance(candidate, dict):
                        raise ProtocolError("缺少交接预览")
                    refreshed = self.task_context_preview(workspace_id, session_id, candidate.get("task_id"),
                                                         candidate.get("source_session_id"), candidate.get("study_ids", []))
                result = task_memory.commit(store, session_id, request, refreshed)
                self.navigation_version += 1
                return result
            finally:
                store.close()

    def _assemble_session_context(self, store, session_id, system, user_text=None,
                                  feedback_source_turn_id=None):
        tools = getattr(self.chat_harness, "context_tools", CHAT_TOOLS)
        link = task_memory.binding(store, session_id)
        facts = []
        if link["task_id"]:
            handoff = task_memory.revision(store, link["task_id"], link["revision"])["body"]
            facts = [item for item in self._context_facts(store.root) if item["study_id"] in handoff.get("study_ids", [])]
            # Facts are scoped by the user's confirmed handoff; no raw files/logs.
        from .research.agent_contracts import execution_contracts
        study_ids = handoff.get("study_ids", []) if link["task_id"] else None
        required = execution_contracts(store.root, self._visible_research_runs(store.root), study_ids)
        from .agent_execution_feedback import turn_feedback
        required.extend(turn_feedback(self, store, session_id, feedback_source_turn_id))
        assembled = task_memory.assemble(store, session_id, system, tools, user_text=user_text,
                                         optional_facts=facts, required_facts=required)
        limits = getattr(self.chat_harness, "context_request_limits", None)
        if limits is not None:
            assembled["request_limits"] = dict(limits)
            size = len(canonical({key: assembled[key] for key in (
                "system", "messages", "tools", "request_limits")}).encode("utf-8"))
            if size > int(os.environ.get("POPPER_CONTEXT_MAX_BYTES", "65536")):
                raise ProtocolError("包含模型限制的输入超过上下文字节上限，未调用模型")
            assembled["input_bytes"] = size
        return assembled

    def _task_source_for_call(self, root, call):
        args = validate_agent_tool_arguments("context.source", call["arguments"])
        store = AgentSessionStore.readonly(root)
        try:
            link = task_memory.binding(store, call["session_id"])
            if not link["task_id"]:
                raise ProtocolError("当前会话尚未确认交接来源")
            current = task_memory.revision(store, link["task_id"], link["revision"])
            if current["revision"] != current["version"]:
                raise task_memory.ContextConflict("任务修订已变化，请先重新核对交接")
            handoff = current["body"]
            allowed = {item["session_id"] for item in handoff.get("sources", [])}
            if args["source_session_id"] not in allowed:
                raise ProtocolError("来源不在当前已确认交接范围内")
            source = task_memory.source_preview(store, args["source_session_id"])
            if source["source_version"] != args["expected_version"]:
                raise task_memory.ContextConflict("来源会话已变化，请重新核对版本")
            if args.get("turn_id"):
                permitted = {item["turn_id"]: item for item in handoff.get("sources", [])
                             if item["session_id"] == args["source_session_id"]}
                source_turn = permitted.get(args["turn_id"])
                if source_turn is None:
                    raise ProtocolError("Turn 不在已确认交接来源内")
                turn = next(item for item in store.turns(args["source_session_id"]) if item["turn_id"] == args["turn_id"])
                if turn["updated"] != source_turn["version"]:
                    raise task_memory.ContextConflict("来源 Turn 已变化，请重新核对交接")
                source["detail"] = task_memory.redact({
                    "session_id": args["source_session_id"], "turn_id": turn["turn_id"], "version": turn["updated"],
                    "status": turn["status"], "user_text": task_memory.redact(turn["user_message"])[:16000],
                    "agent_suggestion": task_memory.redact(turn["assistant_message"] or "")[:16000],
                    "truncated": len(turn["user_message"]) > 16000 or len(turn["assistant_message"] or "") > 16000,
                    "tools": [{"call_id": call["call_id"], "tool_name": call["tool_name"], "status": call["status"],
                               "updated": call["updated"], "result": call["result"] if call["status"] == "completed" else None,
                               "error": call["error"]} for call in store.tool_calls(args["source_session_id"]) if call["turn_id"] == turn["turn_id"]],
                })
            return source
        finally:
            store.close()

    def session_events_payload(self, session_id, *, after=0, limit=200):
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                return {**store.turn_events(session_id, after=after, limit=limit),
                        "integrity": store.verify_turn_events()}
            finally:
                store.close()

    def session_tool_events_payload(self, session_id, *, after=0, limit=200):
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                return {**store.tool_events(session_id, after=after, limit=limit),
                        "integrity": store.verify_tool_events()}
            finally:
                store.close()

    def project_files_payload(self, *, relative=None, workspace_id=None):
        if not self.trusted_local:
            raise ProtocolError("项目代码编辑仅在 --trusted-local 模式下可用")
        with self.ws_lock:
            current_id = self.workspaces.current_wid()
            if workspace_id is not None and workspace_id != current_id:
                raise ProjectFileConflict("工作区已切换，请重新载入项目文件")
            files = ProjectFiles(self.workspace.root)
            result = files.list_files() if relative is None else files.read(relative)
            return {"workspace_id": current_id, **result}

    def save_project_file(self, *, workspace_id, relative, content, expected_sha256):
        if not self.trusted_local:
            raise ProtocolError("项目代码编辑仅在 --trusted-local 模式下可用")
        with self.ws_lock:
            if workspace_id != self.workspaces.current_wid():
                raise ProjectFileConflict("工作区已切换，请重新载入项目文件")
            result = ProjectFiles(self.workspace.root).write(
                relative, content, expected_sha256)
            return {"workspace_id": workspace_id, **result}

    def preview_initial_revision_from_workbench(self, spec):
        if not self.trusted_local:
            raise ProtocolError("创建 CodeRevision 需要可信本机模式")
        if not isinstance(spec, dict) or not isinstance(spec.get("study_id"), str):
            raise ProtocolError("CodeRevision 提案缺少 study_id")
        run_dir = self.research_run_for_study(spec["study_id"])
        with self.ws_lock:
            return preview_initial_revision(run_dir, self.workspace.root, spec)

    def submit_initial_revision_from_workbench(self, spec, proposal_sha256):
        if not self.trusted_local:
            raise ProtocolError("创建 CodeRevision 需要可信本机模式")
        if not isinstance(spec, dict) or not isinstance(spec.get("study_id"), str):
            raise ProtocolError("CodeRevision 提案缺少 study_id")
        run_dir = self.research_run_for_study(spec["study_id"])
        with self.ws_lock:
            return submit_initial_revision(
                run_dir, self.workspace.root, spec, proposal_sha256)

    def preview_project_file(self, *, workspace_id, relative, content, expected_sha256):
        if not self.trusted_local:
            raise ProtocolError("项目代码编辑仅在 --trusted-local 模式下可用")
        with self.ws_lock:
            if workspace_id != self.workspaces.current_wid():
                raise ProjectFileConflict("工作区已切换，请重新载入项目文件")
            result = ProjectFiles(self.workspace.root).preview_write(
                relative, content, expected_sha256)
            return {"workspace_id": workspace_id, **result}

    def create_agent_session(self, title, harness="chat", workspace_id=None):
        resolved_harness, harness_version = harness, None
        if self.chat_harness is not None:
            descriptor = self.chat_harness.descriptor
            if harness not in {"chat", descriptor.name}:
                raise ProtocolError(f"未配置 conversation harness: {harness}")
            resolved_harness = descriptor.name
            harness_version = descriptor.protocol_version
        with self.chat_lock, self.ws_lock:
            self._check_session_workspace(workspace_id)
            store = AgentSessionStore(self.workspace.root)
            try:
                session = store.create_session(
                    title, resolved_harness, self.chat_model,
                    harness_version=harness_version)
                if store.default_session(create=False) is None:
                    store.set_default_session(session["session_id"])
                return session
            finally:
                store.close()

    def _check_session_workspace(self, workspace_id):
        if workspace_id is not None and workspace_id != self.workspaces.current_wid():
            raise SessionMetadataConflict("项目已切换，请重新打开所属项目")

    def update_agent_session(self, workspace_id, session_id, expected_updated, **changes):
        with self.chat_lock, self.ws_lock:
            root = next((item for item in self.workspaces.roots_payload()
                         if item["wid"] == workspace_id), None)
            if root is None:
                raise SessionNotFound("项目不存在")
            store = AgentSessionStore(root["root"])
            try:
                session = store.update_session_metadata(session_id, expected_updated, **changes)
                if workspace_id == self.workspaces.current_wid():
                    self.navigation_version += 1
                return session
            finally:
                store.close()

    def select_agent_session(self, session_id, workspace_id=None):
        with self.chat_lock:
            with self.ws_lock:
                self._check_session_workspace(workspace_id)
                store = AgentSessionStore(self.workspace.root)
                try:
                    store.set_default_session(session_id)
                    self.navigation_version += 1
                except Exception:
                    store.close()
                    raise
            try:
                # Tool previews may acquire ws_lock; never hold it through this projection.
                return self._chat_payload(store)
            finally:
                store.close()

    def set_session_token_budget(self, session_id, token_budget):
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                session = store.set_token_budget(session_id, token_budget)
                return {"session": session, "usage": store.usage_summary(session_id)}
            finally:
                store.close()

    def register_pricing_catalog(self, name, currency, prices):
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                return store.register_pricing_catalog(
                    name, currency, prices, actor="local_user")
            finally:
                store.close()

    def set_session_price_catalog(self, session_id, catalog_id):
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                session = store.set_price_catalog(session_id, catalog_id)
                return {"session": session, "usage": store.usage_summary(session_id)}
            finally:
                store.close()

    def set_session_cost_budget(self, session_id, cost_budget_micros):
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                session = store.set_cost_budget(session_id, cost_budget_micros)
                return {"session": session, "usage": store.usage_summary(session_id)}
            finally:
                store.close()

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
        descriptor = self.chat_harness.descriptor
        session = store.get_session(session_id)
        if session["harness"] == "chat" and session["harness_version"] is None:
            session = store.bind_legacy_chat_harness(
                session_id, descriptor.name, descriptor.protocol_version,
                descriptor.model or self.chat_model)
        if (session["harness"] != descriptor.name
                or session["harness_version"] != descriptor.protocol_version):
            raise ProtocolError(
                f"Session Harness 不兼容: {session['harness']}@"
                f"{session['harness_version'] or 'unversioned'}")
        return session

    def _run_session_turn(self, root, session_id, turn_id, trace_id, system_prompt,
                          cancel_event=None):
        """Run one already-persisted turn; terminal persistence is race-safe."""
        store = AgentSessionStore(root)
        usage = {}
        reply = None
        try:
            try:
                frozen = task_memory.load_input(store, turn_id)
                if frozen:
                    system_prompt, history = frozen["system"], frozen["messages"]
                    configured_tools = getattr(self.chat_harness, "context_tools", frozen["tools"])
                    if digest(configured_tools) != digest(frozen["tools"]):
                        raise ProtocolError("模型工具配置已变化，拒绝用旧预算快照执行；请人工重试")
                    configured_limits = getattr(self.chat_harness, "context_request_limits", None)
                    legacy_limits = {"max_tokens": MODEL_MAX_TOKENS} if configured_limits is not None else None
                    if (frozen.get("request_limits", legacy_limits) != configured_limits
                            and ("request_limits" in frozen or configured_limits is not None)):
                        raise ProtocolError("模型输出或思考限制已变化，拒绝发送旧输入快照；请重新核对")
                else:
                    # Legacy queued work retains its old compatibility path.
                    history = store.model_messages(session_id, turn_id)
                reply = self._invoke_budgeted_harness(
                    frozen, turn_id, system_prompt, history, cancel_event,
                    lambda delta: store.append_turn_delta(turn_id, delta))
                usage = getattr(reply, "usage", {})
                if isinstance(reply, str):
                    return store.complete_turn(turn_id, reply), getattr(reply, "usage", {})
                if (not isinstance(reply, dict) or set(reply) != {"text", "tool_calls"}
                        or not isinstance(reply["text"], str)
                        or not isinstance(reply["tool_calls"], list)
                        or not reply["tool_calls"]):
                    raise ProtocolError("模型回复必须是文本或结构化工具提议")
                proposals = []
                for index, requested in enumerate(reply["tool_calls"]):
                    if (not isinstance(requested, dict)
                            or not {"name", "arguments"}.issubset(requested)
                            or set(requested) - {"name", "arguments", "validation_error"}
                            or not isinstance(requested["name"], str)
                            or not isinstance(requested["arguments"], dict)):
                        raise ProtocolError("模型工具提议结构不合法")
                    tool_name = MODEL_TOOL_ALIASES.get(requested["name"], requested["name"])
                    risk = AGENT_TOOLS.get(tool_name)
                    validation_error = requested.get("validation_error")
                    if validation_error is None:
                        try:
                            if risk is None:
                                raise ProtocolError(f"未登记工具: {requested['name']}")
                            validate_agent_tool_arguments(tool_name, requested["arguments"])
                        except ProtocolError as error:
                            validation_error = {"type": "unknown_tool" if risk is None else "invalid_arguments",
                                                "message": str(error)}
                    if validation_error is not None:
                        if type(usage.get("total_tokens")) is not int or usage["total_tokens"] < 0:
                            raise ProtocolError(str(validation_error.get("message", "参数无效")) +
                                                "；用量无法核实，禁止自动纠错")
                        validation_error = {**validation_error, "original_tool_name": requested["name"],
                                            "executed": False}
                        if "raw_arguments" in validation_error:
                            raw = task_memory.redact(validation_error["raw_arguments"])
                            encoded = canonical(raw).encode("utf-8")
                            validation_error["raw_arguments"] = (raw if len(encoded) <= 32768 else {
                                "retained": False, "bytes": len(encoded),
                                "sha256": digest(raw), "reason": "过大无效参数仅保留摘要校验值，非语义总结"})
                    proposal = {
                        "tool_name": tool_name, "arguments": requested["arguments"],
                        "risk": risk or "write", "idempotency_key": f"model-{turn_id}-{index}",
                        "actor": "agent", "trace_id": trace_id,
                    }
                    if validation_error is not None:
                        proposal["validation_error"] = validation_error
                    proposals.append(proposal)
                turn, _ = store.complete_turn_with_tool_calls(
                    turn_id, reply["text"], proposals)
                return turn, getattr(reply, "usage", {})
            except Exception as error:
                try:
                    from .chat import ChatReplyError, ChatCancelled
                    details = {"type": type(error).__name__, "message": str(error)}
                    if isinstance(reply, dict):
                        # Preserve rejected model proposals as untrusted audit
                        # evidence, never as approved/executed tool messages.
                        # Redaction affects this evidence only, not parameters.
                        try:
                            safe_reply = task_memory.redact_input(reply)
                            body = canonical(safe_reply)
                            size = len(body.encode("utf-8"))
                            evidence = {"source": "rejected model reply; no tool execution",
                                        "bytes": size, "sha256": digest(safe_reply),
                                        "retained": size <= 65536}
                            if evidence["retained"]:
                                evidence["reply"] = safe_reply
                            else:
                                evidence["reason"] = "audit reply exceeds 64 KiB; digest only, not a summary"
                            details["rejected_reply"] = evidence
                        except (TypeError, ValueError):
                            details["rejected_reply"] = {"retained": False,
                                "reason": "reply is not serializable audit data"}
                    if isinstance(error, ChatReplyError):
                        usage = error.usage
                        details["provider"] = task_memory.redact_input({
                            "response_id": error.response_id, "finish_reason": error.finish_reason,
                            "stream_completed": error.stream_completed, "usage": error.usage})
                    elif isinstance(error, ChatCancelled) and error.stream_completed:
                        usage = error.usage
                    return store.fail_turn(turn_id, details), usage
                except ProtocolError:
                    # Cancellation (or another terminal writer) won the race. Never
                    # replace that authoritative terminal state with a late result.
                    return store.get_turn(turn_id), usage
        finally:
            store.close()

    def _invoke_budgeted_harness(self, frozen, turn_id, system_prompt, history,
                                 cancel_event, on_delta):
        """One dispatch boundary for send, tool continuation, steer and retry.

        The durable reservation precedes provider I/O. A crash/transport error
        leaves an unresolved entry, so restart cannot resend an ambiguous call.
        """
        ledger = None
        reserved = settled = False
        try:
            if self.evaluation_budget_path is not None:
                from .research.agent_evaluation_budget import AgentEvaluationBudget
                if frozen is None:
                    raise ProtocolError("评测调用缺少持久输入快照，拒绝发送")
                ledger = AgentEvaluationBudget(self.evaluation_budget_path)
                receipt = ledger.reserve(turn_id, frozen, self._reserve_context(frozen))
                reserved = True
                if receipt["snapshot_sha256"] != digest(frozen):
                    raise ProtocolError("评测预留与执行快照不一致，拒绝发送")
            capabilities = self.chat_harness.descriptor.capabilities
            if "cooperative_cancel" in capabilities:
                reply = self.chat_harness.invoke(system_prompt, history,
                    cancel_event=cancel_event, on_delta=on_delta)
            else:
                reply = self.chat_harness.invoke(system_prompt, history)
            if ledger is not None:
                usage = getattr(reply, "usage", {})
                total = usage.get("total_tokens") if isinstance(usage, dict) else None
                ledger.settle(turn_id, total)
                settled = True
                if type(total) is not int or total < 0:
                    raise ProtocolError("模型实际用量无法核实，评测已停止；不得自动重发")
                if ledger.status()["unresolved_requests"]:
                    raise ProtocolError("模型用量超过预留，评测已停止；需人工核对")
            return reply
        except Exception as error:
            from .chat import ChatReplyError, ChatCancelled
            known_terminal = (isinstance(error, ChatReplyError) or
                              isinstance(error, ChatCancelled) and error.stream_completed)
            if ledger is not None and reserved and not settled and known_terminal:
                total = error.usage.get("total_tokens")
                ledger.settle(turn_id, total if type(total) is int and total >= 0 else None)
                settled = True
            raise
        finally:
            if ledger is not None:
                try:
                    if reserved and not settled:
                        ledger.settle(turn_id, None)
                finally:
                    ledger.close()

    def _validate_turn_input(self, text):
        if self.chat_harness is None:
            raise ProtocolError(CHAT_LABELS["unconfigured_notice"])
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("消息不能为空")
        if len(text) > TEXT_LIMIT:
            raise ProtocolError(f"消息超过长度上限 {TEXT_LIMIT} 字符")

    def send_session_turn(self, session_id, text, idempotency_key, trace_id):
        """Compatibility path: execute synchronously for the legacy chat API."""
        self._validate_turn_input(text)
        with self.chat_lock:
            root = self.workspace.root
            store = AgentSessionStore(self.workspace.root)
            try:
                self._require_session_harness(store, session_id)
                system_prompt = self._chat_system_prompt()
                model_input = self._assemble_session_context(store, session_id, system_prompt, text.strip())
                turn, replayed = store.start_turn(
                    session_id, text.strip(), idempotency_key,
                    actor="local_user", trace_id=trace_id,
                    token_reservation=self._reserve_context(model_input), model_input=model_input)
                turn_id = turn["turn_id"]
            finally:
                store.close()
        if not replayed:
            turn, _ = self._run_session_turn(
                root, session_id, turn_id, trace_id, system_prompt)
        with self.chat_lock:
            store = AgentSessionStore(root)
            try:
                return {"session": store.get_session(session_id), "turn": turn,
                        "replayed": replayed,
                        "messages": store.chat_messages(session_id)}
            finally:
                store.close()

    def launch_session_turn(self, session_id, text, idempotency_key, trace_id, workspace_id=None):
        """Persist a running turn and return immediately while a worker executes it."""
        self._validate_turn_input(text)
        with self.chat_lock, self.ws_lock:
            self._check_session_workspace(workspace_id)
            root = self.workspace.root
            store = AgentSessionStore(root)
            try:
                self._require_session_harness(store, session_id)
                system_prompt = self._chat_system_prompt(root)
                model_input = self._assemble_session_context(store, session_id, system_prompt, text.strip())
                reservation = self._reserve_context(model_input)
                turn, replayed = store.start_turn(
                    session_id, text.strip(), idempotency_key,
                    actor="local_user", trace_id=trace_id, enqueue=True,
                    token_reservation=reservation, model_name=self.chat_model, model_input=model_input)
                job = store.agent_job_for_turn(turn["turn_id"])
                payload = {"session": store.get_session(session_id), "turn": turn,
                           "job": job, "replayed": replayed,
                           "messages": store.chat_messages(session_id)}
            finally:
                store.close()
        if not replayed:
            self._schedule_agent_job(root, job, turn, system_prompt)
        return payload

    def _schedule_agent_job(self, root, job, turn, system_prompt):
        cancel_event = threading.Event()
        with self.turn_jobs_lock:
            if turn["turn_id"] in self._turn_jobs:
                return False
            self._turn_jobs[turn["turn_id"]] = (root, cancel_event)

        def work():
            try:
                self._execute_agent_job(root, job, turn, system_prompt, cancel_event)
            finally:
                with self.turn_jobs_lock:
                    self._turn_jobs.pop(turn["turn_id"], None)
        self._start_job(work)
        return True

    def retry_session_turn(self, turn_id, idempotency_key, trace_id):
        if self.chat_harness is None:
            raise ProtocolError(CHAT_LABELS["unconfigured_notice"])
        with self.chat_lock:
            root = self.workspace.root
            store = AgentSessionStore(root)
            try:
                source = store.get_turn(turn_id)
                self._require_session_harness(store, source["session_id"])
                system_prompt = self._chat_system_prompt(root)
                model_input = self._assemble_session_context(store, source["session_id"], system_prompt,
                    source["user_message"] if source["actor"] != "agent_continuation" else None)
                turn, job, replayed = store.retry_turn(
                    turn_id, idempotency_key,
                    actor=("agent_continuation" if source["actor"] == "agent_continuation"
                           else "local_user"), trace_id=trace_id,
                    token_reservation=self._reserve_context(model_input), model_name=self.chat_model, model_input=model_input)
                payload = {"session": store.get_session(turn["session_id"]),
                           "turn": turn, "job": job, "replayed": replayed,
                           "messages": store.chat_messages(turn["session_id"])}
            finally:
                store.close()
        if not replayed:
            self._schedule_agent_job(
                root, job, turn, system_prompt)
        return payload

    def steer_session_turn(self, turn_id, text, idempotency_key, trace_id):
        self._validate_turn_input(text)
        with self.turn_jobs_lock:
            active = self._turn_jobs.get(turn_id)
        root = active[0] if active else self.workspace.root
        with self.chat_lock:
            store = AgentSessionStore(root)
            try:
                source = store.get_turn(turn_id)
                self._require_session_harness(store, source["session_id"])
                system_prompt = self._chat_system_prompt(root)
                model_input = self._assemble_session_context(store, source["session_id"], system_prompt, text.strip())
                turn, job, replayed = store.steer_turn(
                    turn_id, text, idempotency_key,
                    actor="local_user", trace_id=trace_id,
                    token_reservation=self._reserve_context(model_input), model_name=self.chat_model, model_input=model_input)
                payload = {"session": store.get_session(turn["session_id"]),
                           "turn": turn, "job": job, "replayed": replayed,
                           "messages": store.chat_messages(turn["session_id"])}
            finally:
                store.close()
        if active:
            active[1].set()
        if not replayed:
            self._schedule_agent_job(root, job, turn, system_prompt)
        return payload

    def _execute_agent_job(self, root, job, turn, system_prompt, cancel_event):
        """Claim, heartbeat and terminally reconcile one persistent Agent Job."""
        navigation_version = self.navigation_version
        with self.agent_slots:
            claimed = AgentSessionStore(root)
            try:
                claimed.claim_agent_job(job["job_id"], worker_id=self.agent_worker_id)
            except ProtocolError:
                return
            finally:
                claimed.close()
            heartbeat_stop = threading.Event()

            def heartbeat():
                while not heartbeat_stop.wait(30):
                    lease_store = AgentSessionStore(root)
                    try:
                        lease_store.renew_agent_job(
                            job["job_id"], worker_id=self.agent_worker_id)
                    except ProtocolError:
                        return
                    finally:
                        lease_store.close()

            heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
            heartbeat_thread.start()
            usage = {}
            try:
                _, usage = self._run_session_turn(
                    root, turn["session_id"], turn["turn_id"], turn["trace_id"],
                    system_prompt, cancel_event)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)
            finished = AgentSessionStore(root)
            try:
                finished.finish_agent_job(job["job_id"], usage=usage)
                current = finished.get_turn(turn["turn_id"])
                calls = [call for call in finished.tool_calls(turn["session_id"])
                         if call["turn_id"] == turn["turn_id"]]
                invalid = next((call for call in calls if call["status"] == "invalid"), None)
                if invalid and current["status"] == "completed":
                    if (self.workspace.root.resolve() != Path(root).resolve()
                            or self.navigation_version != navigation_version):
                        finished.set_continuation_state(turn["turn_id"], "blocked", "项目已切换，请核对上下文后继续")
                        invalid = None
            finally:
                finished.close()
            if invalid:
                self._maybe_continue_after_tool(invalid, root)

    def _resume_queued_agent_jobs(self):
        """Resume only durable queued work; running leases are interrupted on recovery."""
        if self.chat_harness is None:
            return
        for item in self.workspaces.roots_payload():
            root = Path(item["root"])
            store = AgentSessionStore(root)
            try:
                jobs = store.queued_agent_jobs()
            finally:
                store.close()
            for job in jobs:
                turn_store = AgentSessionStore(root)
                try:
                    turn = turn_store.get_turn(job["turn_id"])
                    self._require_session_harness(turn_store, turn["session_id"])
                finally:
                    turn_store.close()
                self._schedule_agent_job(
                    root, job, turn, self._chat_system_prompt(root))

    def cancel_session_turn(self, turn_id, reason):
        with self.turn_jobs_lock:
            job = self._turn_jobs.get(turn_id)
        root = job[0] if job else self.workspace.root
        store = AgentSessionStore(root)
        try:
            turn = store.cancel_turn(turn_id, actor="local_user", reason=reason)
            if job:
                job[1].set()
            return {"turn": turn, "messages": store.chat_messages(turn["session_id"])}
        finally:
            store.close()

    def propose_agent_tool(self, session_id, turn_id, tool_name, arguments,
                           idempotency_key, trace_id):
        risk = AGENT_TOOLS.get(tool_name)
        if risk is None:
            raise ProtocolError(f"未登记 Agent tool: {tool_name}")
        validate_agent_tool_arguments(tool_name, arguments)
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                call, replayed = store.propose_tool_call(
                    session_id, turn_id, tool_name, arguments, risk, idempotency_key,
                    actor="agent", trace_id=trace_id)
                return {"tool_call": call, "replayed": replayed}
            finally:
                store.close()

    def decide_agent_tool(self, call_id, decision, reason):
        with self.chat_lock:
            root = self.workspace.root
            store = AgentSessionStore(root)
            try:
                call = store.decide_tool_call(
                    call_id, decision, actor="local_user", reason=reason)
            finally:
                store.close()
        if decision == "reject":
            self._maybe_continue_after_tool(call, root)
        return call

    def cancel_agent_tool(self, call_id, reason):
        with self.chat_lock:
            root = self.workspace.root
            store = AgentSessionStore(root)
            try:
                call = store.cancel_tool_call(
                    call_id, actor="local_user", reason=reason)
            finally:
                store.close()
        self._maybe_continue_after_tool(call, root)
        return call

    def continue_session_after_tools(self, source_turn_id, workspace_root=None):
        """Queue the next model step with native settled tool messages, no fake user input."""
        if self.chat_harness is None:
            raise ProtocolError(CHAT_LABELS["unconfigured_notice"])
        with self.chat_lock, self.ws_lock:
            if self._monitor_stop.is_set():
                raise ProtocolError("工作台正在关闭；真实结果已保存，重启后核对并手动继续")
            root = Path(workspace_root or self.workspace.root).resolve()
            if root != self.workspace.root.resolve():
                raise ProtocolError("项目已切换；真实作业结果已保留，返回原项目核对后继续")
            store = AgentSessionStore(root)
            try:
                source = store.get_turn(source_turn_id)
                self._require_session_harness(store, source["session_id"])
                from .agent_execution_feedback import ExecutionPending, turn_feedback
                feedback = turn_feedback(self, store, source["session_id"], source_turn_id)
                if any(not item["terminal"] for item in feedback):
                    reason = "研究作业仍在执行；等待真实终态后再根据结果继续，不重复执行实验"
                    store.set_continuation_state(source_turn_id, "waiting_job", reason)
                    raise ExecutionPending(reason)
                system_prompt = self._chat_system_prompt(root)
                model_input = self._assemble_session_context(store, source["session_id"], system_prompt,
                                                             feedback_source_turn_id=source_turn_id)
                reservation = self._reserve_context(model_input)
                if self.evaluation_budget_path is not None:
                    from .research.agent_evaluation_budget import AgentEvaluationBudget
                    ledger = AgentEvaluationBudget(self.evaluation_budget_path)
                    try:
                        status, policy = ledger.status(), ledger.policy()
                        if (status["unresolved_requests"] or status["remaining_calls"] <= 0
                                or status["used_tokens"] + reservation > policy["max_tokens"]):
                            raise ProtocolError("评测总账本用量未知或剩余额度不足，禁止续推理")
                    finally:
                        ledger.close()
                turn, job, replayed = store.continue_after_tools(
                    source_turn_id, trace_id=new_trace_id(),
                    token_reservation=reservation, model_name=self.chat_model, model_input=model_input)
                payload = {"session": store.get_session(source["session_id"]),
                           "turn": turn, "job": job, "replayed": replayed,
                           "messages": store.chat_messages(source["session_id"])}
            finally:
                store.close()
        if not replayed:
            self._schedule_agent_job(root, job, turn, system_prompt)
        return payload

    def _maybe_continue_after_tool(self, call, workspace_root=None):
        """Best-effort scheduling; a budget failure never hides the tool outcome."""
        if call["status"] not in {"completed", "failed", "rejected", "cancelled", "interrupted", "invalid"}:
            return
        root = Path(workspace_root or self.workspace.root).resolve()
        try:
            self.continue_session_after_tools(call["turn_id"], root)
        except Exception as error:
            from .agent_execution_feedback import ExecutionPending
            if isinstance(error, ExecutionPending):
                return
            # Other proposals may still await approval, or the budget may need
            # user intervention. Never turn a committed tool result into a
            # false failure; the explicit continuation endpoint can retry.
            store = AgentSessionStore(root)
            try:
                calls = [item for item in store.tool_calls(call["session_id"])
                         if item["turn_id"] == call["turn_id"]]
                pending = any(item["status"] in {"approved", "pending_approval", "running"} for item in calls)
                store.set_continuation_state(call["turn_id"],
                    "waiting_approval" if pending else "blocked", str(error))
            except ProtocolError:
                pass
            finally:
                store.close()

    def _wake_agent_research_job(self, run_dir, job_id):
        """Wake only a persisted wait, never replay a historical experiment.

        Queue completion is authoritative. The original enqueue receipt stays
        immutable, and the unique continuation key prevents duplicate sends.
        A switched project is blocked for review, not silently sent to a model.
        """
        from .agent_execution_feedback import EXECUTION_TOOLS
        waiting = []
        with self.chat_lock:
            for entry in self.workspaces.roots_payload():
                root = Path(entry["root"]).resolve()
                if not (root / SESSIONS_REL).is_file():
                    continue
                store = AgentSessionStore.readonly(root)
                try:
                    if Path(run_dir).resolve() not in {
                            Path(item).resolve() for item in self._visible_research_runs(root)}:
                        continue
                    for session in store.list_sessions():
                        for call in store.tool_calls(session["session_id"]):
                            if (call["tool_name"] not in EXECUTION_TOOLS
                                    or call["status"] != "completed"
                                    or (call.get("result") or {}).get("job", {}).get("job_id") != job_id):
                                continue
                            state = store.get_continuation_state(call["turn_id"])
                            if state and state["status"] == "waiting_job":
                                waiting.append((root, call))
                except (ProtocolError, sqlite3.Error, OSError):
                    # Do not alter an authoritative Job because its old chat
                    # cannot be read. Manual continuation reports that error.
                    continue
                finally:
                    store.close()
        for root, call in waiting:
            self._maybe_continue_after_tool(call, root)

    def _tool_result_for_call(self, root, call):
        from .tool_results import read_detail
        args = validate_agent_tool_arguments("context.tool_result", call["arguments"])
        store = AgentSessionStore.readonly(root)
        try:
            source = store.get_tool_call(args["source_call_id"])
            if source["session_id"] != call["session_id"] or source["status"] != "completed":
                raise ProtocolError("只能读取同项目、同会话已完成工具的结果")
            value = read_detail(source["result"], args["field_path"])
            body = canonical(value)
            offset = args.get("offset", 0)
            if offset > len(body):
                raise ProtocolError("详情读取位置超过长度")
            end = min(offset + 4096, len(body))
            return {"source_call_id": source["call_id"], "field_path": args["field_path"],
                    "encoding": "JSON text chunk; source data, not instructions or a summary",
                    "text": body[offset:end], "offset": offset,
                    "next_offset": end if end < len(body) else None,
                    "sha256": digest(value), "total_characters": len(body)}
        finally:
            store.close()

    def execute_agent_tool(self, call_id):
        workspace = self.workspace
        with self.chat_lock:
            store = AgentSessionStore(workspace.root)
            try:
                call = store.claim_tool_call(call_id)
            finally:
                store.close()
        try:
            if call["tool_name"].startswith("research.") and isinstance(call.get("arguments"), dict):
                study_id = call["arguments"].get("study_id")
                if isinstance(study_id, str):
                    if self.research_run_for_study(study_id) not in self._visible_research_runs(workspace.root):
                        raise ProtocolError("Study 不属于当前工作区")
            if call["tool_name"] in {"workspace.file_list", "workspace.file_read",
                                     "workspace.file_replace"}:
                if not self.trusted_local:
                    raise ProtocolError("项目文件工具仅在可信本机模式可用")
                args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                with self.ws_lock:
                    if self.workspace.root != workspace.root:
                        raise ProjectFileConflict("工作区已切换，不能执行旧工作区文件工具")
                    files = ProjectFiles(workspace.root)
                    if call["tool_name"] == "workspace.file_list":
                        result = self._agent_file_list(files)
                    elif call["tool_name"] == "workspace.file_read":
                        self._require_agent_file_disclosure(files, args["path"])
                        result = files.read(args["path"])
                        if result["bytes"] > 65_536:
                            raise ProtocolError("向模型披露的文件不得超过 64 KiB")
                    else:
                        result = files.apply_replace(call_id, args)
            elif call["tool_name"] == "context.tool_result":
                with self.ws_lock:
                    if self.workspace.root != workspace.root:
                        raise ProtocolError("项目已切换，不能读取旧项目工具详情")
                    result = self._tool_result_for_call(workspace.root, call)
            elif call["tool_name"] == "context.source":
                with self.ws_lock:
                    if self.workspace.root != workspace.root:
                        raise ProtocolError("项目已切换，不能披露旧项目交接来源")
                    result = self._task_source_for_call(workspace.root, call)
            elif call["tool_name"] in revision_tools.REVISION_RISKS:
                result = agent_revisions.execute(self, call, workspace.root)
            elif call["tool_name"] == "research.status":
                validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                result = self.research_status_for_agent(call["arguments"]["study_id"])
            elif call["tool_name"] == "research.study_preview":
                args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                result = preview_study_action(
                    self.research_run_for_study(args["study_id"]), args["action"], args["max_steps"])
            elif call["tool_name"] == "research.study_action":
                args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                if self.evaluation_budget_path is not None:
                    raise ProtocolError("正式 Agent 评测不得用固定 Study 策略代替模型决策，也不能由模型自动消费独立确认资源；请分别审批 Revision")
                job = self.enqueue_study_action(
                    args["study_id"], args["action"], args["max_steps"],
                    args["approval_sha256"], "tool-" + call_id)
                result = {"research_run": str(self.research_run_for_study(args["study_id"])),
                          "job": job}
            elif call["tool_name"] == "research.study_job":
                args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                jobs = StudyJobStore(self.research_run_for_study(args["study_id"]))
                try:
                    jobs.verify()
                    result = jobs.get(args["job_id"])
                    if result["request"]["study_id"] != args["study_id"]:
                        raise ProtocolError("作业不属于该 Study")
                finally:
                    jobs.close()
            elif call["tool_name"] == "research.draft_preview":
                args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                result = self.agent_bound_draft_preview(
                    args["study_id"], args["expected_state_version"],
                    args["title"], args["author_note"])
            elif call["tool_name"] == "research.draft_create":
                args = validate_agent_tool_arguments(call["tool_name"], call["arguments"])
                result = self.agent_create_bound_draft(
                    args["study_id"], args["expected_state_version"],
                    args["title"], args["author_note"], args["approval_sha256"])
            elif call["tool_name"] == "research.inspect_design":
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                result = self.research_design_for_agent(
                    args["study_id"], args["design_id"])
                if result["state_version"] != args["expected_version"]:
                    raise ProtocolError(
                        f"study 版本已变化：期望 {args['expected_version']}，"
                        f"当前 {result['state_version']}；请重新提议读取")
            elif call["tool_name"] == "research.inspect_claim":
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                result = self.research_claim_for_agent(
                    args["study_id"], args["claim_id"])
                if result["state_version"] != args["expected_version"]:
                    raise ProtocolError(
                        f"study 版本已变化：期望 {args['expected_version']}，"
                        f"当前 {result['state_version']}；请重新提议读取")
            elif call["tool_name"] == "research.inspect_observation":
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                result = self.research_observation_for_agent(
                    args["study_id"], args["observation_id"])
                if result["state_version"] != args["expected_version"]:
                    raise ProtocolError(
                        f"study 版本已变化：期望 {args['expected_version']}，"
                        f"当前 {result['state_version']}；请重新提议读取")
            elif call["tool_name"] == "research.execute_design":
                args = call["arguments"]
                validate_agent_tool_arguments(call["tool_name"], args)
                result = self.enqueue_design_execution(
                    args["study_id"], args["design_id"],
                    args["expected_version"], args["design_sha256"])
            elif call["tool_name"] in {"research.create_design",
                                       "research.create_hypothesis",
                                       "research.freeze_design"}:
                study_id, action, payload, expected = agent_research_command(
                    call["tool_name"], call["arguments"])
                run_dir = self.research_run_for_study(study_id)
                research_store = ResearchStore(run_dir / "research.sqlite")
                try:
                    result = research_store.execute_command(
                        study_id, action, payload,
                        idempotency_key="tool-" + call_id,
                        expected_version=expected,
                        actor="agent", trace_id=call["trace_id"])
                finally:
                    research_store.close()
            else:
                command, expected = agent_workspace_command(
                    call["tool_name"], call["arguments"])
                with self.ws_lock:
                    result = workspace.apply_command(
                        command, idempotency_key="tool-" + call_id,
                        expected_version=expected)
            with self.chat_lock:
                store = AgentSessionStore(workspace.root)
                try:
                    finished = store.finish_tool_call(call_id, result=result)
                finally:
                    store.close()
            self._maybe_continue_after_tool(finished, workspace.root)
            return finished
        except ProjectFileUncertain:
            # Do not mark an uncertain filesystem write failed. Startup recovery
            # checks its durable receipt and the current file digest first.
            raise
        except Exception as error:
            with self.chat_lock:
                store = AgentSessionStore(workspace.root)
                try:
                    finished = store.finish_tool_call(
                        call_id, error={"type": type(error).__name__, "message": str(error)})
                finally:
                    store.close()
            self._maybe_continue_after_tool(finished, workspace.root)
            return finished

    def send_chat(self, text):
        """发送一条用户消息：真实调用 BYOK 模型并原子落盘，返回最新对话载荷。

        模型调用失败时保留用户消息，并写入一条带 error 标记的助手条目如实记录
        失败（绝不伪造模型回复）。不写事件链、不推进阶段、不改评测数字。
        """
        if self.chat_harness is None:
            raise ProtocolError(CHAT_LABELS["unconfigured_notice"])
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("消息不能为空")
        if len(text) > TEXT_LIMIT:
            raise ProtocolError(f"消息超过长度上限 {TEXT_LIMIT} 字符")
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                store.import_legacy_chat(self.workspace.root / CHAT_REL, self.chat_model)
                session = store.default_session(create=True, model=self.chat_model)
            finally:
                store.close()
        self.send_session_turn(
            session["session_id"], text.strip(), "chat-" + secrets.token_hex(16),
            new_trace_id())
        return self.chat_payload()

    def terminal_command(self, payload):
        """执行经过结构校验的 ConPTY 生命周期操作。"""
        if not isinstance(payload, dict) or not isinstance(payload.get("op"), str):
            raise ProtocolError("终端请求必须包含 op")
        op = payload["op"]
        if op == "create" and set(payload) == {"op", "cols", "rows"}:
            with self.ws_lock:
                root = self.workspaces.current().root
            return self.terminals.create(payload["cols"], payload["rows"], root=root)
        if op == "read" and set(payload) == {"op", "id", "cursor"}:
            if type(payload["cursor"]) is not int or payload["cursor"] < 0:
                raise ProtocolError("终端 cursor 不合法")
            return self.terminals.get(payload["id"]).read_after(payload["cursor"])
        if op == "input" and set(payload) == {"op", "id", "data"}:
            session = self.terminals.get(payload["id"])
            session.write(payload["data"])
            return {"id": session.id, "alive": True}
        if op == "resize" and set(payload) == {"op", "id", "cols", "rows"}:
            session = self.terminals.get(payload["id"])
            session.resize(payload["cols"], payload["rows"])
            return {"id": session.id, "alive": not session.closed}
        if op == "close" and set(payload) == {"op", "id"}:
            return self.terminals.close(payload["id"])
        raise ProtocolError("终端操作或字段不合法")

    # ── SSE：资源级 diff 广播 ────────────────────────────────────────────────

    def subscribe(self):
        q: queue.Queue = queue.Queue(maxsize=16)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)

    def _broadcast(self, patch):
        frame = ("data: " + canonical(patch) + "\n\n").encode("utf-8")
        for q in tuple(self._subscribers):
            try:
                q.put_nowait(frame)
            except queue.Full:
                # 慢消费者只保留最新一帧（下一帧仍是完整资源快照，可直接覆盖）。
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass

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
        if self.mode == "experiment":
            state_src = self.project / ".popper" / "state.db"
        else:
            state_src = self.project / STATE_DIR
        return {
            "state": (self._path_sig(state_src), self._job_sig(self.job)),
            # 工作区：注册表（登记/切换/移除都会改它）+ 当前根 workspace.json 任一变化。
            "workspace": (self._path_sig(self.workspaces.registry_path),
                          self._path_sig(self.workspaces.current().path)),
            "campaign": (
                self._path_sig(self.campaign_dir / "campaign.json")
                if self.campaign_dir else None,
                self._job_sig(self.campaign_job),
                self._vendor_sig(),
            ),
            # 研究库（manifest + sqlite）任一文件变化都算研究状态变化：
            # 控制器写的是 sqlite，init/manifest 变更落在 research.json。
            "research": tuple(
                (str(run_dir), self._path_sig(run_dir / "research.json"),
                 self._path_sig(run_dir / "research.sqlite"))
                for run_dir in self.research_runs),
            # 自由对话：当前工作区根的 chat.json 任一变化（发送/切换根都会变）。
            # Live deltas are committed to SQLite WAL while the model request
            # holds its connection. Watching only the database misses streaming
            # updates until checkpoint/close.
            "chat": (str(self.workspace.root),
                     self._path_sig(self.workspace.root / SESSIONS_REL),
                     self._path_sig(self.workspace.root / (str(SESSIONS_REL) + "-wal"))),
        }

    def _start_monitor(self):
        baseline = self._fingerprints()
        def loop():
            prev = baseline
            while not self._monitor_stop.wait(1.0):
                try:
                    context = {"workspace_id": self.workspaces.current_wid(), "navigation_version": self.navigation_version}
                    current = self._fingerprints()
                    if prev is None:
                        prev = current
                        continue
                    changed = {name for name in current if current[name] != prev.get(name)}
                    prev = current
                    if not changed:
                        continue
                    patch = {}
                    if "state" in changed:
                        patch["state"] = self.state_payload()
                    if "workspace" in changed:
                        try:
                            patch["workspace"] = self.workspace_payload()
                        except Exception:
                            pass
                    if "campaign" in changed:
                        try:
                            patch["campaign"] = self.campaign_payload()
                        except Exception:
                            pass
                    if "research" in changed:
                        # 逐 run 目录自带错误投影，research_payload 本身不抛。
                        patch["research"] = self.research_payload()
                    if "chat" in changed:
                        # 发送中（LLM 调用可长达 120s）不阻塞 monitor 线程：拿不到锁
                        # 就跳过本 tick，指纹保持未消化，下一 tick 会再推。
                        if self.chat_lock.acquire(blocking=False):
                            try:
                                patch["chat"] = self.chat_payload()
                            finally:
                                self.chat_lock.release()
                        else:
                            # Keep this fingerprint unconsumed so the next tick
                            # retries instead of permanently losing the patch.
                            prev["chat"] = None
                    if patch:
                        if context != {"workspace_id": self.workspaces.current_wid(), "navigation_version": self.navigation_version}:
                            prev = {}
                            continue
                        patch["context"] = context
                        self._broadcast(patch)
                except Exception:
                    # 监控线程永不退出；下一 tick 重试。
                    continue
        self._monitor_thread = threading.Thread(target=loop, daemon=True)
        self._monitor_thread.start()


    def launch(self, action):
        allowed = ({"search", "freeze", "confirm", "replay", "report"}
                   if self.mode == "experiment" else {"run", "verify"})
        if action not in allowed:
            raise ProtocolError("未知操作")
        if action in {"search", "confirm", "run", "verify"} and not self.trusted_local:
            raise ProtocolError("请用 --trusted-local 启动以执行已信任代码")
        if not self.job_lock.acquire(blocking=False):
            raise ProtocolError("已有操作正在执行")
        self.job = {"status": "running", "action": action}

        def work():
            try:
                if self.mode == "experiment":
                    target = Experiment(self.project)
                    try:
                        result = (getattr(target, action)(True) if action in {"search", "confirm"}
                                  else getattr(target, action)())
                    finally:
                        target.close()
                else:
                    target = ReproductionTask(self.project)
                    result = getattr(target, action)(trusted_local=True)
                self.job = {"status": "completed", "action": action, "result": result}
            except Exception as error:
                self.job = {"status": "failed", "action": action, "error": str(error)}
            finally:
                self.job_lock.release()
        self._start_job(work)

    def _start_job(self, target):
        """启动一个可被 `wait_for_jobs()` 等到的后台作业线程。"""
        thread = threading.Thread(target=target, daemon=True)
        with self.job_threads_lock:
            self._job_threads.append(thread)
        thread.start()
        return thread

    def wait_for_jobs(self, timeout=120):
        """等所有已登记的后台作业线程结束，返回仍存活的个数。

        超时不抛异常：调用方（测试与将来的优雅停机）需要知道「还没收尾」这个事实
        本身，而不是在一个自己造的错误里丢掉现场。
        """
        with self.job_threads_lock:
            pending = list(self._job_threads)
            self._job_threads.clear()
        deadline = time.time() + timeout
        for thread in pending:
            thread.join(max(0.0, deadline - time.time()))
        still_alive = [thread for thread in pending if thread.is_alive()]
        if still_alive:
            with self.job_threads_lock:
                self._job_threads.extend(still_alive)
        return len(still_alive)

    def approve_campaign(self, decision, reason="", materialize=None):
        """工作台交互审批：记录决定，批准时在后台线程真正恢复执行 campaign。

        - 批准：写入 `<run_dir>/approvals.json` 之后，用 Orchestrator 重跑该 run_dir
          并注入审批标记（proposal_approved）与 trusted_local 边界，后台线程更新
          campaign_job。恢复所需 LLM 配置缺省不入 —— 无 --base-url/--model 时 LLM
          依赖步骤如实跳过，已产出的候选/检索/提案证据文件保持幂等保留。
        - 驳回：只落盘决定，不执行任何副作用；campaign 保持挂起（可用 CLI 另行处理）。
        """
        if self.campaign_dir is None:
            raise ProtocolError("未配置 --campaign 运行目录，无法审批")
        if decision not in {"approved", "rejected"}:
            raise ProtocolError("审批决定必须为 approved 或 rejected")
        if not self.campaign_lock.acquire(blocking=False):
            raise ProtocolError("已有审批/恢复操作正在执行")
        manifest_path = self.campaign_dir / "campaign.json"
        if not manifest_path.is_file():
            self.campaign_lock.release()
            raise ProtocolError("该 campaign 缺少 manifest（campaign.json），无法审批")
        manifest = read_json(manifest_path)
        pending = manifest.get("waiting_approval")
        if not pending:
            self.campaign_lock.release()
            raise ProtocolError("该 campaign 当前不在挂起审批状态")

        approvals_path = self.campaign_dir / "approvals.json"
        records = read_json(approvals_path) if approvals_path.is_file() else {"records": []}
        if not isinstance(records, dict) or "records" not in records:
            records = {"records": []}
        records["records"].append({
            "step": pending.get("step"), "required": pending.get("required"),
            "decision": decision, "reason": reason,
            "at": datetime.datetime.now().isoformat(),
        })
        write_json(approvals_path, records)
        self.campaign_job = {"status": "idle", "decision": decision}
        if decision == "rejected":
            self.campaign_lock.release()
            return {"decision": "rejected", "run_dir": str(self.campaign_dir)}

        def work():
            try:
                from .campaign_nodes import BUILTIN_NODES
                nodes = self._campaign_nodes or BUILTIN_NODES
                orch = Orchestrator(self.campaign_dir)
                config = {
                    "project": str(self.project),
                    "mode": "trusted_local",
                    "objective": manifest.get("objective") or "",
                    pending.get("required"): True,
                }
                if materialize:
                    config["materialize"] = materialize
                result = orch.run(nodes=nodes, config=config)
                self.campaign_job = {"status": "completed", "decision": "approved",
                                     "result": result}
            except Exception as error:
                self.campaign_job = {"status": "failed", "decision": "approved",
                                     "error": str(error)}
            finally:
                self.campaign_lock.release()
        self._start_job(work)
        return {"decision": "approved", "accepted": True,
                "run_dir": str(self.campaign_dir)}


class Handler(BaseHTTPRequestHandler):
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
        session_route = len(parts) == 5 and parts[:3] == ["api", "v1", "sessions"] and parts[4] == "context"
        if route not in {"/api/v1/context/tasks", "/api/v1/context/preview"} and not session_route:
            return False
        trace = new_trace_id()
        try:
            if not self.authorized() or (method == "POST" and not self.origin_ok()):
                self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, "Same-origin local session required", trace_id=trace))
                return True
            if method == "GET":
                if route == "/api/v1/context/preview" or set(query) != {"workspace_id"} or len(query["workspace_id"]) != 1:
                    raise ProtocolError("任务上下文查询需指定唯一项目")
                result = self.server.task_context_payload(query["workspace_id"][0], parts[3] if session_route else None)
            else:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 131072:
                    raise ProtocolError("交接请求长度不合法")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or data.get("schema_version") != "1.0" or not isinstance(data.get("workspace_id"), str):
                    raise ProtocolError("交接信封不合法")
                if route == "/api/v1/context/preview":
                    if set(data) - {"schema_version", "workspace_id", "session_id", "task_id", "source_session_id", "study_ids"} or not isinstance(data.get("session_id"), str):
                        raise ProtocolError("交接预览字段不合法")
                    result = self.server.task_context_preview(data["workspace_id"], data["session_id"], data.get("task_id"), data.get("source_session_id"), data.get("study_ids"))
                else:
                    sid = parts[3] if session_route else data.get("session_id")
                    if not isinstance(sid, str) or data.get("op") not in {"adopt", "detach"} or set(data) - {"schema_version", "workspace_id", "session_id", "op", "preview", "fields", "title", "idempotency_key", "expected_binding_version", "status"}:
                        raise ProtocolError("交接写请求字段不合法")
                    result = self.server.commit_task_context(data["workspace_id"], sid, data)
            self.send(200, api_success(result, trace_id=trace))
        except (task_memory.ContextConflict, SessionMetadataConflict) as error:
            self.send(409, api_failure(ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace))
        except (SessionNotFound, FileNotFoundError) as error:
            self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace))
        except (ProtocolError, ValueError, TypeError) as error:
            self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace))
        except Exception:
            self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, "无法读取任务上下文，请重试", trace_id=trace, retryable=True))
        return True

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "Invalid Host"})
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        parts = route.strip("/").split("/")
        if self._handle_task_context("GET", route, parts, query):
            return
        if (len(parts) == 5 and parts[:3] == ["api", "v1", "research"]
                and parts[4] == "lifecycle"):
            trace_id = new_trace_id()
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED,
                    "Missing session token", trace_id=trace_id))
            try:
                return self.send(200, api_success(
                    self.server.study_jobs_payload(parts[3]), trace_id=trace_id))
            except (ProtocolError, LookupError, FileNotFoundError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST,
                    str(error), trace_id=trace_id))
        is_benchmark_list = parts == ["api", "v1", "benchmarks"]
        is_benchmark_detail = (len(parts) == 4
                               and parts[:3] == ["api", "v1", "benchmarks"])
        if is_benchmark_list or is_benchmark_detail:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                result = (self.server.benchmark_jobs_payload() if is_benchmark_list
                          else self.server.benchmark_job_detail(parts[3]))
                return self.send(200, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ProtocolError, ValueError, OSError) as error:
                return self.send(409, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
        is_followup_preview = (len(parts) == 5
                               and parts[:3] == ["api", "v1", "research"]
                               and parts[4] == "followup-preview")
        is_evidence_preview = (len(parts) == 5
                               and parts[:3] == ["api", "v1", "research"]
                               and parts[4] == "evidence-preview")
        is_bound_drafts_list = (len(parts) == 5
                                and parts[:3] == ["api", "v1", "research"]
                                and parts[4] == "bound-drafts")
        is_bound_draft_detail = (len(parts) == 6
                                  and parts[:3] == ["api", "v1", "research"]
                                  and parts[4] == "bound-drafts")
        if is_bound_drafts_list or is_bound_draft_detail:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                result = (self.server.bound_drafts_payload(parts[3])
                          if is_bound_drafts_list else
                          self.server.bound_draft_detail(parts[3], parts[5]))
                return self.send(200, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except (FileNotFoundError, LookupError) as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ProtocolError, ValueError, KeyError, OSError) as error:
                return self.send(409, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
        if is_evidence_preview:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                return self.send(200, api_success(
                    self.server.research_evidence_preview(parts[3]), trace_id=trace_id),
                    extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (FileNotFoundError, OSError, ProtocolError, ValueError, KeyError) as error:
                return self.send(409, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
        if is_followup_preview:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                return self.send(200, api_success(
                    self.server.followup_preview(parts[3]), trace_id=trace_id),
                    extra_headers=headers)
            except (FileNotFoundError, LookupError, ProtocolError) as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read follow-up preview",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if parts == ["api", "v1", "design-jobs"]:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                return self.send(200, api_success(
                    self.server.design_jobs_payload(), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read design jobs",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        is_design_detail = (len(parts) == 5
                            and parts[:3] == ["api", "v1", "designs"])
        if is_design_detail:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                return self.send(200, api_success(
                    self.server.research_design_for_agent(parts[3], parts[4]),
                    trace_id=trace_id), extra_headers=headers)
            except (FileNotFoundError, LookupError) as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read design",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        is_design_job = (len(parts) == 4
                         and parts[:3] == ["api", "v1", "design-jobs"])
        if is_design_job:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                return self.send(200, api_success(
                    self.server.design_job_payload(parts[3]), trace_id=trace_id),
                    extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read design job",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        is_revision_job = (len(parts) == 4
                           and parts[:3] == ["api", "v1", "revision-jobs"])
        if is_revision_job:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                return self.send(200, api_success(
                    self.server.revision_job_payload(parts[3]), trace_id=trace_id),
                    extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read revision job",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        is_worktree_events = (len(parts) == 5
                              and parts[:3] == ["api", "v1", "worktrees"]
                              and parts[4] == "events")
        if is_worktree_events:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                return self.send(200, api_success(
                    self.server.revision_worktree_events_payload(parts[3]),
                    trace_id=trace_id), extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read worktree events",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if route == "/api/v1/worktrees":
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                return self.send(200, api_success(
                    self.server.revision_worktrees_payload(), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read worktrees",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        is_session_events = (len(parts) == 5
                             and parts[:3] == ["api", "v1", "sessions"]
                             and parts[4] == "events")
        is_session_tool_events = (len(parts) == 5
                                  and parts[:3] == ["api", "v1", "sessions"]
                                  and parts[4] == "tool-events")
        is_project_files = parts == ["api", "v1", "project-files"]
        is_revision = (len(parts) == 4 and parts[:3] == ["api", "v1", "revisions"])
        if is_revision:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                data = self.server.revision_payload(parts[3])
                return self.send(200, api_success(data, trace_id=trace_id),
                                 extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except ProtocolError as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read revision",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if is_session_events or is_session_tool_events:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id),
                    extra_headers=headers)
            try:
                after = int(query.get("after", ["0"])[0])
                limit = int(query.get("limit", ["200"])[0])
                data = (self.server.session_tool_events_payload(parts[3], after=after,
                                                                limit=limit)
                        if is_session_tool_events else
                        self.server.session_events_payload(parts[3], after=after,
                                                           limit=limit))
                return self.send(200, api_success(data, trace_id=trace_id),
                                 extra_headers=headers)
            except (SessionNotFound, TurnNotFound) as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read session events",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if is_project_files:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized() or not self.server.trusted_local:
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Trusted local session required",
                    trace_id=trace_id), extra_headers=headers)
            try:
                if any(len(values) != 1 for values in query.values()):
                    raise ProtocolError("项目文件查询参数重复")
                if set(query) - {"path", "workspace_id"}:
                    raise ProtocolError("项目文件查询参数不合法")
                result = self.server.project_files_payload(
                    relative=query.get("path", [None])[0],
                    workspace_id=query.get("workspace_id", [None])[0])
                return self.send(200, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except ProjectFileConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except ProtocolError as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "Unable to read project files",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if route in ("/api/v1/workspace", "/api/v1/research", "/api/v1/sessions"):
            trace_id = new_trace_id()
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Missing session token", trace_id=trace_id))
            try:
                if route.endswith("/workspace"):
                    data = self.server.workspace_payload()
                elif route.endswith("/research"):
                    data = self.server.research_payload()
                else:
                    data = self.server.sessions_payload(parse_qs(urlparse(self.path).query).get("scope", [None])[0])
                return self.send(200, api_success(data, trace_id=trace_id),
                                 extra_headers=[("X-Popper-API-Version", "1.0"),
                                                ("X-Trace-Id", trace_id)])
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=[("X-Popper-API-Version", "1.0"),
                                   ("X-Trace-Id", trace_id)])
            except Exception:
                # Do not expose implementation details through the stable API.
                return self.send(500, api_failure(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "Unable to build the requested projection", trace_id=trace_id,
                    retryable=True),
                    extra_headers=[("X-Popper-API-Version", "1.0"),
                                   ("X-Trace-Id", trace_id)])
        if route == "/api/session":
            # 会话令牌引导端点：与之前把 token 注入 HTML 等价（页面本就无需认证即可取到）。
            return self.send(200, {"token": self.server.token})
        if route == "/api/events":
            # SSE 流：EventSource 无法自定义请求头，令牌走 query 参数（loopback 同源）。
            return self._serve_events(query.get("token", [""])[0])
        if route == "/api/state":
            if not self.authorized():
                return self.send(403, {"error": "Missing session token"})
            try:
                self.send(200, self.server.state_payload())
            except Exception as error:
                self.send(400, {"error": str(error)})
            return
        if route in ("/api/literature", "/api/arbor", "/api/campaign", "/api/research",
                     "/api/chat"):
            if not self.authorized():
                return self.send(403, {"error": "Missing session token"})
            try:
                if route == "/api/literature":
                    root = self.server.vendor_workspace_root()
                    self.send(200, {"papers": load_all_literature(root),
                                    "runs": list_vendor_runs(root), "workspace_root": str(root)})
                elif route == "/api/arbor":
                    root = self.server.vendor_workspace_root()
                    self.send(200, {"trees": load_all_arbor(root),
                                    "runs": list_vendor_runs(root), "workspace_root": str(root),
                                    # Arbor 节点状态中文标签：后端单一下发，前端不再硬编码或回退。
                                    "node_status_labels": ARBOR_NODE_STATUS_LABELS})
                elif route == "/api/research":
                    self.send(200, self.server.research_payload())
                elif route == "/api/chat":
                    self.send(200, self.server.chat_payload())
                else:
                    self.send(200, self.server.campaign_payload())
            except Exception as error:
                self.send(400, {"error": str(error)})
            return
        if route == "/api/workspace":
            if not self.authorized():
                return self.send(403, {"error": "Missing session token"})
            try:
                self.send(200, self.server.workspace_payload())
            except Exception as error:
                self.send(400, {"error": str(error)})
            return
        if route == "/api/file":
            if not self.authorized():
                return self.send(403, {"error": "Missing session token"})
            try:
                item = query.get("item", [None])[0]
                if not item:
                    raise ProtocolError("缺失 item 参数")
                with self.server.ws_lock:
                    target = self.server.workspace.deliverable_path(item)
                mime = self._MIME.get(target.suffix.lower(), "application/octet-stream")
                content = target.read_bytes()
                self.send(200, content, mime,
                          extra_headers=[("Content-Disposition",
                                          f'attachment; filename="{target.name}"'),
                                         ("X-Popper-Content-SHA256",
                                          hashlib.sha256(content).hexdigest())])
            except ProtocolError as error:
                self.send(400, {"error": str(error)})
            return
        return self._serve_static()

    def _serve_events(self, token):
        """SSE 长连接：先推一帧全量基线，之后只推变化资源的完整补丁。"""
        if not secrets.compare_digest(token or "", self.server.token):
            return self.send(403, {"error": "Missing session token"})
        try:
            context = {"workspace_id": self.server.workspaces.current_wid(), "navigation_version": self.server.navigation_version}
            full = {"state": self.server.state_payload(),
                    "workspace": self.server.workspace_payload(),
                    "campaign": self.server.campaign_payload(),
                    "research": self.server.research_payload(),
                    "chat": self.server.chat_payload(), "context": context}
            if context != {"workspace_id": self.server.workspaces.current_wid(), "navigation_version": self.server.navigation_version}:
                return self.send(409, {"error": "导航已变化，请重新连接"})
        except Exception as error:
            return self.send(400, {"error": str(error)})
        q = self.server.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(("data: " + canonical(full) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    frame = q.get(timeout=25)
                except queue.Empty:
                    frame = b": keepalive\n\n"
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.server.unsubscribe(q)

    def do_POST(self):
        route = urlparse(self.path).path
        tool_parts = route.strip("/").split("/")
        if self._handle_task_context("POST", route, tool_parts, {}):
            return
        if route in {"/api/v1/research/projects/preview", "/api/v1/research/projects"}:
            trace_id = new_trace_id()
            authorized = self.authorized()
            if authorized and not self.server.trusted_local:
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local or not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED,
                    "Trusted same-origin local session required", trace_id=trace_id))
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 20 * 1024 * 1024:
                    raise ProtocolError("项目准备请求长度不合法")
                body = json.loads(self.rfile.read(size))
                required = {"schema_version", "project_path", "protocol", "files"}
                if route.endswith("/projects"):
                    required.add("approval_sha256")
                if (not isinstance(body, dict) or set(body) != required
                        or body["schema_version"] != "1.0"):
                    raise ProtocolError("项目准备信封不合法")
                if route.endswith("/preview"):
                    result = self.server.preview_project_setup(
                        body["project_path"], body["protocol"], body["files"])
                    return self.send(200, api_success(result, trace_id=trace_id))
                result = self.server.prepare_project(
                    body["project_path"], body["protocol"], body["files"],
                    body["approval_sha256"])
                return self.send(201, api_success(result, trace_id=trace_id))
            except (ValueError, ProtocolError, LookupError, OSError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST,
                    str(error), trace_id=trace_id))
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED,
                    "Unable to prepare project", trace_id=trace_id))
        if (len(tool_parts) >= 6 and tool_parts[:3] == ["api", "v1", "research"]
                and tool_parts[4] == "lifecycle"):
            trace_id = new_trace_id()
            authorized = self.authorized()
            if authorized and not self.server.trusted_local:
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local or not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED,
                    "Trusted same-origin local session required", trace_id=trace_id))
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ProtocolError("Study 请求长度不合法")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or body.get("schema_version") != "1.0":
                    raise ProtocolError("Study 请求信封不合法")
                study_id = tool_parts[3]
                if len(tool_parts) == 6 and tool_parts[5] == "preview":
                    if set(body) != {"schema_version", "action", "max_steps"}:
                        raise ProtocolError("Study 预览字段不合法")
                    result = preview_study_action(self.server.research_run_for_study(study_id),
                                                 body["action"], body["max_steps"])
                elif len(tool_parts) == 6 and tool_parts[5] == "jobs":
                    if set(body) != {"schema_version", "action", "max_steps", "approval_sha256", "idempotency_key"}:
                        raise ProtocolError("Study 执行字段不合法")
                    result = self.server.enqueue_study_action(study_id, body["action"], body["max_steps"],
                                                             body["approval_sha256"], body["idempotency_key"])
                    return self.send(202, api_success(result, trace_id=trace_id))
                elif (len(tool_parts) == 8 and tool_parts[5] == "jobs" and tool_parts[7] == "cancel"
                      and set(body) == {"schema_version"}):
                    result = self.server.cancel_study_job(study_id, tool_parts[6])
                else:
                    raise ProtocolError("Study 操作路径不合法")
                return self.send(200, api_success(result, trace_id=trace_id))
            except (ValueError, ProtocolError, LookupError, FileNotFoundError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id))
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED,
                    "Unable to process Study action", trace_id=trace_id))
        if route in {"/api/v1/research/studies/preview",
                     "/api/v1/research/studies"}:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            authorized = self.authorized()
            if authorized and not self.server.trusted_local:
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Trusted local session required",
                    trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Cross-origin request rejected",
                    trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ProtocolError("Study 初始化请求长度不合法")
                envelope = json.loads(self.rfile.read(size))
                required = ({"schema_version", "run_name", "budget_cap", "proposal_sha256"}
                            if route.endswith("/studies") else
                            {"schema_version", "run_name", "budget_cap"})
                if (not isinstance(envelope, dict) or set(envelope) != required
                        or envelope["schema_version"] != "1.0"):
                    raise ProtocolError("Study 初始化信封不合法")
                if route.endswith("/preview"):
                    result = self.server.preview_study_initialization(
                        envelope["run_name"], envelope["budget_cap"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                result = self.server.initialize_study_from_workbench(
                    envelope["run_name"], envelope["budget_cap"],
                    envelope["proposal_sha256"])
                return self.send(201, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.EXECUTION_FAILED, "Unable to initialize Study",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if route in {"/api/v1/research/revision-drafts/preview",
                     "/api/v1/research/revision-drafts/submit"}:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            authorized = self.authorized()
            if authorized and not self.server.trusted_local:
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Trusted local session required",
                    trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Cross-origin request rejected",
                    trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 400_000:
                    raise ProtocolError("修订提案请求长度不合法")
                envelope = json.loads(self.rfile.read(size))
                required = ({"schema_version", "spec", "proposal_sha256"}
                            if route.endswith("/submit") else {"schema_version", "spec"})
                if (not isinstance(envelope, dict) or set(envelope) != required
                        or envelope["schema_version"] != "1.0"):
                    raise ProtocolError("修订提案信封不合法")
                if route.endswith("/submit"):
                    result = self.server.submit_initial_revision_from_workbench(
                        envelope["spec"], envelope["proposal_sha256"])
                    return self.send(201, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                result = self.server.preview_initial_revision_from_workbench(
                    envelope["spec"])
                return self.send(200, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.EXECUTION_FAILED, "Unable to create CodeRevision",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if route in {"/api/v1/project-files", "/api/v1/project-files/preview"}:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            authorized = self.authorized()
            if authorized and not self.server.trusted_local:
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Trusted local session required",
                    trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Cross-origin request rejected",
                    trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_200_000:
                    raise ProtocolError("项目文件请求长度不合法")
                envelope = json.loads(self.rfile.read(size))
                if (not isinstance(envelope, dict)
                        or set(envelope) != {"schema_version", "workspace_id", "path",
                                             "content", "expected_sha256"}
                        or envelope["schema_version"] != "1.0"):
                    raise ProtocolError("项目文件保存信封不合法")
                operation = (self.server.preview_project_file
                             if route.endswith("/preview") else self.server.save_project_file)
                result = operation(workspace_id=envelope["workspace_id"],
                                   relative=envelope["path"], content=envelope["content"],
                                   expected_sha256=envelope["expected_sha256"])
                return self.send(200, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except ProjectFileConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.EXECUTION_FAILED, "Unable to save project file",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        is_worktree_create = (len(tool_parts) == 5
                              and tool_parts[:3] == ["api", "v1", "revisions"]
                              and tool_parts[4] == "worktrees")
        is_revision_diff_migrate = (len(tool_parts) == 5
                                    and tool_parts[:3] == ["api", "v1", "revisions"]
                                    and tool_parts[4] == "migrate-diff")
        is_initial_revision_execute = (len(tool_parts) == 5
                                       and tool_parts[:3] == ["api", "v1", "revisions"]
                                       and tool_parts[4] == "execute")
        is_worktree_remove = (len(tool_parts) == 5
                              and tool_parts[:3] == ["api", "v1", "worktrees"]
                              and tool_parts[4] == "remove")
        is_worktree_proposal = (len(tool_parts) == 5
                                and tool_parts[:3] == ["api", "v1", "worktrees"]
                                and tool_parts[4] == "proposal")
        is_worktree_submit = (len(tool_parts) == 5
                              and tool_parts[:3] == ["api", "v1", "worktrees"]
                              and tool_parts[4] == "submit")
        is_worktree_execute = (len(tool_parts) == 5
                               and tool_parts[:3] == ["api", "v1", "worktrees"]
                               and tool_parts[4] == "execute")
        is_revision_job_cancel = (len(tool_parts) == 5
                                  and tool_parts[:3] == ["api", "v1", "revision-jobs"]
                                  and tool_parts[4] == "cancel")
        is_design_execute = (tool_parts == ["api", "v1", "design-jobs"])
        is_benchmark_create = (tool_parts == ["api", "v1", "benchmarks"])
        is_followup_create = (tool_parts == ["api", "v1", "research", "followups"])
        is_bound_draft_create = (len(tool_parts) == 5
                                 and tool_parts[:3] == ["api", "v1", "research"]
                                 and tool_parts[4] == "bound-drafts")
        is_bound_draft_review = (len(tool_parts) == 7
                                 and tool_parts[:3] == ["api", "v1", "research"]
                                 and tool_parts[4] == "bound-drafts"
                                 and tool_parts[6] == "review")
        is_design_job_cancel = (len(tool_parts) == 5
                                and tool_parts[:3] == ["api", "v1", "design-jobs"]
                                and tool_parts[4] == "cancel")
        is_design_job_retry = (len(tool_parts) == 5
                               and tool_parts[:3] == ["api", "v1", "design-jobs"]
                               and tool_parts[4] == "retry")
        if (is_worktree_create or is_revision_diff_migrate or is_initial_revision_execute
                or is_worktree_remove
                or is_worktree_proposal or is_worktree_submit
                or is_worktree_execute or is_revision_job_cancel
                or is_design_execute or is_design_job_cancel or is_design_job_retry
                or is_benchmark_create
                or is_followup_create or is_bound_draft_create
                or is_bound_draft_review):
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            authorized = self.authorized()
            if authorized and not self.server.trusted_local:
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED,
                    "Trusted local session required", trace_id=trace_id),
                    extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Cross-origin request rejected",
                    trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= (65536 if is_followup_create or is_bound_draft_create else 4096):
                    raise ProtocolError("请求长度不合法")
                envelope = json.loads(self.rfile.read(size))
                if is_benchmark_create:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "run_name",
                                                 "idempotency_key"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 基准创建信封字段不合法")
                    result = self.server.enqueue_benchmark(
                        envelope["run_name"], envelope["idempotency_key"])
                    return self.send(202, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_bound_draft_review:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "reason"}
                            or envelope["schema_version"] != "1.0"
                            or not isinstance(envelope["reason"], str)
                            or len(envelope["reason"]) > 1000):
                        raise ProtocolError("API v1 证据绑定草稿审校信封字段不合法")
                    result = self.server.review_bound_draft_from_workbench(
                        tool_parts[3], tool_parts[5], envelope["reason"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_bound_draft_create:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "spec"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 证据绑定草稿信封字段不合法")
                    result = self.server.create_bound_draft_from_workbench(
                        tool_parts[3], envelope["spec"])
                    return self.send(201, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_followup_create:
                    required = {"schema_version", "parent_study_id",
                                "expected_parent_manifest_sha256", "new_project_name",
                                "new_run_name", "new_test_path", "candidates",
                                "budget", "objective"}
                    if (not isinstance(envelope, dict) or set(envelope) != required
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 后继研究创建信封字段不合法")
                    result = self.server.create_followup_from_workbench(
                        envelope["parent_study_id"], envelope["new_project_name"],
                        envelope["new_run_name"], envelope["new_test_path"],
                        envelope["candidates"], envelope["budget"],
                        envelope["objective"],
                        envelope["expected_parent_manifest_sha256"])
                    return self.send(201, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_design_execute:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "study_id", "design_id",
                                                 "expected_version", "design_sha256"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 设计执行信封字段不合法")
                    result = self.server.enqueue_design_execution(
                        envelope["study_id"], envelope["design_id"],
                        envelope["expected_version"], envelope["design_sha256"])
                    return self.send(202, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_design_job_cancel:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "reason"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 设计 job 取消信封字段不合法")
                    result = self.server.cancel_design_job(tool_parts[3], envelope["reason"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_design_job_retry:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "expected_attempt",
                                                 "design_sha256"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 设计 job 重试信封字段不合法")
                    result = self.server.retry_design_job(
                        tool_parts[3], envelope["expected_attempt"],
                        envelope["design_sha256"])
                    return self.send(202, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_revision_diff_migrate:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 revision Diff 迁移信封字段不合法")
                    result = self.server.migrate_revision_diff(tool_parts[3])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_initial_revision_execute:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "manifest_sha256"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 初始 revision 执行信封字段不合法")
                    result = self.server.execute_initial_revision(
                        tool_parts[3], envelope["manifest_sha256"])
                    return self.send(202, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_revision_job_cancel:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "reason"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 revision job 取消信封字段不合法")
                    result = self.server.cancel_revision_job(tool_parts[3], envelope["reason"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_worktree_create:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "idempotency_key"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 worktree 创建信封字段不合法")
                    result = self.server.create_revision_worktree(
                        tool_parts[3], envelope["idempotency_key"])
                    return self.send(201, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_worktree_submit:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "proposal_sha256"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 worktree 审批信封字段不合法")
                    result = self.server.submit_worktree_revision(
                        tool_parts[3], envelope["proposal_sha256"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_worktree_execute:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 worktree 执行信封字段不合法")
                    result = self.server.execute_worktree_revision(tool_parts[3])
                    return self.send(202, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if (not isinstance(envelope, dict)
                        or set(envelope) != {"schema_version"}
                        or envelope["schema_version"] != "1.0"):
                    raise ProtocolError("API v1 worktree 操作信封字段不合法")
                result = (self.server.propose_worktree_revision(tool_parts[3])
                          if is_worktree_proposal else
                          self.server.remove_revision_worktree(tool_parts[3]))
                return self.send(200, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.EXECUTION_FAILED, "Worktree operation failed",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        is_tool_proposal = (len(tool_parts) == 5
                            and tool_parts[:3] == ["api", "v1", "sessions"]
                            and tool_parts[4] == "tool-calls")
        is_tool_action = (len(tool_parts) == 5
                          and tool_parts[:3] == ["api", "v1", "tool-calls"]
                          and tool_parts[4] in {"decision", "execute", "cancel"})
        if is_tool_proposal or is_tool_action:
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Invalid session", trace_id=trace_id),
                    extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Cross-origin request rejected",
                    trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65_536:
                    raise ProtocolError("请求长度不合法")
                envelope = json.loads(self.rfile.read(size))
                if not isinstance(envelope, dict) or envelope.get("schema_version") != "1.0":
                    raise ProtocolError("API v1 tool call 信封不合法")
                if is_tool_proposal:
                    required = {"schema_version", "turn_id", "tool_name", "arguments",
                                "idempotency_key"}
                    if set(envelope) != required:
                        raise ProtocolError("tool call proposal 字段不合法")
                    result = self.server.propose_agent_tool(
                        tool_parts[3], envelope["turn_id"], envelope["tool_name"],
                        envelope["arguments"], envelope["idempotency_key"], trace_id)
                    return self.send(201, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                action, call_id = tool_parts[4], tool_parts[3]
                if action == "decision":
                    if set(envelope) != {"schema_version", "decision", "reason"}:
                        raise ProtocolError("tool call decision 字段不合法")
                    result = self.server.decide_agent_tool(
                        call_id, envelope["decision"], envelope["reason"])
                elif action == "cancel":
                    if set(envelope) != {"schema_version", "reason"}:
                        raise ProtocolError("tool call cancel 字段不合法")
                    result = self.server.cancel_agent_tool(call_id, envelope["reason"])
                else:
                    if set(envelope) != {"schema_version"}:
                        raise ProtocolError("tool call execute 字段不合法")
                    result = self.server.execute_agent_tool(call_id)
                return self.send(200, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except (SessionNotFound, TurnNotFound, ToolCallNotFound) as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except SessionIdempotencyConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.IDEMPOTENCY_CONFLICT, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except ToolCallConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.INVALID_TRANSITION, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.EXECUTION_FAILED, "Tool call operation failed",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        session_turn = route.strip("/").split("/")
        is_turn_cancel = (len(session_turn) == 5
                          and session_turn[:3] == ["api", "v1", "turns"]
                          and session_turn[4] == "cancel")
        is_turn_retry = (len(session_turn) == 5
                         and session_turn[:3] == ["api", "v1", "turns"]
                         and session_turn[4] == "retry")
        is_turn_steer = (len(session_turn) == 5
                         and session_turn[:3] == ["api", "v1", "turns"]
                         and session_turn[4] == "steer")
        is_turn_continue = (len(session_turn) == 5
                            and session_turn[:3] == ["api", "v1", "turns"]
                            and session_turn[4] == "continue")
        is_session_turn = (len(session_turn) == 5
                           and session_turn[:3] == ["api", "v1", "sessions"]
                           and session_turn[4] == "turns")
        is_session_select = (len(session_turn) == 5
                             and session_turn[:3] == ["api", "v1", "sessions"]
                             and session_turn[4] == "select")
        is_session_metadata = (len(session_turn) == 5
                               and session_turn[:3] == ["api", "v1", "sessions"]
                               and session_turn[4] == "metadata")
        is_session_budget = (len(session_turn) == 5
                             and session_turn[:3] == ["api", "v1", "sessions"]
                             and session_turn[4] == "budget")
        is_session_pricing = (len(session_turn) == 5
                              and session_turn[:3] == ["api", "v1", "sessions"]
                              and session_turn[4] == "pricing")
        is_session_cost_budget = (len(session_turn) == 5
                                  and session_turn[:3] == ["api", "v1", "sessions"]
                                  and session_turn[4] == "cost-budget")
        if (route in {"/api/v1/sessions", "/api/v1/pricing-catalogs"}
                or is_session_turn or is_session_select or is_session_metadata
                or is_session_budget or is_turn_cancel or is_turn_retry
                or is_turn_steer or is_turn_continue or is_session_pricing
                or is_session_cost_budget):
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Invalid session", trace_id=trace_id),
                    extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Cross-origin request rejected",
                    trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65_536:
                    raise ProtocolError("请求长度不合法")
                envelope = json.loads(self.rfile.read(size))
                if is_session_cost_budget:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "cost_budget_micros"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 session cost budget 信封字段不合法")
                    result = self.server.set_session_cost_budget(
                        session_turn[3], envelope["cost_budget_micros"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if route == "/api/v1/pricing-catalogs":
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {
                                "schema_version", "name", "currency", "prices"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 pricing catalog 信封字段不合法")
                    result = self.server.register_pricing_catalog(
                        envelope["name"], envelope["currency"], envelope["prices"])
                    return self.send(201, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_session_pricing:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "catalog_id"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 session pricing 信封字段不合法")
                    result = self.server.set_session_price_catalog(
                        session_turn[3], envelope["catalog_id"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_session_budget:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "token_budget"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 session budget 信封字段不合法")
                    result = self.server.set_session_token_budget(
                        session_turn[3], envelope["token_budget"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_turn_steer:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {
                                "schema_version", "idempotency_key", "text"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 turn steer 信封字段不合法")
                    result = self.server.steer_session_turn(
                        session_turn[3], envelope["text"],
                        envelope["idempotency_key"], trace_id)
                    return self.send(202, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_turn_retry:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "idempotency_key"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 turn retry 信封字段不合法")
                    result = self.server.retry_session_turn(
                        session_turn[3], envelope["idempotency_key"], trace_id)
                    return self.send(202, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_turn_continue:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 turn continue 信封字段不合法")
                    result = self.server.continue_session_after_tools(session_turn[3])
                    return self.send(202, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_turn_cancel:
                    if (not isinstance(envelope, dict)
                            or set(envelope) != {"schema_version", "reason"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 turn cancel 信封字段不合法")
                    result = self.server.cancel_session_turn(
                        session_turn[3], envelope["reason"])
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_session_metadata:
                    required = {"schema_version", "workspace_id", "expected_updated"}
                    if (not isinstance(envelope, dict) or not required <= set(envelope)
                            or set(envelope) - required not in ({"title"}, {"status"})
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("会话修改信封字段不合法")
                    changes = {key: envelope[key] for key in ("title", "status") if key in envelope}
                    result = self.server.update_agent_session(envelope["workspace_id"], session_turn[3],
                                                              envelope["expected_updated"], **changes)
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if route == "/api/v1/sessions":
                    if (not isinstance(envelope, dict)
                            or set(envelope) - {"workspace_id"} != {"schema_version", "title", "harness"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 session 创建信封字段不合法")
                    result = self.server.create_agent_session(
                        envelope["title"], envelope["harness"], envelope.get("workspace_id"))
                    return self.send(201, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if is_session_select:
                    if (not isinstance(envelope, dict)
                            or set(envelope) - {"workspace_id"} != {"schema_version"}
                            or envelope["schema_version"] != "1.0"):
                        raise ProtocolError("API v1 session 选择信封字段不合法")
                    result = self.server.select_agent_session(session_turn[3], envelope.get("workspace_id"))
                    return self.send(200, api_success(result, trace_id=trace_id),
                                     extra_headers=headers)
                if (not isinstance(envelope, dict)
                        or set(envelope) - {"workspace_id"} != {"schema_version", "idempotency_key", "text"}
                        or envelope["schema_version"] != "1.0"):
                    raise ProtocolError("API v1 turn 信封字段不合法")
                result = self.server.launch_session_turn(
                    session_turn[3], envelope["text"], envelope["idempotency_key"],
                    trace_id, envelope.get("workspace_id"))
                return self.send(202, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except (SessionNotFound, TurnNotFound) as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except SessionMetadataConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except SessionIdempotencyConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.IDEMPOTENCY_CONFLICT, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except SessionBudgetExceeded as error:
                return self.send(409, api_failure(
                    ErrorCode.BUDGET_EXHAUSTED, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.EXECUTION_FAILED, "Agent session operation failed",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if route == "/api/v1/commands/research":
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Invalid session", trace_id=trace_id),
                    extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Cross-origin request rejected",
                    trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65_536:
                    raise ProtocolError("请求长度不合法")
                envelope = json.loads(self.rfile.read(size))
                required = {"schema_version", "idempotency_key", "expected_version",
                            "study_id", "action", "payload"}
                if not isinstance(envelope, dict) or set(envelope) != required:
                    raise ProtocolError("API v1 research 命令信封字段不合法")
                if envelope["schema_version"] != "1.0":
                    raise ProtocolError("不支持的命令 schema_version")
                run_dir = self.server.research_run_for_study(envelope["study_id"])
                store = ResearchStore(run_dir / "research.sqlite")
                try:
                    result = store.execute_command(
                        envelope["study_id"], envelope["action"], envelope["payload"],
                        idempotency_key=envelope["idempotency_key"],
                        expected_version=envelope["expected_version"],
                        actor="local_user", trace_id=trace_id)
                    cursor = store.event_cursor()
                finally:
                    store.close()
                return self.send(200, api_success(
                    result, trace_id=trace_id, event_cursor=cursor),
                    extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(
                    ErrorCode.NOT_FOUND, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except ResearchVersionConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except ResearchIdempotencyConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.IDEMPOTENCY_CONFLICT, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except TransitionError as error:
                return self.send(409, api_failure(
                    ErrorCode.INVALID_TRANSITION, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.EXECUTION_FAILED, "Research command failed",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        if route == "/api/v1/commands/workspace":
            trace_id = new_trace_id()
            headers = [("X-Popper-API-Version", "1.0"), ("X-Trace-Id", trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Invalid session", trace_id=trace_id),
                    extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(
                    ErrorCode.PERMISSION_DENIED, "Cross-origin request rejected",
                    trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65_536:
                    raise ProtocolError("请求长度不合法")
                envelope = json.loads(self.rfile.read(size))
                required = {"schema_version", "idempotency_key", "expected_version",
                            "action", "payload"}
                if not isinstance(envelope, dict) or set(envelope) != required:
                    raise ProtocolError("API v1 workspace 命令信封字段不合法")
                if envelope["schema_version"] != "1.0":
                    raise ProtocolError("不支持的命令 schema_version")
                if not isinstance(envelope["action"], str) or not isinstance(
                        envelope["payload"], dict):
                    raise ProtocolError("action 必须是字符串且 payload 必须是对象")
                command = {"op": envelope["action"], **envelope["payload"]}
                with self.server.ws_lock:
                    ws = self.server.workspace
                    if ws.is_empty():
                        ws.seed_if_empty(ws.project_spec(), ws.current_phase())
                    result = ws.apply_command(
                        command,
                        idempotency_key=envelope["idempotency_key"],
                        expected_version=envelope["expected_version"],
                    )
                return self.send(200, api_success(result, trace_id=trace_id),
                                 extra_headers=headers)
            except WorkspaceVersionConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except WorkspaceIdempotencyConflict as error:
                return self.send(409, api_failure(
                    ErrorCode.IDEMPOTENCY_CONFLICT, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(
                    ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id),
                    extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(
                    ErrorCode.EXECUTION_FAILED, "Workspace command failed",
                    trace_id=trace_id, retryable=True), extra_headers=headers)
        routes = {"/api/action", "/api/adjudicate", "/api/workspace", "/api/materialize",
                  "/api/file", "/api/campaign", "/api/chat", "/api/terminal"}
        if self.path not in routes:
            return self.send(404, {"error": "Not found"})
        if not self.authorized():
            return self.send(403, {"error": "Invalid session"})
        if not self.origin_ok():
            return self.send(403, {"error": "Cross-origin request rejected"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            limit = 1024 if self.path in ("/api/action", "/api/adjudicate", "/api/campaign") \
                else 1_048_576
            if not 0 < size <= limit:
                raise ProtocolError("请求长度不合法")
            payload = json.loads(self.rfile.read(size))
            if self.path == "/api/action":
                if not isinstance(payload, dict) or set(payload) != {"action"} \
                        or not isinstance(payload["action"], str):
                    raise ProtocolError("请求必须仅包含 action 字符串")
                self.server.launch(payload["action"])
                self.send(202, {"accepted": True})
            elif self.path == "/api/chat":
                if not isinstance(payload, dict) or set(payload) != {"text"}:
                    raise ProtocolError("请求必须仅包含 text 字符串")
                self.send(200, self.server.send_chat(payload["text"]))
            elif self.path == "/api/terminal":
                self.send(200, self.server.terminal_command(payload))
            elif self.path == "/api/campaign":
                self._post_campaign(payload)
            elif self.path == "/api/adjudicate":
                self._post_adjudicate(payload)
            elif self.path == "/api/file":
                self._post_file(payload)
            elif self.path == "/api/workspace":
                self._post_workspace(payload)
            else:
                self._post_materialize(payload)
        except (ValueError, ProtocolError) as error:
            self.send(409, {"error": str(error)})

    def _post_adjudicate(self, payload):
        """用户对拒稿风险的裁定（放行/驳回），留痕入事件溯源链。"""
        if not isinstance(payload, dict):
            raise ProtocolError("请求必须为 JSON 对象")
        for key in ("risk_id", "verdict", "reason"):
            if not isinstance(payload.get(key), str):
                raise ProtocolError(f"缺少裁定字段: {key}")
        exp = Experiment(self.server.project)
        try:
            result = exp.adjudicate(payload["risk_id"], payload["verdict"],
                                    payload["reason"])
        finally:
            exp.close()
        self.send(200, result)

    def _post_campaign(self, payload):
        """工作台交互审批（批准/驳回）。批准恢复执行需要 --trusted-local 边界。"""
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            raise ProtocolError("请求必须含 action 字符串")
        action = payload["action"]
        if action not in {"approve", "reject"}:
            raise ProtocolError("campaign 动作必须为 approve 或 reject")
        if not self.server.campaign_dir:
            raise ProtocolError("未配置 --campaign 运行目录，无法审批")
        if action == "approve" and not self.server.trusted_local:
            raise ProtocolError("批准并恢复需以 --trusted-local 启动工作台（将执行已信任代码）")
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ProtocolError("reason 必须为字符串")
        materialize = payload.get("materialize")
        if materialize is not None:
            if not isinstance(materialize, dict):
                raise ProtocolError("materialize 必须为对象")
            if not isinstance(materialize.get("manuscript"), dict):
                raise ProtocolError("materialize.manuscript 必须是对象")
            if materialize.get("template", "md") not in VALID_TEMPLATES:
                raise ProtocolError("未知稿件模板")
            if materialize.get("disclosure", "nature") not in VALID_DISCLOSURES:
                raise ProtocolError("未知披露声明口径")
        decision = "approved" if action == "approve" else "rejected"
        result = self.server.approve_campaign(decision, reason or "", materialize)
        self.send(200, {"ok": True, **result})

    def _post_file(self, payload):
        """写回已登记的物化文本稿件（md/tex/json）。仅覆写，防穿越/防新建。"""
        if not isinstance(payload, dict):
            raise ProtocolError("请求必须为 JSON 对象")
        item = payload.get("item")
        content = payload.get("content")
        expected_sha256 = payload.get("expected_sha256")
        if not isinstance(item, str) or not isinstance(content, str):
            raise ProtocolError("item 与 content 必须为字符串")
        with self.server.ws_lock:
            target = self.server.workspace.write_deliverable(
                item, content, expected_sha256=expected_sha256)
        self.send(200, {"ok": True, "item": item, "filename": target.name,
                        "bytes": target.stat().st_size, "sha256": file_hash(target)})

    def _post_workspace(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("op"), str):
            raise ProtocolError("请求必须含 op 字符串")
        op = payload["op"]
        # 多工作区：切换/移除操作作用于根注册表而非当前根的 workspace.json，
        # 返回切换后的全量工作区载荷（新当前根的 folders + roots 清单）。
        if op in ("workspace_switch", "workspace_remove"):
            wid = payload.get("wid")
            if not isinstance(wid, str) or not wid:
                raise ProtocolError("缺少 wid")
            with self.server.ws_lock:
                if op == "workspace_switch":
                    self.server.workspaces.switch(wid)
                    self.server._activate_workspace_project()
                else:
                    entry = next((row for row in self.server.workspaces.roots_payload()
                                  if row["wid"] == wid), None)
                    if (entry is not None and not entry.get("primary")
                            and self.server.terminals.has_sessions_in_root(entry["root"])):
                        raise ProtocolError("工作区仍有终端会话，请先关闭终端")
                    self.server.workspaces.remove(wid)
                    self.server._activate_workspace_project()
            self.send(200, {"ok": True, **self.server.workspace_payload()})
            return
        with self.server.ws_lock:
            data = self.server.workspace.read()
            self.server.workspace.apply_operation(data, payload)
            self.server.workspace.save(data)
        self.send(200, {"ok": True, "folders": data["folders"]})

    def _post_materialize(self, payload):
        if not isinstance(payload, dict):
            raise ProtocolError("请求必须为 JSON 对象")
        manuscript = payload.get("manuscript")
        if not isinstance(manuscript, dict):
            raise ProtocolError("manuscript 必须是对象")
        template = payload.get("template", "md")
        disclosure = payload.get("disclosure", "nature")
        if template not in VALID_TEMPLATES:
            raise ProtocolError("未知稿件模板")
        if disclosure not in VALID_DISCLOSURES:
            raise ProtocolError("未知披露声明口径")
        with self.server.materialize_lock:
            out = self.server.workspace.ensure_deliverables_dir()
            res = Materializer(self.server.project).materialize(
                manuscript, out, template, disclosure)
            fp = Path(res["manuscript"])
            with self.server.ws_lock:
                if self.server.workspace.is_empty():
                    self.server.workspace.seed_if_empty(
                        self.server.workspace.project_spec(),
                        self.server.workspace.current_phase())
                data = self.server.workspace.read()
                item = self.server.workspace.register_deliverable(
                    data, fp.name, template, disclosure, fp.stat().st_size)
                self.server.workspace.save(data)
        self.send(200, {"status": "materialized", "item": {
            "id": item["id"], "filename": item["filename"], "template": item["template"],
            "disclosure": item["disclosure"], "created": item["created"], "bytes": item["bytes"]}})


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
