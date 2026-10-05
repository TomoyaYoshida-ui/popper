# RFC 0001：核心实体身份与 API v1

- 状态：Draft
- 目标版本：Phase 0 / Popper 0.1
- 范围：身份、关联、API 写入语义与兼容迁移

## 背景

Popper 当前已经包含实验状态机、Research Store、Workspace/Quest、Campaign、文献、证据、稿件和 Worker，但这些子系统在不同阶段独立生长，形成了多套身份与关联方式：

- Workspace 使用 `wid/fid/quid`；
- 核心实验使用无前缀 UUID 作为 run ID；
- Research Controller 使用 `S-`、`H-`、`D-`、`RUN-` 等摘要 ID；
- Campaign 和 vendor 集成大量使用运行目录与文件路径关联；
- Artifact 有时是逻辑 ID，有时直接使用绝对路径；
- Workbench 通过显式目录列表投影 Research 状态。

这些格式在各自模块中有效，但缺少平台级实体关系，阻碍了稳定 API、跨工作区查询、团队部署、权限检查和全链路 trace。

## 目标

1. 为平台核心对象定义稳定种类、主键和关联规则。
2. 明确路径、标题和 UI 索引都不是实体身份。
3. 保持现有研究目录和示例可读，不原地重写历史 ID。
4. 为 `/v1` API 定义统一命令、查询、错误与幂等语义。
5. 为后续 SQLite/PostgreSQL 双适配提供不依赖物理存储的契约。

## 非目标

- 本 RFC 不合并 `state.db`、`research.sqlite` 和 `holdout.sqlite`；它们当前的权限分离是有意设计。
- 本 RFC 不改变科研状态机和预算语义。
- 本 RFC 不引入多租户实现，只预留 `organization_id` 和 actor 语义。
- 本 RFC 不批量改写现有 JSON、SQLite 或工件路径。

## 核心实体

### 产品与身份

| Kind | 新 ID 前缀 | 说明 |
| --- | --- | --- |
| organization | `org_` | 团队版组织；本地版使用固定 local organization |
| project | `prj_` | 一个受管理的代码/研究项目 |
| workspace | `wsp_` | 项目的可操作工作区或 checkout |
| quest | `qst_` | 用户可见的长期研究任务 |
| session | `ses_` | Agent 会话 |
| turn | `trn_` | 会话中的一次可恢复模型执行 |
| tool_call | `tcl_` | Agent 提议的结构化工具调用 |

### 科研对象

| Kind | 新 ID 前缀 | 当前兼容格式 |
| --- | --- | --- |
| study | `std_` | `S-*` |
| hypothesis | `hyp_` | `H-*` |
| design | `dsg_` | `D-*` |
| observation | `obs_` | `OBS-*` |
| decision | `dec_` | `DEC-*` |
| claim | `clm_` | `C001` 或用户提供 ID |

### 实验、执行与证据

| Kind | 新 ID 前缀 | 当前兼容格式 |
| --- | --- | --- |
| experiment_node | `exp_` | 当前无统一对象 |
| revision | `rev_` | `REV-*` |
| run | `run_` | UUID、`RUN-*` |
| job | `job_` | `JOB-*` |
| artifact | `art_` | 路径或内容摘要 |
| evaluation | `evl_` | request ID / response path |
| audit_event | `evt_` | SQLite 自增 seq + hash |

新 ID 统一采用小写前缀加 26 字符随机或内容派生主体。既有 ID 永久合法；读取层必须识别，写入层从迁移完成的模块开始生成新格式。

## ID 与引用规则

1. ID 创建后不可修改、不可复用。
2. 用户可修改的标题、路径和名称不能作为外键。
3. 内容寻址对象使用独立的 `sha256` 字段，不能把业务 ID 等同内容摘要。
4. API 中实体引用统一表示为 `{ "kind": "run", "id": "run_..." }`；内部存储可保留直接 ID 列。
5. 本地路径只能出现在 Workspace/Artifact 的 location 字段，外部 API 默认返回逻辑 ID 和相对定位符。
6. 每个写入实体记录 `schema_version`、`created_at`、`created_by`、`trace_id`。
7. 事件使用存储局部的序号排序；事件 ID 用于跨存储引用，二者不能混用。

## 平台关系

```text
Organization
  └─ Project
      ├─ Workspace
      │   ├─ Quest
      │   └─ AgentSession
      └─ Study
          ├─ Hypothesis
          │   └─ ExperimentDesign
          │       └─ ExperimentNode
          │           ├─ Revision
          │           └─ Run
          │               ├─ Job
          │               ├─ Artifact
          │               └─ EvaluationReceipt
          │                   └─ Observation
          ├─ Decision
          └─ Claim
              └─ EvidenceBinding → Observation / LiteratureEvidence / Artifact
```

`Quest` 是产品任务容器，不是科学实体。一个 Quest 可以关联多个 Study；Study 必须属于唯一 Project，但可以跨多个 Workspace/Revision 执行。

## API v1

### 命令与查询分离

- 查询：`GET /api/v1/{kind}/{id}` 或集合查询；无副作用。
- 命令：`POST /api/v1/commands/{action}`；所有科研写入从命令进入。
- 流：`GET /api/v1/events`；基于 cursor 恢复，不把连接本身当作状态。
- 工件：元数据与二进制传输分离；大文件不进入 JSON 响应。

### 命令信封

```json
{
  "schema_version": "1.0",
  "command_id": "cmd_...",
  "idempotency_key": "client-stable-key",
  "actor": {"type": "user|agent|service", "id": "..."},
  "project_id": "prj_...",
  "trace_id": "trc_...",
  "expected_version": 7,
  "action": "freeze_design",
  "payload": {}
}
```

- `expected_version` 用于防止 Agent 根据过期状态写入。
- 相同 `idempotency_key` 和相同 payload 返回原结果。
- 相同 key 不同 payload 返回 `idempotency_conflict`。
- 命令成功必须返回产生或更新的实体引用与事件 cursor。

第一阶段已经落地两个精简 profile：

- `POST /api/v1/commands/workspace` 接收 `schema_version`、`idempotency_key`、
  `expected_version`、`action`、`payload`；
- `POST /api/v1/commands/research` 额外接收 `study_id`，只允许 Research Kernel
  已公开的治理动作，不能创建 `Run` 或 `Observation`。

两个端点都由服务端生成 `trace_id`，actor/project 从已认证会话与当前工作区推导。
Research 命令的状态事件、study 版本、命令收据和审计事件在同一 SQLite 事务提交；
收据记录首次执行的 actor/trace，并由 append-only 哈希事件链保护。尚未落地的通用
字段不得由客户端伪造，待统一身份与命令网关实现后再提升到完整信封。

Agent Runtime 的第一阶段接口为 `GET/POST /api/v1/sessions` 与
`POST /api/v1/sessions/{session_id}/turns`。Turn 在模型调用前持久化；完成或失败后
再提交终态。进程重启时遗留的 `running` turn 转为 `interrupted`，不会静默重跑。
客户端提供幂等键，相同请求返回原 turn，不会再次调用模型；actor 与首次 trace
随 turn 持久化。

结构化工具调用使用 `POST /api/v1/sessions/{session_id}/tool-calls` 提议，并通过
`/api/v1/tool-calls/{call_id}/decision|execute|cancel` 推进生命周期。风险等级由
服务端工具注册表决定，调用方不能降级；`write/execute` 必须先进入
`pending_approval`，只有明确批准后才能 claim 为 `running`。重启时未完成执行转为
`interrupted`，不自动重放。首批工具仅包含版本化 Workspace Quest 写入，不开放
任意 shell，也不开放 Run/Observation 科研事实写入。

### 成功信封

```json
{
  "schema_version": "1.0",
  "ok": true,
  "data": {},
  "meta": {"trace_id": "trc_...", "event_cursor": 42}
}
```

### 错误信封

```json
{
  "schema_version": "1.0",
  "ok": false,
  "error": {
    "code": "invalid_transition",
    "message": "design 已冻结",
    "details": {},
    "retryable": false
  },
  "meta": {"trace_id": "trc_..."}
}
```

首批稳定错误码：

- `invalid_request`
- `not_found`
- `permission_denied`
- `version_conflict`
- `idempotency_conflict`
- `invalid_transition`
- `budget_exhausted`
- `integrity_failed`
- `dependency_unavailable`
- `execution_failed`

内部异常类和中文错误文本不能成为客户端控制流接口。

## 数据所有权与写入路由

| Action | 权威写入方 |
| --- | --- |
| 创建/修订假设 | Research Kernel / Hypothesis writer |
| 冻结设计 | Research Kernel / Design writer |
| 创建运行意图 | Research Kernel + Budget transaction |
| 提交/取消 Job | Execution service |
| 登记原始工件 | Artifact service |
| 写入评分回执 | Evaluator service |
| 创建 Observation | Research Kernel / Observation writer |
| 创建 Decision/Claim | Research Kernel |
| 会话消息和 turn | Agent Runtime |
| Quest 标题和 UI 布局 | Workspace service |

API handler、React UI、Harness 和 Worker 都不能绕过对应写入方直接修改底层文件或表。

## 兼容迁移

### Stage A：只读规范化

- 增加 `EntityRef`/ID 解析器，识别新旧格式；
- API 投影同时返回 canonical kind 和 legacy ID；
- 为路径型 Artifact 计算逻辑 ID，但不移动文件；
- 建立关系索引，不改写权威源。

### Stage B：新写入采用新 ID

- Workspace/Quest/Session 先迁移；
- 新建 Research 对象使用新 ID；
- Controller 保留 legacy alias，保证旧确认包与 receipt 可验证；
- 所有跨模块链接只写 canonical ID。

### Stage C：存储适配

- 本地 SQLite adapter；
- PostgreSQL adapter；
- 文件系统与 S3 ArtifactStore；
- 兼容测试对同一 Action 序列验证等价结果。

历史数据不做破坏性批量转换。需要导出时生成新的开放 Research Package，并在 manifest 中保留 legacy alias。

## 安全要求

- `actor` 由认证层注入，不能信任请求 payload 自报；
- `project_id` 在 handler 和 service 两层校验；
- Artifact locator 经过项目/存储作用域解析，拒绝绝对路径穿越；
- Worker token 只能写入指定 Project/Run；
- holdout 服务使用独立凭据域，API 不暴露 labels location；
- 错误响应和事件不得包含 API key、authorization header 或完整敏感环境变量。

## 验收测试

1. 所有新 ID 能被解析为唯一 kind；旧 ID 在明确上下文中仍可读取。
2. 标题和路径变化不破坏实体引用。
3. 相同幂等键重复命令只产生一个事件和实体。
4. 相同幂等键不同 payload 被拒绝。
5. 过期 `expected_version` 被拒绝。
6. UI/Harness 无法直接创建 Observation 或 Claim。
7. SQLite 与未来 PostgreSQL adapter 对相同命令序列产生等价快照。
8. API 错误码稳定，客户端不依赖中文 message。
9. 旧研究目录的 verify/replay 结果在迁移前后完全一致。

## 开放问题

- 内容派生 ID 是否允许跨 Project 去重，还是始终带项目作用域；
- LiteratureEvidence 是独立实体还是 Artifact 的带 selector 视图；
- ExperimentNode 与 ExperimentDesign 是否允许多对多；
- 本地模式是否物化固定 `org_local/prj_local`，还是仅在 API 投影生成；
- 事件全局 cursor 在团队版采用数据库序列还是可排序分布式 ID。

这些问题在进入 Stage B 前必须形成后续 ADR；不阻塞 Stage A 的只读规范化。
