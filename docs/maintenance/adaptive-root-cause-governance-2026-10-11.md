# Adaptive 根因治理（2026-10-11）

承接 formal 72 单元基线 [`adaptive-formal-baseline-2026-10-11.md`](adaptive-formal-baseline-2026-10-11.md)。
不再用软跳过堆叠或抬高 `scope_calls` 治标。

## 结构修复

1. **契约拒绝可审计**  
   每次物理尝试的 `model_response_rejected` 回执带 `attempt_index`、`consumed_send`、
   `correction_attempt`、校验 `details`；运行时 `response`/`decision_completed` 拒绝记录带 `audit`。

2. **预算语义**  
   单元 scope 的发送上限只计逻辑请求的首次发送（`attempt_index == 1`）；
   契约纠错的第二次发送仍计入全局与 token，不再吞噬单元科学步配额。
   **不**提高 `scope_calls=24`。

3. **确认稳健性**  
   `freeze_selection` 对 finalist 做 confirmation 划分预检（独立 `PREFLIGHT-*` job，不占用确认 RUN）。
   `confirm()` 失败写入 `category` / `message` / `details`，不再只留空 `ProtocolError`。
   执行失败时 `ProtocolError.category` 取 worker 状态（如 `implementation_failed`）。

4. **选择空 finalist**  
   `freeze_selection` 在无 finalist 时写入 `empty_finalist_reason`
  （`no_successful_attempts` 或 `successful_attempts_superseded_by_revision`），便于与决策上限区分。

## 门禁结果（2026-10-11）

专项回归：Windows 76 passed；Linux 76 passed（3 skipped）。  
付费复验 `pilot-reject-audit` 退出码 0：

| 策略 | 确认完成 | 支持 |
|---|---|---|
| fixed_queue | 2/2 | 0/2 |
| generic_iteration | 2/2 | 0/2 |
| evidence_driven | 2/2 | 0/2 |

- 可审计拒绝回执 34 条，覆盖 4 个模型单元；契约拒绝 5–8 次/格仍全部确认完成（纠错发送不再吃光单元科学步配额）。
- 6/6 单元写入 confirmation 预检且全部 `ok`；无确认失败需分类。
- 不要求 supported 上升；本轮不宣称策略优势。

共享账本在门禁后：约 1135 / 1600 次发送，已知约 1079 万 / 1800 万 token。

## 下一对照准备

- 锁定的 `formal`（公开+生成 72 单元）保持不可变；公开任务样本身份已占用，不能再登记同一 PUBLIC 行。
- 已绑定同指纹的生成后续：`formal-generated-root`（G01–G06，36 单元，未启动 `--live`）。
- 已绑定同指纹的独立预注册 72 单元：`formal-independent-72`（N01–N12 全新生成任务，12×3×2，未启动 `--live`）。
- 2026-10-11：共享账本全局上限显式提高到 3200 次 / 3600 万 token
  （`ledger-amendment-independent-72.json`）；`formal-independent-72` 已重绑指纹并启动 live。
- `formal-generated-root`（36 单元）仍为未启动的备选缩小队列。
- 若要做含独立公开数据的 12 任务 twin，经 `formal-root-independent-public.json` 再跑 `prepare_formal_root.py`。
