"""编排层（LangGraph）。

- 以 LangGraph StateGraph 建模 campaign 图：步骤清单的 `needs` 表达真实依赖——无前置的
  步骤从 START 并发起跑，每条 needs 建一条真边，只有全部前置完成才会触发扇入节点。
- 执行权威只有一处：LangGraph checkpointer（`<run_dir>/graph.sqlite`）保存已执行超步、
  挂起任务与恢复点。`campaign.json` 是面向人与工作台的派生投影（CLI status、工作台审批
  卡片只读），不被读回决定"执行什么"或"从哪里恢复"。
- 语义先跑通：节点函数可注入（无 LLM 时用注册队列/跳过），langgraph 只做控制流 + 中断。
- 两种可复现：审计重放（回放历史响应）vs 复现重跑；fail 语义 success/retryable/fatal。
- 人工审批点（interrupt/resume）：步骤 manifest 可声明 `approval: "<state 标记>"`；
  可通过 approval_artifact 绑定待审产物；标记未满足且产物待审时步骤挂起
  （waiting_approval）并中止该超步；重跑时若 checkpoint 仍有挂起任务则从挂起点续跑
  （已完成分支不重放），否则从 START 重放全图。
  幂等键（已完成的副作用门槛）保证两种路径都不重复副作用。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Annotated, Dict, List, TypedDict

from .core import ProtocolError, read_json, write_json

# LangGraph checkpointer 落盘位置（run_dir 下的执行状态权威）
GRAPH_DB = "graph.sqlite"
_THREAD_ID = "campaign"

# campaign 状态
PENDING, RUNNING, COMPLETED, FAILED = "pending", "running", "completed", "failed"
WAITING_APPROVAL = "waiting_approval"
# 步骤 fail 语义
SUCCESS, RETRYABLE, FATAL = "success", "retryable", "fatal"
# 挂起审计 outcome（不视为完成）
PENDING_APPROVAL = "pending_approval"

# 状态通道的合并规则：并发分支会写同一个 key，LangGraph 要求 Annotated reducer。
_SEVERITY = {PENDING: 0, RUNNING: 1, COMPLETED: 2, RETRYABLE: 3, FAILED: 4}


def _merge_status(left, right):
    """并行分支同时写 status 时取更严的一个（failed > retryable > completed > running > pending）。"""
    if left is None:
        return right
    if right is None:
        return left
    return left if _SEVERITY.get(left, 0) >= _SEVERITY.get(right, 0) else right


def _merge_steps(left, right):
    """并行分支各自追加步骤记录：按图内既定次序归并，同一 key 以最后一次为准。"""
    merged = list(left or [])
    position = {item["key"]: i for i, item in enumerate(merged)}
    for item in right or []:
        if item["key"] in position:
            merged[position[item["key"]]] = item
        else:
            position[item["key"]] = len(merged)
            merged.append(item)
    return sorted(merged, key=lambda item: item.get("index", 0))


class CampaignState(TypedDict, total=False):
    """图状态：只放需要跨超步传递与恢复的执行状态。

    每轮运行配置（project/mode/base_url/model/materialize/_registry 等）不进图状态——
    它们由 run(config=...) 作为运行期只读覆盖层合进节点上下文，既不把不可序列化对象
    写进 checkpoint，也支持恢复时用新配置重建图。动作记录见 `steps`（key + 图内次序 + 结果）。
    """
    objective: str
    status: Annotated[str, _merge_status]
    steps: Annotated[List[Dict], _merge_steps]
    active_project: str


class Orchestrator:
    """把 Popper 逐层命令串成可恢复 campaign。

    steps: [{"key", "fn", "needs"}] 的依赖图；fn(campaign_ctx) -> result。
    ctx 提供统一状态读写，底层落在 run_dir（单一状态源）。
    langgraph 负责状态流转与节点调度，fn 内副作用由 Popper 幂等键保证。
    """

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.manifest_path = self.run_dir / "campaign.json"
        self._fns = {}  # 内存中的可调用节点（持久化时剔除，恢复时宿主注入）
        self._config = {}  # 本轮运行配置（只读覆盖层，不写入 checkpoint）
        self._manifest_lock = threading.Lock()  # 并发分支同时写投影时保护读改写

    # ---- 状态读写（投影：run_dir/campaign.json，面向人与工作台只读展示） ----
    def _load(self):
        if not self.manifest_path.is_file():
            return {"schema_version": "1.0", "objective": None, "status": PENDING,
                    "steps": [], "current": None, "history": []}
        return read_json(self.manifest_path)

    def _save(self, data):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.manifest_path, data)

    def init(self, objective, steps):
        # 持久化时剔除不可序列化的 fn；fn 保留在内存，恢复时由宿主注入。
        self._fns = {s["key"]: s.get("fn") for s in steps if s.get("fn")}
        serializable = [{k: v for k, v in s.items() if k != "fn"} for s in steps]
        data = {
            "schema_version": "1.0", "objective": objective,
            "status": PENDING, "steps": serializable, "current": None,
            "history": [],
        }
        self._save(data)
        self._drop_checkpoint()  # 新一轮 campaign：清掉上一轮的执行状态权威
        return {"status": "initialized", "steps": [s["key"] for s in steps]}

    def _record(self, key, outcome, result):
        with self._manifest_lock:  # 并发分支同时落地投影
            data = self._load()
            # 恢复执行后只在历史中保留旧审批记录，避免工作台继续展示过期审批卡片。
            data.pop("waiting_approval", None)
            data["history"].append({"step": key, "outcome": outcome, "result": result})
            data["current"] = key
            if outcome == FATAL:
                data["status"] = FAILED
            elif outcome == RETRYABLE:
                data["status"] = RETRYABLE
            elif self._all_done(data["history"], data["steps"]):
                data["status"] = COMPLETED
            else:
                data["status"] = RUNNING
            self._save(data)
            return data

    def _mark_waiting(self, key, required):
        """审批挂起：投影落 waiting_approval，执行权威由 checkpointer 保留挂起任务。"""
        with self._manifest_lock:
            data = self._load()
            data["status"] = WAITING_APPROVAL
            data["waiting_approval"] = {
                "step": key, "required": required,
                "reason": f"步骤 {key} 需要人工审批标记 {required}（如 --{required.replace('_', '-')}）",
            }
            data["history"].append({"step": key, "outcome": PENDING_APPROVAL,
                                    "result": {"required": required}})
            self._save(data)

    # ---- LangGraph 图构建与执行 ----
    def build_graph(self, custom_nodes=None, checkpointer=None):
        """按 `needs` 建真实依赖图：无前置的步骤从 START 并发起跑。

        每条依赖建一条真边（扇入由节点内的 needs 屏障兜住"任一父完成即触发"），
        没有后继的节点接 END。返回 (compiled, chain)，chain 是线性拓扑序（只用于展示
        执行次序与依赖校验）。checkpointer 由 run() 注入；不注入时编译出的图可直接
        invoke，用于纯控制流测试（无恢复语义）。
        """
        from langgraph.graph import END, START, StateGraph

        nodes = custom_nodes or {}
        steps = self._load()["steps"]
        chain = self._topo(steps)
        by_key = {s["key"]: s for s in steps}
        graph = StateGraph(CampaignState)
        for position, key in enumerate(chain):
            graph.add_node(key, self._make_runnable(by_key[key], nodes, position))
        depended = set()
        for key in chain:
            needs = by_key[key].get("needs") or []
            if not needs:
                graph.add_edge(START, key)
            for dep in needs:
                graph.add_edge(dep, key)
            depended.update(needs)
        for key in chain:
            if key not in depended:
                graph.add_edge(key, END)
        return graph.compile(checkpointer=checkpointer), chain

    def _make_runnable(self, step, nodes, position=0):
        def runnable(state: CampaignState):
            key = step["key"]
            # fail 语义短路：上游 fatal/retryable 之后不再触发任何副作用。
            if state.get("status") in (FAILED, RETRYABLE):
                return {}
            # 扇入屏障：LangGraph 默认任一父完成即触发，这里等全部 needs 落地才执行。
            needs = step.get("needs") or []
            if needs:
                landed = {item["key"] for item in state.get("steps") or []}
                if not set(needs) <= landed:
                    return {}
            # 运行配置是只读覆盖层：既不进 checkpoint，也支持恢复时换配置重跑。
            ctx = {**state, **self._config}
            # 人工审批点只为实际待审产物挂起；无产物由节点决定跳过或阻断。
            required = step.get("approval")
            if required and not ctx.get(required) and self._approval_is_pending(step):
                self._mark_waiting(key, required)
                raise CampaignInterrupt(key, required)
            fn = nodes.get(key) or self._fns.get(key) or step.get("fn")
            if fn is None:
                raise ProtocolError(f"步骤 {key} 缺执行函数")
            try:
                result = fn(self.run_dir, ctx) or {"outcome": SUCCESS}
            except CampaignFatal as error:
                result = {"outcome": FATAL, "error": str(error)}
            if isinstance(result, dict):
                outcome = qualify(result.get("outcome", SUCCESS))
            else:
                outcome = SUCCESS
            recorded = self._record(key, outcome, result)
            # 返回增量而不是原地改 state：并发分支各自提交，由 reducer 归并。
            update = {"status": recorded["status"],
                      "steps": [{"key": key, "index": position, "result": result}]}
            if isinstance(result, dict) and result.get("active_project"):
                update["active_project"] = str(result["active_project"])
            return update
        return runnable

    def _approval_is_pending(self, step):
        """审批可绑定具体产物；旧版内置 variant manifest 同样受此约束。"""
        artifact = step.get("approval_artifact")
        if artifact is None and (step["key"] == "variant"
                                 and step.get("approval") == "proposal_approved"):
            artifact = {"path": "proposal/proposal.json", "status": "review_required"}
        if artifact is None:
            return True  # 其他自定义审批点保留无条件审批语义。
        path = self.run_dir / artifact["path"]
        if not path.is_file():
            return False
        try:
            value = read_json(path)
        except (ValueError, OSError):
            return False  # 由执行节点报告损坏产物，不向用户请求审批无效内容。
        return isinstance(value, dict) and value.get("status") == artifact["status"]

    def _all_done(self, history=None, steps=None):
        data = self._load() if history is None else {"history": history, "steps": steps}
        # 挂起的步骤不计入完成（恢复后重新执行）。
        latest = {h["step"]: h.get("outcome") for h in data["history"]}
        return all(latest.get(s["key"]) == SUCCESS for s in data["steps"])

    def _topo(self, steps):
        # 简单拓扑排序：无依赖优先，按 needs 归位；若 needs 无法满足则报错。
        done, out = set(), []
        remaining = list(steps)
        while remaining:
            before = len(remaining)
            for s in list(remaining):
                if all(dep in done for dep in s.get("needs", [])):
                    out.append(s["key"])
                    done.add(s["key"])
                    remaining.remove(s)
            if not remaining:
                break
            if len(remaining) == before:
                raise ProtocolError("步骤依赖环或缺失前置，无法拓扑排序")
        return out

    def run(self, nodes=None, stop_for_approval=False, config=None):
        """执行 campaign。到达实际待审的审批点且标记未满足时挂起。

        config：本轮的运行配置（project/mode/base_url/_registry 等），作为只读覆盖层合进
        节点上下文，不写进 checkpoint——既避免把不可序列化对象落盘，也支持恢复时换配置重建图。
        恢复：checkpoint 仍有挂起任务时从挂起点续跑（已完成分支不重放，如 approve 后的重跑）；
        否则视为新一轮，从 START 重放全图（节点幂等保证不重复副作用）。
        两种路径都要求重跑时携带审批参数（如 --proposal-approved）。
        """
        self._config = dict(config or {})
        saver, conn = self._checkpointer()
        try:
            compiled, _ = self.build_graph(nodes, checkpointer=saver)
            thread = {"configurable": {"thread_id": _THREAD_ID}}
            try:
                if compiled.get_state(thread).next:
                    final = compiled.invoke(None, thread)  # 从挂起点续跑
                else:
                    saver.delete_thread(_THREAD_ID)  # 无挂起 = 新一轮，先清历史再重放
                    initial = CampaignState(objective=self._load().get("objective"),
                                            status=PENDING, steps=[])
                    final = compiled.invoke(initial, thread)
            except CampaignInterrupt as interrupt:
                pending = self._load().get("waiting_approval") or {
                    "step": interrupt.step, "required": interrupt.required}
                return {"status": WAITING_APPROVAL, "pending": pending,
                        "resume": f"重跑并携带审批参数（--{interrupt.required.replace('_', '-')}）"}
        finally:
            conn.close()
        steps = [s["key"] for s in final.get("steps", [])]
        return {"status": final.get("status"), "final_step": steps[-1] if steps else None,
                "steps": steps}

    def _checkpointer(self):
        """执行状态权威：`<run_dir>/graph.sqlite`（LangGraph SqliteSaver）。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(str(self.run_dir / GRAPH_DB), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver, conn

    def _drop_checkpoint(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = self.run_dir / (GRAPH_DB + suffix)
            if path.is_file():
                path.unlink()


class CampaignFatal(ProtocolError):
    """步骤致命失败（对应 fail 语义 fatal）。"""


class CampaignInterrupt(Exception):
    """人工审批点挂起信号：图执行中止，等待审批后恢复。"""

    def __init__(self, step, required):
        super().__init__(f"步骤 {step} 等待人工审批标记 {required}")
        self.step = step
        self.required = required


def qualify(outcome):
    """把步骤结果归类为 success/retryable/fatal。"""
    if outcome in (SUCCESS, "passed"):
        return SUCCESS
    if outcome in (RETRYABLE, "retry"):
        return RETRYABLE
    return FATAL
