# 请求可靠性修复

聊天及结构化 JSON 调用共用 stdlib 请求生命周期，默认连接/无数据/总时限
分别为 15/60/180 秒。可通过 `POPPER_REQUEST_CONNECT_SECONDS`、
`POPPER_REQUEST_IDLE_SECONDS`、`POPPER_REQUEST_TOTAL_SECONDS` 配置。
总时限不会被 SSE 心跳重置；取消也覆盖连接和半行读取阶段。

JSON 客户端仅请求 SSE，完整组装后解析 JSON，不回退到非流式。
`finish_reason=length` 的聊天结果一律拒绝执行工具，仍保留终态用量。
中断流中收到的 usage 保留作诊断，不能自动当作完整结算依据。
未知用量保留预算预留并停止该作用域；账本新增状态/原因列，不回写历史记录。

相关原项目验证：92 passed，18 subtests passed（2026-10-08）。
覆盖连接/半行读取取消、心跳总时限、截断工具、流中断、JSON 修正及未知用量。
本地原始输出：`.acceptance/request-tests.log`。
