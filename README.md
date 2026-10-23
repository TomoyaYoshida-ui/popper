# Popper

本地优先的计算实验决策内核（Pre-Alpha）。不是通用科研助手。能力边界见 [docs/capability-boundary.md](docs/capability-boundary.md)。

## 最小离线路径

在本目录、不提供模型端点时：

```powershell
python -m pip install -e .
popper demo init demo-ode --case ode-convergence
popper research init demo-ode demo-run
popper research run demo-run --trusted-local
```

`research run` 必须且只能选择 `--trusted-local` 或 `--sandbox`。这一步登记可证伪预测，并在开发集上由独立评估器计分。确认集只在 `popper research confirm demo-run --trusted-local` 打开一次。判断是 supported / refuted / inconclusive 三态。模型不能改分数。

默认产品路径就是上面的已登记研究（ResearchController + ResearchRuntime）。`popper adaptive` 是描述性基准（默认不开 live）。`popper research campaign` 是编排图，跳过不等于完成。
