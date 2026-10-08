# 用量与失败恢复的边界

参考 OpenCode 的用量/重试分离，以及 Z.ai ZCode 的逐尝试记录和流式重试边界。
参考源码：
- https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.ts
- https://github.com/zai-org/ZCode/blob/29628c9acdb81b703bbd4080c207a0e7ce5e276e/apps/zcode-cli/packages/adapters/src/storage/session-store/repositories/usage.ts
- https://github.com/zai-org/ZCode/blob/29628c9acdb81b703bbd4080c207a0e7ce5e276e/apps/zcode-cli/packages/adapters/src/model/runner-stream.ts

## 发送与统计

请求生命周期记录 not_started、connecting、connected、sending、response_headers、
streaming、generating、completed。应用请求开始写入前才标记 sent；代理 CONNECT
不视为模型请求。取消/时限与发送门禁共用锁，超时返回后禁止迟到连接继续发送。
无法跟踪的注入传输保守标记为可能已发送。

账本分别保存 logical_request_id、attempt_index、transport_phase、output_started、
stream_completed、usage_source、reported_tokens 与 raw_usage_json（只包含合法 token
计数）。服务端完整报告、完整分项推导、部分报告、缺失、未发送分别记录。
不得从回复长度或耗时推断实际 token。预算预留是上界，不是实际用量估算或账单。

known_tokens 是可核实的完整计数；unresolved_reserved_tokens 是未知请求的预算上界；
accounted_tokens 用于预算控制。能证明未发送时，两种预算账本都释放预留；
没有发送阶段证据的历史失败保持原有未知状态，不能追溯改成零。
部分报告保存在独立诊断字段，不放进完整 provider 结算字段。

## 策略

ModelRequestBudget 默认 unknown_usage_policy="stop"，用于科研验收。
交互式调用可显式选择 "reserve"：保留未知请求的完整预算上界，同时允许
在总调用/token 上限内继续其他请求。已登记运行不能静默改变策略。

同一逻辑请求的同一次尝试不能重发；仅在宽松策略下、有记录证明没有开始输出时，
可以在新的尝试编号中重试。正文或非空推理开始后的流中断不自动重放。
完整回答的格式修正属于明确的后续请求，最多一次，单独计费记账。
JSON 客户端每个逻辑调用仍最多两次尝试，科研全局 30 次上限保持不变。

Windows 网络仅读取显式 HTTP_PROXY/HTTPS_PROXY/NO_PROXY 环境变量，
避免无意继承浏览器注册表中的本地代理；不记录代理凭据或 API key。
