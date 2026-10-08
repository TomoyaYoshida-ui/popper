# Windows 重启恢复点

用户准备手动重启 Windows。不要自动重启，也不要启动付费评测。

实施仓库：`C:/Users/wangj/Desktop/research/research-agent-v2`，分支 `autopilot`。

## 当前状态

- WSL 3.0.1.0 官方 MSI 已安装，微软 Authenticode 签名有效。
- VirtualMachinePlatform/Windows Subsystem for Linux 功能已启用，系统返回需要重启；
  重启前 WSL 2 仍报告虚拟化未激活。
- Ubuntu 24.04.5 官方 .wsl 镜像已下载，但尚未安装/初始化。
  SHA-256：bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e。
- Docker Engine、CLI、Buildx、Compose 尚未安装；脚本已准备。
- 通用比较器、冻结候选执行入口、三任务准备及试点评测框架已实现初稿。
  通用/既有 FIGS 相关测试 33 passed、7 subtests passed。
- Windows 离线冒烟完成 3 个开发单元、每单元 4 个候选；没有确认或模型调用。
  模型策略标签在此冒烟中仅为离线夹具，不算真实策略评测。
- 180 次付费阶段尚未开始；原 30 次账本保持原样。

## 本地材料

`.acceptance/transfer-environment/windows-setup.json`：安装状态。
`.acceptance/transfer-environment/download-manifest.json`：官方下载校验。
`.acceptance/transfer-environment/ubuntu-24.04.5-wsl-amd64.wsl`：待安装镜像。
`.acceptance/transfer-tests.log`：测试输出。
`.acceptance/transfer-offline-smoke`：旧源码版本的离线冒烟记录，保留不覆盖。
源码在该冒烟后继续改动，新的离线验证必须使用新的输出目录。

## 重启后继续

1. 读取本恢复点、检查 Git 状态和 `wsl --status`，确认 WSL 2 真能启动。
2. 使用已校验的官方镜像安装 Ubuntu 24.04；必要时等待用户完成首次账户操作。
3. 在 Ubuntu 内执行 `scripts/setup_transfer_linux.sh`，安装 Docker 与 Linux 验证依赖。
   测试使用非 root 的 research-runner，数据与源码置于 Linux 原生文件系统。
4. 完善试点框架的恢复/确认预算、离线验证器、原始证据消融对齐、报告指标及环境门禁。
   完成任务协议和 Windows 全量回归，保留旧 FIGS 比较器的历史兼容。
5. 完成 Linux 全量回归、Linux 沙箱端到端与 Docker 打包检查。
6. 全部门禁通过后，冻结新协议、代码、数据和提示词，再运行 27 单元的真实试点。
   全局 180 次、单模型单元 9 次、预检 6 次，凭据只读 POPPER_API_KEY。

已准备文件：`scripts/setup_transfer_wsl.ps1`、`scripts/setup_transfer_linux.sh`、
`popper/research/paired_comparison.py`、`evaluation/transfer_tasks.py`、
`evaluation/run_transfer_pilot.py`、`integrations/transfer_pilot/model.py`、
`tests/test_transfer_pilot.py`。当前框架尚非最终验收，不能宣称试点已完成。
