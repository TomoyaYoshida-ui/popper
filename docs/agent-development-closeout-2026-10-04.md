# 六组研究收尾：开发证据与解释核验

此记录追加于既有失败和执行修订之后，不覆盖原始日志、用量、单次方案或科学协议。科学结果与工程验收分开；没有独立确认或 Claim 时只能交付开发证据报告。

## 2026-10-04 本轮实现

- `popper/research/development_evidence.py` 由明确项目、Study、Revision、Job、Run、Observation 和 Worker 回执读取证据。读取不创建数据库、不恢复队列、不发起模型调用。旧评估请求 key 与新原生执行 key 均由真实来源绑定。
- 校验不可变源码、研究事件完整性、入队身份、Worker 输入/输出、宿主 RHS 回执及评分摘要；逐案例重算使用原评分器且必须与真实 Observation 一致。失败无 Observation 时不补造误差。
- 分开呈现执行完成、轨迹有效、改善阈值。能量漂移仅在原定输出采样点计算，不代表所有内部步；RHS 查询数不等于全部算术运算。缺少宿主回执时查询总数标未知，已知部分单列。
- `DevelopmentReview` 是来源绑定的应用审查记录，不是第二套科研状态。事实可确定性核验，机制解释仍标未验证。不能由操作人员补填旧预测、选获胜方案、把科学效果差的成功执行冒充技术失败修复。
- 核对入口只允许闭环 1/2 各一次补充根请求，固定幂等 intent 在出站之前保存。仍须核对当前冻结、完整 CI 回归回执、实际读隔离、已知用量及共享账本。不会自动审批、重发、取消或启动确认。
- Job 与工具卡复用现有页面。实际浏览器发现 HTTP envelope 的诊断字段被旧 `data.job` 提取丢弃，已修复并增加传输测试。技术细节仍折叠，保留输入和对象归属。

## 已执行与模拟边界

- 初批前端全套：138 项通过。后续历史预览差异兼容后为 154 项通过；生产构建通过。Monaco 大块警告仍存在，未隐藏。20 个暂存资源与构建一致，10 个演示模板与源文件一致；暂存不与全量回归并行。
- localhost OpenAI 兼容 SSE 专项：停止分支，以及结构化提案→独立审批→Revision→真实 Job/Observation→停止分支通过。仅 quadratic 诊断投影标为 `simulation_only`；实验、队列和审批不伪造。这不代表正式 oscillator 数值验证。
- 来源/事实审查 35 项、一次核对请求模拟 17 项、证据专项 33 项通过；后续合跑与全量终态以下方追加回执为准。
- 只读整理六组现有证据：12 次候选开发执行，5 个 Study，0 个 Claim；单次组 2 没有 Study，不补充假设。报告位于 `evaluation/runs/agent-oscillator-2026-10-01-v3/acceptance/development-closeout-before-review-v2/`。
- 截至此只读快照：累计 37 次请求、527308 tokens、未结清 0；实际持久授权上限为 60 次请求、400000000 tokens。冻结旧提示中的 400000 上限作为历史材料保留，不篡改过去输入。

## 冻结与正式接续门

执行修订 13 已冻结，SHA-256 `ad7f7a08909854c96f0f7f98fe7dd3ff23c9dc843ed2ed15c5d16f92c18fcb32`。评分器、参考数据、Research Kernel 和确认规则未修改。

2026-10-04 19:45（本地）冻结后完整 CI 命令结束：1338 passed、5 skipped、675 subtests passed，退出码 0，`sources_stable=true`，耗时 756.84 秒。日志为 `acceptance/full-regression-development-closeout-rev13-2026-10-04.log`，SHA-256 `94ce7965f15d35f67618c828bb5c255768ed02b1029ea25eaa120c85ba5182b2`；原始日志与独立退出码回执均保留。跳过为 TTC 字体、Windows 符号链接权限、两个沙箱不可用分支和 Linux 专属 mount namespace；不作为这些分支在 Windows 上已通过。

随后执行修订 13 的真实读隔离启用检查通过：47 个受保护文件候选读拒绝，宿主读取正常，`network_block=false`。启用前账本为 37 次请求。此证据不表示 Windows 完全断网。旧修订 12 的通过记录没有借作本轮验收。

干净 wheel 安装后的首次只读复核发现旧失败 Job 的队列 `run_id=null`，而真实 Worker 已有不可变 Run 身份。原报告保持不变，复核工具按 Revision/dev 内容寻址及真实 Worker manifest 双重校验兼容；没有为失败补造 Observation 或成绩。后续复核结果另行追加。

浏览器实际核对了 1440/1024/760px、浅深主题、Job→Observation 跳转、标签去重、键盘分栏、刷新及未发送输入保留，截图保留于 `acceptance/development-closeout-1440-dark.jpg` 和 `acceptance/development-closeout-760-light.jpg`。本轮未手动重跑完整编辑冲突或独立确认：既有编辑自动测试通过，确认因当前候选无资格而不消费资源。临时 8768 无模型服务、空闲验收 Shell 已结束，视口覆盖已恢复。

历史原生预览曾因 `call.preview=null` 而无法打开差异，尽管持久 `call.result.diff` 存在。本轮前端统一来源选择器修复并实测 `tcl_shb5db53wsgj6hlxt3qkycve7m` 差异、去重和来源显示，历史预览不能充当创建/执行批准。截图为 `acceptance/development-closeout-historical-diff.jpg`。临时无模型服务再次结束。

## 真实补充诊断与失败记录

在修订 13 全量、读拒绝和账本核对通过后，闭环 2、闭环 1 分别发出一次根请求，分别批准只读 `research.revision_job` 后由真实工具结果续推理，合计新增 4 次模型请求。

- 闭环 2：来源 Turn `trn_j23eyqz4dps7at7axwjf2oxsqi`；固定输出限制内 JSON/源码截断，不能作为完整新候选。保留原回复与输入摘要，应用记录为 `unverified_source`、`decision=null`；没有人工补全或第二次诊断根请求。
- 闭环 1：来源 Turn `trn_krlywpi4whcpan36mz4oiipf6m`；回复包含明确候选 index 4、预测、反证及完整源码，但缺少 `kind` 且事实指针基于完整工具回执 envelope，不符合当前 DevelopmentReview 的诊断树格式。原回复不改写；应用审查仍为 `unverified_source`、`decision=null`。有来源的新候选可以作为一次独立原生提案接续的理由，但不赋予执行权限，也不能补填或升格旧审查记录；该分支以下方实际回执为准。
- 两条 source audit 的不可变摘要分别为 `0f7f63cc9cc4d2b024f0f6b021ce5807106882fb40caaf61512bb3d9b6db5123`、`1a086a81722daedaa91b16f702b4bda51fd4e4baaf850be49d60cae08a61d386`。机制解释仍未验证。
- 结算后累计 41 / 60 次请求、617780 / 400000000 tokens，未结清 0；历史超预留、未分配用量调整仍保留，不重置额度。

`acceptance/development-closeout-after-diagnostics-v1/` 为新的只读快照，报告摘要 `877da4fac73a61080020ad35f7267fa23aaa91999c82043e53efc2b586fd1d0d`；新增审查来源可追溯，原报告未覆盖。当前仍为 12 次真实候选执行、0 Claim，不能称科研增益通过。

## 干净安装环境的本机制品复核与源码重放

新环境 `C:/Users/wangj/Desktop/research/_tmp_development_rev13`，Python 3.10.6，从本地构建 wheel 安装而非导入仓库 checkout。首次失败复核及修复后复核分别保留，不覆盖旧报告。

- 保留制品复核：`retained-verification-after-reader-tests.json`，17 条，退出码 0。7 条原失败候选仍没有 Observation 或历史数值成绩。
- 隔离源码重放：`isolated-replay/result.json`，17 条独立新执行，退出码 0，10 条原有 Observation 的误差全部严格复现一致，所有实际读拒绝通过；宿主接受 RHS 查询合计 3280995。重放程序和环境摘要保留于 intent/result。
- 重放失败候选得到的 1000000 失败惩罚仅是新重放结果；执行覆盖门仍失败，绝不补记原失败成绩或生成原 Observation。原项目、Study、确认及模型账本未被重放写入。
- 这仅是同一机器的新虚拟环境与隔离源码复现，不称第三方独立验证；没有内核级断网。不增加付费调用，也不消费确认资源。

正式次序：第二组补充核对 → 第一组失败反馈 → 保存最终候选或停止来源。第三组已停止，不重启。候选只有模型明确提出且范围/预算核验通过才执行，分别审批，不由剩余预算驱动强制探索。

独立确认只在服务端预览具备资格且用户分别批准后进行；当前历史候选均无资格，不绕过、不消费、不重试。单次组没有有效成绩时，不宣称已取得公平的数值性能增益对照。

## 2026-10-05 执行修订 14 接续验收

执行修订 14 摘要为 `b7ede30dd11bb064f478f01141d236f7e9a160ec396917f075dc99ee995f7ae9`。本次修订处理 JSON 数字事实的精确比较（不将布尔或字符串转换为数字），并增加来源绑定、只允许一次的原生候选提案入口。历史格式不合规审查仍保留原文及拒绝状态；不补填 `kind`、改写事实指针、修补模型源码或追认审批。

- 接续入口及受影响后端合跑：95 passed、59 subtests passed。前端为上述 154 项，构建与 20 项暂存资源检查已完成；冻结后不并行修改源码或暂存资源。
- 调用前只读核对 `evaluation/request_authored_candidate.py` 摘要为 `83c60152abf39fc4df1440616b6b8458602ff363e7ce95ec1473fbf3ec9e93fe`，与修订 14 冻结 reason 中的入口摘要一致。该文件不在 Popper 包内源码列表，故显式核对并记录，不能只依赖包内源码稳定检查。
- 全量回归于本地 2026-10-05 00:24 启动、00:53 结束：1358 passed、5 skipped、701 subtests passed，耗时 1700.40 秒，退出码 0、`sources_stable=true`。日志为 `acceptance/full-regression-development-closeout-rev14-2026-10-05.log`，摘要 `dc3c68fb8d05462dc0f18e486ea0a968cad4f86ea8778796114d43d5b1915b3b`；独立 `.receipt.json` 保留。随后修订 14 实际读隔离通过：47 个保护文件候选读拒绝，宿主读取正常，网络未内核级隔离。
- 最终 wheel 摘要 `ea10fd42eb4803c8ca01ce26a6715c42b09fa590e8129966d7f9e44226c2dec1`，2180447 字节；包内 124 个 Python 源文件逐项匹配修订 14 冻结摘要。
- 独立新环境 `C:/Users/wangj/Desktop/research/_tmp_development_rev14` 安装该 wheel。只读复核最新 `development-closeout-after-diagnostics-v1` 报告的 17 条制品通过，退出码 0，回执为该环境的 `retained-verification.json`。此次没有模型调用、源码重放、确认消费或原项目写入。
- 修订 13 的 17 条隔离源码重放保留为前一执行版本证据；不将其说成修订 14 的重新执行。当前科学评分、参考数据与隔离求解器组件未变。

实际只读预检发现第一组来源明确提出的 index 4 已有执行尝试：当前 attempted indices 为 `[0,3,4]`。Hypothesis 的 `untested` 生命周期标签并不表示从未执行，失败也计入尝试。入口正确拒绝该候选，没有创建出站 intent、模型请求、Revision 或 Job；操作人员未替换为剩余 `[1,2,5]`。此前“该候选未测”的准备假设被实际权威记录否定，以本段实测为准。完整来源保留，不伪造模型停止决定，也不发第二次补充诊断根请求。

拒绝回执：`acceptance/authored-candidate-preflight-refusal-rev14-2026-10-05.json`。新的六组只读快照为 `acceptance/development-closeout-rev14-preflight-blocked-v1/`，报告摘要 `54cfd02087877317cb4b5383ffb716ef6e0fae282c789ae0312cc5f0920a3702`；旧报告保持不变。账本仍为 41 次请求、617780 tokens、未结清 0，候选执行仍为 12 次、0 Claim。第二组截断回答和第三组停止决定不接续。

并行只读审查发现旧 `approve_native_development_tool.py` 在父请求产生纠错子请求时可能遗漏等待，并缺少全注册根/研究队列静默检查。因此本轮不使用旧审批入口，安全修复的测试和新的冻结回归另行记录。此工程缺口不能通过增加模型费用绕过。

## 执行修订 15：审批等待与负结果呈现

旧审批入口的安全修复已实现：最新冻结修订的完整 CI/读隔离检查、唯一项目和研究注册、全项目会话/工具/队列及研究队列静默检查均在构造 Workstation 前完成。合法同 Turn 待批兄弟仍需逐项审批；其他上下文或 approved/running 阻挡。等待同时核对线程、持久模型 Job/Turn 和研究队列；未知用量或超时记录不确定状态，不把工具终态冒充续推理完成。旧回执只读核对，不创建工作站、重发或重复执行。

报告导出修复两处歧义：有对照而候选无成绩时，SVG 的候选缺失计数也包含该行，并明确标记“无 Observation/无成绩”；CSV 保留旧列，增加 Job 终态、宿主案例状态、真实 scored_count 和执行完成/失败/未完成计数。`failed_cases` 仅表示已评分轨迹的失败，不能把无 Observation 的 0 解释为执行成功；原案例字段不完整时新增计数留空。

- 两项实际候选拒绝回归新增；审批、导出、候选、诊断请求和事实审查合跑 127 passed、87 subtests passed。20 项资源一致性再次通过；没有新增前端源码改动。
- 新快照 `acceptance/development-closeout-presentation-fixed-v1/`，报告摘要 `014bfa29dbe4ce589d5798bad87069cd063192dbb944c5240b5eecada30b7d57`。误差和能量 SVG 均标出 14 个无成绩候选/系统组合，原报告未覆盖。
- 浏览器真实查看并保存全图：`acceptance/development-closeout-missing-metrics-fixed.jpg`、`acceptance/development-closeout-energy-missing-fixed.jpg`。仅访问 8769 静态报告，不配置模型或恢复科研队列；临时标签和该服务已结束，没有设置视口覆盖。
- 独立安装环境再次只读核对新快照 17 条，退出码 0；回执为 `_tmp_development_rev14/retained-verification-presentation-fixed.json`。没有重放、模型或确认消费。
- 修订 15 摘要 `cdab858036d4ee0a1a30c7bd1607a6e2ceb5040f09d991a4c501f33d894e3d20`，reason 明确绑定两项 evaluation helper 和三项测试源码摘要；包内科学源码未变。全量串行回归于本地 2026-10-05 01:13 启动，日志 `acceptance/full-regression-development-closeout-rev15-2026-10-05.log`，此段写入时仍在运行，不能报告最终通过。完整退出码、源码稳定及实际读隔离通过前不启用新执行修订。

当前仍有 12 次候选开发执行、41 次请求、617780 tokens、0 Claim/确认。第一组拒绝重复候选、第二组截断、第三组停止及单次组原始失败保持不变。新审批修复并不授予继续尝试或独立确认的权限。

## 复核命令与限制

```powershell
python -m evaluation.closeout_development --campaign evaluation/runs/agent-oscillator-2026-10-01-v3/formal-campaign-qwen-v2 --output <新的报告目录>
python scripts/stage_frontend.py --check
python -m pytest -q -rfEs --tb=short
```

报告与后续隔离源码重放是本机验证，不称第三方独立复现；Windows 无内核级断网，读隔离不等于完全封闭。无真实确认结果不能声称达到预注册科学增益标准。使用正常服务前检查所有持久队列；临时验收服务没有模型客户端，结束后关闭临时端口并恢复浏览器视口。不公开发布。
