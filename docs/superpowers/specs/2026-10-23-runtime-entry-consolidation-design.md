# 运行时入口收口设计

日期：2026-10-23  
状态：已口头批准（用户选方案 A）；待审阅本文件后实施  
仓库：`research-agent-v2` / 分支 `autopilot`

## 问题

产品同时暴露三条可跑路径：

1. 已登记研究：`ResearchController` + `ResearchRuntime`（默认科研产品路径）
2. `popper adaptive` / `AdaptiveResearchRuntime`（描述性基准）
3. `popper research campaign` / `Orchestrator`（编排图）

能力边界与 `unified-runtime-plan.md` 已写明执行权，但 CLI、工作台、API 载荷仍可能让新人把 Campaign / Adaptive 当成默认科研内核。本设计只做**入口收口**，不合并 Runtime 实现。

## 目标

- 默认科研产品路径在 help、工作台文案、status/API 字段上只指向 Controller + Runtime。
- Adaptive / Campaign 继续可用，但标注为基准 / 编排。
- 科学结果权威仍只在 ResearchStore；编排完成 ≠ 研究结论。

## 非目标

- 不合并 `AdaptiveResearchRuntime` / `Orchestrator` 代码
- 不改变判断三态、确认集一次消费、独立评估计分
- 不做审计级查新、不做论文生成、不接第三方技能库
- 不禁止 Adaptive / Campaign 运行

## 方案（已选 A）

### 1. CLI

- 顶层与 `research` help：保持「主路径 = 已登记研究」表述，统一措辞。
- `adaptive` 与 `campaign` 相关 help / description：固定可读前缀或等价短标签  
  - Adaptive：`[基准]` 或现有「描述性基准夹具（非默认科研产品路径…）」  
  - Campaign：`[编排]` 或现有「编排图…跳过≠完成」
- 不删除子命令。

### 2. 工作台

- Campaign 面板：保留并强化「编排进度 / 非主科研路径」眉题与说明（已有基础文案则对齐本 spec）。
- Adaptive / 跨领域基准相关 UI：短说明「描述性基准，非默认科研路径」。
- 不改布局结构，只改文案或轻量状态提示。

### 3. 轻量预检与载荷字段

- Campaign status / `build_campaign_payload`（及等价 HTTP 响应）增加稳定字段，例如：  
  - `orchestration_only: true`  
  - `scientific_authority: "research_store"`  
  字段名以现有 JSON 风格为准；语义固定。
- Adaptive 启动回执或 status：标明非默认产品路径，例如  
  - `fixture: true`  
  - `default_product_path: false`  
  （或与现有 adaptive 结果 JSON 合并的等价键）
- **不**拦截正常跑实验；**不**因缺字段拒绝旧客户端读进度（新字段为附加；旧客户端可忽略）。

### 4. 文档

- 更新 `docs/capability-boundary.md`、`docs/maintenance/unified-runtime-plan.md`：增加「入口收口已落地」短节，链到本文件。
- 本文件为权威设计说明。

### 5. 测试

- 断言 CLI help 或 parser description 含基准/编排/非默认路径语义。
- 断言 campaign payload 含 `orchestration_only` 与 `scientific_authority`。
- 断言 adaptive 相关回执含 fixture / 非默认路径语义。
- 现有 campaign / adaptive / research 主路径回归不得误红。

## 成功标准

1. 只看 help 与工作台，不会把 Campaign/Adaptive 当成默认科研内核。
2. 科学权威字段指向 ResearchStore；编排完成不能被读成「研究结论已出具」。
3. 演示冻结路径相关测试仍绿；不引入新的第二套计分权威。

## 实施顺序（批准本文件后）

1. 载荷字段 + 单测  
2. CLI help 对齐 + 单测  
3. 工作台文案  
4. 能力边界 / unified-runtime-plan 短节  
5. 相关 pytest（及必要的前端 vitest）  
6. 单独 commit（不做大合并、不 push 除非用户要求）

## 风险与回退

- 风险：旧脚本若对 JSON schema 做严格枚举可能报未知键 → 文档说明为附加字段；若出现真实破坏再改为嵌套 `meta` 对象。  
- 回退：删除新字段与文案即可，无数据迁移。
