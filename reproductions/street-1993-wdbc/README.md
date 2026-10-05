# Street et al. (1993) WDBC 结果复现

目标论文：W. Nick Street, William H. Wolberg, Olvi L. Mangasarian, “Nuclear feature extraction for breast tumor diagnosis,” Proceedings of SPIE 1905, 861–870 (1993), DOI `10.1117/12.148698`。

目标 claim：在 569 个 WDBC 病例上，使用 mean texture、worst area、worst smoothness 三个特征和一个分离平面，10 折交叉验证准确率达到 97%。

## 复现等级

这是方法近似复现。数据集、样本量、三个特征和 10 折交叉验证与论文一致；原始 fold 分配、MSM-T 软件和 robust linear programming 参数没有公开，因此不能做逐位或算法完全一致的复现。

主方法是预先登记的 L1 正则 soft-margin 线性规划分离平面。另报告三特征和全部 30 特征的线性 SVM，帮助区分“线性平面可达到相近性能”和“原 MSM-T 实现被精确复现”。

## 运行

```powershell
# 在仓库根目录执行；新目录必须不存在或为空
python -m pip install -e ".[ml]"
python scripts/prepare_wdbc_reproduction.py ..\popper-wdbc-local
python -m popper experiment reproduce inspect ..\popper-wdbc-local
python -m popper experiment reproduce run ..\popper-wdbc-local --trusted-local
python -m popper experiment reproduce status ..\popper-wdbc-local
```

准备脚本复制已登记输入及哈希固定的数据到新目录并调用通用 `ReproductionTask` 初始化，不携带被 Git 忽略的本机状态。`inspect` 检查论文 claim 的指标、报告值、证据定位，以及协议是否引用同一 claim 和带哈希的数据。执行成功后 Popper 记录所有必需产物的哈希并自动调用离线核验器。准备脚本已提供 `source/wdbc.data`，因此这一流程无需联网；若手动建立缺少该文件的新目录，`reproduce.py` 才会尝试从 UCI 官方 HTTPS 地址获取它。协议固定在 `protocol.json`；逐折预测保存在 `predictions.csv`；摘要保存在 `results.json`。`verify.py` 不训练模型，只从保存预测重新计算指标和复现状态。

已经注册或执行过的目录不能再次初始化或重跑。如需重新执行，用准备脚本创建另一个空目录，以保留原证据链。仓库中追踪的 [REPORT.md](./REPORT.md) 是历史报告；干净检出没有对应的权威运行数据库，不能把它当作当前已核验任务状态。

本次运行的完整结论见 [REPORT.md](./REPORT.md)。

本复现只评价历史数据上的计算结果，不能用于临床诊断，也不验证论文中的图像分割系统或后续前瞻性病例结果。
