# Popper

Popper 是一个本地优先的科研实验推进 Agent。它在显式登记的实验协议下复跑基线、探索候选、生成不可变代码版本、执行开发实验，并把结论绑定到可重算的逐样本证据。

参与开发请先看 [贡献指南](CONTRIBUTING.md)、[架构](ARCHITECTURE.md) 与 [路线图](ROADMAP.md)。仓库尚未确定并加入正式 `LICENSE`，因此目前不把它描述为已完成的开源发布。

从构建出的 wheel 安装后，可用内置的非 ML、无需模型 API 的示例立即开始：

```powershell
$wheel = (Get-ChildItem .\wheelhouse\popper_research-*.whl | Select-Object -First 1).FullName
python -m pip install $wheel
popper demo init .\ode-project --case ode-convergence
popper research init .\ode-project .\ode-run
popper research run .\ode-run --trusted-local
popper research status .\ode-run
popper serve .\ode-project --research-run .\ode-run
```

另一个内置示例为 `--case throughput-sort`。`--trusted-local` 会执行示例中的本地 Python 代码，只用于可信项目；这两个示例不构成新的科学发现。
维护者修改 `examples/ode-convergence` 或 `examples/throughput-sort` 后，需运行
`python scripts/stage_demos.py` 更新随包模板；CI 会检查模板与源码一致，并从 wheel
独立安装后运行两个示例的一步研究流程。
CI 还用安装后的 wheel 完成一次非 ML 实验确认、导出含原始预测的复现包，
在另一目录解压并执行 `experiment replay --portable` 重算已确认结论。

发布 wheel 内含预构建的工作台页面和 JS/CSS 资源；从 wheel 安装并运行 `popper serve`
不要求目标机器安装 Node.js。开发者修改 `frontend/` 后需运行 `npm ci --prefix frontend`、
`npm run build --prefix frontend` 和 `python scripts/stage_frontend.py` 更新包内资源。
CI 会重建前端、检查暂存资源是否一致，并实际构建 wheel 验证页面资源完整。
源码分发包（sdist）也包含同一组资源；CI 会从 sdist 再构建 wheel 验证这一发布路径。
CI 还会把 wheel 独立安装到临时目录，启动真实本地 HTTP 工作台并请求首页、编译资源和
会话端点，防止“压缩包里有文件但安装后无法服务”的假阳性。
Windows 可选依赖 CI 还会从 wheel 独立安装 Popper，经 HTTP 终端接口启动真实
ConPTY/PowerShell、写入工作区文件并关闭会话；缺少后端时测试失败，不记为通过。
终端的 xterm.js 与终端样式作为懒加载块，只在打开终端视图时下载；打包检查覆盖全部
编译资源（包括不直接写在 `index.html` 里的懒加载块），暂存脚本也会清理旧前端构建
缓存，避免增量 wheel 混入过期 JS/CSS。
CI 会上传本次构建的 wheel、sdist 和逐文件 SHA-256 清单，便于复查具体构建产物；
该清单不是依赖 SBOM、签名来源证明或正式发布。

当前版本仍是工程原型。预注册代码可以选择可信本地或 OS 原生沙箱（Windows 低完整性 + Job Object，或 Linux bubblewrap 命名空间）；模型生成代码强制进入沙箱 worker，不能降级为可信本地执行。该沙箱限制写入和进程资源，并在 `--sandbox` 消费测试集时对已注册的测试数据集施加内核级读隔离（Windows 强制完整性 `NO_READ_UP` 下读它得 `PermissionError`；Linux 以 `--ro-bind /dev/null <path>` 遮蔽，读它得空内容），候选进程读不到保留集标签；其余本机读取仍未隔离，因此合成示例和开发集结果不算科研创新或盲测证明。Linux 后端要求 `bwrap` 能真的建立非特权用户命名空间（Ubuntu 23.10+/24.04+ 需为 `/usr/bin/bwrap` 开 AppArmor `userns` 例外），可用性由实跑一次沙箱探活得出，不满足时 `--sandbox` 显式报错而不是静降级；macOS seatbelt 尚未实现。

离线跨领域内核基准可运行 `popper research benchmark <新输出目录> --trusted-local`，随后运行
`popper research verify-benchmark <输出目录>`。默认在同一 Research Kernel 上执行内置
ODE 收敛与排序吞吐案例，保留项目、研究运行目录、可用时的 Claim 证据附录及机器可读
`benchmark.json`。报告包含真实终态、预算、Claim 裁定、输入/研究清单摘要与描述性耗时；
吞吐案例可能因实测噪声没有 Claim，此时明确记录零 Claim，不生成虚假附录。
这不是模型能力或科学优越性基准，`--trusted-local` 会执行内置候选代码；不同机器的
耗时不能直接排名。校验依赖保留的本地研究目录和源制品，并非第三方便携复现证明。
以 `--trusted-local` 启动的工作台可在「假设环」中后台运行同一基准。请求先写入当前工作区
`.popper/benchmark-jobs.sqlite` 的幂等 Job 与哈希事件链，输出存于
`.popper/benchmarks/<运行名称>/`；页面可在刷新后查看真实状态和通过重验的报告。服务重启
会继续尚未领取的排队 Job；对上次运行中的 Job，只有完整报告通过原始证据核验才补记成功，
否则标为 interrupted，不自动重跑或覆盖部分输出。

## 运行证据驱动 Research Controller

先初始化一个普通 Popper 实验，再建立独立研究运行目录：

```powershell
python -m popper experiment init <实验项目目录>
python -m popper research init <实验项目目录> <research-run目录>
python -m popper research run <research-run目录> --trusted-local
python -m popper research status <research-run目录>
```

Agent 新建的设计若已冻结、评估协议一致，且干预精确匹配现有预注册候选，
可单独在沙箱中执行（会消耗开发预算；不修改原始候选集合）：

```powershell
python -m popper research execute-design <research-run目录> --design <已冻结设计ID>
```

若要研究**全新配置**，须明确提供新的独立确认集，在新 Project/Study 中重新预注册；
不能向旧 Study 追加候选或沿用它的测试数据：

```powershell
python -m popper research followup <父research-run目录> <新项目目录> <新research-run目录> `
  --candidates-json <候选配置列表.json> --new-test <独立确认集.json> --budget 1
```

命令在写入前检查父研究身份、新候选和确认集与父实验各数据划分的摘要及样本不重叠；
新项目冻结 `followup-lineage.json`，新 Study 清单和工作台会显示父 Study 与新确认集摘要。
在 `--trusted-local` 工作台的“假设搜索环”中，也可从已登记父 Study 的“创建后继 Study”表单完成同一流程：先读取父清单摘要，再填写新候选 JSON、预算和独立确认集路径。工作台只接受父项目同级目录树内已有的确认集文件；新项目和运行目录以名称分别创建在父项目、父 run 的同级目录，并持久登记到工作台，重启后仍可见。若项目创建后 Study 初始化失败，保留新项目及其预注册输入以供检查和 `research init` 恢复，不会暗中删除。
工作台对相同请求的重复提交会核验已创建项目、血缘、数据摘要和 Study 事件链后返回同一 Study；仅项目已建立而 Study 尚未建立时，会在核验后继续初始化。目录相同但请求内容不同会拒绝，不会覆盖已有研究。
这是一项自适应后继研究，父研究证据只作探索背景；命令不保证新候选代码能成功执行，
更不把它追认为父研究的原始预注册结果。摘要与样本不重叠只能验证数据差异，
不能证明新确认集此前从未被研究者或模型看过；其来源与隔离仍需独立审计。

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

如需工作台右侧的真实终端（Windows ConPTY + xterm.js），安装 workstation extra，并以可信本地模式启动。该模式会向浏览器会话开放当前已登记工作区根中的 PowerShell，只能用于你明确信任的本地目录：

```powershell
python -m pip install -e ".[ml,workstation]"
python -m popper serve examples\breast-cancer-wisconsin --trusted-local
```

工作台只在同时满足可信本地授权、Windows、pywinpty 和 PowerShell 可用时启用终端入口；否则入口禁用。界面采用项目/会话侧栏、持续可见的 Agent 对话、右侧资源标签和底部工具面板；交互终端、运行日志与事件审计分别呈现。文件、证据和稿件通过标签打开，操作指南见 [工作台使用与验收](docs/workbench.md)。

稿件编辑器只写回已登记的 `.md/.tex/.json` 产物。读取时返回文件字节 SHA-256；保存必须提交该摘要，若文件已在其他窗口或进程中变化则返回冲突、保留当前编辑内容，不静默覆盖。服务端在当前工作台锁内核对摘要并使用同目录临时文件原子替换。切换稿件、离开编辑器、切换工作区或刷新页面前会提示未保存内容；外部进程恰在核对与替换之间写入的竞态仍需文件系统级协作锁才能完全消除。

终端是真实的 PowerShell 子进程，不是命令日志视图。单个工作台最多同时保留 4 个终端
会话；关闭面板会结束并释放会话，读线程异常后仍会尝试回收子进程。终端尺寸在分配
ConPTY 前校验。切换工作区后新建的终端从新当前根启动，已有终端仍留在原根；请求不能
自行指定未登记路径。附加根仍有终端会话时，工作台拒绝摘除该根并要求先关闭终端；
终端会话只在服务进程内存中存活，服务重启不会恢复交互式 shell。
服务端仅保留最近 2048 个输出块且最多 262144 个字符；持续海量输出会丢弃较早内容，
前端收到游标缺口时清空旧视图并明确提示截断，不把缺失输出伪装成完整终端记录。
若浏览器异常断开而未送达关闭请求，服务端会在连续 15 分钟无读取、输入或尺寸更新后
结束会话；后台每 30 秒检查一次，正常打开的面板通过轮询保持活跃。
若 PowerShell 子进程终止失败，服务端不会把会话伪报为已关闭或释放名额；句柄保留，
后续关闭与空闲回收可重试。

## 立即查看真实 ML 接入

仓库中的 `examples/breast-cancer-wisconsin` 使用 scikit-learn 内置的真实公开诊断数据，并已完成一轮实验。启动只读工作台：

```powershell
# 在仓库根目录（含 pyproject.toml 的目录）执行
python -m popper serve examples\breast-cancer-wisconsin
```

打开 `http://127.0.0.1:8765/`。只读模式可以查看真实运行、结论、证据和审计事件，不会执行项目代码。

同一项目或显式登记的附加工作区根只能由一个本地工作台实例持有；重启时也会重新锁住
注册表里持久保存的附加根。第二个实例会在启动恢复
任何 Turn、论文复现或 revision Job 之前报错；进程退出后操作系统释放锁。关闭监听时若
后台作业尚未结束，工作区锁保持到作业写入收尾，避免新实例把活跃工作误记为中断。

如需在工作台里自由询问当前实验、指标或结论，可接入 OpenAI 兼容的 BYOK 对话端点；key 只从环境变量读取。远程端点必须使用 HTTPS，本机端点可使用 HTTP：

```powershell
$env:POPPER_API_KEY = "你的 key"
python -m popper serve examples\breast-cancer-wisconsin `
  --chat-base-url https://api.example.com/v1 --chat-model your-model
```

如需每次启动都使用同一提供商，可将 `POPPER_API_KEY`、`POPPER_CHAT_BASE_URL`、
`POPPER_CHAT_MODEL` 设为当前用户的持久环境变量，然后直接运行 `python -m popper serve <project>`。
命令行中的 `--chat-base-url`、`--chat-model` 仍可临时覆盖默认值。密钥不要写入仓库；
变更持久环境变量后，需重启启动 Popper 的终端和运行中的 Popper 服务。

可信本机模式的「假设环」可从当前已执行 `popper init`、仍处于 `searching` 阶段的
预注册实验建立首个 Study。先预览项目、研究目标、指标、候选数、预算和输出目录，
再单独确认；服务端重新核对冻结输入及提案摘要后初始化 Research Kernel，并把研究
运行目录持久登记到工作台。这个动作不执行实验，也不会替用户编造研究协议。非 ML
领域包使用同一入口；尚未预注册的项目仍须先准备并初始化 `experiment.json`。

可信本机模式的工作台「项目代码」页可浏览登记工作区内的 UTF-8 文本源码并保存修改。
写回前须先审查服务端生成的文件差异；保存要求文件的原始 SHA-256 未变化，外部修改
会返回冲突而不会覆盖。保存保留 UTF-8 BOM 与原文件权限。已冻结的实验输入
在此页只读，应通过 Revision 流程修改。该接口只在 `--trusted-local` 下开放，不创建新文件，
并限制文件类型、大小和遍历范围；普通远程模式不可用。

对已冻结的预注册 Python 源码，「项目代码」页另提供 Revision 草稿：选择当前 Study
中未测试候选的冻结设计，在独立草稿中编辑并预览服务端差异，再显式确认创建不可变
初始 CodeRevision。确认动作绑定源码 SHA、Study 版本、设计与提案摘要；它不覆盖
原项目文件，不执行实验、不消耗预算，也不生成 Observation。创建后还须在项目代码页
或假设环的 Revision 详情中单独确认执行；执行进入持久 Job 队列，由 Research Controller
沿现有预算、沙箱、Run、评分与 Observation 路径处理，可查看状态和取消。非预注册文件
不能借此入口创建 Revision，创建成功本身不能当作实验完成。

可信本机模式下，对话 Agent 还可提议 `workspace.file_list`、`workspace.file_read` 和
`workspace.file_replace`。文件列表与源码正文须用户批准后才会发送给模型；替换仅允许
旧文本唯一匹配、文件 SHA-256 未变化且未冻结的源码。审批卡展示服务端生成的 diff，
批准后写入的结果由独立文件编辑回执绑定工具调用 ID；进程在写盘与完成工具事件之间
中断时，会用回执和文件摘要进行启动恢复。该能力不代替研究 Revision：已冻结的实验
输入仍不可原地修改，也不会因 Agent 编辑而产生 Observation 或 Claim。

Agent 会话与 turn 保存在当前工作区根的 `.popper/sessions.sqlite`；切换工作区根会切换到该根自己的会话。v1 Turn 在模型调用前先持久化为 `running`，立即返回 `202 Accepted` 并由受管后台作业执行；界面状态来自持久化记录而不是临时动画。用户可停止运行中的 Turn，取消会原子写成 `interrupted/Cancelled`，迟到的模型回复不能覆盖该终态。每次生命周期转换还会在同一事务追加 `turn.started/completed/failed/cancelled/interrupted` 事件，可通过 `GET /api/v1/sessions/{session_id}/events?after={cursor}&limit={n}` 增量审计。Turn 事件采用与 Research、worktree、执行 Job 和论文复现相同的规范化前向哈希链协议；读取接口返回完整性结果，并核对事件重放终态与 Turn 快照。新写入的 Turn 事件还绑定用户消息、助手终稿、错误及身份字段的快照摘要；模型历史组装前会复核，不一致时拒绝调用模型。工具调用另有同事务写入的 `tool_events` 前向哈希链，绑定工具名、参数、审批、状态、结果和错误等完整快照；工具执行入口及模型历史读取均验证它。`GET /api/v1/sessions/{session_id}/tool-events?after={cursor}&limit={n}` 可分页查看工具状态、actor、trace、时间和摘要及链完整性，不复制工具结果正文。旧 Turn 事件和旧工具调用没有这些内容摘要，仅保留原有兼容校验，不宣称历史内容已验证。哈希链未签名，也不能防止拥有数据库完全写权限者重写整条链；需要外部可信锚点才能证明防恶意重写。完成、失败或进程重启后的 interrupted 状态均如实保留，带客户端幂等键、actor 与 trace；旧 `/api/chat` 仍保留同步兼容语义。旧 `.popper/chat.json` 会一次性兼容导入。自由对话不写实验事件链，不会推进阶段或改动评测数字。未配置端点时，工作台如实显示未配置并禁用发送，不生成伪回复。

原生 OpenAI/DeepSeek Harness 使用 SSE 增量响应。文本 delta 会持续写入 turn 的 `assistant_draft` 并由工作台实时投影，因此刷新页面不会丢失已生成内容；点击停止会协作关闭底层 HTTP 响应。流式工具调用按 OpenAI `index` 重组后再进入结构化审批状态机，不执行未完成的 JSON 参数。

工具提议必须经服务端校验；写工具需人工批准。`research.status` 只接受已显式登记的 Study ID，读取真实研究状态摘要（状态、预算、实体数量、溯源图完整性），不会执行实验或写研究库。由于结果可能披露未公开研究内容，该工具虽只读仍需用户在工作台批准。一个 Turn 的全部工具进入完成、失败、拒绝或取消终态后，运行时自动排队一个持久化的模型继续推理 Turn，以匹配 `tool_call_id` 的原生工具消息反馈结果，不伪造用户消息；重复请求只会重放同一个继续推理 Job。待批准或尚在运行的调用不会被伪装为已执行。预算不足时工具结果仍保留，用户可调整预算并在工作台手动继续。当前另有两个 Quest 工作区工具，这还不是可自主执行科研实验的通用 Agent。

`research.freeze_design` 是第一个经 Research Kernel 执行的 Agent 写工具：模型只能提议已登记 Study 的设计 ID 与期望状态版本；工作台从研究库只读投影显示权威设计内容和摘要供人工核对，批准后交给 `ResearchStore.execute_command` 的版本、状态机、事件和同事务幂等收据门禁。它仅冻结设计，不运行实验或生成 Observation/Claim；版本变化会拒绝陈旧提议。进程若在研究命令提交后、ToolCall 记账前退出，重启仅在收据与原命令匹配且研究事件链完整时补记完成，不再次应用科研写入。

工作台的「范围限定结论」卡片可在 Study 终结后显式核验证据并下载 JSON/Markdown 附录。该只读入口每次从已登记 Research Run 重新核对事件链、冻结输入和 Claim 来源；下载内容与 CLI 的 `research materialize evidence` 导出一致。下载后的文件不是独立审稿或便携复现包，研究状态改变后应重新生成。

以 `--trusted-local` 启动的工作台还可从同一卡片创建证据绑定草稿：自动且恰好一次纳入当前 Study 的全部 Claim，作者说明始终标为未验证。服务端将草稿结构、证据附录、生成稿件和绑定清单存放在项目 `.popper/research-bound-drafts/` 下，重复提交同一结构会逐项重验并返回既有草稿；文件被改动不会被静默覆盖。该入口只创建 `draft_needs_review`，不会自动审校、投稿或登记发表。

已保存的证据绑定草稿可在工作台重新打开；每次打开都会重新核验稿件与原始研究证据，并检查稿件流程状态与事件链一致。人工审校登记需要填写说明、明确确认，并在写入本地状态前再次通过同一校验；该状态不构成真实投稿或发表证明。

已批准的冻结设计可进入持久化执行 Job。工作台可取消排队中的 Job，或对运行中的 Job 发出持久化取消请求并转交 Worker；运行中的取消是协作式的，界面会显示“停止中”。若请求到达前权威 Run/Observation 已完成，仍报告成功；否则 Job 进入 cancelled，已经登记的研究预算按原有结算规则处理。取消、重启和终态都可由设计 Job 事件链校验。
已取消的 Job 可由用户显式重试；每次重试递增 `attempt`，请求携带预期次数与冻结设计摘要，重复提交同一次重试不会再启动一次实验。服务端在重新排队前复查当前研究状态和执行条件，而不是自动重放已取消的工作。

`research.create_hypothesis` 允许模型向已登记且 active 的 Study 提出领域中立、具备适用范围、可检验预测、证伪条件和替代解释的假设。工作台在人工批准前展示 Study 与提案摘要；研究库在同一事务中生成假设 ID、登记 `untested` 快照、追加事件并写入幂等收据。它不会把假设标为已获支持，不创建 Run、Observation 或 Claim；状态版本冲突会拒绝写入，重启只凭匹配收据补记已提交结果。

`research.create_design` 允许模型针对同一 Study 内当前 `untested` 假设提出领域中立的实验设计草稿。提案必须声明假设版本、干预、对照、基线、指标与方向、评分器、分析单位、评估范围、重复取值和最小有意义效应；服务端与 Research Kernel 分别校验，批准后原子生成设计 ID、`draft` 快照、事件与幂等收据。结果显式标为 `execution_ready=false`：草稿本身不会启动 Run，也不能当成可运行实验。冻结后仅当干预精确匹配已有预注册候选、评估协议一致且运行门禁通过，才可使用受限执行入口；任意新配置尚不能从该入口运行。

模型可先提议 `research.inspect_design`，在用户批准披露后读取已登记 Study 中单个设计的权威内容、对应的真实假设、状态版本与 SHA-256 摘要，再判断是否应提议 `research.freeze_design`。若假设已修订，结果明确标注设计引用的版本是否仍为当前假设版本。请求必须绑定预期研究状态版本；审批后若版本变化，拒绝披露并要求重新提议。未登记 Study、其他 Study 的设计和文件路径均不能作为读取目标；查看操作不写研究库。
Agent 的 `research.status`、`research.inspect_design` 与开发 Observation 读取在披露前统一用只读连接重放研究事件链与评分制品，并核对冻结清单、来源图及读取前后的状态版本。`research.status` 的待披露摘要会先显示在审批卡；校验失败时界面禁用批准，服务端即使收到误批准也不会返回伪造内容。被外部改动但仍可被普通状态投影显示的快照不会作为可信模型上下文返回。

`research.inspect_claim` 允许模型提议读取已登记 Study 的单条 Claim。审批预览和批准后的工具结果均由研究库、来源图与原始评分制品重新推导证据绑定，并携带验证级别与适用范围限制；不把原始输入交给模型。执行时重新检查预期 Study 版本，若证据读取期间状态变化或来源图失效则拒绝披露。它是证据查看入口，不会产生新 Claim，也不证明机制或超出预注册范围的泛化。

对仍在进行的研究，`research.status` 只列出有限数量的开发范围 Observation ID；模型可再提议 `research.inspect_observation` 读取单项开发评估。审批预览与执行均拒绝 `confirmation` 等非开发范围，即使用户误批准也不会把保留集数值送入模型后续推理。其余开发范围读取会重验研究事件链、冻结清单、来源图和评分制品，并要求预期状态版本一致。工具只返回结构化数值、范围、来源 Run、制品摘要与限制，不返回评分制品路径或原始输入。开发 Observation 不能当作最终 Claim；已终结结果只能走范围限定的 Claim 证据入口。
对话的自动系统上下文也只注入 `dev` 运行摘要，不逐条注入确认/测试集 Run 的分数或逐样本结果；终结后的范围限定结论由 Claim 证据入口单独核验。
自动系统上下文在注入开发指标前以只读方式执行实验 `replay`：重验事件链、冻结输入、评分器、运行工件和保存的预测。若数据库投影与事件不一致，拒绝本次模型调用，也不留下悬挂的 Turn；普通状态投影中的数字不会直接被当作模型事实。
研究证据附录生成与 Agent 的 Observation 校验使用 ResearchStore 的只读 SQLite 连接，不在读取时创建数据库或执行 schema 迁移；旧库若需迁移，必须通过显式写入路径处理。

设计查看还附带只读 `controller_routing_readiness` 审计：分别列出原自动 Controller 路由和已注册候选窄入口的前置条件，核对干预/对照、评分绑定、重复协议、开发范围、冻结状态、预算及是否已有同配置证据。两个“路由就绪”字段都**不是执行许可**；输入、Evaluator 和沙箱仍须执行时核验，因此 `execution_ready` 保守地保持 false。Agent 新建草稿通常会显示未冻结，不能被误认为可运行。

`research.execute_design` 只让模型提出执行请求，必须由用户核对设计摘要、Study 版本、预注册匹配与预算影响并批准。批准后工具返回持久 Job，而不是 Observation 或结论；后台由 Controller 再次核验并在沙箱中执行。工作台设计卡也提供同一受限入口及 Job 状态。新配置仍须创建新的预注册实验，不能改写既有研究清单或重复登记同一开发结果为独立证据。

对这两个工具，若进程恰在工作区命令提交后、ToolCall 完成记录前退出，重启时只凭匹配原命令的持久化幂等收据补记完成，绝不重放写入。缺少或冲突的收据会记为 `OutcomeUnknown`，提示检查工作区状态；收据仅是本地工作区文件中的事务记录，不是防恶意篡改的密码学证明。

异步模型调用由 `.popper/sessions.sqlite` 中的持久化 Agent Job 调度。Turn 与 queued Job 原子创建，Job 记录稳定 `job_` ID、状态、attempt、worker identity 和 lease；本地服务器默认最多同时执行两个模型 Job。排队任务可以在调用模型前取消。进程重启只自动续跑从未领取的 queued Job；已经 running 的调用会明确记为 interrupted，不做可能重复计费或副作用的静默重试。

Worker 会在长调用期间续约 lease，其他 worker 不能冒用身份续约。模型 provider 在 SSE 最终帧返回的 prompt/completion/total token 会写入 Job 并聚合到当前 Session，工作台显示真实累计 token；provider 未提供 usage 时记录为 0，不做不可靠估算。当前尚未内置各厂商价格，因此不会把 token 猜算成货币成本。

失败或中断消息可以在工作台显式重试。`POST /api/v1/turns/{turn_id}/retry` 不修改旧 Turn，而是原子创建带 `parent_turn_id` 的新 Turn 和 queued Job；请求要求新的幂等键，重复提交不会再次调用模型。重试链最多 10 层，旧错误、流式草稿、事件和用量始终保留用于审计。

模型生成期间也可以输入新指令并选择“转向并发送”。`POST /api/v1/turns/{turn_id}/steer` 会在一个事务内把旧 Turn 终结为 `Steered`、取消旧 Job，并创建关联的新 Turn/Job；事务提交后协作关闭旧 HTTP 流。已有草稿仍可审计，迟到的旧回复不能覆盖新方向，同一转向幂等重放不会重复调用模型。

每个 Session 可在工作台设置 token 硬额度，也可留空表示无限额。`POST /api/v1/sessions/{session_id}/budget` 持久化额度；入队事务按完整上下文、工具 schema 和最大输出计算保守 token 预留，只有 `已消费 + 活跃预留 + 新预留 <= 额度` 才创建 Turn/Job。额度不足返回 `budget_exhausted`，不会调用模型或留下半成品记录；Job 取消或终结会释放预留，并以 provider 的真实 usage 结算。

价格由管理员显式登记，不随代码发布硬编码。`POST /api/v1/pricing-catalogs` 接收目录名称、三字母货币代码以及逐模型输入/输出每百万 token 的微货币单价，生成不可变、内容寻址的 `prc_` ID；`POST /api/v1/sessions/{session_id}/pricing` 在首个 Job 前绑定目录。每个 Job 快照模型和目录，最终 `cost_micros` 只按真实 provider usage 结算；后来登记的新目录不会改变历史费用，已有 Job 后也不能切换目录或混合币种。

绑定价格目录后，可通过 `POST /api/v1/sessions/{session_id}/cost-budget` 或工作台设置微货币硬额度。每个 Job 按保守 token 上界和目录中的较高费率预留费用；只有 `已结算费用 + 活跃预留 + 新预留 <= 额度` 才能入队。取消、失败或成功终态都会释放预留并以真实 usage 结算；额度不足不会创建 Turn/Job，也不会调用模型。

工作台对话通过 Conversation Harness v1 接入模型。Harness 描述符公开名称、显示名、协议版本、模型与 `text/streaming_text/cooperative_cancel/native_tools/provider_usage` 能力；Session 固定所用 Harness 及版本，工作台可查看。执行只调用统一 `invoke` 协议，不再依靠 `hasattr` 猜能力。旧 callable 通过兼容适配器接入；历史未版本化 `chat` Session 只允许一次性迁移绑定，配置不匹配时拒绝执行。

研究工作台的每个新 Research Run 会原子绑定不可变 CodeRevision 及其 manifest
SHA-256。`GET /api/research` 同时返回 `experiment_graph`，把假设、设计、代码修订、
运行、观察和决策连接成可审计溯源图。读取时会重算 revision 身份和代码文件摘要；
历史未绑定运行或任何工件篡改都会显示为 `integrity.issues`，不会被界面静默修复。
Git 项目中的新 revision 同时固定 HEAD commit、项目相对路径与源文件脏状态摘要；
非 Git 项目明确标记为 `unversioned`。独立服务完成的外部确认则绑定已验签 submission
及 control/candidate 代码清单摘要，以外部源码节点进入同一张图，不冒充本地 revision。
新 revision 的 unified diff 也进入内容身份并随 manifest 冻结。工作台可从实验图打开
任一 CodeRevision 查看经服务端重新校验的完整差异；历史未保存 diff 的 revision 明确
显示不可用，不会根据当前工作目录临时重算。

以 `--trusted-local` 启动时，工作台可为带 Git 锚点的 revision 创建隔离编辑副本。
Popper 从固定 commit 创建 detached worktree，覆盖并校验 revision 文件；创建使用
幂等键，生命周期跨服务器重启持久化。移除操作只允许作用于 Popper 专用临时根中已
登记的 worktree。编辑副本不会改变原 revision，也不会自动成为可执行科研事实。
可信本地模式重启时会核对遗留 `provisioning`：Git 锚点与全部冻结文件摘要均吻合才补记
`active`，否则记为 `failed` 并保留现场供检查；相同幂等键不会再返回永久悬置状态。
检查后可在工作台显式清理失败副本；已有目录必须仍是同一 Git worktree 且 commit 匹配，
否则拒绝删除，避免把路径上后来出现的其他数据误删。
若清理恰在 Git 移除后、事件提交前中断，可重试“移除”；只有 Git 已无该路径的
worktree 登记时才补记 `removed`，残留 Git 登记不会被伪装成成功。
编辑完成后必须先“冻结提案”：仅允许修改父 revision 文件或新增 Python 文件，删除、
重命名、非代码文件和项目外修改都会被拒绝。批准动作绑定提案摘要，并从冻结正文创建
不可变后继 revision；冻结后的晚到编辑不会混入。后继 revision 仍需经过正常 Run、
独立 Evaluator 和证据门禁，不能因为人工批准就直接成为 Observation 或 Claim。
worktree 的创建、激活、提案冻结、审批提交、失败与移除均写入前向哈希事件链，并与
当前快照原子提交。工作台会验证事件链和状态重放；事件或快照被外部修改时明确报告
审计损坏，不会继续把最新状态当作可信事实。验证还会核对 revision、commit、幂等键、
专用路径、冻结提案摘要和后继执行结果等快照字段；旧版迁移记录的路径缺少原始事件证明，
不能倒推其迁移前的真实性。
创建、移除、冻结、审批和后继执行在产生 Git、revision 或实验副作用前都会先验证整份
worktree 注册表；完整性失败时拒绝写入，保留现场供排查。
批准后的后继 revision 可从工作台执行，但仍经过正常预算、Research Run、沙箱 Worker、
执行覆盖检查、独立 Evaluator 和 Observation 写入。成功状态绑定真实 Run ID；若该
design 已由另一个 revision 产生观察，系统拒绝把既有结果冒充为当前 revision。人工
审批本身不会跳过实验，也不会直接产生 Claim。
执行请求不会占住 HTTP 连接：服务先持久化 `JOBR-*`，返回 `202 Accepted`，后台再从
`queued` 推进到 `running/succeeded/failed/cancelled/interrupted`。Job 保存阶段和
百分比进度并拥有独立前向哈希审计链；工作台轮询权威记录展示进度并允许取消。运行中
取消会协作传入 Research Worker，排队取消不会启动实验。服务重启会继续尚未领取的
queued Job；已经运行的 Job 明确标记 interrupted，并同步终结对应 worktree 执行，
避免静默重复产生科研副作用。

旧版本若创建时尚未把 Diff 纳入 revision identity，可以显式运行
`popper research migrate-diffs <run_dir>`，或在可信本地工作台点击“迁移旧 Diff”。迁移
只允许从不可变 parent/child 代码快照重建，并生成 `DFA-*` 内容寻址旁路证明，绑定两侧
manifest 摘要和 Diff 摘要；原 `revision.json` 一个字节也不会改。缺少不可变父快照的
旧 root revision 仍显示“不可证明”，系统禁止用当前可变项目目录补猜历史。

Agent 工具调用同样持久化：写工具先显示审批卡，未经批准不能执行；批准、拒绝、取消、失败和中断状态都可追踪。工具注册表提供 Quest、项目源码差异审查、Study 状态与证据读取，以及 Study 方案预览和持久执行 Job。Agent 经用户批准后才能提交研究运行或独立确认；返回 Job 时尚不能宣称结果，必须查询成功终态并引用真实 Observation/Claim。Agent 还可预览并请求生成包含当前 Study 全部已核验 Claim 的证据绑定草稿；作者说明明确标为未验证，创建需单独审批且只形成待审稿件。工具不能直接写入 Run、Observation 或科研结论，也不开放任意 shell。

在可信本机工作台的「假设环」中，可填写本机绝对目录导入已有 `experiment.json`，或提交协议 JSON 与已登记代码、数据文件的 UTF-8 文本映射新建项目。预览会在临时目录调用领域包校验协议与输入，显示目标、执行入口、数据划分、重复单位、指标和预算；确认后才写入缺失文件并预注册，已有文件不会覆盖。初始化中断会保留现场，协议或输入变化需重新预览。已登记项目再预览并创建 Study，然后逐次预览研究运行。每次执行返回持久 Job，页面可查看状态、请求停止，并在完成后刷新研究状态。策略请求独立确认时，页面单独列出选定候选、剩余预算和一次性消费警告；批准后由同一 Controller 在沙箱中执行。服务重启时，已排队 Job 会重新核对研究状态再启动，运行中的 Job 标记中断供审查，不会自动重试独立确认。确认生成 Claim 后，工作台可生成带证据绑定的可审校稿件草稿。

Research Kernel 不再把“重复实验”硬编码成机器学习训练种子。DomainPack 明确声明
`train_seed`、`independent_run` 或 `analysis_unit`；数据内分析单元会从各划分导出真实
区块 ID，并贯穿 ExperimentDesign、Worker、独立 Evaluator、Observation 与确认阶段。
发布验收会用 ODE 收敛阶、Wisconsin 公开数据和处理效应区块研究跑同一个 Controller
与 Provenance Graph。候选自报指标、伪造区块或把分析单元改叫 seed 都不能绕过域包计分。

本地独立确认结束后，Research Kernel 会从冻结设计、两项确认 Observation 与停止
Decision 派生不可变 Claim。Claim 记录方向一致的效应量、预设阈值及证据引用，状态仅为
`supported_in_scope` 或 `not_supported_in_scope`；其适用范围限于本次登记的独立确认，
不证明机理，也不自动推广到未登记人群、数据或条件。研究工作台可追溯这些引用；
外部签名确认和边界检验仍使用各自的裁定语义，不能被解释成这类本地 Claim。
成功的外部确认另行生成 `signed_external_positive_effect` 或
`signed_external_scope_boundary` Claim：前者判断整体方向统一效应，后者要求整体效应
低于阈值且至少两个预注册切片跨越阈值。研究库重放时重新核验签名合同、冻结提交、
回执、Run 与逐切片 Observation；工作台显示回执摘要和 `engineering_same_account`
信任级别。同账户工程签名不能当作机构独立复核。
V2 边界路径的验收测试同时覆盖真实沙箱 runner、私有保留集服务计分、签名回执、
Controller 接受、Claim 图投影与重启幂等；服务端仍保管标签，runner 只接收无标签特征。

OpenAI/DeepSeek 兼容模型通过原生 `tools/tool_calls` 提交请求。模型能看到当前真实
Workspace 的 `state_version`、文件夹 ID 和 Quest ID；模型回复与工具提议原子保存，
但提议不会自动执行，仍须在工作台审批卡中由用户确认。

干净检出不包含被忽略的 `.popper-reproduction/` 运行状态；先在新目录登记并运行冻结的 Street/WDBC 论文复现，再打开工作台。以下命令在仓库根目录执行，需安装 ML 可选依赖：

```powershell
python -m pip install -e ".[ml]"
python scripts/prepare_wdbc_reproduction.py ..\popper-wdbc-local
python -m popper experiment reproduce run ..\popper-wdbc-local --trusted-local
python -m popper serve ..\popper-wdbc-local --port 8766
```

准备脚本只复制固定哈希的数据和已登记输入，不覆盖非空目录，也不复制本机旧状态或历史输出。运行复现代码需要你信任该案例；本地数据已随仓库提供，无需首次联网下载。打开 `http://127.0.0.1:8766/` 后，工作台会显示这次运行的报告值、复现值、冻结协议、产物指纹和领域核验结果。

离线发布验收可运行 `python .github/scripts/e2e_frozen_reproduction.py`：它复制已登记输入与固定哈希的数据到临时目录，实际重跑并核验论文 claim、研究图和事件链，还验证产物篡改会被审计拒绝。冻结核验器使用 `primary_accuracy`；通用裁定层仅在缺少规范 `accuracy` 时接受 `primary_<metric>` 旧字段，并在裁定中记录来源，不改写已冻结输入。

复现任务同时投影到统一科研溯源词汇：论文来源、冻结协议、runner/verifier 代码快照、
Run、Artifact、Observation 与 Decision 组成可检查的图。领域核验器只能证明产物内部
一致，不能自行决定“复现成功”；Popper 核心独立读取预注册指标、论文报告值和绝对
容差，产生 `supports_reproduction` 或 `does_not_reproduce`。因此执行与核验成功仍可
诚实得出“未复现”。ReproductionTask 的生命周期以
`.popper-reproduction/reproduction.sqlite` 为写权威：领取、执行完成、核验、失败和重启恢复
都在 SQLite 事务中同时更新快照并追加前向哈希事件；并发执行只有一个事务可以领取任务。
`.popper-reproduction/state.json` 仅是旧工具的兼容投影，删除或篡改它不会改变权威状态。
服务启动时会把遗留在 `running/verifying` 的任务显式记为 interrupted，而不是伪装成仍在运行。

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

已形成 Research Claim 的研究可导出确定性证据附录：

```powershell
popper research materialize evidence <项目目录> <Research Run目录> <新输出目录>
popper research materialize verify-evidence <项目目录> <Research Run目录> <输出目录>
```

输出的 `research-evidence.json` 将裁定、方向统一效应、阈值、Decision、Observation、
评分制品摘要和签名回执绑定；`research-evidence.md` 从同一记录生成。校验会重放研究库、
核对冻结 manifest 与来源图，并检查两份导出文件未变。它依赖原研究目录和来源制品，
不是便携复现包，也不等于人工审校稿件或机构独立复核；输出目录必须是新目录，
防止覆盖已有稿件。

证据附录还可用于结构化草稿：`popper research materialize bound-draft <项目目录>
<Research Run目录> <证据附录目录> <新草稿目录> --manuscript-json <结构文件>`；
对应校验命令为 `verify-bound-draft`（参数相同）。结构文件包含 `schema_version: "1.0"`、
`title` 与 `sections`；每节的 `blocks` 只能是 `{"kind":"claim","claim_id":"..."}`
或 `{"kind":"author_note","text":"..."}`。系统要求本 Study 的每个 Claim 恰好出现一次，
结果数字只由权威绑定渲染；作者说明单独标记“未验证”且拒绝裸数字。输出状态是
`draft_needs_review`，并非投稿或发表证明。
人工审校后先用 `popper research paper review-bound`（需填写 `--reason`）登记审校时的
稿件与证据哈希，再用 `popper research paper submit-bound` 和
`publish-bound`（传入项目、Research Run、证据附录、稿件路径及 `--draft-spec-json`）
记录本地事务状态；提交必须与审校时的校验记录完全一致，发布必须与提交时一致。
两步都会重新校验 Claim、原始研究证据、稿件和绑定清单，
通用 `paper submit/publish` 不能绕过。`published` 在此仅是本地流程记录，
返回的 `publication_attested=false` 明确表示未核验期刊、DOI 或外部发表事实。
若进程恰在稿件状态 JSON 已写入、事件链尚未写入之间中断，默认门禁会拒绝继续。
管理员核对后可显式运行 `popper research paper recover-projection <项目目录>
--manuscript <稿件路径> --reason <恢复原因>`：它只接受事件链完整且 JSON 恰好多出
一个合法尾部迁移的情形，先记录恢复审计事件，再撤回该未提交迁移；任意分叉、多个
缺失事件或数据库领先的情形仍拒绝自动修复。

`popper research materialize repro <项目目录> <输出目录>` 默认导出登记的代码、输入、环境记录和（仅在完成确认后）test 摘要。ZIP 带未签名的逐文件 SHA-256 清单；返回的 ZIP SHA-256 应通过独立可信渠道保存。包内 `verify_claims.py` 只检查摘要 delta 一致性，不能从逐样本预测独立计分。

在已完成确认且原始证据审计通过的项目上，可显式加 `--include-raw-evidence`：导出 `.popper/state.db` 与事件登记的运行工件，在交付 ZIP 前临时解压并实际执行一次 `replay --portable`。接收者安装兼容的 Popper 后，解压并运行 `python -m popper experiment replay . --portable`，由保存的逐样本预测重新计算指标与 claim。`--portable` 只跳过原宿主 Python/平台/解释器路径比较，仍校验登记输入、评估器哈希、事件链和工件；它不会重跑模型、证明恶意篡改不可能，也不能保证跨环境数值完全一致。原始预测、日志和数据库可能含敏感信息，分享前必须人工审查；清单未签名，整体 ZIP 哈希需要外部可信锚点。

## 已接入的开源组件

Popper 当前直接调用工作区中的 ResearchStudio-Idea 和 scientific-agent-skills Arbor。先检查许可证、入口和文件指纹：

工作台的文献库与 Arbor 视图只读取**当前工作区根**下的 `integrations/runs/<run>`。以下相对路径命令请在启动 `popper serve` 时指定的项目根目录中执行；在其他目录执行时，请改用该工作区内的绝对路径。切换工作区后，视图只显示切换后工作区的产物，不会扫描 Popper 源码或安装目录。

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
