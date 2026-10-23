# 能力边界

包名 popper-research，状态 Pre-Alpha。这是本地优先的计算实验决策内核：先登记可证伪预测，密封开发集与确认集，由独立评估器计分，再给出 supported / refuted / inconclusive。确认集只打开一次。模型不能改分数。

**演示冻结（2026-10-23）**：扩能力收口停在本文件所述范围。默认演示路径是已登记研究的证据闭环；下面「能演示 / 不能当产品」以此为准。大块工程债（合并三条运行时、审计级查新 FPR、接第三方技能库、开放写论文）故意延后，不要在冻结点上继续堆半成品。验收数字见 `docs/maintenance/working-tree-verification.md`（冻结 HEAD `c929f08` 一带）。

## 现在能演示的

- 冻结计分任务上的开发循环，以及一次确认。
- 判断三态：supported、refuted、inconclusive。
- FIGS 与 Adaptive 目前的结局是证据不足（inconclusive）。这不能当成新发现，也不能当成某种策略更优。
- 有界能力（均非开放 agent）：文献 / 查新 / 已下载全文摘录进规划且必须引用真实 id；会话记忆笔记；聊天数字结论门禁；稿件物化的 claim/ev/ref 门禁。

## 还不能当产品用的

- 通用文献阅读 agent（开放拉取 PDF、任意综述）
- 审计级可信查新（受控语料 FPR / 反证幻觉率验收产品）
- 关键词新颖性分数
- 占位语料
- 自动写论文 / 一键生成 Nature 全文（见下节：仅有 claim 门禁的稿件校验）
- 湿实验或理论证明
- 跨会话、跨周的长程科研记忆（停一周再接着读文献、续写项目叙事）

Ideation 的 novelty 与 `corpus_seed` 是非科学脚手架。没有真实检索近邻之前，默认不输出 novelty 数值分数。

## 查新（Scoop）：跳过≠完成，非 provisional 可引用

Campaign 的 scoop 步骤缺模型、非 trusted-local 或配置不足时 outcome 为 skipped，不是 success；provisional（全文不足）为 retryable，都不能当成「查新已完成」。前端阶段「查新（Scoop）」只在 history 记 success 时标完成。

已落盘且 `status=completed` 的 scoop 命中可按稳定 id（`SCOOP-*`）、来源、DOI/arXiv、标题与截断摘录进入规划上下文（最多 12 条）。provisional 报告不进入可引用集合。规划动作若声称依据查新 / scoop / prior art / 无撞车，必须引用上下文里允许的 `SCOOP-*` 或 `LIT-*` id；查新为空或仅 provisional、又无文献命中时，直接拒绝，不让模型写「查新完成/无撞车」。

这仍不是审计级 FPR 验收产品，也不接第三方技能库或通用全文阅读。

## 有界全文摘录（已下载公开 HTTPS 产物）

若 study / campaign 的 scoop 深潜已落盘公开 PDF 全文（`step5` 中 `access=fulltext`，且 `papers/*.txt` 在仓库根下），可把**截断摘录**投影进规划上下文：稳定 id（`FT-*`）、可关联的 `LIT-*` / `SCOOP-*`、DOI/arXiv、可选页码或字符偏移、截断正文。条数与字符有上限。

规划或聊天若声称依据「论文正文 / 某页说了…」，必须引用这些 `FT-*` id；没有已下载全文时直接拒绝，不让模型凭空写页码或正文。不自动对整个互联网拉全文；只使用已落盘、且 fulltext 模块已接受的公开 HTTPS 产物。

**全文投影范围：仅 scoop 落盘。** literature 检索命中等其它路径没有与 scoop `step5`+`papers/*.txt` 同级的稳定全文契约，因此不发明假路径并入 `fulltext_context`。

这是有界摘录引用，不是通用阅读 agent，也不是论文写作。

## 稿件写作门禁（不是自动写论文）

物化 / 导出稿件若含数字结论或科学主张，必须带 `[[claim:]]` / `[[ev:]]` / `[[ref:]]` 标记，且引用的 `observation_id` 或 `LIT-` / `SCOOP-` / `FT-*` 必须在当前 study 上下文可引用。缺标记或 id 无效 → 门禁失败，拒绝物化，不能静默当成论文完成。前端不得把门禁失败画成导出成功。

已有 materialize / 模板路径只加强校验与失败语义；产品仍不是自动写论文工具。

## 会话记忆（已进研究循环，仍有界）

本次 study 的 `run_dir/memory` 会进入规划上下文：工作记忆摘要与有界的短期条目（假设修订摘要、判断三态摘要、拒绝原因等）。循环在关键动作落盘后可写入短条目；条数与长度有上限，不会把全文聊天灌进记忆。

记忆只是未验证的会话笔记（`trust: unverified_session_notes`）。它不能覆盖独立评估分数，也不能改写 judgment 三态；模型仍不能改分。`MEM-*` 不能当作 observation 引用。CLI `popper research memory` 仍可用，主循环不再是唯一入口。

这不是「停一周再接着做」的长程科研产品。

## 执行权与默认入口

默认产品路径：已登记研究走 `popper research init` → `popper research run` / `confirm`（ResearchController + ResearchRuntime）。CLI 默认 help 与本文件以此为准。

1. 已登记项目的科学执行权在 ResearchController + ResearchRuntime。
2. `popper adaptive` / AdaptiveResearchRuntime 是描述性基准夹具，也是第二套契约。默认不开 live，不是默认科研产品路径。
3. `popper research campaign` / Orchestrator 是编排图，不是另一套科学内核。科学结果仍落 ResearchStore。编排步骤（含 idea、文献、scoop、dispatch、propose、variant、materialize）因缺查询、缺模型、非 trusted-local 或缺前置而未真正执行时，outcome 是 skipped，不是 success。跳过不等于研究完成；已有产物的幂等复用仍记 success。

本轮不合并上述三条路径的实现代码，也不增加第五个循环。

聊天不推进实验，也不改评测数字。

## 文献摘录与只读问答

已经落在本次 study 或 campaign 里的检索命中，可以按稳定 id、来源、DOI 或 arXiv、标题和一段截断摘录进入规划上下文，最多 12 条。两个标识都没有时，不把别的字符串装成 DOI 或 arXiv。规划动作如果声称依据文献，必须引用这些 id；调用方明确要求文献依据而命中为空时，直接拒绝，不让模型现写综述。不引用文献的计算实验动作仍按原规则执行。

摘要级文献摘录仍不读取全文 PDF。已下载全文的有界摘录引用见上一节。查新引用规则见更上节。

聊天仍然只读：不推进实验，不写事件链，不改评测数字。助手若给出测量值、百分比、区间、p 值或改善幅度，必须同时带上本次 study 的 observation_id，或上下文里已有的文献 / scoop / 全文摘录 id。没有这些标识的数字结论会改成一段拒绝，不会当成科学结论。声称「论文正文 / 某页」而无 `FT-*` 时同样拒绝。

这些补充不能当成项目已经能独立做科研。
