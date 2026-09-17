"""Loopback-only workstation for one explicitly selected experiment project."""
import datetime
import hashlib
import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import Experiment, ProtocolError, canonical, read_json, write_json
from .reproduction import ReproductionTask, STATE_DIR
from .review import GATE_STATUS_LABELS, MODE7_NAMES, RISK_NAMES
from .workspace import (
    DISCLOSURE_LABELS,
    TEMPLATE_LABELS,
    VALID_DISCLOSURES,
    VALID_TEMPLATES,
    Workspace,
)
from .materialize import Materializer
from .orchestrator import Orchestrator


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


def load_literature(project):
    """读取真实 paper-search 检索产物（只读 vendor run）。

    结构：integrations/runs/<project>/literature/search-*.json
    返回 papers 列表；无则空。
    """
    base = Path(__file__).resolve().parents[1] / "integrations" / "runs"
    run_dir = base / project
    papers = []
    searchfiles = list((run_dir / "literature").glob("search-*.json")) \
        if (run_dir / "literature").is_dir() else []
    for path in searchfiles:
        try:
            data = read_json(path)
            for p in data.get("papers", []) or []:
                papers.append(p)
        except Exception:
            continue
    return papers


def load_arbor(project):
    """读取真实 Arbor 假设树产物（只读 vendor run）。

    结构：integrations/runs/<project>/.arbor/tree.json + run.json
    返回 {run, nodes, frontier, evidence}（投影，与 vendor arbor-state 对齐）。
    """
    base = Path(__file__).resolve().parents[1] / "integrations" / "runs"
    arbor = base / project / ".arbor"
    tree_path = arbor / "tree.json"
    if not tree_path.is_file():
        return None
    try:
        tree = read_json(tree_path)
        run = read_json(arbor / "run.json") if (arbor / "run.json").is_file() else {}
        nodes = list(tree.get("nodes", []).values() or tree.get("nodes", []))
        # nodes 可能是 dict（id→node）或 list；统一成 list
        if isinstance(nodes, dict):
            nodes = list(nodes.values())
        root = tree.get("root", {})
        if root and not any(n.get("id") == "n0" for n in nodes):
            nodes.insert(0, root)
        frontier = [{"id": n["id"]} for n in nodes if n.get("status") in {"pending", "executed"}]
        evidence = [{"node_id": n.get("id"),
                     "dev_score": (n.get("metadata") or {}).get("dev_score"),
                     "result": (n.get("metadata") or {}).get("result", "")}
                    for n in nodes if n.get("status") in {"executed", "support", "contradict", "merged"}]
        return {"run": run, "root": root, "nodes": nodes, "frontier": frontier, "evidence": evidence}
    except Exception:
        return None


def list_vendor_runs():
    """列出 integrations/runs 下所有 vendor 运行目录（论文/假设器的真实数据源根目录）。"""
    base = Path(__file__).resolve().parents[1] / "integrations" / "runs"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def load_all_literature():
    """读取全部真实 paper-search 检索产物（所有 run 合并）。"""
    base = Path(__file__).resolve().parents[1] / "integrations" / "runs"
    papers = []
    for run in list_vendor_runs():
        papers.extend(load_literature(run))
    return papers


def load_all_arbor():
    """读取全部真实 Arbor 假设树产物。"""
    arbor_runs = []
    for run in list_vendor_runs():
        data = load_arbor(run)
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
    if (root / STATE_DIR / "state.json").is_file():
        return "reproduction"
    if (root / ".popper" / "state.db").is_file():
        return "experiment"
    raise ProtocolError("项目既不是已初始化实验，也不是已注册复现任务")


class Workstation(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, project, trusted_local=False, campaign_dir=None):
        self.project = Path(project).resolve()
        self.mode = project_mode(self.project)
        self.token = secrets.token_urlsafe(32)
        self.trusted_local = trusted_local
        self.job_lock = threading.Lock()
        self.job = {"status": "idle"}
        self.campaign_dir = Path(campaign_dir) if campaign_dir else None
        if self.campaign_dir is not None and self.mode != "experiment":
            raise ProtocolError("--campaign 仅对实验项目生效（当前为论文复现模式）")
        self.campaign_lock = threading.Lock()
        self.campaign_job = {"status": "idle"}
        # 审批恢复所用内置节点；测试可注入 fake 以便离线验证调用链。
        self._campaign_nodes = None
        self.ws_lock = threading.Lock()
        self.materialize_lock = threading.Lock()
        self.workspace = Workspace(self.project, self.ws_lock)
        # SSE 订阅者：monitor 线程按资源指纹做 diff，只广播变化资源的完整补丁。
        self._subscribers: set[queue.Queue] = set()
        super().__init__(address, Handler)
        self._start_monitor()

    # ── 资源 payload：HTTP 端点与 SSE monitor 共用同一份构造逻辑 ──────────────

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
            state = ReproductionTask(self.project).audit()
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
                       "artifacts": artifacts}
        payload.update({"job": self.job, "trusted_local": self.trusted_local,
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
        """构造 GET /api/workspace 的响应。"""
        with self.ws_lock:
            if self.workspace.is_empty():
                self.workspace.seed_if_empty(
                    self.workspace.project_spec(),
                    self.workspace.current_phase())
            data = self.workspace.read()
        return {"schema_version": data["schema_version"], "meta": data["meta"],
                "folders": data["folders"],
                "deliverables": self.workspace.list_deliverables(data),
                # 稿件模板与 AI 披露口径中文标签：后端单一下发，前端不再硬编码或回退。
                "template_labels": TEMPLATE_LABELS,
                "disclosure_labels": DISCLOSURE_LABELS,
                # 合法模板与披露口径取值清单：后端单一下发，前端不再硬编码。
                "valid_templates": sorted(VALID_TEMPLATES),
                "valid_disclosures": sorted(d for d in VALID_DISCLOSURES if d)}

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
        papers = load_all_literature()
        arbor = load_all_arbor()
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
            "workspace": self._path_sig(self.project / ".popper" / "workspace.json"),
            "campaign": (
                self._path_sig(self.campaign_dir / "campaign.json")
                if self.campaign_dir else None,
                self._job_sig(self.campaign_job),
                self._vendor_sig(),
            ),
        }

    def _start_monitor(self):
        def loop():
            prev = None
            while True:
                time.sleep(1.0)
                try:
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
                    if patch:
                        self._broadcast(patch)
                except Exception:
                    # 监控线程永不退出；下一 tick 重试。
                    continue
        threading.Thread(target=loop, daemon=True).start()


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
        threading.Thread(target=work, daemon=True).start()

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
        threading.Thread(target=work, daemon=True).start()
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
        return Path(__file__).resolve().parents[1] / "frontend" / "dist"

    _MIME = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".mjs": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8",
        ".map": "application/json; charset=utf-8",
        ".ico": "image/x-icon",
        ".png": "image/png",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }

    def _serve_static(self):
        dist = self._frontend_dist()
        if not (dist / "index.html").is_file():
            # 不再回退到任何内嵌/原型 HTML：前端未构建时显式报错。
            return self.send(503, {"error": "前端未构建：请先在 frontend/ 执行 npm install 与 npm run build"})
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

    def authorized(self):
        return self.host_ok() and secrets.compare_digest(self.headers.get("X-Popper-Token", ""), self.server.token)

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

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "Invalid Host"})
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
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
        if route in ("/api/literature", "/api/arbor", "/api/campaign"):
            if not self.authorized():
                return self.send(403, {"error": "Missing session token"})
            try:
                if route == "/api/literature":
                    self.send(200, {"papers": load_all_literature(),
                                    "runs": list_vendor_runs()})
                elif route == "/api/arbor":
                    self.send(200, {"trees": load_all_arbor(),
                                    "runs": list_vendor_runs(),
                                    # Arbor 节点状态中文标签：后端单一下发，前端不再硬编码或回退。
                                    "node_status_labels": ARBOR_NODE_STATUS_LABELS})
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
                self.send(200, target.read_bytes(), mime,
                          extra_headers=[("Content-Disposition",
                                          f'attachment; filename="{target.name}"')])
            except ProtocolError as error:
                self.send(400, {"error": str(error)})
            return
        return self._serve_static()

    def _serve_events(self, token):
        """SSE 长连接：先推一帧全量基线，之后只推变化资源的完整补丁。"""
        if not secrets.compare_digest(token or "", self.server.token):
            return self.send(403, {"error": "Missing session token"})
        try:
            full = {"state": self.server.state_payload(),
                    "workspace": self.server.workspace_payload(),
                    "campaign": self.server.campaign_payload()}
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
        routes = {"/api/action", "/api/adjudicate", "/api/workspace", "/api/materialize",
                  "/api/file", "/api/campaign"}
        if self.path not in routes:
            return self.send(404, {"error": "Not found"})
        if not self.authorized():
            return self.send(403, {"error": "Invalid session"})
        if self.headers.get("Origin") not in {None, f"http://{self.headers.get('Host')}"}:
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
        if not isinstance(item, str) or not isinstance(content, str):
            raise ProtocolError("item 与 content 必须为字符串")
        with self.server.ws_lock:
            target = self.server.workspace.write_deliverable(item, content)
        self.send(200, {"ok": True, "item": item, "filename": target.name,
                        "bytes": target.stat().st_size})

    def _post_workspace(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("op"), str):
            raise ProtocolError("请求必须含 op 字符串")
        op = payload["op"]
        handler = {
            "folder_create": (["name"], lambda w, d: w.folder_create(d, payload["name"])),
            "folder_rename": (["fid", "name"], lambda w, d: w.folder_rename(d, payload["fid"], payload["name"])),
            "folder_remove": (["fid"], lambda w, d: w.folder_remove(d, payload["fid"])),
            "task_create": (["fid"], lambda w, d: w.task_create(d, payload["fid"], payload.get("title"))),
            "task_rename": (["fid", "quid", "title"], lambda w, d: w.task_rename(d, payload["fid"], payload["quid"], payload["title"])),
            "task_toggle": (["fid", "quid"], lambda w, d: w.task_toggle(d, payload["fid"], payload["quid"], bool(payload.get("done", False)))),
            "task_remove": (["fid", "quid"], lambda w, d: w.task_remove(d, payload["fid"], payload["quid"])),
            "task_move": (["from_fid", "quid", "to_fid"],
                          lambda w, d: w.task_move(d, payload["from_fid"], payload["quid"], payload["to_fid"])),
        }.get(op)
        if handler is None:
            raise ProtocolError("未知工作区操作")
        needed, apply = handler
        for key in needed:
            if key not in payload or payload[key] is None:
                raise ProtocolError(f"缺少 {key}")
        with self.server.ws_lock:
            data = self.server.workspace.read()
            apply(self.server.workspace, data)
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


def serve(project, port, trusted_local=False, campaign_dir=None):
    project_mode(project)
    server = Workstation(("127.0.0.1", port), project, trusted_local, campaign_dir)
    print(f"Popper workstation: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
