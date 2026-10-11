# Pydantic 规划器开发烟测（2026-10-09）

本记录只覆盖一次工程门禁：真实端点上的有界开发运行能够留下完整回执。它不进入历史 27 单元分母，也不构成策略比较或迁移证明。旧验收文档与旧账本未改写。

工作树基线提交为 `72db7e4`。烟测运行的是尚未提交的工作树，涉及 `popper/cli.py`、`popper/research/runtime.py`、`popper/research/store.py`、`pyproject.toml`，以及新增的 `popper/transport/pydantic_http.py`、`popper/transport/pydantic_planner.py`、`tests/test_pydantic_planner.py`。本记录不提交这些改动。

## 第一次请求

目录 `evaluation/runs/pydantic-smoke-20261009/`。T01、`evidence_driven`，确认集保持关闭。离线预检完成 1 次开发执行后，第一次真实请求在接收阶段失败。

- 状态：`failed`，分类 `incompatible_request`，耗时 21.125 秒，开发执行 0。
- 回执：`request_started` 1，`model_response_received` 0，`model_output_accepted` 0，`action_completed` 0。完整性校验通过，规划状态为失败。
- 账本 `model-calls.sqlite`：1 次尝试，已知用量 0，未结算预留 22,409 token，用量状态 `unknown`，原因 `incompatible_request`。
- 结果文件 SHA-256：`cec88f8a67cebdea3c0fd106c7273abb82dc4cd15c0236ef3f8f9e1dba7d77ae`。

该研究会话已经写下 `request_started`，且没有持久的 `response_received`。再次进入同一研究会话会进入恢复阻断，不能续跑，也不能补写这次失败。

随后的单独探针 `probe/` 留下了服务端正文。HTTP 状态 400，请求键为 `messages`、`model`、`stream`、`stream_options`、`tool_choice`、`tools`。服务端消息为：思考模式下 `tool_choice` 不支持 `required` 或 object。该探针回执的失败分类写成了 `stream_interrupted`，未结算预留 12,514 token；这是该次诊断回执的记录状态。探针账本与烟测账本分开。

## 端点适配与成功烟测

对该端点，规划器在存在 `thinking_budget` 时不再强制工具选择，并沿用 `request_output_limits` 的 `enable_thinking`、`thinking_budget` 与 `max_completion_tokens`。动作字段仍由研究协议 schema 校验，逻辑请求仍最多两次尝试。

字段对齐之后的探针 `probe3/` 一次调用即被接受，结算 2,505 token。更早的 `probe2/` 有两次已结算调用，共 2,753 token，动作因缺少 `kind` 被契约拒绝；这两次只用于确认字段，不计入下面的烟测分母。

成功烟测写在 `evaluation/runs/pydantic-smoke-20261009/live/`，使用新的研究会话和新账本。

- 状态：`development_completed`。规划在实验上限停止，原因 `experiment_limit`。耗时 110.547 秒。
- 确认集保持关闭。完整性校验通过。冻结选择留下最终候选。
- 开发执行 4 条，假设标识互不相同。研究存储中的 run 为 5。开发范围判断 8 条：反驳 2、支持 2、证据不足 4。模型解读被拒绝 1 条，已接受的模型解读为 0。
- 回执：`request_started` 4，`model_response_received` 4，`model_response_rejected` 0，`model_output_accepted` 4，`action_completed` 4。
- 账本：4 次调用均一次成功并结算，已知用量 28,105 token，未结算预留 0。调用上限 9，token 上限 250,000，未知用量策略为停止。
- 成功请求键包含 `enable_thinking`、`thinking_budget`、`max_completion_tokens`、`tool_choice` 与 `tools`，HTTP 状态 200。
- 结果文件 SHA-256：`9a98ee7ad316aa93de9e4859fc12f6a991ecb88b5f9943458ea13ce419240717`。

离线测试 `tests/test_pydantic_planner.py` 与 `tests/test_research_runtime.py` 在同一工作树上为 28 项通过。

## 尚未执行

新的研究问题尚未指定。在指定并冻结独立数据、预注册竞争假设、独立分析单位和完整确认名单之前，不发送新的付费研究请求，也不打开确认集。

历史 27 单元、T03 失败记录，以及已结算的 51,932 token 账本均未重跑、未改写。`evaluation/run_transfer_pilot.py` 仍使用原来的 JSON 客户端。
