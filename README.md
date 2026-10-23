# Popper

本地优先的计算实验决策内核（Pre-Alpha）。不是通用科研助手。能力边界见 [docs/capability-boundary.md](docs/capability-boundary.md)。验收记录见 [docs/maintenance/working-tree-verification.md](docs/maintenance/working-tree-verification.md)。

## 默认只演示这一条

已登记研究的证据闭环（`ResearchController` + `ResearchRuntime`）。科学写入（judgment / 确认集）只有一条合法路径；Adaptive 是基准夹具入口，Campaign 是编排图——**都不另立计分权威**，也不急着揉成一个巨类。

```powershell
python -m pip install -e .
popper demo smoke
```

或逐步走：

```powershell
popper demo init demo-ode --case ode-convergence
popper research init demo-ode demo-run
popper research run demo-run --trusted-local
popper research confirm demo-run --trusted-local
popper research status demo-run --brief
```

`research run` / `confirm` 必须且只能选 `--trusted-local` 或 `--sandbox`。确认集只打开一次。判断三态：supported / refuted / inconclusive。模型不能改分数。`demo smoke` / `status --brief` 用短摘要读结果；`inconclusive` 不是失败。

旁路（需要时再用）：`popper adaptive`（描述性基准，默认不开 live）；`popper research campaign`（编排；dev/freeze/confirm 须 `planning_protocol=research-runtime/v1`，跳过≠完成）。
