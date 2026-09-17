"""Campaign 内置节点：把真实 Popper 命令接线为可恢复编排步骤。

每个节点签名 fn(run_dir, state) -> dict（outcome ∈ success/retryable；致命失败抛
CampaignFatal）。节点必须幂等：checkpoint 仍有挂起任务时从挂起点续跑，否则从 START
重放全图；可恢复语义由节点自身的完成门槛保证——读取现有产物/状态即跳过，不重复副作用。

配置经运行期只读覆盖层合进 state（run(config=...) 组装，不进 checkpoint）：
  project      项目目录（Experiment/Materializer/Workspace 所在）
  mode         "trusted_local" | "sandboxed"（执行边界，dev/confirm 必需）
  base_url/model  可选 BYOK LLM（idea/scoop 依赖）
  queries / start_year / end_year  文献检索配置（literature 依赖）
  materialize  稿件 JSON 配置 {manuscript, template, disclosure}
LLM 依赖步骤在未提供 base_url/model 时跳过并如实标注 skipped_reason。
"""
from __future__ import annotations

from pathlib import Path

from .core import Experiment, read_json
from .candidate_contract import contract_from_spec, validate_candidate
from .materialize import Materializer
from .orchestrator import CampaignFatal
from .vendors import VendorRegistry
from .workspace import Workspace

# 默认编排步骤：Idea → 文献 → Scoop → Arbor → arbor-dispatch → 代码提案 → 变体物化
# → Popper dev → freeze → confirm → materialize。
# 依赖关系满足 Orchestrator._topo 的拓扑排序（needs 必须先完成）。
DEFAULT_CAMPAIGN_STEPS = [
    {"key": "idea", "needs": []},
    {"key": "literature", "needs": []},
    {"key": "scoop", "needs": ["idea", "literature"]},
    {"key": "arbor", "needs": ["idea", "scoop"]},
    {"key": "dispatch", "needs": ["idea", "scoop", "arbor"]},
    {"key": "propose", "needs": ["idea", "scoop"]},
    {"key": "variant", "needs": ["propose"], "approval": "proposal_approved",
     "approval_artifact": {"path": "proposal/proposal.json", "status": "review_required"}},
    {"key": "dev", "needs": ["dispatch", "variant"]},
    {"key": "freeze", "needs": ["dev"]},
    {"key": "confirm", "needs": ["freeze"]},
    {"key": "materialize", "needs": ["confirm"]},
]


def _registry(state) -> VendorRegistry:
    return state.get("_registry") or VendorRegistry()


def _experiment(state) -> Experiment:
    project = state.get("active_project") or state.get("project")
    if not project:
        raise CampaignFatal("campaign 缺少 project 配置")
    root = Path(project)
    if not (root / ".popper" / "state.db").is_file():
        raise CampaignFatal("项目未初始化，请先运行 popper experiment init")
    return Experiment(root)


def _llm(state):
    return bool(state.get("base_url") and state.get("model"))


def _incomplete(reason, **details):
    """必要科研证据可恢复地阻断后续步骤，不把未执行工作当作完成。"""
    return {"outcome": "retryable", "reason": reason, **details}


def idea_node(run_dir, state):
    """① 生成层：idea-next 导航器。无 LLM 或已有候选时跳过/复用，如实标注。"""
    target = Path(run_dir) / "idea"
    contract = None
    if state.get("project"):
        exp = Experiment(state["project"])
        try:
            contract = contract_from_spec(exp.verify_inputs()["spec"])
        finally:
            exp.close()
    candidate = target / "phase3_revise" / "final_candidate.json"
    if candidate.is_file():
        if contract is not None:
            validate_candidate(read_json(candidate), contract)
        return {"outcome": "success", "process": "existing", "candidate": str(candidate)}
    if not _llm(state):
        return {"outcome": "success", "skipped": True,
                "reason": "未提供 --base-url/--model，跳过 Idea 导航（可手动 popper vendor idea-next 补全）"}
    query = state.get("query") or state.get("objective") or ""
    options = {"evaluation_contract": contract} if contract is not None else {}
    result = _registry(state).idea_next(target, query, state["base_url"], state["model"], **options)
    if not candidate.is_file():
        raise CampaignFatal("Idea 未生成候选，停止后续科研步骤")
    if contract is not None:
        validate_candidate(read_json(candidate), contract)
    return {"outcome": "success", "process": result.get("process", "manual"),
            "navigation_step": result.get("navigation", {}).get("step"),
            "candidate": str(candidate) if candidate.is_file() else None}


def literature_node(run_dir, state):
    """文献检索：paper-search（请求指纹缓存，天然幂等）。"""
    if state.get("mode") != "trusted_local":
        return {"outcome": "success", "skipped": True,
                "reason": "paper-search 需 --trusted-local（当前 mode 非 trusted_local）"}
    queries = state.get("queries")
    if not queries:
        return {"outcome": "success", "skipped": True,
                "reason": "未提供 --queries-json，跳过文献检索"}
    result = _registry(state).paper_search(
        Path(run_dir), [str(q) for q in queries],
        int(state.get("start_year", 2015)), int(state.get("end_year", 2026)),
        trusted_local=True)
    return {"outcome": "success", "cache": result.get("cache"),
            "papers": len(result.get("papers", [])), "artifact": result.get("artifact")}


def scoop_node(run_dir, state):
    """② 过滤层：Scoop-Check 七步可恢复状态机（需 BYOK LLM）。"""
    if state.get("mode") != "trusted_local":
        return {"outcome": "success", "skipped": True,
                "reason": "scoop-run 需 --trusted-local（当前 mode 非 trusted_local）"}
    if not _llm(state):
        return {"outcome": "success", "skipped": True,
                "reason": "未提供 --base-url/--model，跳过 Scoop-Check（可手动 popper vendor scoop-run）"}
    result = _registry(state).scoop_run(
        Path(run_dir) / "idea", Path(run_dir) / "scoop",
        state["base_url"], state["model"],
        int(state.get("start_year", 2015)), int(state.get("end_year", 2026)),
        **({"refresh_fulltext": True} if state.get("refresh_fulltext") else {}),
        trusted_local=True)
    report_path = Path(run_dir) / "scoop" / "step7.json"
    phase = (read_json(report_path).get("status") if report_path.is_file()
             else result.get("phase") or result.get("status"))
    details = {"phase": phase, "last_step": result.get("last_step") or result.get("step")}
    if not report_path.is_file():
        return _incomplete("Scoop 尚未生成 step7.json 报告，停止后续科研步骤", **details)
    if phase == "provisional" and not state.get("allow_provisional"):
        return _incomplete("Scoop 全文证据不足（provisional）；可用 --refresh-fulltext 重试，不能视为查新完成",
                           **details)
    if phase not in {"completed", "provisional"}:
        return _incomplete("Scoop 报告尚未完成，停止后续科研步骤", **details)
    return {"outcome": "success", **details}


def arbor_node(run_dir, state):
    """③ 验证层：初始化 Arbor 假设树 + 桥接 idea/scoop 证据（均幂等）。"""
    registry = _registry(state)
    arbor_dir = Path(run_dir) / "arbor"
    if not (arbor_dir / ".arbor" / "tree.json").is_file():
        registry.arbor_init(arbor_dir, state.get("objective") or "Popper campaign",
                            "popper-dev-v1", "popper-test-v1")
    idea_dir = Path(run_dir) / "idea"
    scoop_dir = Path(run_dir) / "scoop"
    linked = []
    if (idea_dir / "phase3_revise" / "final_candidate.json").is_file():
        linked.append({"from": "idea", "status": registry.idea_to_arbor(idea_dir, arbor_dir)["status"]})
    report_path = scoop_dir / "step7.json"
    if report_path.is_file():
        report = read_json(report_path)
        if report.get("status") == "provisional" and not state.get("allow_provisional"):
            linked.append({"from": "scoop", "status": "skipped",
                           "reason": "Scoop 报告为 provisional，未放行（--allow-provisional），跳过桥接"})
        else:
            allow = report.get("status") == "provisional"
            linked.append({"from": "scoop",
                           "status": registry.scoop_to_arbor(idea_dir, scoop_dir, arbor_dir,
                                                             allow_provisional=allow)["status"]})
    view = registry.arbor_state(arbor_dir)
    return {"outcome": "success", "nodes": len(view["nodes"]),
            "frontier": len(view["frontier"]), "linked": linked}


def dispatch_node(run_dir, state):
    """arbor-dispatch：BYOK 模型基于 Idea+Scoop 证据从预注册候选选配置并 dev 计分。

    幂等（dispatches.json execution_key 缓存）；缺 LLM/Scoop 报告/桥接节点/非
    searching 阶段时如实跳过。
    """
    if state.get("mode") != "trusted_local":
        return {"outcome": "success", "skipped": True,
                "reason": "arbor-dispatch 需 --trusted-local（当前 mode 非 trusted_local）"}
    if not _llm(state):
        return {"outcome": "success", "skipped": True,
                "reason": "未提供 --base-url/--model，跳过 arbor-dispatch"}
    arbor_dir, scoop_dir, idea_dir = (Path(run_dir) / "arbor",
                                      Path(run_dir) / "scoop", Path(run_dir) / "idea")
    report_path = scoop_dir / "step7.json"
    if not report_path.is_file():
        return {"outcome": "success", "skipped": True, "reason": "缺少 Scoop 报告，跳过 arbor-dispatch"}
    report = read_json(report_path)
    if report.get("status") not in {"completed", "provisional"}:
        return {"outcome": "success", "skipped": True, "reason": "Scoop 报告未完成，跳过 arbor-dispatch"}
    if report.get("status") == "provisional" and not state.get("allow_provisional"):
        return {"outcome": "success", "skipped": True,
                "reason": "Scoop 报告为 provisional，未放行（--allow-provisional），跳过 arbor-dispatch"}
    links_path = arbor_dir / ".popper-integration" / "idea-arbor-links.json"
    links = read_json(links_path) if links_path.is_file() else {}
    links_list = links.get("links", []) if isinstance(links, dict) else []
    if not links_list:
        return {"outcome": "success", "skipped": True, "reason": "无 idea→arbor 桥接节点，跳过 arbor-dispatch"}
    node = links_list[0]["node_id"]
    exp = _experiment(state)
    try:
        if exp.state()["phase"] != "searching":
            return {"outcome": "success", "skipped": True,
                    "reason": "实验已冻结/完成，跳过 arbor-dispatch"}
    finally:
        exp.close()
    result = _registry(state).arbor_dispatch(
        idea_dir, scoop_dir, arbor_dir, state["project"], node,
        state.get("base_url"), state.get("model"), trusted_local=True,
        allow_provisional=report.get("status") == "provisional")
    return {"outcome": "success", "status": result.get("status"),
            "candidate_index": result.get("mapping", {}).get("candidate_index")}


def propose_node(run_dir, state):
    """code-propose：BYOK 模型基于 Idea+Scoop 生成受控代码修改（需 completed scoop 报告）。"""
    if not _llm(state):
        return {"outcome": "success", "skipped": True,
                "reason": "未提供 --base-url/--model，跳过 code-propose"}
    idea_dir, scoop_dir = Path(run_dir) / "idea", Path(run_dir) / "scoop"
    if not (idea_dir / "phase3_revise" / "final_candidate.json").is_file():
        return _incomplete("缺少 Idea candidate，无法执行 code-propose")
    report_path = scoop_dir / "step7.json"
    if not report_path.is_file():
        return _incomplete("缺少 Scoop 报告，无法执行 code-propose")
    phase = read_json(report_path).get("status")
    if phase != "completed":
        return _incomplete("Scoop 报告未完成（需 completed），无法执行 code-propose", phase=phase)
    if not state.get("project"):
        raise CampaignFatal("campaign 缺少 project 配置")
    result = _registry(state).code_propose(
        idea_dir, scoop_dir, Path(run_dir) / "proposal", state["project"],
        state.get("base_url"), state.get("model"))
    proposal_path = Path(run_dir) / "proposal" / "proposal.json"
    if not proposal_path.is_file():
        return _incomplete("code-propose 未生成 proposal.json，不能进入提案审批")
    if read_json(proposal_path).get("status") != "review_required":
        raise CampaignFatal("code-propose 产物状态不是 review_required，不能进入提案审批")
    return {"outcome": "success", "status": result.get("status"),
            "proposal": result.get("proposal")}


def variant_node(run_dir, state):
    """code-materialize：把已审批 proposal 物化为隔离派生项目（需显式 --proposal-approved）。"""
    proposal_path = Path(run_dir) / "proposal" / "proposal.json"
    if not proposal_path.is_file():
        if _llm(state):
            return _incomplete("缺少 proposal，无法物化研究变体")
        return {"outcome": "success", "skipped": True,
                "reason": "缺少 proposal，继续运行预注册配置队列"}
    try:
        proposal = read_json(proposal_path)
    except (ValueError, OSError) as error:
        raise CampaignFatal("proposal.json 无法读取，不能物化变体") from error
    if not isinstance(proposal, dict) or proposal.get("status") != "review_required":
        raise CampaignFatal("proposal 状态不是 review_required，不能物化变体")
    if not state.get("proposal_approved"):
        return _incomplete("提案未审批（code-materialize 需 --proposal-approved）；人工 review "
                           "proposal/proposal.diff 后以 --proposal-approved 重跑")
    if not state.get("project"):
        raise CampaignFatal("campaign 缺少 project 配置")
    result = _registry(state).code_materialize(
        Path(run_dir) / "proposal", state["project"],
        config_index=int(state.get("config_index", 0)), approved=True)
    if result.get("status") != "ready" or not result.get("project"):
        raise CampaignFatal("变体物化未返回 ready 项目，拒绝继续原项目实验")
    active_project = str(result["project"])
    # 以返回值而非原地改写 state 上报派生项目：图状态由 reducer 归并，原地改不会生效。
    return {"outcome": "success", "status": result["status"], "project": active_project,
            "cache": result.get("cache"), "active_project": active_project}


def dev_node(run_dir, state):
    """Popper 开发集搜索（基线 + 候选循环，预算内可恢复；冻结后跳过）。"""
    if not state.get("mode"):
        raise CampaignFatal("dev 节点需要 --trusted-local 或 --sandbox")
    exp = _experiment(state)
    try:
        phase = exp.state()["phase"]
        if phase != "searching":
            return {"outcome": "success", "phase": phase, "note": "开发集搜索已完成，跳过"}
        exp.search(trusted_local=state["mode"] == "trusted_local",
                   sandboxed=state["mode"] == "sandboxed")
        return {"outcome": "success", "phase": "searching",
                "dev_runs": len(exp.results("dev"))}
    finally:
        exp.close()


def freeze_node(run_dir, state):
    """冻结 dev 最优候选（仅 searching 可冻结）。"""
    exp = _experiment(state)
    try:
        phase = exp.state()["phase"]
        if phase != "searching":
            return {"outcome": "success", "phase": phase, "note": "候选已冻结或已确认，跳过"}
        after = exp.freeze()
        return {"outcome": "success", "phase": "frozen", "selected": after.get("selected")}
    finally:
        exp.close()


def confirm_node(run_dir, state):
    """一次性最终确认：消费测试集产出 claim。"""
    if not state.get("mode"):
        raise CampaignFatal("confirm 节点需要 --trusted-local 或 --sandbox")
    exp = _experiment(state)
    try:
        phase = exp.state()["phase"]
        if phase != "frozen":
            return {"outcome": "success", "phase": phase, "note": "最终确认已完成，跳过"}
        claim = exp.confirm(trusted_local=state["mode"] == "trusted_local",
                            sandboxed=state["mode"] == "sandboxed")
        return {"outcome": "success", "phase": "completed",
                "delta": claim.get("delta"), "status": claim.get("status")}
    finally:
        exp.close()


def materialize_node(run_dir, state):
    """成果物化：渲染稿件并登记到 workspace（已物化则跳过）。"""
    target = state.get("active_project") or state.get("project")
    project = Path(target) if target else None
    mat = state.get("materialize")
    if not project or not mat or not isinstance(mat.get("manuscript"), dict):
        return {"outcome": "success", "skipped": True,
                "reason": "未提供 project/--materialize-json，跳过成果物化"}
    template = mat.get("template", "md")
    disclosure = mat.get("disclosure", "nature")
    out = project / "deliverables"
    path = out / f"manuscript.{template}"
    if path.is_file():
        return {"outcome": "success", "skipped": True, "manuscript": str(path),
                "reason": "已物化，跳过"}
    res = Materializer(project).materialize(mat["manuscript"], out, template, disclosure)
    fp = Path(res["manuscript"])
    ws = Workspace(project)
    if ws.is_empty():
        ws.seed_if_empty(ws.project_spec(), ws.current_phase())
    data = ws.read() or {}
    ws.register_deliverable(data, fp.name, template, disclosure, fp.stat().st_size)
    ws.save(data)
    return {"outcome": "success", "manuscript": str(fp), "template": template,
            "bytes": fp.stat().st_size}


BUILTIN_NODES = {
    "idea": idea_node,
    "literature": literature_node,
    "scoop": scoop_node,
    "arbor": arbor_node,
    "dispatch": dispatch_node,
    "propose": propose_node,
    "variant": variant_node,
    "dev": dev_node,
    "freeze": freeze_node,
    "confirm": confirm_node,
    "materialize": materialize_node,
}
