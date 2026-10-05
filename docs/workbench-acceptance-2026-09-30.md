# 工作台收尾验收 · 2026-09-30

## 范围与结果

单用户 Windows 本地 Web 工作台。保留现有项目数据、未提交改动、Kernel 状态和审批规则；没有公开发布。正常前端端口 5173、后端 8765。临时模型验收端口 8768/8769 不作为日常服务。

本次新增请求竞争保护、审批重复点击保护、已批准工具的版本校验、SSE 数据库 WAL/首帧变化检测及断流失败处理。工具卡使用执行时的文件摘要，不把后来刷新预览的磁盘版本冒充已执行结果。已结论 Study 禁用重复运行和确认入口。旧研究/审批记录收纳技术详情，保留原操作。

## 自动检查

- 前端：`npm test --prefix frontend`，8 个文件、29 项通过。
- 前端：`npm run build --prefix frontend` 通过，仍显示大块警告。
- 包内资源：`python scripts/stage_frontend.py --check`，20 个文件与构建一致；`tests/test_packaged_web.py` 2 项通过。
- 相关后端回归：packaged_web、server_chat、server_multiworkspace、workbench_transport 合计 82 项通过。
- 最终后端全量：`python -m pytest -q`，995 passed、5 skipped、544 subtests passed，耗时 650.42 秒。跳过项未按通过计算。
- `tests/test_workbench_transport.py` 的 6 项测试使用 HTTP 模拟模型、真实 Workstation、持久会话和 SSE：增量流、原生分片工具提案、审批执行与结果续推理、断流保留部分文本、503 后显式重试/幂等、取消/转向、预算拒绝不发请求。
- 前端覆盖迟到响应不回写其他项目/会话、同项目请求同步去重、过期审批禁用、关联 Job 的明确 Study 身份、完成工具的历史输入版本、未保存保护与恢复冲突。

全量结果对应最后的生产后端修改；此后仅前端、验收脚本和文档收尾，前端和包资源另行重新检查。不是干净安装环境或跨平台发布认证。

## 性能与可访问性

Monaco 从全功能注册改为必要功能注册，保留编辑/差异、高亮、查找、撤销、折叠、剪贴板、缩进和键盘焦点。关闭 EditContext，采用可访问 textarea，实际浏览器编辑和保存通过。语言与 Worker 仍本地按需加载。

主 Monaco 懒加载 JS：基线 gzip 1,042.47 kB → 777.30 kB，降低 25.44%；主页面 gzip 103.53 kB。JSON 语言服务另有约 255.73 kB gzip；不能把主块改善解释为全部编辑器资源改善。首次对话不导入 Monaco，编辑器仅通过懒加载入口引用。

四种主题正文和次级文字颜色令牌在工作台各背景上的自动对比度检查达到 4.5:1；这不是所有图标、图表或第三方编辑器颜色的完整 WCAG 认证。浏览器检查浅/深主题、1440/1024/760px，1024px 收起侧栏后对话和工作区同时可用，760px 使用主面板切换。键盘分栏调整至 460px、标签方向键和模态焦点有交互测试。减少动画已有媒体规则，本轮未切换 Windows 系统动画偏好；鼠标拖动未在本次新增验收中单独留证。

## 浏览器真实操作

隔离项目 `C:\Users\wangj\AppData\Local\Temp\popper-browser-new-20260930` 从浏览器填写协议与四个登记输入，校验、确认预注册、新建 Study，不是仅导入已有研究。

Study `S-c71e864774c23525`，候选开发 Job `JOBS-c87769ae170f8d48c50c226b`、独立确认 Job `JOBS-b516f41918786b3b115d6ee5`。确认前展示候选、范围、预算及一次性消耗警告；完成后 4 项 Observation、花费 4/4、Claim `clm_wbp7bex4tjzii5rbxlerlgbscy`。效应 1.026643513970993，阈值 0.5，只在预注册确认范围内成立。

证据绑定稿件 `b481faaa3c7298ce5b8a947fff6daf0b478ede2c8a39b4babe2332792481ee0c` 在右侧阅读，再跳转证据页核验成功；确认 Observation、Run、SHA-256 与 `EB-a787c01fe7ce03093df088e0` 一致。未登记人工审校完成或声称已发表。

此前独立 ODE/WDBC 完整路径不重复执行。WDBC 隔离项目 `popper-workbench-wdbc-20260930` 的 Study `S-955442ba273b0347`、Claim `clm_d465726ga4yjz55uqtzs5loiiq` 本次只读重新核验通过，验证级别 `local_research_store_and_source_artifacts`；稿件仍引用真实确认结果，accuracy 效应 0.03508771929824561、阈值 0.02。它不是医学有效性证明。

临时非冻结 `notes.txt` 实际编辑 → 差异预览 → 版本校验保存。外部修改后拒绝覆盖并保留草稿；新页面从 IndexedDB 恢复时展示版本冲突和禁用写回。恢复的未提交对话输入、Study 与标签仍存在。后续实际走通切换项目的保存和放弃分支，放弃不修改磁盘；取消分支保留项目与 Shell，收起后变量输出 42。成功切换后终端结束，返回创建新 Shell，不伪装恢复旧进程。

标签去重、关闭其他标签、最大化/恢复分栏、窄窗口对话/工作区切换实际通过。构建更新使旧懒加载资源失效时，编辑器错误被限制在面板内，重新载入可恢复，不导致整页白屏。刷新滚动精确像素、全部鼠标拖动组合未单独留证。

## 真实模型与模拟模型：严格区分

真实 MiMo 使用合成临时项目 `popper-acceptance-lk8eksfa`，会话 `ses_kmtmhafbdm72kc6oi4ahvlee6m`，预算 20,000 tokens，最多 3 次请求。实际仅 1 次：prompt 4,849、completion 64、total 4,913，供应商返回用量已核验。

真实原生 `workspace.file_read` 提案、披露预览、批准及读取合成 `notes.txt` 成功；对象跳转保留未提交输入。工具结果续推理最初被服务端预留检查拒绝：`limit=20000, projected=23098`，当时停止且没有自动追加外部请求。

后续用户明确授权预算 4,000,000 tokens，在原会话设置中写入该额度。恢复临时服务时从持久化作业读取此前 1 次请求及 4,913 tokens，不重置请求数；仍最多 3 次。点击“根据真实工具结果继续”，第 2 次请求成功，返回正确的合成标记 `POPPER_SYNTHETIC_42` 与执行结果 SHA-256。该次用量 5,205 tokens，累计 prompt 9,883、completion 235、total 10,118；两个作业均成功、预留归零、剩余额度 3,989,882。

真实续推理文本的流式草稿持久化为 195 字符，与最终回复一致；浏览器刷新后仍显示两个有序 Turn、真实工具结果和未提交输入。没有第 3 次请求、没有读取其他文件或启动实验。此处的额度是 token 上限，不是人民币金额或已消费额度；未配置价格目录，不报告金额。

额外无网络预算回归 3 项及 HTTP/SSE 回归 6 项合计 9 项通过，覆盖恢复后仍最多 3 次、增加额度后缺失用量仍停止、已消费额度阻止外呼。本次仅修改验收脚本/测试与记录，未重新运行此前 995 项全量回归，不把新测试计入该旧全量结果。

模拟服务 `scripts/workbench_acceptance_server.py --mode mock` 经本地 OpenAI 兼容 HTTP 流调用真实后端。浏览器实际批准工具，真实读取合成文件，模型读取结果后续推理，两个 Turn 顺序正确；异常分支由上述 HTTP/SSE 自动测试稳定复现。恢复仅允许临时验收目录；真实模式 `--resume-real-project` 必须存在全部成功且用量已核验的历史作业，从历史恢复计数，遇到失败、活动作业或未核验用量拒绝启动。

## 截图与留存

截图保存在本机临时目录，不含密钥，不把临时路径作为可移植发布包内容：

- `C:\Users\wangj\AppData\Local\Temp\popper-workbench-final-20260930.jpg`：正常服务最终工作台。
- `popper-workbench-1440-dark.jpg`、`popper-workbench-1440-light.jpg`：桌面主题。
- `popper-workbench-1024-light.jpg`：收起侧栏后的分栏。
- `popper-workbench-760-workspace.jpg`、`popper-workbench-760-chat.jpg`：窄窗口双面板切换。
- `popper-editor-conflict-acceptance.jpg`：恢复冲突，禁止写回。
- `popper-mimo-budget-acceptance.jpg`：真实调用后的预算拒绝。
- `popper-mimo-completed-20260930.jpg`：授权增额后真实续推理完成、预算与已用量。

临时项目及截图保留用于复查；正常 5173/8765 服务保留，临时验收服务结束。未向模型发送已有项目内容、日志或密钥。团队能力、分布式执行、Linux 原生终端、干净环境安装认证及公开发布不在本轮交付内。上述未留证项明确保留为限制，不以本地测试通过掩盖；真实模型续推理原有阻塞已经在用户授权增额后解除。
