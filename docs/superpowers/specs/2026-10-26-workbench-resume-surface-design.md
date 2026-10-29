# 工作台露出同 run_dir resume（切片 E）

日期：2026-10-26  
状态：已实现  
前置：切片 D（`9141e92`，单槽 `resume_checkpoint` + `project_resume_context` + CLI `--brief`）  
范围：工作台 `/api/research` 投影与 LoopView 与 CLI brief **同源**露出续作指纹；**不**升格 MEM-* / checkpoint 为 evidence

## 背景

切片 D 已让 CLI `research status --brief` 带上 `resume` 短字段，但 `read_research_state` → `status_brief` 未注入 `resume`，工作台 LoopView 看不到 checkpoint。隔几天打开 UI 时仍缺「停在哪 / 下一步意图」。

capability-boundary 仍将跨周长程记忆列为非产品；本切片只把 **同 run_dir** 的有界 resume 接到投影与 UI。

## 目标

1. `read_research_state(run_dir)` 在调用 `summarize_research_status` **之前**注入  
   `resume = project_resume_context(run_dir, phase=…, integrity_ok=…)`  
   （与 CLI brief 同一 helper；无 runtime session 时 `last_runtime_step` / `last_action_kind` 可为 null，仍可有 checkpoint 文本）
2. 沿用现有 `summarize_research_status` 输出：  
   `status_brief.resume = { last_runtime_step, last_action_kind, checkpoint }`
3. LoopView：有 `status_brief.resume.checkpoint` 时，在 `read_as` 下显示一行 muted「续作意图（未验证笔记）：…」；有 `last_action_kind` / step 则附带，无则省略
4. 无 checkpoint 且无 last_*：**不渲染**该行（空 study 不刷噪声）
5. `api.ts` 为 `status_brief` 增加可选 `resume`；前端单测覆盖有/无 checkpoint；既有 inconclusive 行为不变

## 信任与门禁（不变）

- checkpoint / resume 仍是 `unverified_session_notes`，不能当 observation / evidence
- 文案不得暗示「已恢复科学权威」或「记忆=证据」
- 不新增写 checkpoint 的 UI；写入仍走 CLI `research memory note`

## 非目标

- 跨 study 索引、周总结、task_memory 合并
- 工作台内编辑 / 覆盖 checkpoint 的表单
- 新 HTTP API 或前端单独拉 memory
- 审计级 scoop、开放写论文、跨周长程记忆产品化

## 实现要点

| 位置 | 改动 |
|------|------|
| `popper/research/readout.py` | `summarize` 前注入 `project_resume_context` |
| `popper/demo.py` | 已有 brief `resume` 字段；确认 readout 路径走通即可 |
| `frontend/src/api.ts` | `status_brief.resume?` 类型 |
| `frontend/src/components/LoopView.tsx` | 条件渲染续作意图一行 |
| 测试 | LoopView vitest；可选 readout/demo 侧：有 checkpoint → `status_brief.resume.checkpoint` 非空 |
| 文档 | 能力边界可补一句「工作台 brief 同源露出」；本 spec 状态改为已实现 |

## 验收

- CLI `--brief` 与工作台 `status_brief.resume` 字段形状一致
- 写入 checkpoint 后刷新工作台可见；无 checkpoint 时 UI 无该行
- 能力边界仍写「同 run_dir 有界；非跨周产品」
- 相关 Python 测 + `frontend` vitest 通过

## 后续（本切片外）

跨 study 索引；UI 写 checkpoint（若需要再单独设计）。
