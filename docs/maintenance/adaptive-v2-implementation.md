# 自主科研 v2：实施与运行说明

本次新增 `research-runtime/v2`，保留现有 v1 研究行为、历史实验与账本。
新任务冻结目标、指标、数据划分、公共基线和候选权限；问题、代码、
实验、修订与收据追加到原 ResearchStore。执行仍复用 RevisionStore、
LocalWorker、预算账本和独立 Judgment。模型没有评分写权限。

## 结论语义

原 Claim 的绝对差阈值是历史描述性事实，原事件不改写。
读取接口新增 scientific_status、judgment_ref、effect_type、effect_unit、
effect_value、threshold、ci95。UI、证据附录和新稿件使用绑定到同一假设
版本和确认观察对的独立科学判断。没有匹配 Judgment 时不宣称统计支持。

## 环境与入口

Python 3.10+，安装 `.[adaptive]`。正式生成代码执行要求 Linux、非 root、
bubblewrap；仅挂载运行库、冻结代码、输入和输出。宿主研究数据、
确认标签、凭据与评分器不挂载。原本的 Windows 工作台仍可使用。

```text
popper adaptive prepare --root <batch-root> --pilot
popper adaptive develop --root <batch-root> --live
popper adaptive freeze --root <batch-root>
popper adaptive confirm --root <batch-root>
popper adaptive review --root <batch-root> --live
popper adaptive report --root <batch-root>
```

省略 --pilot 准备 12 任务 / 72 单元。真实开发需要相同源码指纹的
engineering-gates.json 回执；不以固定脚本冒充真实模型策略。
模型凭据只读 POPPER_API_KEY。缺少 --live 不发送付费请求。

研究、试点和盲评使用父目录中的同一请求账本，总上限 1600 次发送、
1800 万 token。研究单元上限 24 次发送 / 25 万 token；每次逻辑请求
最多两次发送。问题生成、纠错和代码生成计入额度。盲评每项两遍，
单遍最多两次发送 / 25000 token；未知用量保留预留并阻止进一步调用。

## 数据与统计边界

六个公开任务和六个新生成任务分别报告。生成器和种子仅留在宿主。
数据暴露账本检查样本身份与完整输入，确认名单必须等到所有单元
完成开发终态后冻结。重复使用同一任务确认数据只允许冻结的同一
cohort；不允许用不同名单或新研究重开。

确认训练用 train+dev，候选只收确认特征。分析单位等权 MAE，
10000 次配对 bootstrap，95% 区间，实质阈值为相对改善大于 5%。
公开数据缺少可靠独立单位依据时，科学判断保持证据不足，描述性
预测效果仍保留；不得据公开行级划分声称物理机制或新发现。

## 验收状态

初始工作树快照保存在 `.acceptance/adaptive-v2-baseline`。
最新验证日志保存在 `.acceptance/adaptive-v2-validation`；应以该目录
的实际回执判断完成情况。代码存在、离线测试通过、真实试点完成、
正式对照完成分别记账，不能相互替代。

截至 2026-10-13：比较质量不再接受 `independence_verified` 布尔值。
见 `adaptive-sampling-audit-2026-10-13.md`。
`formal-iid-72` 描述性综合见
`adaptive-formal-iid-72-synthesis-2026-10-13.md`（确认 67/72；
supported 18、refuted 26、inconclusive 23、not_evaluated 5）。
修订须留复验容量（软拒绝）；修订容量指纹已冻，live 未开。
见根因治理文档。不宣称策略优势。`formal-generated-root` 保持未启动。
