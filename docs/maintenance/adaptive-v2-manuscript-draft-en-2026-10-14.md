# Auditable Contracts, Declared Sampling, and Reserved Revision Capacity: A Descriptive Protocol for an Adaptive Scientific-Agent Pipeline

Provenance. This is a descriptive methods manuscript in English, expanded from [`adaptive-v2-manuscript-draft-2026-10-14.md`](adaptive-v2-manuscript-draft-2026-10-14.md) and the outline in [`adaptive-v2-manuscript-outline-2026-10-14.md`](adaptive-v2-manuscript-outline-2026-10-14.md). Every count is taken from [`adaptive-v2-cohort-index-2026-10-14.md`](adaptive-v2-cohort-index-2026-10-14.md), [`adaptive-v2-descriptive-closeout-2026-10-14.md`](adaptive-v2-descriptive-closeout-2026-10-14.md), and the locked cohort syntheses named below. Counts were not recomputed for this draft. The unit of comparison is the **task**. Repeats are not independent data sets. Failures remain in the denominator. This manuscript does not open `--live`, does not reopen a locked cohort, does not start `formal-generated-root`, and does not claim superiority among the three strategies.

---

## Abstract

Adaptive scientific agents close a loop of planning, experimentation, revision, and judgment. When a failed contract check is soft-skipped, when a correction resend consumes a cell’s quota of scientific steps, or when a Boolean independence flag confers comparative validity, the engineering denominator becomes entangled with the scientific outcome. This article records an auditable descriptive protocol. Every contract rejection carries a per-attempt receipt. The scope send cap of a cell counts only the first send of a logical request and does not raise `scope_calls` above 24. A comparison may be marked `valid` only when the sampling declaration matches the data and at least 30 independent units are present. Before a new explanation version is entered, a revision must leave capacity for re-testing. The unit of comparison is the task. Repeats are not independent data sets, and failures remain in the denominator. Locked cohorts are accounted separately and are not commensurable. Confirmation completions were 66/72, 61/72, 61/72, 67/72, and 6/6. Scientific four-way counts are reported inside each cohort. Public partitions that lacked a citable sampling record had no supported or refuted outcomes, or remained at insufficient evidence. Exploratory paired bootstrap intervals for `formal-iid-72` are reported separately and labeled non-conclusive. In a six-cell revision-capacity pilot, every cell completed confirmation and had a finalist. Capacity soft-reject receipts were zero. Six cells are too few to estimate a trigger rate. This manuscript does not claim superiority among the three strategies.

---

## 1. Introduction

Adaptive scientific agents place hypothesis generation, experiment execution, revision of an explanation, and judgment of evidence on one pipeline [6]. The pipeline has to answer two kinds of question at once: whether a cell finished in the engineering sense, and which preregistered judgment the cell received. If those questions share one vague label such as “skipped” or “did not pass,” the denominator is contaminated. A contract failure that is soft-skipped is then counted with later scientific evidence. A second send, issued only to correct a contract, can exhaust the cell’s scientific-step quota before the scientific step is done. On public data, a Boolean flag can treat a row-level split as an independent analysis unit and mark a comparison `valid`, while the sampling process itself has no citable record. Treating non-independent observations as independent replicates is the pseudoreplication problem [1]. In survey sampling, inference is tied to the sampling unit, not to a flag attached after the rows are split [2].

The same entanglement appears after revision. The scientific rule is that a new explanation version does not inherit support from the old one: once revision raises the version, an earlier successful experiment is no longer a finalist. If the experiment slots are already full, planning is already on its last step, or the current version has no successful experiment, the pipeline can still revise and then exhaust the decision or send budget with no re-test. The end state is an empty finalist and a confirmation recorded as `not_evaluated`. That cell is unevaluated inside an engineering denominator. It is not the same event as a preregistered `refuted` or `inconclusive`. Writing both as “the hypothesis was not supported” turns a capacity-design outcome into a scientific refutation.

This article records the accounting rules that were actually gated, in the order the gates landed. It does not select a superior strategy. Contract rejection became an auditable receipt. The scope send cap counts only the first send of a logical request; a correction resend still counts toward the global send total and toward tokens, and `scope_calls` stays 24. Comparative `valid` is no longer conferred by the Boolean `independence_verified`. It requires a sampling declaration that matches the data and at least 30 independent units. A revision must leave re-test capacity before a new version is entered: if any one of experiment slot, planning step, or a successful test of the current version is missing, the revision is soft-rejected. The methodological object is this restatable, gateable protocol, and the ledger that keeps contract rejection, budget, sampling, and revision capacity outside the scientific four-way counts. Traditions of adaptive experimentation [7], auditable scientific software [8], and structured validation of model outputs [9] are not reviewed here. The maintenance timeline is not a literature review.

Evidence is presented on separate ledgers, one locked cohort at a time. The unit of comparison is the task. Repeats on the same task are not an independent data set. Failures stay in that cohort’s own denominator. The five cohorts differ in task identity, source fingerprint, and ledger balance, so confirmation completions and the counts of supported, refuted, inconclusive, and `not_evaluated` are read inside the cohort that produced them. Where a public partition has no citable sampling record, the scientific judgment stays at insufficient evidence. Under the sampling-audit fingerprint, `formal-iid-72` also has an exploratory paired analysis, reported in its own section and labeled non-conclusive. The revision-capacity pilot reports an engineering end state only: the pattern “revised, then unevaluated for lack of a re-test” did not reappear, and the trigger rate of the capacity soft reject is still not estimable. The descriptive endpoint does not depend on opening a live run.

---

## 2. Methods

Method semantics match the descriptive closeout. Runtime and gate names are the names used in the locked reports. Send, correction, and ledger behavior are described in `popper/research/adaptive_runtime.py`. Sampling and governance detail sit in [`adaptive-sampling-audit-2026-10-13.md`](adaptive-sampling-audit-2026-10-13.md) and [`adaptive-root-cause-governance-2026-10-11.md`](adaptive-root-cause-governance-2026-10-11.md). This manuscript does not change code.

### 2.1 Pipeline and unit of comparison

Each cohort crosses tasks, strategies, and settings to form cells. The strategy names are `evidence_driven`, `generic_iteration`, and `fixed_queue`. A formal cohort has 12 tasks × 3 strategies × 2 repeats, which is 72 task cells. The revision-capacity pilot has 2 tasks × 3 strategies × 1 repeat, which is 6 cells. Comparisons and completion rates use the **task** as the unit. A repeat describes another run of the same task. Two repeats are not written up as two independent data sets.

Engineering outcome and scientific outcome are recorded apart. Confirmation complete means the cell finished development and finished confirmation. A finalist is a successful experiment that confirmation can cite. After a revision raises the version, an older success is not inherited as a finalist of the new version. The scientific judgment has four cells: supported, refuted, inconclusive, and `not_evaluated`. The first three are preregistered judgments [3]. `not_evaluated` stays in the completion denominator and means the cell did not form a confirmable scientific judgment. Engineering failure, absence of a finalist, and an unfinished confirmation all remain in the denominator. They are not dropped from the completion rate.

### 2.2 Auditable contract rejection

Contract-layer rejection is ledgered apart from the scientific outcome. When a physical attempt is rejected for a validation failure, the `model_response_rejected` receipt carries `attempt_index`, `consumed_send`, `correction_attempt`, and validation `details`. Rejection records on `response` and `decision_completed` carry `audit`. A cell can therefore be replayed attempt by attempt: the contract text or a field failed, or a scientific judgment was reached. Soft skips are not used to push contract failures into later steps. A gate smoke test such as `pilot-reject-audit` checks only that this receipt path is bound. It does not enter the comparison table.

### 2.3 Budget semantics

The scope send cap of a cell counts only the first send of a logical request, that is `attempt_index == 1`. A second send triggered by contract correction still counts in the global send total and in tokens. It does not consume that cell’s quota of scientific steps. The quota remains `scope_calls=24`. The cap is not raised to offset correction traffic. Budget exhaustion stays a visible outcome in the engineering denominator, and a correction resend does not finish a scientific step that has not yet been taken.

### 2.4 Sampling audit

Comparison quality is no longer granted `valid` by the Boolean `independence_verified`. That Boolean had a meaning on the prediction-comparison and judgment paths. Locked older reports are not reopened because the semantics were tightened. Under the sampling audit, a comparison may be `valid` only when the declaration matches the data and the number of independent units is at least 30. Two declaration paths are defined.

- `iid_row`: the generator version is `adaptive-tasks/v1`, and each grouping key equals the feature hash of that row.
- `documented_cluster`: the grouping key belongs to a cluster that was registered in advance, and a citable sampling record is attached. Without that record, the status remains `insufficient_independent_units`.

Existing public rows from the UCI collection have no such record [10]. The scientific judgment then stays at insufficient evidence. A descriptive predictive result may remain in the record. A mechanism or a new finding may not be claimed from the row-level split. The check name is `sampling_audit`.

### 2.5 Revision capacity

A new version does not inherit old support. The gate check is `revision_capacity`. On a revision, `validate` soft-rejects when any one of three conditions holds: (1) the experiment slots are full; (2) planning is already on its last step; (3) the current version has no successful experiment. The soft reject keeps “can this revision still be re-tested?” inside the capacity, so a finalist can be produced again on the new version. The check does not raise the cell’s send cap. A capacity soft reject and a contract-layer field rejection are different paths. A `model_response_rejected` receipt for a missing field still follows the contract receipt. It is not counted as a firing of the capacity soft reject.

### 2.6 Ledgers and fingerprints

Each cohort is bound to its own source fingerprint and runs under its own ledger balance. Task identity and the confirmation roster are locked per cohort. Locked directories and locked reports are immutable. They are `formal`, `formal-independent-72`, `formal-root`, `formal-iid-72`, and `pilot-revision-capacity`. If the sample is later extended, the extension uses a new cohort name, a separate ledger plan, and a preregistration [3]. It does not reopen an old exposure row or an old confirmation roster. The 36 cells of `formal-generated-root` remain unstarted. They are not a next round on which this manuscript depends. Because fingerprints and ledgers differ, supported counts are not added across cohorts and are not arranged into one strategy table.

### 2.7 Cohort design and analysis statement

The five locked cohorts are as follows. `formal` is the first round and contains public tasks and generated tasks. `formal-independent-72` is the newly generated tasks N01–N12, ledgered apart from the first round. `formal-root` is public tasks U01–U06 plus generated tasks R01–R06. `formal-iid-72` is generated tasks I01–I12, with sampling declaration `iid_row`. `pilot-revision-capacity` is the pilot PR01–PR02 under the revision-capacity fingerprint. The analysis is within-cohort four-way counts and engineering completion. The exploratory pairs are excerpted only in the `formal-iid-72` section and are labeled non-conclusive. A pair is computed only on tasks for which both strategy means are complete. A task that is not complete remains in the completion denominator. This manuscript does not pool cohorts. It does not read the supported count of any cohort as a strategy advantage or as recovery relative to an earlier round.

The exploratory intervals in Section 3.3 are copied from the locked synthesis. They were not re-estimated here. Bootstrap resampling is the method family of those intervals [4, 5]. The locked synthesis, not this draft, is the source of the numerical endpoints. Resample count, seed, and interval variant are not restated, because this manuscript does not recompute them.

---

## 3. Results

### 3.1 Separate cohort ledgers

Table 1 summarizes size, confirmation completion, and the scientific four-way counts for the five locked cohorts. The numbers match the cross-cohort index and the descriptive closeout. Each row is read on its own. The denominator of a completion rate is that cohort’s own task cells. Failures stay in the denominator.

**Table 1. Confirmation completion and scientific four-way counts for locked cohorts (separate ledgers, not a ranking)**

| Cohort | Size | Confirmation complete | supported | refuted | inconclusive | not_evaluated | Engineering note |
|---|---|---|---|---|---|---|---|
| formal | 72 | 66/72 | 6 | 21 | 39 | 6 | Baseline record; public partition has no supported or refuted |
| formal-independent-72 | 72 | 61/72 | 0 | 30 | 31 | 11 | Newly generated; ledgered apart from formal |
| formal-root | 72 | 61/72 | 4 | 14 | 43 | 11 | Public independence not verified; all 4 supported are in the generated partition |
| formal-iid-72 | 72 | 67/72 | 18 | 26 | 23 | 5 | Sampling-audit fingerprint; 5 not evaluated = no re-test after revision |
| pilot-revision-capacity | 6 | 6/6 | 0 | 1 | 5 | 0 | Revision-capacity pilot; every cell had a finalist |

Source: [`adaptive-v2-cohort-index-2026-10-14.md`](adaptive-v2-cohort-index-2026-10-14.md). Confirmation completions of 61/72, 66/72, and 67/72, and 6/6 in the pilot, are the proportions of each cohort’s pipeline that reached confirmation. Supported counts of 0, 4, 6, and 18 are the numbers of task cells that fell in supported inside that cohort, under that cohort’s sampling declaration and fingerprint. Neither column is a strategy ordering. The supported counts are not summed into a total.

### 3.2 Within-cohort description

**formal (first round, public and generated, 72).** Confirmation complete was 66/72. The four-way counts were supported 6, refuted 21, inconclusive 39, and `not_evaluated` 6. The locked synthesis records the partitions as follows. The public partition had supported 0, refuted 0, inconclusive 33, and `not_evaluated` 3. The generated partition had supported 6, refuted 21, inconclusive 6, and `not_evaluated` 3. All 6 supported cells and all 21 refuted cells were in the generated partition. The public partition had no supported outcome and no refuted outcome. Its judgments were almost all inconclusive. Confirmation evidence quality in that synthesis is recorded as `insufficient_independent_units` 33 and `valid` 33, which matches the rule that a public row without an auditable independent unit does not support a mechanism claim. This cohort is the baseline record for later separate ledgers. It is not a reference ranking of strategies.

**formal-independent-72 (N01–N12, 72).** Every task in this cohort was newly generated. There is no public partition. The ledger is separate from the first-round `formal` cohort. Confirmation complete was 61/72. The four-way counts were supported 0, refuted 30, inconclusive 31, and `not_evaluated` 11. Refuted and inconclusive are preregistered judgments. The 11 unevaluated cells stay in the completion denominator and are ledgered apart from scientific refutation. The 61 cells that completed confirmation are recorded as `valid` in that synthesis. The 11 cells have no confirmation evidence. The generated partition of the first round had supported 6. This cohort had supported 0. The tasks, fingerprints, and ledgers of the two rounds differ. The difference shows only that the two locked records are not commensurable. It cannot be read as a strategy becoming ineffective.

**formal-root (U01–U06 and R01–R06, 72).** Confirmation complete was 61/72. The four-way counts were supported 4, refuted 14, inconclusive 43, and `not_evaluated` 11. Confirmation complete in the public partition U01–U06 was 35/36. That partition had no supported outcome and no refuted outcome. Its scientific judgments were inconclusive or `not_evaluated`. `independence_verified` on the public rows was false. Those rows are not treated as independent analysis units, so the public side stays at insufficient evidence. All 4 supported cells were in the generated partition R01–R06. Confirmation complete in that generated partition was 26/36, with refuted 14, inconclusive 8, and `not_evaluated` 10. The 4 supported cells are a count inside that partition. They cannot be read as recovery relative to the generated partition of the first round. Pooled pairs across the public and generated partitions are not a conclusion.

**formal-iid-72 (I01–I12, `iid_row`, 72).** This cohort ran under the sampling-audit fingerprint. The twelve tasks are generated `iid_row` tasks. There is no public partition. The ledger is separate from the three earlier formal cohorts. Confirmation complete was 67/72. The four-way counts were supported 18, refuted 26, inconclusive 23, and `not_evaluated` 5. The 67 cells that completed confirmation are `valid`, and their sampling declaration is the auditable `iid_row` path. Five cells have no confirmation evidence. All five are cells in which revision raised the version and left no finalist: in 3 cells development finished but the successful experiments were replaced by revision, and in 2 cells the successful experiments were replaced when the cell send budget was exhausted. They are counted as unevaluated in the engineering denominator. Refuted 26 and inconclusive 23 are preregistered judgments, kept apart from those 5 engineering outcomes. Supported 18 is a count in this cohort under this fingerprint. It cannot be read as recovery or as an advantage relative to earlier cohorts. Exploratory pairs are in Section 3.3 and are not a comparative conclusion of this section. `formal-generated-root` was still unstarted when this cohort was locked.

### 3.3 Exploratory pairs in formal-iid-72 (non-conclusive)

This section only excerpts `paired_strategy_comparisons` from the locked `formal-iid-72` synthesis. The calculation is restricted to tasks for which both task means are complete. A failed task does not enter the difference. It remains in the completion denominator of Section 3.2, which is 67/72. The section is an exploratory description. It is not a strategy ranking and not a defensible comparative conclusion.

**Table 2. Exploratory pairs for formal-iid-72 (non-conclusive)**

| Contrast | Complete tasks | Mean difference | Exploratory bootstrap 95% interval |
|---|---|---|---|
| evidence_driven − generic_iteration | 7/12 | 0.092 | [−0.362, 0.631] |
| evidence_driven − fixed_queue | 12/12 | −0.093 | [−0.217, −0.006] |
| generic_iteration − fixed_queue | 7/12 | −0.202 | [−0.635, 0.054] |

Source: [`adaptive-formal-iid-72-synthesis-2026-10-13.md`](adaptive-formal-iid-72-synthesis-2026-10-13.md). The interval for `evidence_driven` minus `generic_iteration` crosses zero, on 7/12 complete tasks. The interval for `evidence_driven` minus `fixed_queue` lies entirely below zero, and 12/12 tasks are complete. The interval for `generic_iteration` minus `fixed_queue` crosses zero, on 7/12 complete tasks. These intervals describe paired differences on the complete-task subset only. They are not a claim of strategy superiority and not a defensible comparative conclusion. Whether an interval crosses zero does not change that restriction.

### 3.4 Revision-capacity pilot (engineering end state only)

`pilot-revision-capacity` ran under the revision-capacity fingerprint. The tasks are PR01 and PR02, six cells in total, ledgered apart from the four formal cohorts. The engineering end state is as follows. Confirmation complete was 6/6. All six cells finished development and finished confirmation. Every selection stage had a finalist. There was no `empty_finalist_reason`. Relative to the five `formal-iid-72` cells in Section 3.2 in which revision left no finalist and the cell was `not_evaluated`, this pilot did not reproduce that engineering pattern. All six cells completed confirmation with a finalist present.

`revise_hypothesis` was applied in 2 cells (`PR02-generic_iteration-r0` and `PR02-evidence_driven-r0`). Both cells still had `attempt_completed` after the revision. The capacity soft-reject phrases appeared 0 times in the receipts. The phrases checked in the locked synthesis are `Revision requires a remaining experiment slot`, `remaining planning step`, and `successful test of the current explanation version`. A further 7 revision requests were `model_response_rejected` at the contract layer because `revise_hypothesis` lacked `rationale`. Those are contract rejections. They are not firings of the capacity gate. The receipts show revisions that occurred while re-test capacity remained, or revision requests that the contract layer rejected first. The pilot therefore records an engineering end state: after revision, a re-test was still possible and a finalist was selected. It does not record a count of triggered soft rejects.

Six cells are too few to estimate the trigger rate of the capacity soft reject. In Table 1 the scientific four-way counts for this cohort are supported 0, refuted 1, inconclusive 5, and `not_evaluated` 0. Supported 0 is not interpreted as strategy failure or strategy success. This pilot is not a control arm for the formal cohorts, and the trigger rate is not extrapolated.

---

## 4. Discussion

Scientific judgments on the public data stop at insufficient evidence because the sampling declaration cannot confer `valid`. The public partition of the first round had no supported outcomes and no refuted outcomes. The public partition of `formal-root` likewise had neither, and the independence flag did not treat the public rows as independent units. A descriptive prediction may stay in the record. A mechanism or a new finding requires a citable sampling record of the `documented_cluster` kind, or an `iid_row` declaration on generated tasks that already matches the data. Another round of public row-level splits does not produce supported outcomes out of a missing record.

The three strategies enter each cohort as parallel conditions under the protocol. They are not treatments waiting to be ordered. Within-cohort supported counts, the paired intervals on the complete-task subset of `formal-iid-72`, and the 0 supported cells in the pilot all remain descriptive. An interval that lies below zero, an interval that crosses zero, or a complete-task count of 12/12, states what the paired difference looks like on that subset. Incomplete tasks in the completion denominator are not removed because of where an interval sits, and the interval does not stand in for them.

The revision-capacity soft reject did not appear in the pilot receipts. Two revisions occurred while capacity remained. Other requests were stopped first by a contract-layer rejection for a missing field. The trigger rate is therefore still unknown. What the six cells show is an engineering end state: every cell had a finalist, and the `formal-iid-72` pattern of “revision replaced a successful experiment and no re-test remained” did not reappear. That end state is bound to the pilot’s size and fingerprint. It is not extrapolated to a trigger frequency in the formal cohorts. The scientific four-way counts are not read as proof that the gate is effective.

The cohorts are not commensurable. `formal`, `formal-independent-72`, `formal-root`, `formal-iid-72`, and the pilot differ in task identity, source fingerprint, and ledger balance, and each confirmation roster is locked to its cohort. Placing the 6 supported cells of the first-round generated partition, the 0 of the independent cohort, the 4 of the root generated partition, and the 18 of `formal-iid-72` in one sequence yields four locked records set side by side. It does not yield a strategy curve. A later extension that aims at a defensible comparison needs a newly named cohort, a separate ledger, and a preregistration. Any comparative conclusion would bind only to that new cohort. Locked directories stay as they are.

The 36 cells of `formal-generated-root` have no study library. They remain unstarted, and they are not a missing experiment of this manuscript. The descriptive draft cites the locked index, the closeout, and the cohort syntheses. In the default state, the work is to keep the pointers among those documents and this draft. Reopening a live run is a separate case, and only under the three conditions listed in the freeze note: a review trigger, an upgraded claim, or a contradiction among gates. Those conditions are not premises this manuscript needs in order to stand.

---

## 5. Limitations

The sampling audit leaves public UCI rows at `insufficient_independent_units` or at an equivalent insufficient-evidence status. That is a state of the record. It is not a complete assessment of the scientific content of the public data. The `documented_cluster` path has, in the locked cohorts, no citable sampling record from which `valid` could be granted. This manuscript therefore cannot show supported or refuted outcomes under that path.

The trigger rate of the capacity soft reject has no observation. The count in the pilot receipts is 0, and the denominator is 6 cells. Estimating a trigger frequency at formal scale from that ratio goes beyond the locked evidence. The 5 unevaluated cells in `formal-iid-72` occurred on a fingerprint from before the revision-capacity check was written. The pilot did not reproduce that pattern, and it does not rewrite those 5 cells.

The exploratory pairs depend on the complete-task subset. Two of the contrasts in `formal-iid-72` have only 7/12 tasks complete. The mean difference and the bootstrap interval on that subset are not defined for the incomplete tasks, and the incomplete tasks remain inside the 67/72 denominator. The intervals therefore cannot stand in for a strategy comparison of the full cohort.

Setting the cohorts side by side is limited by fingerprints and ledgers. The index puts five cohorts on one page so that the separate ledgers can be looked up. It is not a meta-analysis. A repeat is not an independent data set, and task cells cannot be added across cohorts. Gate smoke tests do not supply a contrast effect.

The draft remains at a descriptive endpoint. A target journal has not been selected. Related work is cited for language-model scientific agents, adaptive and sequential experimentation, auditable scientific software, structured validation of model outputs, and the UCI repository; those traditions are not reviewed here. Vector figures and an author-contribution statement are not included. Those are writing-layer gaps. They are not filled by opening a live run. The gate timeline in the maintenance documents does not replace a literature review.

---

## What this manuscript does not claim

- Superiority or a ranking among the three strategies (`evidence_driven`, `generic_iteration`, `fixed_queue`).
- A pooled supported count across cohorts, or a synthesized overall ranking, or a proof of recovery.
- Reading supported 6 in `formal` and supported 0 in `formal-independent-72` as a strategy becoming ineffective.
- Reading supported 18 in `formal-iid-72` as recovery or as an advantage relative to an earlier round.
- Treating the exploratory paired bootstrap intervals as a defensible comparative conclusion, whether or not an interval crosses zero.
- A mechanism or a new finding from a public row-level split that lacks `documented_cluster` or that cannot be granted `valid`.
- Reading the pilot’s scientific counts, including supported 0, as strategy failure or strategy success, or extrapolating a soft-reject trigger rate from *n* = 6 and 0 receipts.
- Treating a gate smoke test such as `pilot-reject-audit` as a row of the comparison table.
- Implying that a descriptive manuscript is unfinished until `--live` is opened again or `formal-generated-root` is started.

---

## References

External sources below are standard works for the methods named in the text.

1. Hurlbert, S. H. (1984). Pseudoreplication and the design of ecological field experiments. *Ecological Monographs*, 54(2), 187–211.
2. Kish, L. (1965). *Survey Sampling*. New York: John Wiley & Sons.
3. Nosek, B. A., Ebersole, C. R., DeHaven, A. C., & Mellor, D. T. (2018). The preregistration revolution. *Proceedings of the National Academy of Sciences*, 115(11), 2600–2606.
4. Efron, B. (1979). Bootstrap methods: Another look at the jackknife. *The Annals of Statistics*, 7(1), 1–26.
5. Efron, B., & Tibshirani, R. J. (1993). *An Introduction to the Bootstrap*. New York: Chapman & Hall.
6. Boiko, D. A., MacKnight, R., Kline, B., & Gomes, G. (2023). Autonomous chemical research with large language models. *Nature*, 624, 570–578. https://doi.org/10.1038/s41586-023-06792-0
7. Robbins, H. (1952). Some aspects of the sequential design of experiments. *Bulletin of the American Mathematical Society*, 58(5), 527–535. https://doi.org/10.1090/S0002-9904-1952-09620-8
8. Beaulieu-Jones, B. K., & Greene, C. S. (2017). Reproducibility of computational workflows is automated using continuous analysis. *Nature Biotechnology*, 35(4), 342–346. https://doi.org/10.1038/nbt.3780
9. Willard, B. T., & Louf, R. (2023). Efficient guided generation for large language models. arXiv:2307.09702. https://arxiv.org/abs/2307.09702
10. Kelly, M., Longjohn, R., & Nottingham, K. The UCI Machine Learning Repository. https://archive.ics.uci.edu

### Locked internal sources

Counts and protocol semantics are taken from the following repository documents. They are data sources for this draft, not external literature.

| Content | Locked file |
|---|---|
| Cross-cohort counts and engineering notes | `docs/maintenance/adaptive-v2-cohort-index-2026-10-14.md`; `docs/maintenance/adaptive-v2-descriptive-closeout-2026-10-14.md` §3.1 |
| Method semantics | Closeout §2; `docs/maintenance/adaptive-sampling-audit-2026-10-13.md`; `docs/maintenance/adaptive-root-cause-governance-2026-10-11.md` |
| Send, correction, and ledger names | `popper/research/adaptive_runtime.py` (cited as semantics only; this draft does not edit it) |
| formal partitions | `docs/maintenance/adaptive-formal-descriptive-synthesis-2026-10-11.md` |
| formal-independent-72 | `docs/maintenance/adaptive-independent-72-synthesis-2026-10-12.md` |
| formal-root partitions | `docs/maintenance/adaptive-formal-root-synthesis-2026-10-12.md` |
| formal-iid-72 counts and Table 2 | `docs/maintenance/adaptive-formal-iid-72-synthesis-2026-10-13.md` |
| Pilot engineering end state | `docs/maintenance/adaptive-pilot-revision-capacity-synthesis-2026-10-13.md` |
| Default stop and the separate conditions for a live run | `docs/maintenance/adaptive-v2-next-frozen-2026-10-14.md` |
| Chinese draft this English text follows | `docs/maintenance/adaptive-v2-manuscript-draft-2026-10-14.md` |
