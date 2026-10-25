# 同 run_dir 可恢复检查点（切片 D）

日期：2026-10-25  
状态：已实现  
范围：隔数天回到**同一 study / run_dir** 时，有有界「停在哪 / 下一步意图」；**不**做跨 study 长程叙事，不把 MEM-* 升格为 observation

## 背景

今日已有：

- Store 权威（observation / judgment / 分数）
- `run_dir/memory` 有界 MEM-* 会话笔记（≤16 条，不进 evidence）
- Runtime 步进可续跑

缺口：人隔几天打开时，没有单槽「我打算干什么」检查点；`status --brief` 不含「上次规划停在哪一步」。

capability-boundary 仍将「跨周长程科研记忆」列为非产品；本切片只补 **同 run_dir** 的有界 resume。

## 目标

1. **单槽** `resume_checkpoint`：人工写入意图文本（≤240 字）；新写覆盖旧；不占 16 条 FIFO（或独立字段 `checkpoint`）
2. **只读** `resume` 投影进规划上下文：`phase` / `integrity_ok` / `last_runtime_step` / `last_action_kind` / `checkpoint` / 最近 MEM kinds（无 metric 数字）
3. CLI：`popper research memory note --kind resume_checkpoint --text "..."`（默认 `memory_dir=<run_dir>/memory`，或接受 run_dir）
4. 可选：`research status --brief` 增加 `resume` 摘要字段（不改 `read_as` 科学结论语义）

## 信任与门禁（不变）

- `trust: unverified_session_notes`（checkpoint）+ store 派生字段分开标注
- MEM-* / checkpoint **不得**进入 `evidence_refs`、稿件 known_ids、聊天数字门禁
- 禁止 evaluation 字段（与现有 `_FORBIDDEN_ENTRY_KEYS` 一致）
- `refs` 仅允许 `OBS-` / `LIT-` / `SCOOP-` / `FT-*`（拒绝 `MEM-`）

## 非目标

- 跨 run_dir / 跨 workspace 项目叙事或文献续读 agent
- 用 resume/MEM 覆盖 judgment 或 evaluator 分数
- 自动灌聊天 turn；无上限 JSONL / 向量库
- 在边界文档宣称「长程记忆已产品化」

## 实现要点

| 位置 | 改动 |
|------|------|
| `popper/research/memory_context.py` | kind `resume_checkpoint`；单槽读写；ref 校验 |
| `popper/research/resume_context.py`（新） | `project_resume_context(run_dir, …)` |
| `popper/research/runtime.py` | `context()` 增加 `resume` |
| `popper/cli.py` | `research memory note` |
| `popper/demo.py` `summarize_research_status`（可选） | brief 附带 `resume` 短字段 |
| 测试 | `test_memory_context` / `test_resume_context` / runtime 拒 MEM evidence |

## 验收

- 写入 checkpoint → 再投影只有一条；覆盖后旧文消失
- `context()["resume"].last_runtime_step` 与 store 一致
- MEM-* 作 `evidence_refs` 仍拒绝
- 文档：同 run_dir 有界 checkpoint/resume；非跨周产品

## 后续（本切片外）

跨 study 索引、周总结、task_memory 合并。
