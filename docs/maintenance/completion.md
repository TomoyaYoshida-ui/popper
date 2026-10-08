# 原项目修复与 FIGS 验收

实施位置：`C:/Users/wangj/Desktop/research/research-agent-v2`。
这是原项目自己的 Git 仓库，当前分支为 `autopilot`；外层仓库里的
`.experimental/judgment-prototype` 保留为参考，不作为原项目修复的验收依据。
全部提交仅在本地，原有文档删除状态保留；原始实验记录、环境和凭据不入库。

## 完成的修复

- 建立原项目源码/测试基线，生成 30,107 项历史记录清单；完整清单留在
  `.acceptance/historical-records.json`，数据库只记录元数据，不打开保护数据。
- 聊天和 JSON 使用共用 SSE 生命周期；连接/无数据/总时限为 15/60/180 秒。
  连接等待、半行读取、重试等待均可取消，总时限不被心跳重置。
- 截断结果即使工具参数可以解析，也不得执行工具；保留可核实的用量。
- 将实际计数、部分报告、未知预留和未发送证明分开。未发送释放预留，
  部分报告不冒充完整结算；严格科研模式与有预算上限的交互模式分开。
- 逐逻辑请求/尝试记录，已开始输出的生成不自动重放；移除固定 20 秒等待。
- 增加不可变、可重放的假设判断记录；旧运行不自动回填，模型不能写入分数。
  决策/反思引用判断与观察，阈值状态和科研判断分别展示。
- FIGS 通过原项目执行/独立评估/确认入口运行，固定依赖、数据哈希、划分、
  模型种子、规则预算和分组 bootstrap；确认重拟合文件必须等于 train + dev。

## 验证

冻结基线：1,459 passed、59 skipped、734 subtests passed。
本轮全量回归：1,501 passed、44 skipped、738 subtests passed。
最新用量/恢复专项：59 passed、2 subtests passed。
科学判断/FIGS/里程碑专项：10 passed、2 subtests passed。
前端：162 项测试通过，构建成功；wheel 的工作台资源、离线演示和隔离安装的
CLI 入口检查通过。原始输出保存在 `.acceptance/`，不是外层原型的 64 项测试。

FIGS 离线与真实 Qwen 流程均完成，研究存储完整性通过。
最终相对分组 MAE 改善为 0.09664595516747511，95% 区间为
[-0.07875428856482976, 0.2436434160304608]；两个竞争解释均为 inconclusive。
真实模型生成假设、选择实验并分析开发证据；最终判断由独立结果驱动。
本轮共 6 次真实请求尝试，包含 3 次失败和 3 次成功，均计入同一 30 次总上限。

## 保留的记录与边界

- 三次早期失败没有充分的发送阶段证据，不能追溯记为零；原有未知用量预留保留。
  后续成功使用显式环境代理配置，避免误读 Windows 浏览器注册表代理。
- `.acceptance/figs-offline`：导入 imodels 时缺少可用 home 导致执行失败，已修复为
  在运行输出目录设置 matplotlib 缓存。
- `.acceptance/figs-offline-v2`：worker 未声明/转存模型审计制品，已修复为显式输出。
- `.acceptance/figs-offline-v3` 使用 train-only 确认训练，产生了不同结果。
  它不是原型 train + dev 重拟合协议的复现，排除出迁移验收；原始结果不删除、不改写。
  规范复现使用 v4 与 figs-live-direct，保留了原型的 inconclusive 结果。
- 公开确认数据此前已使用。本轮是迁移/工程复现，不是新的独立科研确认，
  不能据此声称机制证明、科研发现或 agent 优越性。
- Linux 沙箱尚未在本机执行。44 个跳过项包含可选编排/ConPTY 依赖和平台相关
  字体、符号链接、沙箱用例，不计为验证通过；CI 已补齐 figs extra 与能力探测。
- 外部签名确认暂不支持 confirmation_train，明确拒绝，不静默改变训练身份。

## 复现入口

在项目根目录建立独立环境并安装 `requirements-validation.txt` 后：

```powershell
python -m pytest -q -rfEs --tb=short
python evaluation/run_figs_acceptance.py --output .acceptance/new-offline
```

省略 `--csv` 会下载并校验固定哈希的公开数据。显式加 `--live` 才调用 Qwen，
凭据只从 `POPPER_API_KEY` 读取；同一输出父目录共用 30 次请求账本。
已有运行/确认数据不会被覆盖。依赖、摘要与接口细节分别见
`requirements-validation.txt`、`completion-summary.json`、`usage-and-recovery.md`。
