# 科学执行权硬收敛

日期：2026-10-23  
状态：确认闸已落地；本轮补齐 Adaptive 评估写入收敛与统一科学入口  
仓库：`research-agent-v2` / 分支 `autopilot`

## 已完成

1. **确认集**：`claims.confirm` / `confirm_signed` 必须经 `authorize_confirmation`。
2. **Campaign confirm**：禁止 `Experiment.confirm`；需要 `research-runtime/v1` + Controller。
3. **证据写入**：`record_judgment` 与终态假设状态（supported / contradicted）必须经 `authorize_evidence_write`。
4. **Adaptive `_evaluate`**：worker 调度仍在 Adaptive；独立比较子进程与 observation/judgment/终态写入经 `scientific_execution`（强制证据授权）。`_run` 仍是本地 JobSpec/Worker（与 Controller 的 worker 同型能力，不另立计分权威）。
5. **统一科学入口**：`ScientificRuntime`（`runtime_facade.py`）打开 registered / adaptive 会话。Orchestrator **不是** ScientificRuntime（`campaign_is_not_scientific_runtime()`）。

## 刻意不做的「物理三合一」

不把 `ResearchController`、`AdaptiveResearchRuntime`、`Orchestrator` 揉成一个 5000 行类。产品层已收敛为：

- 科学：`ScientificRuntime` → Controller 或 Adaptive（评估写入共享模块）
- 编排：Orchestrator（确认必须进 Controller）

## 成功标准

- 未授权 `record_judgment` / 终态 status / `claims.confirm` 均失败。
- Adaptive 评估发布路径只存在于 `scientific_execution`。
- Adaptive 基准经 `ScientificRuntime.open_adaptive` 构造。
