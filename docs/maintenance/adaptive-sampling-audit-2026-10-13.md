# 抽样审计门禁（2026-10-13）

公开分区没有 supported 或 refuted，不是因为再跑一轮就会出现。  
[`interval_verdict`](../../popper/research/judgment.py) 在比较质量不是 `valid` 时返回 `inconclusive`。  
原先 [`evaluate`](../../popper/research/prediction_comparison.py) 只要单位不少于 30 且 `independence_verified` 为 true 就给 `valid`。  
公开任务把这个布尔值设成「不是公开数据」，分组键又是整行特征的哈希。  
同一船型的不同航速、同一建筑几何的不同朝向会被当成不同单位。  
把布尔值改成 true 不会改变抽样。

## 现在什么算 valid

比较包里的布尔值不再授予 `valid`。只有声明和数据对得上，且单位不少于 30：

- `iid_row`：生成器版本是 `adaptive-tasks/v1`，每个分组键等于该行特征的哈希。
- `documented_cluster`：分组键属于预先登记的簇，并附有可引用的抽样记录，说明这些簇是独立实验。没有记录时质量保持 `insufficient_independent_units`。

现有 UCI 行没有这种记录，声明是 `feature_row_hash`，不能变成 `valid`。  
旧报告不重新出具。已锁定的 `formal`、`formal-independent-72`、`formal-root` 目录、报告和暴露行不改。

## 已冻结与已绑定

2026-10-13：抽样审计指纹已手写冻入 `engineering-gates.json`（检查项 `sampling_audit`）。  
已绑定 `formal-iid-72`：I01–I12，全部 `iid_row` / `adaptive-tasks/v1`，kinds 0–5 各两次，72 单元。  
准入回执：`formal-iid-72-admission.json`。任务级配对比较的任务数已预注册为 12。  
2026-10-13：`formal-iid-72` 已跑完，退出码 0。确认完成 67/72。  
科学判断 supported 18、refuted 26、inconclusive 23、not_evaluated 5。  
描述性综合见 `adaptive-formal-iid-72-synthesis-2026-10-13.md`。不宣称策略优势。

`formal-generated-root` 仍是 6 个任务、旧指纹，保持未启动。  
前几轮生成分区的 supported（6、0、4、本轮 18）不能合并成策略排名。
