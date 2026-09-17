# Pima 三方对照（§9.2）· 人工 / 预注册 / 反馈 Agent

更新：2026-09-11（Agent 臂已用修复后的 proposer 重跑，见「修复记录」）

同一研究问题上，同数据（同一 SHA-256 划分）、同候选集合（7 个）、同预算（budget=4）、同算力/种子（seeds=[11,29,47]），仅选择策略不同。

| 臂 | 选择策略 | 冻结的候选 | confirm delta | 独立有效改善(≥0.02) |
|---|---|---|---|---|
| 人工（脚本化直觉） | human: A→B→D→E | median+RF(d12) | **-0.0108** | 否 |
| 预注册（注册原序） | prereg: A→B→C→D | median+LR(C=0.1) | **+0.0000** | 否 |
| 反馈 Agent（LLM） | agent: A→D→F→E | **none+RF(d6)** | **+0.0216** | **是** |

> 候选编号：A=median+LR(C1)，B=median+LR(C.01)，C=median+LR(C.1)，D=median+RF(d6)，E=median+RF(d12)，F=none+RF(d6)，G=none+SVC。预算 4。

## §9.2 指标对照

| 指标 | 人工臂 | 预注册臂 | 反馈 Agent 臂 |
|---|---|---|---|
| 总尝试（dev 候选） | 4 | 4 | 4 |
| 有效完成（成功） | 4 | 4 | 4 |
| dev 上未改善（错误建议/浪费，Δdev≤0） | 2 | 2 | 1 |
| 独立确认后有效改善 | 无（Δ=-0.0108） | 无（Δ=0.0000） | **有（Δ=+0.0216）** |
| 人工纠错时间 | 0（脚本化人工，非实时） | — | —（记录 LLM 调用轮数 4 作为替代） |
| 预算消耗 | 4/4 | 4/4 | 4/4 |

Agent 臂探索轨迹（`candidate_proposed` 事件）：A（验证主假设）→ **D**（median+LR 差于基线后转向非线性 RF）→ **F**（追当前最优）→ E。冻结 F，测试集 delta +0.0216（supports_threshold）。

## 关键发现（如实）

1. **反馈 Agent 在预算内找到了真实最佳候选 F(none+RF d6, dev=0.79)**，人工臂与预注册臂均未触达（顺序后段）。Agent 臂在 dev 上仅 1 次无效建议（浪费最少），且是唯一获得独立 test 有效改善（≥0.02）的臂。
2. **反馈 Agent 的价值依赖正确的选择器**：修复前 proposer 的 `dev_mse` 字段误名（实为 accuracy）与弱提示词导致 LLM 每轮返回 index=0（=队列），Agent 臂与预注册臂完全重合、无有效改善。修复（指标感知 + 明确要求利用观察分数差异化选候选）后，Agent 臂探索路径分化并命中最优。
3. **人工臂（脚本化）** 先试 RF 变体，冻结 dev 次优候选（E），test 上反而略负（-0.0108）。
4. **预注册臂** 按注册序先试完 3 个 LR，冻结 dev 最佳 LR(C=0.1)，test 上无改善。
5. **最终确认**：仅 Agent 臂 supports_threshold；人工/预注册 insufficient_evidence。结论与 M2 一致：**中位数插补不优于直接保留零值**（best 是不插补+RF）。本对照为描述性，未做显著性检验。

## 修复记录（阻断性问题）

`popper/proposer.py` 的 BYOK 选择器在本任务上「趋同于队列」（LLM 全部返回 index=0）的根因与修复：
- **字段误名**：`{"config": ..., "dev_mse": r["mean"]}` —— 实际指标是 accuracy（方向 max），命名 `dev_mse` 误导 LLM 语义。
- **缺少方向与引导**：prompt 未告诉模型指标方向、未要求「依据已测分数差异化选择最可能改进的候选」，LLM 保守选首项。
- 修复：改为 `dev_score` + 从 feedback 携带 metric/direction（lower/higher is better）+ system prompt 明确「不要默认选第一项，基于观察分数推理（如非线性模型可能超越线性基线）」。`index` 语义不变（0-based 索引到 remaining_candidates）。
- 修复后单测不变（`test_protocol.py` 22 passed），agent 臂端到端重跑验证：4 轮提案 index=0→2→3→2，探索路径分化并命中全局最优。

## 说明与边界

- **人工臂为脚本化人工排序**（非实时交互），「人工纠错时间」记 0，如实标注。
- 候选 G(none+SVC) 三臂均未在预算内触达，属覆盖限制。
- 反馈 Agent 仅能选已注册候选、只接收开发集摘要（BYOK 最小暴露），不注入新配置。
- 三臂 runner 均为可信本地模式（`--trusted-local`）；无容器隔离、无显著性检验。

## 产物
- 三臂项目：`examples/pima-comparison/{human,prereg,agent}/`（各自 .popper 事件链、report.md）
- Agent 臂提案事件：`examples/pima-comparison/agent/.popper/state.db`（candidate_proposed，source=byok）