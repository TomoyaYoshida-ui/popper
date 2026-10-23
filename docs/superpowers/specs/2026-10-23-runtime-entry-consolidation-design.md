# 科学执行权硬收敛（确认路径）

日期：2026-10-23  
状态：用户否决表面入口收口（方案 A）；批准并执行方案 B  
仓库：`research-agent-v2` / 分支 `autopilot`

## 问题

Adaptive 与旧 Campaign/Experiment 路径可以自行消费确认集、写 judgment/claim，形成第二套科学执行权威。只改 CLI/UI 文案不能解决问题。

## 目标（本轮）

1. `ResearchStore.claims.confirm` / `confirm_signed` **只能**在 `scientific_authority.authorize_confirmation` 上下文中调用。
2. `ResearchController` 与 `AdaptiveResearchRuntime.confirm` 均经该授权写入；Adaptive 不得保留未授权直连。
3. Campaign 的 `confirm_node` **禁止**回退到 `Experiment.confirm`；无 `planning_protocol=research-runtime/v1` 时明确失败。
4. 测试锁死：未授权直连 confirm 失败；Adaptive/Controller 授权路径仍可用；Campaign 旧确认旁路失败。

## 非目标（本轮）

- 不把 Adaptive 的 `_run`/`_evaluate` 整段迁进 Controller（开发执行收敛可后续）
- 不物理合并 Runtime 类层次
- 不做审计级查新 / 论文生成

## 成功标准

仓库中不存在可执行的「未授权 `claims.confirm`」路径；Campaign 不能再靠 Experiment 静默完成确认。
