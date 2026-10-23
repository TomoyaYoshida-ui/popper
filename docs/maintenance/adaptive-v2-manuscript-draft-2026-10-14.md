# 契约可审计、抽样可声明、修订留容量：自适应科学代理流水线的描述性方法学

声明：本稿是描述性方法学正文草稿，由 [`adaptive-v2-manuscript-outline-2026-10-14.md`](adaptive-v2-manuscript-outline-2026-10-14.md) 扩写。数字只来自 [`adaptive-v2-cohort-index-2026-10-14.md`](adaptive-v2-cohort-index-2026-10-14.md)、[`adaptive-v2-descriptive-closeout-2026-10-14.md`](adaptive-v2-descriptive-closeout-2026-10-14.md) 及各队列已锁 synthesis。比较单位是 **task**；重复不是独立数据集。失败留在分母。本稿不开 `--live`，不重开已锁队列，不启动 `formal-generated-root`，不宣称三种策略优劣。

---

## 摘要

自适应科学代理在规划、实验、修订与判断的闭环中运行。合同校验失败若被软跳过，纠错发送若吞噬单元科学步配额，或以布尔独立性直接授予比较有效，工程分母就会与科学结局缠在一起。本文记录一套可审计的描述性协议：合同拒绝逐次回执；单元 scope 只计逻辑请求的首次发送，且不抬高 `scope_calls`；比较有效仅在抽样声明与数据相符、独立单位不少于 30 时成立；修订必须留下复验容量。比较单位是任务，失败留在分母，已锁队列分账、彼此不可通约。五条队列的确认完成分别为 66/72、61/72、61/72、67/72 与 6/6，科学四格按队列报告。公开分区在缺少可引用抽样记录时没有 supported 或 refuted，或停在证据不足。formal-iid-72 的探索性配对区间单独列出，并标明非结论。修订容量试点六格均完成确认且均有 finalist，容量软拒绝收据为 0，样本量不足以估计触发率。本文不宣称三种策略的优劣。

---

## 1. 引言

自适应科学代理把假设的提出、实验的执行、解释的修订和证据的判断放在同一条流水线里。这条流水线要同时回答两类问题：工程上这一格有没有跑完，科学上这一格的预注册判断落在哪一档。两类问题一旦共用一个含糊的「跳过」或「未通过」，分母就被污染。合同层校验失败若被软跳过，后续科学计数会把本该留在工程侧的拒绝算进证据；单元发送配额若被契约纠错的二次发送吃掉，科学步还没走完，预算已经见底；公开数据上若用一个布尔标记把行级划分当成独立分析单位，比较就可能被标成有效，而抽样过程本身并无可引用记录。

同一类混淆也出现在修订之后。科学语义要求新版本不继承旧版本的支持：修订抬高假设版本后，旧的成功实验不能再充当 finalist。若此时实验槽已满、规划已到最后一步，或当前版本还没有任何成功实验，流水线仍可能继续修订，然后在没有复验的情况下耗尽决策或发送预算。结局是 finalist 为空，确认落到 `not_evaluated`。这个结局是工程分母里的未评，与科学上的 refuted 或 inconclusive 不是同一件事。把二者写成「假设未得到支持」，会把容量设计问题说成科学否证。

针对上述环节，本文沿门禁实际落地的时间线，记录一套分账规则，而不是挑选一种更优策略。合同拒绝改为可审计回执，单元 scope 的发送上限只计逻辑请求的首次发送，纠错重发仍计入全局与 token，同时保持 `scope_calls=24` 不变。比较是否 `valid` 不再由 `independence_verified` 布尔授予，而改为抽样声明与数据对得上、且独立单位不少于 30。其后，修订在进入新版本之前须留下复验容量：实验槽、规划步与当前版本已成功这三项任一不满足，即软拒绝。方法学对象是这套可复述、可门禁的协议，以及把合同拒绝、预算、抽样与修订容量从科学四格里拆开的记账方式。

证据按已锁队列分账呈现。比较单位是任务；同一任务上的重复不构成独立数据集；失败留在各队列自己的分母里。五条队列的任务身份、源码指纹和账本余量并不相同，因此确认完成率与 supported、refuted、inconclusive、`not_evaluated` 只在队列内部阅读。公开分区在抽样记录缺失时，科学判断停在证据不足。`formal-iid-72` 在抽样审计指纹下另有一组探索性配对，正文单独成节并标明非结论。修订容量试点只报告工程结局：未复现「修订后无复验则未评」，且容量软拒绝的触发率仍不可估。描述性终点不依赖再开 live 才成立。

---

## 2. 方法

方法语义与描述性收口一致。下文使用运行时与门禁中的概念名，便于与已锁报告对照；实现细节见 `adaptive_runtime` 的发送、纠错与账本记账说明，以及 [`adaptive-sampling-audit-2026-10-13.md`](adaptive-sampling-audit-2026-10-13.md)、[`adaptive-root-cause-governance-2026-10-11.md`](adaptive-root-cause-governance-2026-10-11.md)。本稿不改代码。

### 2.1 流水线与比较单位

每条队列在任务、策略与设定的交叉上形成单元。策略名为 `evidence_driven`、`generic_iteration` 与 `fixed_queue`。正式队列的规模为 12 个任务 × 3 种策略 × 2 次重复，共 72 个任务单元；修订容量试点为 2 个任务 × 3 种策略 × 1 次重复，共 6 个单元。比较与完成率的单位是 **task**。重复用于描述同一任务上的再次运行，不把两次重复写成两份独立数据集。

一格的工程结局与科学结局分开记录。确认完成指该格开发完成并完成确认。finalist 是可供确认引用的成功实验；修订抬高版本后，旧成功不再继承为新版本的 finalist。科学判断分为四格：supported、refuted、inconclusive、`not_evaluated`。前三格是预注册判断；`not_evaluated` 留在完成率分母中，表示该格没有形成可确认的科学判断。工程失败、无 finalist、确认阶段未完成，都保持在分母里，不从完成率中剔除。

### 2.2 契约拒绝可审计

合同层拒绝与科学结局分账。每一次物理尝试若因校验失败而被拒绝，`model_response_rejected` 回执带有 `attempt_index`、`consumed_send`、`correction_attempt` 以及校验 `details`。运行时在 `response` 与 `decision_completed` 上的拒绝记录带有 `audit`。由此可以逐次回放：这一次是合同文本或字段不合格，还是科学判断已经做出。软跳过不再用来把合同失败堆进后续步骤。门禁烟测（如 `pilot-reject-audit`）只检验这条回执路径是否绑定，不进入对照主表。

### 2.3 预算语义

单元 scope 的发送上限只计算逻辑请求的首次发送，即 `attempt_index == 1`。契约纠错触发的第二次发送仍计入全局发送次数与 token，但不再占用该单元的科学步配额。配额数字保持 `scope_calls=24`，不以抬高上限来抵消纠错消耗。这样，预算耗尽仍是工程分母中的可见结局，同时纠错重发不再把尚未完成的科学步提前吃完。

### 2.4 抽样审计

比较质量不再由布尔 `independence_verified` 授予 `valid`。该布尔曾出现在预测比较与判断路径的语义中；已锁旧报告不因语义收紧而重开。在抽样审计下，只有声明与数据对得上，且独立单位不少于 30，比较才可为 `valid`。两条声明路径为：

- `iid_row`：生成器版本为 `adaptive-tasks/v1`，每个分组键等于该行的特征哈希。
- `documented_cluster`：分组键属于预先登记的簇，并附可引用的抽样记录。没有记录时，状态保持 `insufficient_independent_units`。

现有公开 UCI 行没有此类记录。此时科学判断停在证据不足：描述性预测效果可以保留在记录中，但不得依据行级划分声称机制或新发现。检查项为 `sampling_audit`。

### 2.5 修订容量

新版本不继承旧支持。门禁检查项为 `revision_capacity`，由 `validate` 在修订时执行软拒绝。三条条件任一成立即拒绝本次修订：（1）实验槽已满；（2）已经是最后一步规划；（3）当前版本尚无成功实验。软拒绝把「修订之后还能否复验」留在容量内，使 finalist 有机会在新版本上重新产生。该检查不提高单元发送上限。容量软拒绝与合同层字段拒绝不是同一条路径：缺字段的 `model_response_rejected` 仍走契约回执，不计入容量软拒绝的触发。

### 2.6 账本与指纹分账

各队列绑定各自的源码指纹，并在各自的账本余量下运行。任务身份与确认名单按队列锁定。已锁目录与已锁报告不可变，包括 `formal`、`formal-independent-72`、`formal-root`、`formal-iid-72` 与 `pilot-revision-capacity`。扩样若发生，须使用新的队列名、单独的账本计划与预注册，不得重开旧的暴露行或旧的确认名单。`formal-generated-root` 的 36 个单元保持未启动，且不是本稿所依赖的下一轮。因为指纹与账本不同，跨队列的 supported 计数不可相加，也不可排成一张策略总榜。

### 2.7 队列设计与分析声明

五条已锁队列的身份如下。`formal` 为第一轮，含公开任务与生成任务。`formal-independent-72` 为全新生成任务 N01–N12，与第一轮分账。`formal-root` 为公开任务 U01–U06 加生成任务 R01–R06。`formal-iid-72` 为生成任务 I01–I12，抽样声明为 `iid_row`。`pilot-revision-capacity` 为修订容量指纹下的试点 PR01–PR02。分析只做队列内的四格计数与工程完成率。探索性配对仅在 `formal-iid-72` 一节摘录，并标明非结论：配对只在两侧任务均值都齐全的任务上计算，不齐全的任务仍留在完成率分母中。本稿不做跨队列合并，也不把任一队列的 supported 读成策略优势或相对前轮的恢复。

---

## 3. 结果

### 3.1 跨队列分账

表 1 汇总五条已锁队列的规模、确认完成与科学四格。数字与跨队列索引、描述性收口一致。表内各行独立阅读：完成率的分母是该队列自己的任务单元，失败保留在分母中。

**表 1. 已锁队列的确认完成与科学四格（分账，非排名）**

| 队列 | 规模 | 确认完成 | supported | refuted | inconclusive | not_evaluated | 工程要点 |
|---|---|---|---|---|---|---|---|
| formal | 72 | 66/72 | 6 | 21 | 39 | 6 | 基线；公开无 supported/refuted |
| formal-independent-72 | 72 | 61/72 | 0 | 30 | 31 | 11 | 全新生成；与 formal 分账 |
| formal-root | 72 | 61/72 | 4 | 14 | 43 | 11 | 公开独立性未验证；4 supported 全在生成区 |
| formal-iid-72 | 72 | 67/72 | 18 | 26 | 23 | 5 | 抽样审计；5 未评=修订后未复验 |
| pilot-revision-capacity | 6 | 6/6 | 0 | 1 | 5 | 0 | 修订容量试点；均有 finalist |

来源：[`adaptive-v2-cohort-index-2026-10-14.md`](adaptive-v2-cohort-index-2026-10-14.md)。确认完成从 61/72 到 67/72（试点为 6/6）描述的是各队列流水线走到确认的比例。supported 从 0 到 18 描述的是各队列内部、在各自抽样与指纹条件下落入支持的任务单元数。这两列都不构成策略排序，也不合成「总支持数」。

### 3.2 分队列描述

**formal（第一轮，公开 + 生成，72）。** 确认完成 66/72。科学四格为 supported 6、refuted 21、inconclusive 39、`not_evaluated` 6。已锁 synthesis 将分区记为：公开分区 supported 0、refuted 0、inconclusive 33、`not_evaluated` 3；生成分区 supported 6、refuted 21、inconclusive 6、`not_evaluated` 3。全部 6 个 supported 与全部 21 个 refuted 都在生成区。公开分区没有 supported，也没有 refuted，判断几乎全部为 inconclusive。确认证据质量在该综合中记为 `insufficient_independent_units` 33 与 `valid` 33，与「公开行缺少可审计独立单位则不得声称机制」一致。本队列是后续分账的基线记录，不是策略优劣的参照排名。

**formal-independent-72（N01–N12，72）。** 本队列全部为新生成任务，没有公开任务分区，与第一轮 formal 分开记账。确认完成 61/72。科学四格为 supported 0、refuted 30、inconclusive 31、`not_evaluated` 11。refuted 与 inconclusive 是预注册判断；11 个未评留在完成率分母中，与科学反驳分账。已完成确认的 61 格在该综合中记为 `valid`，11 格没有确认证据。第一轮生成区的 supported 为 6、本队列 supported 为 0，两轮任务、指纹与账本不同。这个差别只说明两条已锁记录不可通约，不能读成策略从有效变为失效。

**formal-root（U01–U06 + R01–R06，72）。** 确认完成 61/72。科学四格为 supported 4、refuted 14、inconclusive 43、`not_evaluated` 11。公开分区 U01–U06 的确认完成为 35/36；该分区没有 supported，也没有 refuted，科学判断为 inconclusive 或未评。公开行的 `independence_verified` 为 false，不把这些行当作独立分析单位，因此公开侧停在证据不足。4 个 supported 全部位于生成分区 R01–R06；该生成分区确认完成 26/36，refuted 14、inconclusive 8、`not_evaluated` 10。生成区的 4 个 supported 是该分区内部的计数，不能读成相对第一轮生成区的恢复。跨公开与生成分区合并配对，不作为结论。

**formal-iid-72（I01–I12，`iid_row`，72）。** 本队列在抽样审计指纹下运行，十二个任务为 `iid_row` 生成任务，没有公开分区，与前序三条正式队列分开记账。确认完成 67/72。科学四格为 supported 18、refuted 26、inconclusive 23、`not_evaluated` 5。已完成确认的 67 格为 `valid`，其抽样声明为可审计的 `iid_row`；5 格没有确认证据。这 5 格都是修订抬高版本后没有 finalist：3 格开发完成但成功实验被修订取代，2 格在单元发送预算耗尽时成功实验被修订取代。它们计入工程分母中的未评。refuted 26 与 inconclusive 23 是预注册判断，与这 5 格工程失败分开。supported 18 是本队列、本指纹下的计数，不能读成相对前序队列的恢复或优势。探索性配对见 §3.3，不在本节作比较结论。`formal-generated-root` 在本队列锁定时仍未启动。

### 3.3 formal-iid-72 的探索性配对（非结论）

本节只摘录 `formal-iid-72` 已锁 synthesis 中的 `paired_strategy_comparisons`。计算限于两侧任务均值都齐全的任务。失败任务不进入差值，同时仍留在 §3.2 的完成率分母（67/72）之中。本节是探索性描述，不是策略排名，也不是可辩护的比较结论。

**表 2. formal-iid-72 探索性配对（非结论）**

| 对比 | 齐全任务 | 均值差 | 探索性 bootstrap 95% CI |
|---|---|---|---|
| evidence_driven − generic_iteration | 7/12 | 0.092 | [−0.362, 0.631] |
| evidence_driven − fixed_queue | 12/12 | −0.093 | [−0.217, −0.006] |
| generic_iteration − fixed_queue | 7/12 | −0.202 | [−0.635, 0.054] |

来源：[`adaptive-formal-iid-72-synthesis-2026-10-13.md`](adaptive-formal-iid-72-synthesis-2026-10-13.md)。evidence_driven 相对 generic_iteration 的区间跨过 0，齐全任务为 7/12。evidence_driven 相对 fixed_queue 的区间位于负侧，且 12/12 任务齐全。generic_iteration 相对 fixed_queue 的区间跨过 0，齐全任务为 7/12。上述区间只描述齐全任务子集上的配对差，不得作为策略优势或可辩护比较结论。区间是否跨过 0，都不改变这一限制。

### 3.4 修订容量试点（仅工程结局）

`pilot-revision-capacity` 在修订容量指纹下运行，任务为 PR01 与 PR02，共 6 个单元，与四条正式队列分开记账。工程结局如下。确认完成 6/6，六格均为开发完成且确认完成。选择阶段均有 finalist，没有 `empty_finalist_reason`。相对 §3.2 中 formal-iid-72 的 5 格「修订后无 finalist → 未评」，本试点没有复现该工程失败模式：6 格都在有 finalist 的情况下完成确认。

已应用 `revise_hypothesis` 的单元为 2 格（`PR02-generic_iteration-r0`，`PR02-evidence_driven-r0`），两格在修订之后仍有 `attempt_completed`。容量软拒绝短语在收据中出现 0 次；已锁 synthesis 核对的短语为 `Revision requires a remaining experiment slot`、`remaining planning step` 与 `successful test of the current explanation version`。另有 7 次修订请求因 `revise_hypothesis` 缺少 `rationale` 而走合同层 `model_response_rejected`，这是契约拒绝，不是容量门禁触发。收据所显示的情形是：修订发生在仍有复验容量时，或修订请求先被合同层拒绝。试点因此记录的是工程结局——修订之后仍能复验并选出 finalist——而不是软拒绝被触发的次数。

n=6 不足以估计容量软拒绝的触发率。表 1 中本队列的科学四格为 supported 0、refuted 1、inconclusive 5、`not_evaluated` 0；其中 supported 为 0 不解释为策略失败或成功。本试点不是正式队列的对照，也不外推触发率。

---

## 4. 讨论

公开数据上的科学判断停在证据不足，原因是抽样声明无法授予 `valid`。第一轮公开分区没有 supported 与 refuted；formal-root 的公开分区同样没有这两格，且独立性标记未把公开行当作独立单位。描述性预测可以留在记录里。机制或新发现需要 `documented_cluster` 这类可引用抽样记录，或生成任务上已经对得上的 `iid_row`。再跑一轮公开行级划分，并不能从「缺少记录」推出 supported。

三种策略在各队列中的角色是协议下的并行条件，不是待排序的处理。supported 的队列内计数、formal-iid-72 齐全子集上的配对区间，以及试点中的 0 个 supported，都停在描述。区间落在负侧、跨过 0，或齐全任务达到 12/12，只说明该子集上的配对差长什么样。完成率分母里的未完成任务不因区间的位置而被排除在外，也不被区间「补偿」。

修订容量的软拒绝在试点收据中未被触发。2 格修订发生在仍有容量之时，另有合同层缺字段拒绝先于容量检查拦住请求。因此触发率仍然未知。6 个单元给出的信息是工程终态：均有 finalist，未复现 formal-iid-72 那种「修订取代成功实验后无法复验」的未评。这个终态绑定试点规模与该指纹，不外推到正式队列的触发频率，也不把科学四格解释成门禁是否「有效」的证明。

队列之间不可通约。formal、formal-independent-72、formal-root、formal-iid-72 与试点的任务身份不同，源码指纹不同，账本余量不同，确认名单按队列锁定。把第一轮生成区的 6 个 supported、独立队列的 0、root 生成区的 4 与 iid-72 的 18 排成序列，得到的是四份已锁记录的并置，不是一条策略曲线。扩样若要形成新的可辩护比较，须新命名队列、单独账本与预注册；比较结论只绑定该新队列。已锁目录保持不变。

`formal-generated-root` 的 36 个单元没有研究库，保持未启动，并且不是本稿的缺失实验。描述性成稿引用的是已锁索引、收口与各队列 synthesis。默认状态下维护这些文档与成稿之间的指针即可。live 重开仅在冻结说明所列的审稿触发、宣称升级或门禁矛盾三类条件出现时另案讨论；那些条件不构成本稿成立的前提。

---

## 5. 局限

抽样审计把公开 UCI 行停在 `insufficient_independent_units` 或等价的证据不足。这是记录状态，不是对公开数据科学内容的完备评估。`documented_cluster` 路径在已锁队列中没有一组可引用抽样记录可供授 `valid`，因此本稿不能展示该路径下的 supported 或 refuted。

容量软拒绝的触发率没有观测点。试点收据中的计数是 0，分母是 6 个单元。用这个比例估计正式规模下的触发频率，超出已锁证据。formal-iid-72 的 5 个未评发生在修订容量检查写入之前的指纹上；试点未复现该模式，同时也不回溯改写那 5 格的数字。

探索性配对依赖齐全任务子集。formal-iid-72 有两组对比只有 7/12 任务齐全。子集上的均值差与 bootstrap 区间对未齐全任务没有定义，而未齐全任务仍在 67/72 的分母里。因此区间不能代替全队列的策略比较。

跨队列并置受到指纹与账本的限制。索引表把五条队列放在同一页，是为了分账检索，不是为了元分析。重复不是独立数据集，任务单元不能跨队列相加。门禁烟测不提供对照效应。

成稿仍停留在描述性终点。目标期刊格式、文献综述、英文全文、矢量图与作者贡献声明属于写作层缺口，不通过新开 live 填补。相关工作的引用须另补；维护文档中的门禁时间线不代替文献综述。

---

## 数据来源

| 内容 | 已锁文件 |
|---|---|
| 跨队列数字与工程要点 | `adaptive-v2-cohort-index-2026-10-14.md`；`adaptive-v2-descriptive-closeout-2026-10-14.md` §3.1 |
| 方法语义 | 收口 §2；抽样审计 `adaptive-sampling-audit-2026-10-13.md`；治理时间线 `adaptive-root-cause-governance-2026-10-11.md` |
| formal 分区 | `adaptive-formal-descriptive-synthesis-2026-10-11.md` |
| formal-independent-72 | `adaptive-independent-72-synthesis-2026-10-12.md` |
| formal-root 分区 | `adaptive-formal-root-synthesis-2026-10-12.md` |
| formal-iid-72 计数与表 2 | `adaptive-formal-iid-72-synthesis-2026-10-13.md` |
| 试点工程结局 | `adaptive-pilot-revision-capacity-synthesis-2026-10-13.md` |
| 默认停止与 live 另案条件 | `adaptive-v2-next-frozen-2026-10-14.md` |

---

## 本稿不宣称

- 三种策略（`evidence_driven` / `generic_iteration` / `fixed_queue`）的优劣或排名。
- 跨队列合并 supported，或合成「总排名」「恢复证明」。
- 把 formal 的 supported 6 到 formal-independent-72 的 supported 0 读成策略失效。
- 把 formal-iid-72 的 supported 18 读成相对前轮的恢复或优势。
- 把探索性配对的 bootstrap 区间（无论是否跨过 0）当作可辩护比较结论。
- 在缺少 `documented_cluster` 或不能授予 `valid` 的公开行级划分上声称机制或新发现。
- 用试点的科学计数（supported 为 0）解释策略成败，或由 n=6、收据 0 次外推软拒绝触发率。
- 将 `pilot-reject-audit` 等门禁烟测当作对照主表。
- 暗示必须再开 `--live` 或启动 `formal-generated-root`，描述性成稿才算完成。

英文稿：[`adaptive-v2-manuscript-draft-en-2026-10-14.md`](adaptive-v2-manuscript-draft-en-2026-10-14.md)。
