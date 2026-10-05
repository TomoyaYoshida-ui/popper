# Popper Roadmap

路线图以可验证交付为准，不以功能数量或版本日期代替完成定义。每个阶段只有在退出标准满足后才进入下一阶段。

## 当前原则

- 优先收敛已有能力，不继续堆叠孤立 CLI 和页面；
- 保持本地优先，同时为团队部署保留明确适配层；
- 每个功能先定义数据所有者、失败语义、幂等键和验收 Gate；
- 至少使用一个非 ML 案例和一个数据驱动案例验证通用性；
- 公开报告失败和限制，不用演示成功替代系统评测。

## Phase 0：架构收口与可信基线

目标周期：1–2 周。

### 交付内容

- 修复当前测试失败，恢复主分支全绿；
- 冻结 `Project / Quest / Study / Session / ExperimentNode / Run / Artifact / Claim` 的定义；
- 建立统一 ID、schema version、actor、trace ID 和时间字段规范；
- 定义 `/v1` API 错误模型和幂等协议；
- 明确 SQLite、本地工件库和现有研究数据库的迁移路径；
- 删除或标记重复、废弃和未接入主流程的入口；
- 建立端到端验收脚本和固定演示快照。

### 退出标准

- 全量测试通过；
- 数据模型和 API 契约评审完成；
- 旧示例和运行目录仍能读取；
- 所有主流程写操作可以定位数据所有者；
- 项目文档明确“当前保证”和“目标能力”。

## Popper 0.1：Verifiable Loop

目标：形成可公开发布、任何人能够安装和验证的可信研究闭环。

### 产品能力

- 创建 Project、Quest 和 Study；
- 导入文献证据并登记 Hypothesis；
- 创建和冻结 ExperimentDesign；
- 创建不可变 Revision；
- 运行本地或沙箱实验；
- 由独立 Evaluator 产生 Observation；
- 根据证据提交 Decision；
- 一次性确认与 Claim 生成；
- 离线 replay、完整性验证和报告；
- 一个统一 Workbench 展示全部状态。

### 领域验证

- 非 ML：ODE 收敛或算法吞吐实验；
- 数据驱动：Wisconsin 公开数据实验；
- 论文复现：至少一个冻结协议复现任务。

### 发布 Gate

- 不合规动作拦截率 100%；
- 已定义篡改检测率 100%；
- 故障恢复不重复消费预算或确认资源；
- 关键声明证据可追溯率 100%；
- 三类案例使用同一 Research Kernel；
- 安装、快速开始和复现文档可由新环境执行。

## Popper 0.2：Research Workbench

目标：从可信实验内核发展为日常可用的完整科研工作区。

### 产品能力

- 多 Project/Quest 工作区；
- 文件、代码编辑器和真实 PTY；
- 持久化 Agent Session；
- DeepSeek Harness 与至少一个编码 Agent Harness；
- 流式消息、工具调用、计划审批、中止、转向和恢复；
- Git Experiment Graph；
- 运行日志、工件、Diff 和证据关联；
- 文献、假设、实验、证据和稿件统一导航；
- 长期 Campaign 的进度、等待和人工审批。

### 工程 Gate

- 进程重启后会话和 Campaign 可继续；
- 重复请求不会产生重复 Run 或 Observation；
- 所有运行能恢复源码和输入快照；
- 终端和可信本地能力默认关闭并明确授权；
- UI 不包含伪数据或只能展示、不能执行的核心按钮。

## Popper 0.5：Open Research Platform

目标：支持小团队和外部贡献者开展真实研究。

### 平台能力

- Organization、成员和 RBAC；
- PostgreSQL 元数据与事务事件；
- 以 `TransactionConnection` 一致性套件验收 SQLite/PostgreSQL；当前 SQLite
  参考实现已接入 revision execution job、worktree、论文复现、Agent Session 和
  Research Store/预算账本；PostgreSQL 仍需真实数据库一致性验收；
- S3 兼容工件库；
- 持久化任务队列和 Remote Worker；
- 项目配额、模型用量和计算成本；
- 审计日志与 OpenTelemetry；
- 稳定 Harness、DomainProtocol、Evaluator、ExecutionBackend SDK；
- 插件 manifest、兼容矩阵和示例模板；
- 团队项目共享、审批和只读审计角色；
- 公共 benchmark 与结果浏览器。

### 发布 Gate

- 项目之间数据、密钥和 Worker 权限隔离；
- API 和数据库迁移具备兼容测试；
- Worker 失联、重复回调和乱序事件可恢复；
- Alpha SLO 达成并公开容量测试；
- 至少三个外部领域插件不修改核心即可运行；
- 至少一个外部贡献者完成从安装到提交插件的全过程。

## Popper 1.0：Research Operating System

目标：形成稳定、可治理的开放科研 Agent 协议和生产平台。

### 能力范围

- 本地、Docker、Kubernetes、Slurm 和远程 GPU 执行；
- 高可用控制平面、备份恢复和灾难演练；
- Secret Manager、短期凭据和 Worker 身份；
- 多组织配额、计费接口和资源治理；
- 稳定的科研对象、Action 和 Evidence 协议；
- 领域注册表、插件签名和安全审查；
- 公开 RFC、版本策略和弃用政策；
- 社区维护的领域模板与 benchmark；
- 可导出的开放研究包，不锁定 Popper 服务。

### 1.0 Gate

- 核心协议在一个稳定周期内保持向后兼容；
- 生产 SLO、RPO 和 RTO 经故障演练验证；
- 关键安全边界通过独立审查；
- 研究包可以在干净环境由第三方复现；
- 社区治理、维护者权限和发布流程公开；
- 项目拥有真实外部用户、贡献者和跨领域案例。

## 横向工作流

以下工作不等待某个版本结束，而是贯穿整个路线：

### 测试与评测

- 单元、契约、状态机性质测试；
- 故障注入和恢复测试；
- 路径、权限、密钥和恶意工件安全测试；
- 跨领域端到端研究；
- Agent 贡献、成本和失败类型评估；
- 性能、容量和长时间稳定性测试。

### 开源与社区

- `CONTRIBUTING.md` 与开发环境一键启动；
- Good First Issue 和领域插件教程；
- RFC 与 Architecture Decision Record；
- 公开 roadmap、changelog 和兼容策略；
- 可复查的 release artifacts 与 SBOM；
- 定期发布失败分析和研究复现报告。

### 文档、开放评测与系统研究

- 用户指南与管理员指南分离；
- API、插件、数据模型和威胁模型文档化；
- 每个保证绑定实现和测试；
- 公司技术报告持续公开 Research Kernel、可验证执行和证据链的设计与实证边界；
- 系统论文与开放评测使用公开 benchmark、真实用户场景和外部复现证据，服务于技术验证与社区建设，不作为产品路线的截止条件。

## 近期执行顺序

1. 恢复测试全绿，停止在不稳定基线上增加功能；
2. 完成核心实体与 API v1 RFC；
3. 把现有工作台收敛为唯一产品入口；
4. 建立持久化 Agent Harness 与会话恢复；
5. （已完成）为旧 revision 提供显式、可审计且不改写原身份的 Diff 迁移工具；
6. （已完成）ODE、Wisconsin 与处理效应案例使用同一 Research Kernel；论文复现已统一溯源、核心裁定及 SQLite 事务生命周期事件，包含并发领取和崩溃恢复；
7. 发布首个可复现版本，再开始团队基础设施建设。
