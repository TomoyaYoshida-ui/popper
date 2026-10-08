# 独立迁移试点：重启后的环境准备

实施对象：原仓库 `C:/Users/wangj/Desktop/research/research-agent-v2`，分支 `autopilot`。

Windows 手动重启后，已验证 Ubuntu 24.04.5 LTS 可在 WSL 2 启动。
WSL 3.0.1.0；Linux 内核 6.18.40.1-microsoft-standard-WSL2。
官方 Ubuntu 镜像校验值及 Windows MSI 签名见此前恢复点；下载材料留本地。

通过 Docker 官方 Ubuntu apt 仓库安装并启动：

- Docker Engine/CLI 29.8.2。
- containerd 2.3.6。
- Buildx 0.38.0。
- Compose 5.6.0。

测试账户为非 root 的 `research-runner`（UID 1000），没有 Docker 组权限。
源码复制到 `/home/research-runner/transfer-validation`，不是 `/mnt/c`。
源码导出器只读取 Git 已登记且仍存在的文件及明确指定的新验证脚本，排除运行目录、数据库和凭据。
未改变原文档删除状态，未挂载原确认数据库，未开放 Docker TCP daemon。

已安装 Linux 固定依赖：numpy 2.2.6、scikit-learn 1.7.2、pandas 2.3.3，
FIGS 上游固定为 `e8efb7e7b599e4d0f3e3399174e6684d125acb6f`。
其他完整依赖版本由 `requirements-validation.txt` 固定。

这份记录确认安装完成，不代表门禁通过。首次 Linux 沙箱端到端在 CLI 构造时失败：
`research metrics` 重复注册导致 Python 3.12 的 argparse 抛出异常。
该已有入口问题已经定位，修复与入口测试将在接口阶段提交。
Docker 首次拉取官方 Python 镜像发生 EOF，第二次拉取及构建成功，但入口仍被同一 CLI 问题阻断。
需使用修复后的同一源码版本重做相应验收。

Windows 全量、Linux 全量及离线 27 单元闭环正在验证，付费评测尚未开始。
本地原始安装与验证日志保存在 `.acceptance/transfer-environment` 及 Linux 用户目录下。
没有推送远端。
