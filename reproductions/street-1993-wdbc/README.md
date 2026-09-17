# Street et al. (1993) WDBC 结果复现

目标论文：W. Nick Street, William H. Wolberg, Olvi L. Mangasarian, “Nuclear feature extraction for breast tumor diagnosis,” Proceedings of SPIE 1905, 861–870 (1993), DOI `10.1117/12.148698`。

目标 claim：在 569 个 WDBC 病例上，使用 mean texture、worst area、worst smoothness 三个特征和一个分离平面，10 折交叉验证准确率达到 97%。

## 复现等级

这是方法近似复现。数据集、样本量、三个特征和 10 折交叉验证与论文一致；原始 fold 分配、MSM-T 软件和 robust linear programming 参数没有公开，因此不能做逐位或算法完全一致的复现。

主方法是预先登记的 L1 正则 soft-margin 线性规划分离平面。另报告三特征和全部 30 特征的线性 SVM，帮助区分“线性平面可达到相近性能”和“原 MSM-T 实现被精确复现”。

## 运行

```powershell
# 在本文件所在目录（reproductions/street-1993-wdbc）执行
python -m popper experiment reproduce inspect .
python -m popper experiment reproduce init .
python -m popper experiment reproduce run . --trusted-local
python -m popper experiment reproduce status .
```

`inspect` 先检查论文 claim 的指标、报告值、证据定位，以及协议是否引用同一 claim 和带哈希的官方数据集。`reproduction.json` 将领域脚本注册为通用 `ReproductionTask`。初始化会冻结论文记录、协议、执行器和核验器；执行成功后 Popper 记录所有必需产物的哈希并自动调用离线核验器。`reproduce.py` 首次运行会从 UCI 官方 HTTPS 地址下载 `wdbc.data`。协议固定在 `protocol.json`；逐折预测保存在 `predictions.csv`；摘要保存在 `results.json`。`verify.py` 不训练模型，只从保存预测重新计算指标和复现状态。

已经注册或执行过的目录不能再次初始化或重跑。如需重新执行，应复制论文记录、协议和代码到一个没有 `.popper-reproduction` 的新任务目录，以保留原证据链。

本次运行的完整结论见 [REPORT.md](./REPORT.md)。

本复现只评价历史数据上的计算结果，不能用于临床诊断，也不验证论文中的图像分割系统或后续前瞻性病例结果。
