# 工作台写入同 run_dir resume_checkpoint（切片 F）

日期：2026-10-29  
状态：**已实现**  
前置：切片 D（单槽 checkpoint + CLI note）+ 切片 E（工作台 `status_brief` / LoopView 只读露出）  
范围：工作台可**覆盖写入**同 run_dir 的有界续作意图；仍是未验证会话笔记，**不**升格为 evidence

## 背景

D/E 已打通「CLI 写入 → 投影/brief → 工作台只读露出」。隔几天打开 UI 时能看见 checkpoint，但改意图仍必须切回 `popper research memory note`，演示与日常续作路径断裂。

capability-boundary 仍将跨周长程记忆列为非产品；本切片只补 **同 run_dir** 写入口，与 CLI 共用 `record_resume_checkpoint`。

## 目标

1. 新增最小写 API（建议 `POST /api/research/resume` 或挂在既有 research 命令通道）：  
   body `{ run_dir | study_id, text }`；服务端调用既有 `record_resume_checkpoint`（≤240 字、单槽覆盖）
2. LoopView：在现有 resume 只读行旁增加「记下续作意图」短表单（单行/短文本 + 保存）；成功后刷新 `/api/research`，仍走同源 `status_brief.resume`
3. 信任文案：保存按钮旁固定「未验证笔记，不能当证据」；失败时明确错误，不静默当成功
4. 前端 + HTTP/服务层单测：有文本 → checkpoint 覆盖；空/超长 → 拒绝；投影字段形状与 CLI brief 一致

## 数据流

```
LoopView 表单
  → POST resume write（trusted-local / 既有 serve 闸）
  → record_resume_checkpoint(run_dir, text)
  → run_dir/memory 单槽落盘
  → GET /api/research → read_research_state → project_resume_context
  → status_brief.resume.checkpoint → LoopView 只读行更新
```

无新记忆模型、无跨 study 索引、不进 `evidence_refs` / 稿件 known_ids / 数字门禁。

## 非目标

- 跨 study / 跨周索引、周总结、task_memory 合并
- 自动根据聊天或 runtime 步进灌 checkpoint（本切片仅人工显式写入）
- 编辑任意 MEM-* FIFO；多槽历史；向量检索
- 审计级 scoop、开放写论文、三 runtime 物理并类
- 把 UI 写成「已恢复科学权威」

## 验收

- 工作台写入后刷新可见；再写覆盖旧文；CLI `research status --brief` 与 `status_brief.resume` 同源
- 空文本 / 超长拒绝；无 run_dir 不创建噪声目录（沿用 D 的 memory 语义）
- 能力边界仍写「同 run_dir 有界；非跨周产品」；可补一句「工作台可覆盖写入 checkpoint」
- 相关 Python + frontend vitest 通过（不做全量 pytest 作为本切片门槛）

## 过度宣称红线

UI 能写 checkpoint ≠ 长程科研记忆产品；checkpoint ≠ observation / judgment。
