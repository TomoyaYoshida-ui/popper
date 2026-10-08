# 通用比较接口的补强与验证

原项目增加的 `paired-unit-ci/v1` 保留既有 FIGS Concrete 比较路径。
独立评估器重新计算配对单位的相对 MAE 改善、10,000 次 bootstrap 和 95% 区间，
不足 30 个单位时统一返回证据不足；训练种子不作为独立单位。

补强制品绑定：重算模型配置摘要，验证 numpy/scikit-learn/pandas 固定版本，
FIGS 审计必须记录固定上游提交。审计中的树能够重算实际预测，规则数限制仍为 12。
已经成功执行的模型事实与模型提出的未经验证判断分开记录。

Windows 局部回归 `tests/test_transfer_pilot.py`、`tests/test_scientific_judgment.py`、
`tests/test_figs_native.py`：11 passed、2 subtests passed。
后续增加了确认重复调用、确认预算结算、模糊中断停止及 CLI 入口验证，
`tests/test_transfer_pilot.py`、`tests/test_cli_entry.py`：8 passed。
日志分别位于本地 `.acceptance/transfer-tests-v2.log`、`transfer-tests-v3.log`。

Linux/容器入口首次验证发现原 CLI 重复注册 `research metrics`。
修复将 `register`、`list` 共用同一解析器，并避免 `list` 落入登记处理分支。
入口测试确认帮助页及指标列表均可执行。此问题不是 WSL 沙箱本身的故障。

局部测试不能代替完整迁移评测；全量回归与完整离线/真实闭环另行记录。
