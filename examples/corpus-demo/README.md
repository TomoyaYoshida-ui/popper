# 语料演示（corpus-demo）

本目录是一份**种子/示例语料**，用于验证语料库规模与增量机制，**不代表已标定 novelty**。

- `corpus/records.json`：由 `popper research corpus seed` 生成，含 200 条记录，四类分布（gap / contradiction / negative_result / foresight 各 50）。
- 每条仅含方法学/工程级描述（如"AutoEP 类工具：缺少统一的实验结果登记契约"），**不编造真实论文的精确数字结论**；`novelty_tag` 均为 `null`（未标定）。
- 用途：验证 `Corpus` 的批量写入、record_id 去重与幂等增量；可作为后续增量标定的起点。不作为已核实的科研结论或 novelty 判定输入。

重新生成（幂等）：
```
popper research corpus seed examples/corpus-demo/corpus --count 200
popper research corpus stats examples/corpus-demo/corpus
```