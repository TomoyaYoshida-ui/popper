---
name: popper-execute
description: 实验闭环（HTR）。把研究设计落成真实实验：假设树持久化、executor 隔离执行、held-out merge gate 准入、results.json 权威通道，负结果驱动 pivot。当用户要"把这个 idea 跑出真改进或证伪"时使用——本套 skills 里唯一能产出机械事实的一环。
license: MIT（整合自 scientific-agent-skills arbor）+ 自研 results.json/REE 隔离
metadata:
  version: "1.0"
  skill-author: Popper
  sources: scientific-agent-skills (arbor), 自研 (results.json 权威通道, REE 容器隔离)
---

# popper-execute — 实验闭环（HTR）

## 什么时候用

- 预注册表冻结后，把方法/发现型假设跑成"真跑赢"或"证伪"的机械事实；
- 反复优化一个可评分 artifact（模型/方法/配置）的场景。

## 铁律

1. **executor 不可改假设**：executor 可修自己的代码、可重跑，但 `h_n` 固定——否则返回的分数不再是该节点的证据；
2. **results.json 是数字唯一权威通道**：指标名必须 ∈ metrics.json 注册表（未知 = L0 失败）；stdout 解析不算数；
3. **held-out 不可触碰**：`E_test` 只经受控 evaluator 读取，任何代码触碰 held-out 路径 → `heldout_read` 审计事件 + 拒绝；
4. **隔离**：executor 在独立容器（REE）跑，与主流程不共享状态。

## 工作流（HTR 六步，源自 arbor）

1. **Observe**：从假设树重读状态（不靠压缩后的对话记忆）；
2. **Ideate**：条件化于树的证据，提出可证伪子假设（深度 1=方向，深度 2=具体干预）；
3. **Select**：按信息量选（不是纯分数最大化）；
4. **Dispatch**：executor 在隔离容器并行跑一个假设，返回 `{dev_score, result, insight, branch_ref}`；
5. **Backpropagate**：把叶子观察抽象为方向级/全局约束，向上传播（**HTR 增益主要来自这一步**）；
6. **Decide**：剪枝（记录理由 = 负约束）/ 扩展 / **merge gate**——只有 `E_test` 上改进才准入新 best。

预算：K=5 轮；超轮次强制收敛或降级为方向报告。

**results.json 格式**（与 metrics.json 注册表对齐）：

```json
{"entries": [{"name": "ours_f1", "value": 0.832, "mean": 0.831, "std": 0.004,
              "n_seeds": 5, "per_seed": [0.828, 0.830, 0.833, 0.834, 0.830]}]}
```

**负结果 pivot**：负结果（含 merge gate 拒绝）喂回 `popper-ideate` 的 `negative-result-pivot` 算子，驱动假设修正。

## 确定性脚本

- 假设树状态管理：`tree.py`（vendor arbor，MIT）；
- results.json 校验：`validate_results.py`（已实现）；
- 密封守门：`heldout_guard.py`；
- 统计判定：`stats_check.py`（bootstrap/CV + BH-FDR + paired test，按预注册表口径）。

## 边界

- 本 skill 只保证"数字真实 + δ 机械可判"，不保证"δ 一定显著"——负结果是合法输出；
- 需要可计算的目标；无计算目标的纯理论研究跳过本 skill。
