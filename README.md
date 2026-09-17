# Popper

Popper 是一个本地优先的科研实验推进 Agent。它在显式登记的实验协议下复跑基线、探索候选、生成不可变代码版本、执行开发实验，并把结论绑定到可重算的逐样本证据。

当前版本仍是工程原型。预注册代码可以选择可信本地或 OS 原生沙箱（Windows 低完整性 + Job Object，或 Linux bubblewrap 命名空间）；模型生成代码强制进入沙箱 worker，不能降级为可信本地执行。该沙箱限制写入和进程资源，并在 `--sandbox` 消费测试集时对已注册的测试数据集施加内核级读隔离（Windows 强制完整性 `NO_READ_UP` 下读它得 `PermissionError`；Linux 以 `--ro-bind /dev/null <path>` 遮蔽，读它得空内容），候选进程读不到保留集标签；其余本机读取仍未隔离，因此合成示例和开发集结果不算科研创新或盲测证明。Linux 后端要求 `bwrap` 能真的建立非特权用户命名空间（Ubuntu 23.10+/24.04+ 需为 `/usr/bin/bwrap` 开 AppArmor `userns` 例外），可用性由实跑一次沙箱探活得出，不满足时 `--sandbox` 显式报错而不是静降级；macOS seatbelt 尚未实现。

## 运行证据驱动 Research Controller

先初始化一个普通 Popper 实验，再建立独立研究运行目录：

```powershell
python -m popper experiment init <实验项目目录>
python -m popper research init <实验项目目录> <research-run目录>
python -m popper research run <research-run目录> --trusted-local
python -m popper research status <research-run目录>
```

`research run` 会实际执行基线和候选，由单独计分进程从逐样本预测重算开发指标，然后根据预注册阈值选择继续对照、停止或请求确认。只有状态为 `ready_for_confirmation` 时才运行一次性确认：

```powershell
python -m popper research confirm <research-run目录> --trusted-local
```

使用 DeepSeek 策略时，每个成功的候选开发实验之后还会调用模型反思。模型必须引用真实基线/候选 Observation，并说明替代解释；若继续对照，只能选择尚未实验的已登记候选，为其追加科学假设版本和区分性预测。存储层在同一事务内保存新假设、新冻结设计、Reflection 和 Decision，旧实验记录与原预算保持连续。恢复运行会先补齐中断的反思，再执行已安排的对照，不重新选择或重跑已完成实验。修订发生在前序结果之后，明确不算原始预登记假设；预测文字的科学区分力仍需独立审查。

也可以用 `research run ... --auto-confirm` 在达到开发阈值后自动确认。确认会消费最终测试集，不能撤销或用于继续搜索。

确认前的准备失败会释放未使用预算，并保留取消的尝试；确认测试一旦开始，失败也会结算消费且拒绝再次执行。若测试已完成而独立计分或结论保存中断，恢复只处理保存的预测和证据。`confirming` / `confirmation_failed` 状态不会进入后续模型选择或搜索；沙箱生成 revision 的独立确认门槛仍然保留。

接入 DeepSeek 时，密钥只从环境读取。模型负责生成可证伪假设并在合法动作中选择；指标始终由确定性评估进程计算：

```powershell
$env:POPPER_API_KEY = "你的密钥"
python -m popper research init <实验项目目录> <research-run目录> `
  --base-url https://api.deepseek.com --model deepseek-flash
python -m popper research run <research-run目录> --trusted-local `
  --base-url https://api.deepseek.com --model deepseek-flash
```

让控制器在每轮选中假设后自主生成、执行并独立计分代码 revision：

```powershell
python -m popper research run <research-run目录> --sandbox --autonomous-code `
  --base-url https://api.deepseek.com --model deepseek-flash
```

自主代码循环遇到 worker 明确报告的实现失败时，在实验预算允许的情况下自动修复一次，以失败 revision 为不可变父版本重新执行并独立计分。基础设施失败、完整性错误和预算不足不会触发自动修复；修复仍失败时保留工程失败记录，不作为科学反证。修复反馈包含失败状态、错误类型及经过制品 SHA-256 校验的 stdout/stderr 尾部（每份最多 8192 字节），保留截断标记，并明确作为不可信执行数据传入模型。

生成代码在每个 seed 上还须记录执行改动证据：以原始注册代码为基准，找出新增或改变的可执行语句，再核对实际行轨迹，得到分级 `coverage`。检测到改动语句却一处都没执行（未调用的 helper、未进入的函数/分支）仍不能产生 Observation，失败原因进入自动修复反馈。`research status` 的 `execution_gates` 展示 `coverage`、`uncovered_files` 与原因，轨迹与回执一并保存摘要，后续篡改会阻断状态读取与继续运行。

这是保守的执行覆盖证据，不是科学机制检验。删除语句、只改条件或定义头、同一行上的定义/函数体，以及纯注释与格式改动都无法由行事件观测，得到空改动集时记为 `ambiguous` 证据而不拒绝；只有轨迹证明检测到的改动语句一处都没执行（`coverage: none`）或轨迹不完整（`coverage: unknown`）才判为实现失败。轨迹在候选进程内采集，不能作为对抗性隔离证明；命中改动行也不证明它改变预测、改善效果或验证科学机制。这些结论需后续机制对照。

也可以单独实现一个候选，或以失败 revision 为不可变父版本生成修复版：

```powershell
python -m popper research implement <research-run目录> --hypothesis <H-id> `
  --base-url https://api.deepseek.com --model deepseek-flash
python -m popper research implement <research-run目录> --hypothesis <H-id> `
  --parent-revision <REV-id> --base-url https://api.deepseek.com --model deepseek-flash
```

GPU 作业需先显式分配设备，例如 `$env:POPPER_GPU_DEVICES = "0"`，再传 `--gpu-count 1`。自主代码达到开发阈值后会停在确认边界；当前版本拒绝用原始注册代码冒充该 revision 做最终确认，需先接入隔离的 holdout worker。

无模型模式属于 `registered_hypothesis_baseline`，用于离线回归和公平对照。当前独立计分进程仍在同一 OS 账户下：`--sandbox` 能挡住候选进程读已注册的测试标签（Windows 强制完整性标签 / Linux mount namespace 遮蔽，都是内核强制），但账户级/主机级隔离仍需部署成不同凭据域的外部评估服务。

计分协议 v2 强制使用完整预注册 seed 集，并核对原始数据摘要、真实计分源码指纹、服务身份和逐 seed 统计。新研究在初始化时绑定计分实现；运行中改变实现会停止。相同请求 ID 的不同内容不能覆盖旧评分证据。历史 v1 回执保留可审计，不会原地升级；未绑定计分源码的旧研究拒绝继续 run/implement/confirm，需建立新研究运行目录，并继续使用原家族预算。

## 本机真实 GPU 训练验收

已在 RTX 3050 Laptop GPU 上完成 Windows 沙箱训练：三个 seed 各运行 60 步，验证 CUDA 张量、非零梯度/参数更新、代码执行覆盖和独立开发计分。最终评分实现的复跑结果在 `evaluation/runs/gpu-training-20260912T171729772476Z/trial-summary.json`。这是固定 MLP 工程验收，未执行基线对比、未调用模型生成算法、未计分最终测试集。

使用已准备好的隔离 CUDA 环境重复验收，每次写入新目录：

```powershell
$env:POPPER_GPU_DEVICES = "0"
.\.venv-gpu\Scripts\python.exe evaluation/run_gpu_training_trial.py
```

该环境安装 PyTorch `2.12.0+cu126`，不改变系统 Python 的 CPU 版 PyTorch。worker 将 PyTorch/CUDA 缓存限定在各作业临时目录，并保留失败日志；没有 CPU 回退。此验收不代表通用大模型训练能力，显存硬配额尚未实现。

首次在任意子目录使用前安装本地命令；真实分类示例同时安装 ML 依赖：

```powershell
# 在仓库根目录（含 pyproject.toml 的目录）执行
python -m pip install -e ".[ml]"
```

## 立即查看真实 ML 接入

仓库中的 `examples/breast-cancer-wisconsin` 使用 scikit-learn 内置的真实公开诊断数据，并已完成一轮实验。启动只读工作台：

```powershell
# 在仓库根目录（含 pyproject.toml 的目录）执行
python -m popper serve examples\breast-cancer-wisconsin
```

打开 `http://127.0.0.1:8765/`。只读模式可以查看真实运行、结论、证据和审计事件，不会执行项目代码。

查看已经核验的论文复现任务：

```powershell
python -m popper serve reproductions\street-1993-wdbc --port 8766
```

打开 `http://127.0.0.1:8766/`。工作台会自动识别 `ReproductionTask`，显示论文报告值、复现值、冻结协议、报告、逐项产物指纹和领域核验结果。

用 `--campaign <run_dir>` 让工作台监控并交互审批挂起的 campaign（默认编排挂起在 variant 物化审批点）。统一进度页会显示挂起区块，可查看 `proposal.diff` 预览后「批准并恢复」或「驳回」；批准需 `--trusted-local` 启动，后台线程真正恢复执行：

```powershell
python -m popper serve examples\breast-cancer-wisconsin --trusted-local `
  --campaign integrations\runs\my-campaign
```

Campaign 遇到 `fatal` 会立即停止后续步骤并标记 `failed`；遇到 `retryable` 会停止本轮并保留可重跑状态。完成判定只接受每个步骤最近一次的 `success`，旧成功记录不能覆盖新的失败或重试。

批准代码提案后，后续开发集搜索、冻结、最终确认和稿件输出使用 `variant-project`。原项目继续作为提案来源；变体路径记录在 campaign 的 variant 步骤结果中，恢复时通过物化缓存重新接入，不重复消费最终测试。

新复现任务在注册前运行 `python -m popper experiment reproduce inspect <任务目录>`。检查器要求 `paper.json` 提供可定位的 claim 证据与明确指标契约，并要求 `protocol.json` 引用同一 claim、官方数据地址和预期数据 SHA-256；不一致时拒绝初始化。

## 从头运行真实数据项目

复制 `examples/breast-cancer-wisconsin` 到一个新目录，确保其中没有 `.popper`，然后运行：

```powershell
python examples\breast-cancer-wisconsin\prepare_data.py
python -m popper experiment init examples\breast-cancer-wisconsin
python -m popper experiment search examples\breast-cancer-wisconsin --trusted-local
python -m popper experiment freeze examples\breast-cancer-wisconsin
python -m popper experiment confirm examples\breast-cancer-wisconsin --trusted-local
python -m popper experiment replay examples\breast-cancer-wisconsin
python -m popper experiment report examples\breast-cancer-wisconsin
python -m popper serve examples\breast-cancer-wisconsin
```

`--trusted-local` 表示你确认项目代码可以在当前用户权限下执行。当前超时控制不构成沙箱，未知来源代码不能使用此模式。

## 接入自己的项目

M0 支持显式适配的数值回归与二分类项目：

1. 在项目根目录编写 `experiment.json`，参考 `examples/quadratic/experiment.json`。
2. 准备互不重叠的 `train.json`、`dev.json`、`test.json`。回归记录使用 `id/x/y`；二分类记录使用 `id/features/label`。
3. 实验入口接受 `--train --input --output --config --seed`。输出必须是 `[{"id": "...", "prediction": 0.0}]`；不能自报指标。
4. 先运行 `python -m popper experiment scan <项目目录>` 查看支持范围，再执行 `experiment init`。

完整契约与保证边界见[技术方案](./docs/技术方案.md)。

## 已接入的开源组件

Popper 当前直接调用工作区中的 ResearchStudio-Idea 和 scientific-agent-skills Arbor。先检查许可证、入口和文件指纹：

```powershell
python -m popper vendor inspect
```

启动或继续 Idea Spark 导航，并初始化 Arbor 假设树：

```powershell
python -m popper vendor idea-next integrations\runs\my-idea --query "研究问题"
python -m popper vendor arbor-init integrations\runs\my-arbor `
  --objective "研究目标" --dev-eval "开发集评估命令" --test-eval "最终评估命令"
```

当 Idea Spark 已生成 `phase2_generate_output.json`、`refined_candidate.json` 或 `final_candidate.json` 后，将 canonical candidate 接入 Arbor：

```powershell
python -m popper vendor idea-to-arbor integrations\runs\my-idea integrations\runs\my-arbor
python -m popper vendor arbor-state integrations\runs\my-arbor
```

桥接记录包含候选文件、SHA-256 和 Arbor node id；重复执行不会重复创建节点。完整命令与接入边界见[开源组件集成说明](./integrations/README.md)。

运行 ResearchStudio 的真实多源论文检索：

```powershell
python -m pip install -e ".[research]"
python -m popper vendor paper-search integrations\runs\my-scoop `
  --query "mechanism query" --query "adjacent problem query" `
  --start-year 2024 --end-year 2026 --trusted-local
```

对 Idea candidate 执行可恢复的 Scoop Check，并把文献证据接入 Arbor：

```powershell
$env:POPPER_API_KEY = "你的密钥"
python -m popper vendor scoop-run integrations\runs\my-idea integrations\runs\my-scoop `
  --base-url https://你的服务/v1 --model 你的模型名 --trusted-local
python -m popper vendor scoop-status integrations\runs\my-scoop
python -m popper vendor scoop-to-arbor integrations\runs\my-idea `
  integrations\runs\my-scoop integrations\runs\my-arbor
```

全文不可达时 Scoop 状态为 `provisional`，默认禁止进入 Arbor；只有明确传入 `--allow-provisional` 才能保留该降级状态并继续。

将 Arbor pending node 映射到 Popper 中已预注册的候选配置，并执行开发集评估：

```powershell
python -m popper vendor arbor-evaluate integrations\runs\my-arbor <实验项目目录> `
  --node n1 --candidate-index 0 --trusted-local
```

候选索引来自 `experiment.json` 的 `candidates`。适配器先取得基线开发集结果，再执行指定候选，由 Popper 控制器计分，并把原始分数、按指标方向统一的改善量、baseline/candidate run ID 回写到 Arbor。该命令只允许 `dev`，不能消费最终测试。

也可以让 BYOK 模型判断 Idea 是否能由现有候选配置检验：

```powershell
python -m popper vendor arbor-dispatch integrations\runs\my-idea `
  integrations\runs\my-scoop integrations\runs\my-arbor <实验项目目录> `
  --node n1 --base-url https://你的服务/v1 --model 你的模型名 --trusted-local
```

模型只能返回一个已注册 `candidate-index`，不能创建配置或修改代码。如果现有候选无法检验 Idea，命令返回 `not_implementable`，保留 pending node 且不消耗实验预算。

把 Arbor 树与 Popper 开发集证据投影为 AI-Research-SKILLs autoresearch 的长期研究记录：

```powershell
python -m popper vendor research-snapshot integrations\runs\my-arbor <实验项目目录>
```

输出位于 Arbor 运行目录的 `.popper-integration/autoresearch`，包含 `snapshot.json`、`research-log.md` 和 `findings.md`。内容由实际节点、分数、运行 ID 和输入哈希生成，不由模型补写实验事实。

使用 AI-Research-SKILLs 的 ML training recipe 生成受控代码提案：

```powershell
python -m popper vendor code-propose <idea-run> <scoop-run> <proposal-run> <实验项目目录> `
  --base-url https://你的服务/v1 --model 你的模型名
```

该命令只生成 `proposal.json` 和 `proposal.diff`。模型只能修改 `experiment.json` 已登记的 Python `code_files`，每项修改必须绑定原文件 SHA-256，并通过路径、大小和 Python 语法检查。工作项目不会被修改，结果状态固定为 `review_required`。

审查 diff 后，将 proposal 物化为隔离的双版本实验：

```powershell
python -m popper vendor code-materialize <proposal-run> <实验项目目录> `
  --config-index 0 --approved
```

`config-index=0` 表示用原实验 baseline 配置比较原始/补丁代码，1 及以后依次表示原实验 candidates。输出项目位于 `<proposal-run>/variant-project`，含 baseline/candidate 两套源码、来源清单和新的 Popper 协议。之后可运行：

```powershell
python -m popper vendor arbor-evaluate <arbor-run> <proposal-run>\variant-project `
  --node n2 --candidate-index 0 --trusted-local
```

物化器会重新验证 proposal、源代码 SHA-256、Python 语法及 `proposal.diff` 一致性。原项目保持不变。

## 可选 BYOK 候选选择

默认搜索按已注册候选队列执行，明确标记为 `registered_queue`。如需让兼容 OpenAI Chat Completions 的模型根据开发集结果选择下一个已注册候选：

```powershell
$env:POPPER_API_KEY = "你的密钥"
python -m popper experiment search <项目目录> --trusted-local `
  --base-url https://你的服务/v1 --model 你的模型名
```

发送内容仅包括研究目标、尚未尝试的候选配置和开发集指标摘要；测试集反馈不会发送。模型不能创建未注册配置。远程接口必须使用 HTTPS，本机接口可以使用 HTTP。

## 科研链路恢复与契约边界

Campaign 的 Idea 阶段会读取已冻结的指标、基线、种子和改善阈值，要求候选绑定相同的 `evaluation_contract`。结构化契约或预测指标不匹配时最多修正一次；再次失败就停止，不保存为有效候选。缓存候选也要通过当前契约检查。文本指标检查是保守规则，不能替代对科学假设的语义审查。

模型 JSON 客户端遇到格式错误或输出截断时最多重试一次，输出上限由 8000 增至 16000；认证错误立即停止；瞬时连接错误及 429/502/503/504 与格式错误共用最多两次尝试的上限。对应运行目录的 `model-diagnostics` 保存脱敏响应、`finish_reason`、usage 和尝试次数，不保存请求凭据。Scoop 初筛每批最多 5 篇，并按输入指纹保存批次结果，恢复时复用已通过校验的批次。

论文检索适配器使用 8 秒连接超时、15 秒读取超时和单次 HTTP 尝试；每个来源进程最多运行 60 秒，超时后自动终止该来源并保留其他来源结果。来源失败和限流信息保留在检索警告中。此降级不会把摘要检索升级成全文验证，全文不足时仍为 `provisional`。

## 全文获取与证据不足后的恢复

全文解析器支持 DOI 跳转后直接返回的 PDF、出版社页面的 `citation_pdf_url`、登记的开放获取地址，以及 Crossref 的公开 PDF 链接。按文件内容识别 PDF，不再要求 URL 以 `.pdf` 结尾；403、登录限制和非公开用途链接仍保留为未获取。可使用本机 `pdftotext`，或安装 research 可选依赖使用 `pypdf`。

旧版 provisional 报告会自动重新检查一次全文入口。之后需要主动重试时，使用 `popper vendor scoop-run ... --refresh-fulltext` 或 `popper research campaign run ... --refresh-fulltext`；此前的 step5–7 与状态会归档到运行目录的 history，新全文产物放在独立修订目录，原始候选和检索结果保持可追溯。

全文核验必须返回实际原文中的连续片段；不匹配时最多纠正一次，仍不匹配就不授予 fulltext 状态。模型不能改写抓取器的产物哈希，文献比较必须完整覆盖选中论文且 closest_paper_id 必须属于该集合。

在模型研究模式下，全文证据不足会返回 `retryable` 并停止，不能用一次空审批跳过该阶段。只有真实存在 `review_required` 代码提案才出现物化审批；无模型的预注册配置队列仍可正常运行。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖正常闭环、证据重算、数据划分重叠、路径越界、错误/自报指标、输入与证据篡改、预算、恢复以及最终测试不可重复使用。

## 项目结构

- `popper/core.py`：实验契约、执行、控制器计分、状态机与证据重算。
- `popper/proposer.py`：可选 BYOK 候选选择器。
- `popper/server.py` 与 `workstation.html`：仅绑定本机的实验工作台。
- `examples/quadratic`：合成工程演示。
- `examples/throughput-sort`：staged_artifacts 形状试点（算法吞吐；候选只报告原始测量，指标由域包派生）。
- `examples/ode-convergence`：staged_artifacts 序列测量试点（数值收敛阶；候选只报告误差序列，阶数由域包拟合派生）。
- `examples/breast-cancer-wisconsin`：scikit-learn 真实公开数据二分类接入。
- `reproductions/street-1993-wdbc`：Street 等人 1993 年论文中三特征、单分离平面、10 折准确率 claim 的方法近似复现。
- `popper/reproduction.py`：通用 `ReproductionTask` 契约，冻结论文与协议输入，统一执行、产物哈希和领域核验器。
- `popper/vendors.py` 与 `integrations/vendors.json`：开源组件指纹登记、受限执行、Idea/Scoop→Arbor 桥接和 Arbor→Popper 开发集执行。
- `popper/scoop.py`：ResearchStudio Scoop Check 七步协议、全文证据和 novelty verdict 的可恢复编排。
- `popper/code_proposal.py`：AI-Research-SKILLs 指导下的哈希绑定、语法校验、只生成 diff 的代码提案门。
- `popper/code_variant.py`：将已审核 diff 物化为原始/候选双版本的隔离 Popper 实验。
- `prototype-fusion.html`：原始产品交互原型，保留作设计参考。
- `skills`：前期整合的科研 skills 规划；实际运行时接入以 `integrations/vendors.json` 为准。
