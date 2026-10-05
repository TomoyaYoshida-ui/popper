# RFC 0002：事务存储适配器与领域事件边界

状态：Accepted（SQLite 参考实现已落地；PostgreSQL 实现待真实服务验收）

## 决策

Popper 将数据库能力拆成两层：

1. `TransactionConnection` 只负责 SQL 执行、显式读写事务、嵌套保存点和连接生命周期；
2. Research、Session、Reproduction、worktree 与 Job Store 继续拥有表结构、合法状态迁移、
   权威写入方、幂等规则和事件重放。

不建立“任意 payload + 任意 status”的通用事件仓库。那种抽象会允许基础设施层绕过
科研领域规则，也无法证明 PostgreSQL 与 SQLite 产生等价科学状态。

## 事务契约

- 写事务必须在读取并校验 expected state/version 之前取得排他写入资格；
- SQLite 使用 `BEGIN IMMEDIATE`，不能先读后升级锁；
- PostgreSQL adapter 必须使用行锁或等价的原子条件更新，并在冲突时返回稳定冲突类型；
- 事件、快照、幂等收据、预算预留必须位于同一事务；
- 嵌套领域操作使用保存点，内层失败不得自动提交或回滚外层已接受操作；
- adapter 不重写 SQL 业务语义，也不解释领域事件；
- 连接错误、序列化冲突与约束冲突必须可区分，不能统一吞成“执行失败”。

## 当前实现

`popper.storage.SQLiteStorageAdapter` 是可执行参考实现，声明：

- dialect：`sqlite`；
- parameter style：`qmark`；
- write lock：`begin_immediate`；
- nested transactions：保存点。

持久 Revision Execution Job、worktree registry、论文复现、Agent Session 和 Research
Store/预算账本已迁入该端口。并发领取仍由状态条件更新保护，取消与重启回收先取得写事务、
再读取权威状态。Research schema 与预算 schema 在同一个写事务升级；故障注入验证任一
部分失败时两者和版本标记一起回滚。

## PostgreSQL 验收门槛

只有满足以下条件才可声明 PostgreSQL 支持：

1. 使用真实 PostgreSQL（不是 SQLite mock）运行同一 adapter conformance suite；
2. 对同一领域 Action 序列产生等价快照、事件 payload 和幂等结果；
3. 两个独立连接并发领取同一 Job，只有一个成功；
4. 事务中途故障不留下事件/快照半提交；
5. 序列化重试不重复扣预算、不重复创建 Run 或调用外部 Worker；
6. migration 支持向前升级与备份恢复演练；
7. 性能报告包含事件追加、状态读取和 32 个并发 writer，而不是只测空查询。

在上述门槛完成前，产品文档只能写“PostgreSQL-ready storage port”，不能写“支持
PostgreSQL”或“团队版已完成”。

## 后续迁移顺序

1. Revision Execution Job（已完成）；
2. worktree registry 与 Reproduction lifecycle（已完成）；
3. Agent Session/Job（已完成 SQLite 端口迁移）；
4. Research Store 与预算账本（已完成 SQLite 端口迁移）；
5. PostgreSQL adapter、真实数据库一致性测试与团队租户隔离。

每一步都必须保持原数据库可原地读取，禁止为了统一接口批量重写历史事件哈希。
