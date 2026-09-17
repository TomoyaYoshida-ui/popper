import argparse
import json
import sys
from pathlib import Path

from .core import Experiment, ProtocolError, initialize, read_json
from .evaluation import Evaluator
from .brownfield import Brownfield
from .campaign_nodes import BUILTIN_NODES, DEFAULT_CAMPAIGN_STEPS
from .corpus import Corpus
from .corpus_seed import seed as seed_corpus
from .evidence import EvidenceStore, MetricsRegistry, run_checks
from .gates import evaluate_gate, read_gate_context
from .ideation import IdeationRun
from .isolation import isolation_status
from .memory import Memory
from .novelty_measure import run as run_a3
from .orchestrator import Orchestrator
from .materialize import Materializer
from .manuscript_flow import ManuscriptFlow
from .observability import Tracer
from .proposer import make_proposer
from .review import RejectionReview
from .reproduction import ReproductionTask, initialize_reproduction, inspect_reproduction
from .vendors import VendorRegistry
from .harness import names as harness_names, resolve as resolve_harness
from .research import DeepSeekResearchPolicy, ResearchController
from .research.revisions import RevisionStore
from .research.workers import RemoteWorker


def _reject_dual_exec_mode(args):
    """--trusted-local 与 --sandbox 互斥：同时提供直接拒绝，避免混淆执行边界。"""
    if getattr(args, "trusted_local", False) and getattr(args, "sandbox", False):
        raise ProtocolError("--trusted-local 与 --sandbox 互斥，只能选择一种执行边界")


def _build_code_harness(name, base_url, model, diagnostics_dir):
    """按名构造编码 Agent 接入；未指定时返回 None，由控制器从策略推导。

    需要端点的接入（``from_endpoint``）必须显式给出 --base-url/--model：缺端点时
    直接报错，而不是退回到「没有提案者」再在执行阶段失败。
    """
    if not name:
        return None
    harness_cls = resolve_harness(name)
    if hasattr(harness_cls, "from_endpoint"):
        if not (base_url and model):
            raise ProtocolError(f"--harness {name} 需要同时提供 --base-url 和 --model")
        return harness_cls.from_endpoint(base_url, model, diagnostics_dir)
    return harness_cls()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="popper", description="Popper 实验推进 Agent · M0")
    commands = parser.add_subparsers(dest="command", required=True)
    research = commands.add_parser(
        "research", help="研究流程：证据驱动控制器 / campaign 编排 / 想法 / 语料 / 证据 / 稿件")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    research_init = research_commands.add_parser("init", help="从已初始化实验建立研究契约与假设")
    research_init.add_argument("project", type=Path)
    research_init.add_argument("run_dir", type=Path)
    research_init.add_argument("--budget-cap", type=float, default=None)
    research_init.add_argument("--base-url", default=None,
                               help="可选 OpenAI 兼容端点，用模型生成可证伪假设")
    research_init.add_argument("--model", default=None)
    research_init.add_argument("--holdout-contract", type=Path, default=None)
    research_init.add_argument("--holdout-public-key", type=Path, default=None,
                               help="独立取得的服务 Ed25519 公钥文本文件；必须在研究开始前固定")
    for research_name in ("run", "confirm"):
        sub = research_commands.add_parser(research_name)
        sub.add_argument("run_dir", type=Path)
        sub.add_argument("--trusted-local", action="store_true")
        sub.add_argument("--sandbox", action="store_true")
        sub.add_argument("--worker-url", default=None,
                         help="远端分离式 worker 服务端基址（如 https://box:8670）；"
                              "留空则用本地 LocalWorker")
        sub.add_argument("--base-url", default=None,
                         help="run 可选：让模型从内核允许的动作中选择")
        sub.add_argument("--model", default=None)
        if research_name == "run":
            sub.add_argument("--max-steps", type=int, default=None)
            sub.add_argument("--auto-confirm", action="store_true",
                             help="达到开发阈值后自动冻结并一次性消费确认集")
            sub.add_argument("--autonomous-code", action="store_true",
                             help="由模型实现选中假设；生成代码只在 --sandbox worker 中执行")
            sub.add_argument("--harness", choices=harness_names(), default=None,
                             help="指定实现代码的编码 Agent 接入（默认从 --base-url/--model 推导）")
    research_status = research_commands.add_parser("status")
    research_status.add_argument("run_dir", type=Path)
    prepare_confirmation = research_commands.add_parser("prepare-confirmation")
    prepare_confirmation.add_argument("run_dir", type=Path)
    accept_confirmation = research_commands.add_parser("accept-confirmation")
    accept_confirmation.add_argument("run_dir", type=Path)
    accept_confirmation.add_argument("receipt", type=Path)
    research_implement = research_commands.add_parser(
        "implement", help="让模型实现候选假设，并在受限 worker 中运行开发集")
    research_implement.add_argument("run_dir", type=Path)
    research_implement.add_argument("--hypothesis", required=True)
    research_implement.add_argument("--base-url", required=True)
    research_implement.add_argument("--model", required=True)
    research_implement.add_argument("--parent-revision", default=None)
    research_implement.add_argument("--gpu-count", type=int, default=0)
    research_implement.add_argument("--worker-url", default=None,
                                   help="远端分离式 worker 服务端基址")
    research_implement.add_argument("--harness", choices=harness_names(), default=None,
                                    help="指定实现代码的编码 Agent 接入（默认从 --base-url/--model 推导）")
    experiment = commands.add_parser(
        "experiment", help="实验项目：生命周期推进 / 复现任务 / 确定性验收")
    experiment_commands = experiment.add_subparsers(dest="experiment_command", required=True)
    vendor = commands.add_parser("vendor", help="检查并调用已登记的开源能力组件")
    vendor_commands = vendor.add_subparsers(dest="vendor_command", required=True)
    web = commands.add_parser("serve", help="打开本地实验工作台")
    web.add_argument("project", type=Path)
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--trusted-local", action="store_true")
    web.add_argument("--campaign", type=Path, default=None,
                     help="监控并交互审批该 campaign 运行目录（含 vendor 步骤时须位于 workspace "
                          "integrations/runs 内；仅实验模式生效）")
    corpus = research_commands.add_parser("corpus", help="领域语料库与社区共享协议（opt-in）")
    corpus_commands = corpus.add_subparsers(dest="corpus_command", required=True)
    for name in ("init", "stats", "list-community", "verify-community"):
        sub = corpus_commands.add_parser(name)
        sub.add_argument("corpus_dir", type=Path)
        if name == "verify-community":
            sub.add_argument("--source", type=Path, default=None)
    corpus_add = corpus_commands.add_parser("add")
    corpus_add.add_argument("corpus_dir", type=Path)
    corpus_add.add_argument("--type", required=True,
                            choices=("gap", "contradiction", "negative_result", "foresight"))
    corpus_add.add_argument("--literature", required=True)
    corpus_add.add_argument("--description", required=True)
    corpus_add.add_argument("--novelty", choices=("major", "incremental", "trivial"), default=None)
    corpus_add.add_argument("--support-level",
                            choices=("full", "partial", "none", "unavailable"), default=None)
    corpus_share = corpus_commands.add_parser("share")
    corpus_share.add_argument("corpus_dir", type=Path)
    corpus_share.add_argument("--record", required=True)
    corpus_seed = corpus_commands.add_parser("seed", help="批量导入领域语料种子（幂等，可增量）")
    corpus_seed.add_argument("corpus_dir", type=Path)
    corpus_seed.add_argument("--count", type=int, default=200)
    corpus_seed.add_argument("--seed-file", type=Path, default=None, help="外部种子 JSON")
    ideation = research_commands.add_parser("ideate", help="生成层候选创新点（ideation + novelty + 预注册）")
    ideate_commands = ideation.add_subparsers(dest="ideate_command", required=True)
    ideate_register = ideate_commands.add_parser("register", help="登记一个组合算子")
    ideate_register.add_argument("run_dir", type=Path)
    ideate_register.add_argument("--name", required=True)
    ideate_register.add_argument("--a", required=True)
    ideate_register.add_argument("--b", required=True)
    ideate_register.add_argument("--order", choices=("A->B", "B->A"), default="A->B")
    ideate_new = ideate_commands.add_parser("new", help="由语料生成候选卡")
    ideate_new.add_argument("corpus_dir", type=Path)
    ideate_new.add_argument("run_dir", type=Path)
    ideate_new.add_argument("--operator", default="gap-to-problem",
                            help="基础算子名或已登记的组合算子名")
    ideate_new.add_argument("--question")
    metrics = research_commands.add_parser("metrics", help="指标注册表管理")
    metrics_commands = metrics.add_subparsers(dest="metrics_command", required=True)
    metrics_list = metrics_commands.add_parser("list")
    metrics_list.add_argument("--registry", type=Path, default=None)
    evidence = research_commands.add_parser("evidence", help="claim registry + 稿件校验（CS 语义版）")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    ev_claim = evidence_commands.add_parser("claim", help="登记一个 claim")
    ev_claim.add_argument("store_dir", type=Path)
    ev_claim.add_argument("claim_id")
    ev_claim.add_argument("--text", required=True)
    ev_claim.add_argument("--category", choices=("claim", "note", "method", "observation", "reference"),
                         default="claim")
    ev_ref = evidence_commands.add_parser("reference", help="登记引用状态")
    ev_ref.add_argument("store_dir", type=Path)
    ev_ref.add_argument("reference_id")
    ev_ref.add_argument("--status", choices=("real", "potential", "hallucinated"), default="real")
    ev_ref.add_argument("--metadata-artifact", type=Path,
                        help="status=real 时必需：解析器保存且含 reference_id/doi/url/id 的 JSON")
    ev_bind = evidence_commands.add_parser("bind", help="绑定 evidence 到 claim")
    ev_bind.add_argument("store_dir", type=Path)
    ev_bind.add_argument("claim_id")
    ev_bind.add_argument("evidence_id")
    ev_bind.add_argument("--value", required=True)
    ev_bind.add_argument("--source", required=True)
    ev_bind.add_argument("--artifact", required=True)
    ev_bind.add_argument("--selector", required=True,
                         help="JSON 点路径（如 metrics.accuracy）或 text:字面值")
    ev_fix = evidence_commands.add_parser("fix", help="局部修复 claim 的证据绑定")
    ev_fix.add_argument("store_dir", type=Path)
    ev_fix.add_argument("claim_id")
    ev_fix.add_argument("evidence_id")
    ev_check = evidence_commands.add_parser("check", help="对稿件运行离线校验")
    ev_check.add_argument("store_dir", type=Path)
    ev_check.add_argument("manuscript", type=Path)
    gate = experiment_commands.add_parser("gate", help="挂载点门禁（L0 确定性 + L1 语义+统计）")
    gate_run = gate.add_subparsers(dest="gate_command", required=True)
    gate_eval = gate_run.add_parser("evaluate", help="评估一个门禁挂载点的 context JSON")
    gate_eval.add_argument("context_path", type=Path)
    evaluate = experiment_commands.add_parser(
        "evaluate", help="评测验收 + 消融回归 + 验收报告（确定性，无 LLM）")
    evaluate_commands = evaluate.add_subparsers(dest="evaluate_command", required=True)
    eval_replay = evaluate_commands.add_parser("replay", help="A4a 机械复现：离线 replay 重算 delta 一致性")
    eval_replay.add_argument("project_dir", type=Path)
    eval_ablation = evaluate_commands.add_parser("ablation", help="消融回归（记录型，不重跑模型）")
    eval_ablation.add_argument("project_dir", type=Path)
    eval_ablation.add_argument("--remove", required=True, choices=("search", "gate", "replay"))
    eval_report = evaluate_commands.add_parser("report", help="汇总 A4a/A4b/A2/可复现包/语料·门禁就绪度")
    eval_report.add_argument("project_dir", type=Path)
    eval_a3 = evaluate_commands.add_parser("a3", help="A3 查新可靠性：受控语料 FPR + 反证幻觉率（确定性）")
    eval_a3.add_argument("case_dir", type=Path)
    review = research_commands.add_parser("review-risk", help="拒稿风险预检（R1-R7 + 7-mode）")
    review.add_argument("--report", type=Path, default=None,
                        help="风险输入 report JSON（各风险判定字段）")
    review.add_argument("--manuscript", type=Path, default=None, help="稿件文件（与 evidence 联用）")
    review.add_argument("--evidence-dir", type=Path, default=None, help="evidence store 目录")
    brownfield = experiment_commands.add_parser("brownfield", help="Brownfield 稿件路径（已有稿件+代码）")
    bf_commands = brownfield.add_subparsers(dest="bf_command", required=True)
    bf_load = bf_commands.add_parser("load")
    bf_load.add_argument("workspace", type=Path)
    bf_load.add_argument("--recursive", action="store_true")
    bf_ingest = bf_commands.add_parser("ingest")
    bf_ingest.add_argument("workspace", type=Path)
    bf_ingest.add_argument("--confirm-all", action="store_true")
    bf_reproduce = bf_commands.add_parser("reproduce")
    bf_reproduce.add_argument("workspace", type=Path)
    bf_reproduce.add_argument("--paper-values-json", type=Path, default=None)
    bf_reproduce.add_argument("--tolerance", type=float, default=0.05)
    bf_audit = bf_commands.add_parser("audit")
    bf_audit.add_argument("workspace", type=Path)
    bf_audit.add_argument("--evidence-dir", type=Path, required=True)
    bf_audit.add_argument("--manuscript", type=Path, required=True)
    bf_gap = bf_commands.add_parser("gap-report")
    bf_gap.add_argument("workspace", type=Path)
    bf_gap.add_argument("--evidence-dir", type=Path, default=None)
    bf_gap.add_argument("--manuscript", type=Path, default=None)
    bf_gap.add_argument("--report-json", type=Path, default=None)
    materialize = research_commands.add_parser(
        "materialize", help="物化层：稿件模板 + 复现包（确定性交付）")
    materialize_commands = materialize.add_subparsers(dest="materialize_command", required=True)
    mat_manuscript = materialize_commands.add_parser("manuscript", help="由 manuscript JSON 渲染稿件模板")
    mat_manuscript.add_argument("project_dir", type=Path)
    mat_manuscript.add_argument("out_dir", type=Path)
    mat_manuscript.add_argument("--manuscript-json", type=Path, required=True)
    mat_manuscript.add_argument("--template", choices=("md", "tex", "docx", "pdf"), default="md")
    mat_repro = materialize_commands.add_parser("repro", help="构建可一键重跑的复现包")
    mat_repro.add_argument("project_dir", type=Path)
    mat_repro.add_argument("out_dir", type=Path)
    paper = research_commands.add_parser("paper", help="稿件事务流编排（写作/投稿/传播）")
    paper_commands = paper.add_subparsers(dest="paper_command", required=True)
    paper_submit = paper_commands.add_parser("submit", help="提交：reviewed->submitted（含 submit 前 gap 报告）")
    paper_submit.add_argument("project", type=Path)
    paper_submit.add_argument("--manuscript", type=Path, required=True, help="稿件文件（供 gap 检查）")
    paper_submit.add_argument("--evidence-dir", type=Path, default=None, help="EvidenceStore 目录")
    paper_submit.add_argument("--reason", default=None)
    paper_publish = paper_commands.add_parser("publish", help="发表：submitted->published")
    paper_publish.add_argument("project", type=Path)
    paper_publish.add_argument("--manuscript", type=Path, default=None)
    paper_publish.add_argument("--reason", default=None)
    paper_revert = paper_commands.add_parser("revert", help="回退到更早状态（必须携带原因）")
    paper_revert.add_argument("project", type=Path)
    paper_revert.add_argument("to", choices=("draft", "reviewed", "submitted", "published"))
    paper_revert.add_argument("--reason", required=True)
    paper_revert.add_argument("--manuscript", type=Path, default=None)
    paper_status = paper_commands.add_parser("status", help="查看稿件状态")
    paper_status.add_argument("project", type=Path)
    paper_share = paper_commands.add_parser("share", help="登记稿件去向（复用 corpus.share 契约）")
    paper_share.add_argument("project", type=Path)
    paper_share.add_argument("--manuscript", type=Path, default=None)
    paper_share.add_argument("--destination", default=None, help="去向（预印本/发表/社区池）")
    paper_share.add_argument("--community-dir", type=Path, default=None, help="社区共享池目录")
    campaign = research_commands.add_parser("campaign", help="LangGraph 编排 campaign（可恢复）")
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    camp_init = campaign_commands.add_parser("init", help="初始化 campaign（步骤以 JSON 传入；缺省用内置默认编排）")
    camp_init.add_argument("run_dir", type=Path)
    camp_init.add_argument("--objective", required=True)
    camp_init.add_argument("--steps-json", type=Path, default=None,
                           help="步骤清单 JSON（key/needs）；缺省用内置 Idea→文献→Scoop→Arbor→dev→freeze→confirm→materialize")
    camp_run = campaign_commands.add_parser("run", help="执行 campaign（内置节点接线真实 Popper 命令）")
    camp_run.add_argument("run_dir", type=Path)
    camp_run.add_argument("--project", type=Path, required=True, help="Popper 实验项目目录（须已 init）")
    camp_run.add_argument("--trusted-local", action="store_true")
    camp_run.add_argument("--sandbox", action="store_true")
    camp_run.add_argument("--base-url", default=None, help="BYOK OpenAI 兼容端点（idea/scoop 依赖）")
    camp_run.add_argument("--model", default=None)
    camp_run.add_argument("--refresh-fulltext", action="store_true", help="重试全文并归档旧查新结果")
    camp_run.add_argument("--queries-json", type=Path, default=None, help="文献检索查询列表 JSON")
    camp_run.add_argument("--start-year", type=int, default=2015)
    camp_run.add_argument("--end-year", type=int, default=2026)
    camp_run.add_argument("--materialize-json", type=Path, default=None,
                          help="稿件 JSON {manuscript, template, disclosure}")
    camp_run.add_argument("--proposal-approved", action="store_true",
                          help="审批 code-propose 产物并物化变体项目（code-materialize 需显式审批）")
    camp_run.add_argument("--allow-provisional", action="store_true",
                          help="放行 provisional Scoop 报告（桥接/dispatch 默认不放行）")
    camp_run.add_argument("--config-index", type=int, default=0,
                          help="变体物化的配置索引（0=baseline，后续=candidates）")
    camp_status = campaign_commands.add_parser("status", help="查看 campaign 进度")
    camp_status.add_argument("run_dir", type=Path)
    memory = research_commands.add_parser(
        "memory", help="上下文三层记忆（工作/短期/长期 + 指针外置 + 驱逐）")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    mem_working = memory_commands.add_parser("working")
    mem_working.add_argument("memory_dir", type=Path)
    mem_working.add_argument("--text", required=True)
    mem_working.add_argument("--clear", action="store_true")
    mem_summary = memory_commands.add_parser("summary")
    mem_summary.add_argument("memory_dir", type=Path)
    mem_summary.add_argument("--json", type=Path, default=None, help="写入短期摘要的 JSON 文件")
    mem_push = memory_commands.add_parser("push")
    mem_push.add_argument("memory_dir", type=Path)
    mem_push.add_argument("--content", required=True)
    mem_push.add_argument("--importance", type=float, default=1.0)
    mem_eject = memory_commands.add_parser("evict")
    mem_eject.add_argument("memory_dir", type=Path)
    mem_eject.add_argument("--target", type=int, default=0)
    mem_snap = memory_commands.add_parser("snapshot")
    mem_snap.add_argument("memory_dir", type=Path)
    observe = research_commands.add_parser("observe", help="V1.5 可观测性：trace / spans / privacy")
    observe_commands = observe.add_subparsers(dest="observe_command", required=True)
    obs_trace = observe_commands.add_parser("trace")
    obs_trace.add_argument("run_dir", type=Path)
    obs_trace.add_argument("--type", required=True)
    obs_trace.add_argument("--model", required=True)
    obs_trace.add_argument("--token-in", dest="token_in", type=int, default=0)
    obs_trace.add_argument("--token-out", dest="token_out", type=int, default=0)
    obs_trace.add_argument("--latency-ms", dest="latency_ms", type=int, default=0)
    obs_trace.add_argument("--event", default="")
    observe_commands.add_parser("spans").add_argument("run_dir", type=Path)
    observe_commands.add_parser("privacy").add_argument("run_dir", type=Path)
    vendor_commands.add_parser("inspect")
    idea_next = vendor_commands.add_parser("idea-next")
    idea_next.add_argument("run_dir", type=Path)
    idea_next.add_argument("--query")
    idea_next.add_argument("--base-url")
    idea_next.add_argument("--model")
    idea_to_arbor = vendor_commands.add_parser("idea-to-arbor")
    idea_to_arbor.add_argument("idea_run_dir", type=Path)
    idea_to_arbor.add_argument("arbor_run_dir", type=Path)
    idea_to_arbor.add_argument("--parent", default="n0")
    paper_search = vendor_commands.add_parser("paper-search")
    paper_search.add_argument("run_dir", type=Path)
    paper_search.add_argument("--query", action="append", required=True)
    paper_search.add_argument("--start-year", type=int, required=True)
    paper_search.add_argument("--end-year", type=int, required=True)
    paper_search.add_argument("--max-papers", type=int, default=10)
    paper_search.add_argument("--sources", nargs="+")
    paper_search.add_argument("--min-score", type=int)
    paper_search.add_argument("--no-parallel", action="store_true")
    paper_search.add_argument("--trusted-local", action="store_true")
    scoop_run = vendor_commands.add_parser("scoop-run")
    scoop_run.add_argument("idea_run_dir", type=Path)
    scoop_run.add_argument("scoop_run_dir", type=Path)
    scoop_run.add_argument("--base-url", required=True)
    scoop_run.add_argument("--model", required=True)
    scoop_run.add_argument("--refresh-fulltext", action="store_true", help="重试全文获取并归档旧的下游证据")
    scoop_run.add_argument("--start-year", type=int, default=2022)
    scoop_run.add_argument("--end-year", type=int, default=2026)
    scoop_run.add_argument("--trusted-local", action="store_true")
    scoop_status = vendor_commands.add_parser("scoop-status")
    scoop_status.add_argument("scoop_run_dir", type=Path)
    scoop_to_arbor = vendor_commands.add_parser("scoop-to-arbor")
    scoop_to_arbor.add_argument("idea_run_dir", type=Path)
    scoop_to_arbor.add_argument("scoop_run_dir", type=Path)
    scoop_to_arbor.add_argument("arbor_run_dir", type=Path)
    scoop_to_arbor.add_argument("--parent", default="n0")
    scoop_to_arbor.add_argument("--allow-provisional", action="store_true")
    arbor_evaluate = vendor_commands.add_parser("arbor-evaluate")
    arbor_evaluate.add_argument("arbor_run_dir", type=Path)
    arbor_evaluate.add_argument("experiment_dir", type=Path)
    arbor_evaluate.add_argument("--node", required=True)
    arbor_evaluate.add_argument("--candidate-index", type=int, required=True)
    arbor_evaluate.add_argument("--trusted-local", action="store_true")
    arbor_dispatch = vendor_commands.add_parser("arbor-dispatch")
    arbor_dispatch.add_argument("idea_run_dir", type=Path)
    arbor_dispatch.add_argument("scoop_run_dir", type=Path)
    arbor_dispatch.add_argument("arbor_run_dir", type=Path)
    arbor_dispatch.add_argument("experiment_dir", type=Path)
    arbor_dispatch.add_argument("--node", required=True)
    arbor_dispatch.add_argument("--base-url", required=True)
    arbor_dispatch.add_argument("--model", required=True)
    arbor_dispatch.add_argument("--allow-provisional", action="store_true")
    arbor_dispatch.add_argument("--trusted-local", action="store_true")
    research_snapshot = vendor_commands.add_parser("research-snapshot")
    research_snapshot.add_argument("arbor_run_dir", type=Path)
    research_snapshot.add_argument("experiment_dir", type=Path)
    code_propose = vendor_commands.add_parser("code-propose")
    code_propose.add_argument("idea_run_dir", type=Path)
    code_propose.add_argument("scoop_run_dir", type=Path)
    code_propose.add_argument("proposal_run_dir", type=Path)
    code_propose.add_argument("experiment_dir", type=Path)
    code_propose.add_argument("--base-url", required=True)
    code_propose.add_argument("--model", required=True)
    code_materialize = vendor_commands.add_parser("code-materialize")
    code_materialize.add_argument("proposal_run_dir", type=Path)
    code_materialize.add_argument("experiment_dir", type=Path)
    code_materialize.add_argument("--config-index", type=int, default=0)
    code_materialize.add_argument("--approved", action="store_true")
    arbor_init = vendor_commands.add_parser("arbor-init")
    arbor_init.add_argument("run_dir", type=Path)
    arbor_init.add_argument("--objective", required=True)
    arbor_init.add_argument("--dev-eval", required=True)
    arbor_init.add_argument("--test-eval", required=True)
    arbor_init.add_argument("--material", default=".")
    arbor_init.add_argument("--metric-direction", choices=("min", "max"), default="max")
    arbor_init.add_argument("--branching", type=int, default=3)
    arbor_init.add_argument("--max-depth", type=int, default=2)
    arbor_init.add_argument("--budget", type=int, default=12)
    for name in ("arbor-observe", "arbor-status", "arbor-validate", "arbor-state", "arbor-cycle"):
        sub = vendor_commands.add_parser(name)
        sub.add_argument("run_dir", type=Path)
    arbor_add = vendor_commands.add_parser("arbor-add")
    arbor_add.add_argument("run_dir", type=Path)
    arbor_add.add_argument("--parent", required=True)
    arbor_add.add_argument("--hypothesis", required=True)
    arbor_evidence = vendor_commands.add_parser("arbor-evidence")
    arbor_evidence.add_argument("run_dir", type=Path)
    arbor_evidence.add_argument("--node", required=True)
    arbor_evidence.add_argument("--dev-score", type=float, required=True)
    arbor_evidence.add_argument("--result", required=True)
    arbor_evidence.add_argument("--insight", required=True)
    arbor_evidence.add_argument("--branch-ref", required=True)
    arbor_propagate = vendor_commands.add_parser("arbor-propagate")
    arbor_propagate.add_argument("run_dir", type=Path)
    arbor_propagate.add_argument("--node", required=True)
    arbor_propagate.add_argument("--insight", required=True)
    arbor_propagate.add_argument("--to-root", action="store_true")
    arbor_prune = vendor_commands.add_parser("arbor-prune")
    arbor_prune.add_argument("run_dir", type=Path)
    arbor_prune.add_argument("--node", required=True)
    arbor_prune.add_argument("--reason", required=True)
    arbor_merge = vendor_commands.add_parser("arbor-merge")
    arbor_merge.add_argument("run_dir", type=Path)
    arbor_merge.add_argument("--node", required=True)
    arbor_merge.add_argument("--test-score", type=float, required=True)
    arbor_merge.add_argument("--branch-ref", required=True)
    for name in ("init", "scan", "search", "freeze", "confirm", "status", "report", "replay", "recover"):
        sub = experiment_commands.add_parser(name)
        sub.add_argument("project", type=Path)
        if name in ("search", "confirm"):
            sub.add_argument("--trusted-local", action="store_true",
                             help="执行已信任代码（无文件写作用域）")
            sub.add_argument("--sandbox", action="store_true",
                             help="OS 沙箱执行：Windows 低完整性文件写作用域（仅运行工作区可写）")
        if name == "search":
            sub.add_argument("--base-url", help="OpenAI-compatible /v1 URL；会发送目标、候选配置和开发集指标")
            sub.add_argument("--model")
        if name == "recover":
            sub.add_argument("--runner-stopped", action="store_true", help="确认原执行进程已结束")
    reproduce = experiment_commands.add_parser("reproduce", help="运行通用论文复现任务")
    reproduce_commands = reproduce.add_subparsers(dest="reproduce_command", required=True)
    for name in ("inspect", "init", "run", "verify", "status"):
        sub = reproduce_commands.add_parser(name)
        sub.add_argument("project", type=Path)
        if name in {"run", "verify"}:
            sub.add_argument("--trusted-local", action="store_true")
    archive = experiment_commands.add_parser("archive", help="审计日志压缩归档（冷存储分层，不删除原库）")
    archive.add_argument("project", type=Path)
    isolation = experiment_commands.add_parser("isolation", help="安全边界 / 隔离状态自检")
    isolation.add_argument("project", type=Path)
    worker = research_commands.add_parser(
        "worker", help="研究 worker 运行时：分离式执行与 /v1/jobs 远端服务")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    supervise = worker_commands.add_parser("supervise", help=argparse.SUPPRESS,
                                           description="分离式 supervisor：detached 执行单个 job（隐藏命令）")
    supervise.add_argument("root", type=Path)
    supervise.add_argument("job_id")
    supervise.add_argument("--revisions", type=Path, default=None)
    worker_serve = worker_commands.add_parser(
        "serve", help="以 /v1/jobs REST 暴露 LocalWorker（部署到云端 GPU 盒子）")
    worker_serve.add_argument("jobs_root", type=Path)
    worker_serve.add_argument("--revisions", type=Path, required=True,
                              help="不可变 CodeRevision 仓库根目录")
    worker_serve.add_argument("--host", default="0.0.0.0")
    worker_serve.add_argument("--port", type=int, default=8670)
    worker_serve.add_argument("--token-env", default="POPPER_WORKER_TOKEN")
    args = parser.parse_args(argv)
    experiment = None
    research_controller = None
    try:
        if args.command == "serve":
            from .server import serve
            serve(args.project, args.port, args.trusted_local, args.campaign)
            return 0
        elif args.command == "research":
            if args.research_command == "worker":
                if args.worker_command == "supervise":
                    from .research.workers.supervisor import supervise_job
                    result = supervise_job(args.root, args.job_id, args.revisions)
                else:
                    from .research.workers.server import serve as serve_worker
                    serve_worker(args.jobs_root, args.revisions, args.host, args.port,
                                 args.token_env)
                    return 0
            elif args.research_command == "init":
                if bool(args.base_url) != bool(args.model):
                    raise ProtocolError("--base-url 和 --model 必须同时提供")
                policy = (DeepSeekResearchPolicy(args.base_url, args.model,
                                                 args.run_dir / "model-diagnostics")
                          if args.model else None)
                result = ResearchController.initialize(
                    args.project, args.run_dir, policy=policy,
                    budget_cap=args.budget_cap,
                    confirmation_contract=read_json(args.holdout_contract) if args.holdout_contract else None,
                    confirmation_public_key=args.holdout_public_key.read_text(encoding="utf-8").strip()
                    if args.holdout_public_key else None)
            elif args.research_command == "corpus":
                corpus = Corpus(args.corpus_dir)
                if args.corpus_command == "init":
                    result = corpus.initialize()
                elif args.corpus_command == "add":
                    result = corpus.add(args.type, args.literature, args.description,
                                        args.novelty, support_level=args.support_level)
                elif args.corpus_command == "share":
                    result = corpus.share(args.record)
                elif args.corpus_command == "stats":
                    result = corpus.stats()
                elif args.corpus_command == "list-community":
                    result = {"records": corpus.list_community()}
                elif args.corpus_command == "seed":
                    result = seed_corpus(args.corpus_dir, count=args.count,
                                         seed_file=args.seed_file)
                else:
                    result = corpus.verify_community(args.source)
            elif args.research_command == "ideate":
                run = IdeationRun(args.run_dir)
                if args.ideate_command == "register":
                    result = run.register_operator(args.name, args.a, args.b, args.order)
                else:
                    corpus = Corpus(args.corpus_dir)
                    card = run.ideate(corpus, operator=args.operator, question=args.question)
                    result = {"card": card, "commit": run.commit(card)}
            elif args.research_command == "metrics":
                registry = MetricsRegistry(args.registry)
                result = {"metrics": registry.load()}
            elif args.research_command == "evidence":
                store = EvidenceStore(args.store_dir)
                if args.evidence_command == "claim":
                    result = store.register_claim(args.claim_id, args.text, args.category)
                elif args.evidence_command == "reference":
                    result = store.register_reference(args.reference_id, args.status,
                                                       metadata_artifact=args.metadata_artifact)
                elif args.evidence_command == "bind":
                    result = store.bind(args.claim_id, args.evidence_id, args.value,
                                        args.source, args.artifact, selector=args.selector)
                elif args.evidence_command == "fix":
                    result = store.fix_claim(args.claim_id, args.evidence_id)
                else:
                    result = run_checks(args.manuscript, store)
            elif args.research_command == "review-risk":
                report = read_json(args.report) if args.report else None
                store = EvidenceStore(args.evidence_dir) if args.evidence_dir else None
                reviewer = RejectionReview(store, args.manuscript)
                if args.manuscript:
                    result = reviewer.from_manuscript(args.manuscript, report)
                else:
                    result = reviewer.assess(report)
            elif args.research_command == "materialize":
                materializer = Materializer(args.project_dir)
                if args.materialize_command == "manuscript":
                    manuscript_json = read_json(args.manuscript_json)
                    result = materializer.materialize(manuscript_json, args.out_dir,
                                                      args.template)
                else:
                    result = materializer.build_repro_package(args.out_dir)
            elif args.research_command == "paper":
                flow = ManuscriptFlow(Experiment(args.project))
                if args.paper_command == "submit":
                    result = flow.submit(args.manuscript, args.evidence_dir, args.reason)
                elif args.paper_command == "publish":
                    result = flow.publish(args.manuscript, args.reason)
                elif args.paper_command == "revert":
                    result = flow.revert(args.manuscript, args.to, args.reason)
                elif args.paper_command == "share":
                    result = flow.share(args.manuscript, args.destination, args.community_dir)
                else:
                    result = flow.status()
            elif args.research_command == "campaign":
                orch = Orchestrator(args.run_dir)
                if args.campaign_command == "init":
                    steps = (read_json(args.steps_json) if args.steps_json
                             else DEFAULT_CAMPAIGN_STEPS)
                    result = orch.init(args.objective, steps)
                elif args.campaign_command == "run":
                    _reject_dual_exec_mode(args)
                    mode = "trusted_local" if args.trusted_local else \
                        ("sandboxed" if args.sandbox else None)
                    if mode is None:
                        raise ProtocolError("campaign run 需要 --trusted-local 或 --sandbox")
                    manifest = orch._load()
                    # vendor 节点（idea/literature/scoop/arbor/dispatch/propose/variant）要求
                    # 运行目录位于 workspace 根 integrations/runs 内（VendorRegistry._run_dir 约束）；
                    # 纯 Experiment 步骤（dev/freeze/confirm/materialize）不受限。
                    if any(s["key"] in {"idea", "literature", "scoop", "arbor",
                                        "dispatch", "propose", "variant"}
                           for s in manifest.get("steps", [])):
                        runs_root = (VendorRegistry().project_root / "integrations" / "runs").resolve()
                        if not args.run_dir.resolve().is_relative_to(runs_root):
                            raise ProtocolError(
                                "含 vendor 步骤时 campaign run_dir 必须位于 "
                                "<workspace>/integrations/runs 内")
                    config = {
                        "project": str(args.project.resolve()),
                        "mode": mode,
                        "base_url": args.base_url,
                        "model": args.model,
                        "queries": read_json(args.queries_json) if args.queries_json else None,
                        "start_year": args.start_year,
                        "end_year": args.end_year,
                        "materialize": read_json(args.materialize_json)
                        if args.materialize_json else None,
                        "proposal_approved": args.proposal_approved,
                        "allow_provisional": args.allow_provisional,
                        "refresh_fulltext": args.refresh_fulltext,
                        "config_index": args.config_index,
                        "objective": (manifest.get("objective") or ""),
                    }
                    result = orch.run(nodes=BUILTIN_NODES, config=config)
                else:
                    result = orch._load()
            elif args.research_command == "memory":
                mem = Memory(args.memory_dir)
                if args.memory_command == "working":
                    if args.clear:
                        mem.clear_working()
                        result = {"status": "cleared"}
                    else:
                        mem.set_working(args.text)
                        result = {"status": "set", "bytes": mem.working_size()}
                elif args.memory_command == "summary":
                    if args.json:
                        mem.set_summary(read_json(args.json))
                    result = {"summary": mem.summary()}
                elif args.memory_command == "push":
                    result = mem.externalize(args.content, args.importance)
                elif args.memory_command == "evict":
                    evicted = mem.evict(args.target)
                    result = {"evicted": evicted, "count": len(evicted)}
                else:
                    result = mem.snapshot()
            elif args.research_command == "observe":
                tracer = Tracer(args.run_dir)
                if args.observe_command == "trace":
                    span = {"type": args.type, "model": args.model,
                            "token_in": args.token_in, "token_out": args.token_out,
                            "latency_ms": args.latency_ms, "event": args.event}
                    tracer.trace(span)
                    result = {"status": "traced", "count": len(tracer.spans())}
                elif args.observe_command == "spans":
                    result = {"spans": tracer.spans()}
                else:
                    result = {"privacy_clean": tracer.privacy_scan()}
            else:
                if args.research_command in {"run", "confirm", "implement"}:
                    if bool(args.base_url) != bool(args.model):
                        raise ProtocolError("--base-url 和 --model 必须同时提供")
                    policy = (DeepSeekResearchPolicy(args.base_url, args.model,
                                                     args.run_dir / "model-diagnostics")
                              if args.model else None)
                else:
                    policy = None
                worker = None
                if getattr(args, "worker_url", None):
                    worker = RemoteWorker(args.worker_url,
                                          revisions=RevisionStore(args.run_dir / "revisions"))
                research_controller = ResearchController(args.run_dir, policy=policy,
                                                         worker=worker,
                                                         harness=_build_code_harness(
                                                             getattr(args, "harness", None),
                                                             getattr(args, "base_url", None),
                                                             getattr(args, "model", None),
                                                             args.run_dir / "model-diagnostics"))
                if args.research_command == "run":
                    result = research_controller.run(
                        args.trusted_local, args.sandbox, args.max_steps,
                        args.auto_confirm, args.autonomous_code)
                elif args.research_command == "confirm":
                    result = research_controller.confirm(args.trusted_local, args.sandbox)
                elif args.research_command == "implement":
                    result = research_controller.implement(
                        args.hypothesis, args.parent_revision, args.gpu_count)
                elif args.research_command == "prepare-confirmation":
                    result = research_controller.prepare_external_confirmation()
                elif args.research_command == "accept-confirmation":
                    result = research_controller.accept_external_confirmation(read_json(args.receipt))
                else:
                    result = research_controller.status()
        elif args.command == "experiment" and args.experiment_command == "reproduce":
            if args.reproduce_command == "inspect":
                result = inspect_reproduction(args.project)
            elif args.reproduce_command == "init":
                result = initialize_reproduction(args.project)
            else:
                task = ReproductionTask(args.project)
                if args.reproduce_command == "run":
                    result = task.run(args.trusted_local)
                elif args.reproduce_command == "verify":
                    result = task.verify(args.trusted_local)
                else:
                    result = task.audit()
        elif args.command == "experiment" and args.experiment_command == "gate":
            result = evaluate_gate(read_gate_context(args.context_path))
        elif args.command == "experiment" and args.experiment_command == "brownfield":
            bf = Brownfield(args.workspace)
            if args.bf_command == "load":
                result = bf.load(True)  # B1 递归扫描整个工作区
            elif args.bf_command == "ingest":
                result = bf.ingest(args.confirm_all)
            elif args.bf_command == "reproduce":
                paper = read_json(args.paper_values_json) if args.paper_values_json else {}
                result = bf.reproduce(paper, args.tolerance)
            elif args.bf_command == "audit":
                store = EvidenceStore(args.evidence_dir)
                result = bf.audit(store, args.manuscript)
            else:
                store = EvidenceStore(args.evidence_dir) if args.evidence_dir else None
                reviewer_report = read_json(args.report_json) if args.report_json else None
                result = bf.gap_report(store, args.manuscript, reviewer_report)
        elif args.command == "vendor":
            registry = VendorRegistry()
            if args.vendor_command == "inspect":
                result = registry.inspect()
            elif args.vendor_command == "idea-next":
                result = registry.idea_next(args.run_dir, args.query,
                                            args.base_url, args.model)
            elif args.vendor_command == "idea-to-arbor":
                result = registry.idea_to_arbor(
                    args.idea_run_dir, args.arbor_run_dir, args.parent)
            elif args.vendor_command == "paper-search":
                result = registry.paper_search(args.run_dir, args.query, args.start_year,
                                               args.end_year, args.max_papers, args.sources,
                                               args.min_score, not args.no_parallel,
                                               args.trusted_local)
            elif args.vendor_command == "scoop-run":
                result = registry.scoop_run(args.idea_run_dir, args.scoop_run_dir,
                                            args.base_url, args.model,
                                            args.start_year, args.end_year,
                                            refresh_fulltext=args.refresh_fulltext,
                                            trusted_local=args.trusted_local)
            elif args.vendor_command == "scoop-status":
                result = registry.scoop_status(args.scoop_run_dir)
            elif args.vendor_command == "scoop-to-arbor":
                result = registry.scoop_to_arbor(
                    args.idea_run_dir, args.scoop_run_dir, args.arbor_run_dir,
                    args.parent, args.allow_provisional)
            elif args.vendor_command == "arbor-evaluate":
                result = registry.arbor_evaluate(
                    args.arbor_run_dir, args.experiment_dir, args.node,
                    args.candidate_index, args.trusted_local)
            elif args.vendor_command == "arbor-dispatch":
                result = registry.arbor_dispatch(
                    args.idea_run_dir, args.scoop_run_dir, args.arbor_run_dir,
                    args.experiment_dir, args.node, args.base_url, args.model,
                    args.trusted_local, args.allow_provisional)
            elif args.vendor_command == "research-snapshot":
                result = registry.research_snapshot(args.arbor_run_dir, args.experiment_dir)
            elif args.vendor_command == "code-propose":
                result = registry.code_propose(
                    args.idea_run_dir, args.scoop_run_dir, args.proposal_run_dir,
                    args.experiment_dir, args.base_url, args.model)
            elif args.vendor_command == "code-materialize":
                result = registry.code_materialize(
                    args.proposal_run_dir, args.experiment_dir,
                    args.config_index, args.approved)
            elif args.vendor_command == "arbor-init":
                result = registry.arbor_init(args.run_dir, args.objective, args.dev_eval,
                                             args.test_eval, args.material, args.metric_direction,
                                             args.branching, args.max_depth, args.budget)
            elif args.vendor_command == "arbor-state":
                result = registry.arbor_state(args.run_dir)
            elif args.vendor_command == "arbor-cycle":
                result = registry.arbor_cycle(args.run_dir)
            elif args.vendor_command == "arbor-add":
                result = registry.arbor_add(args.run_dir, args.parent, args.hypothesis)
            elif args.vendor_command == "arbor-evidence":
                result = registry.arbor_evidence(args.run_dir, args.node, args.dev_score,
                                                  args.result, args.insight, args.branch_ref)
            elif args.vendor_command == "arbor-propagate":
                result = registry.arbor_propagate(args.run_dir, args.node, args.insight, args.to_root)
            elif args.vendor_command == "arbor-prune":
                result = registry.arbor_prune(args.run_dir, args.node, args.reason)
            elif args.vendor_command == "arbor-merge":
                result = registry.arbor_merge(args.run_dir, args.node, args.test_score,
                                               args.branch_ref)
            else:
                result = registry.arbor_read(args.run_dir, args.vendor_command.removeprefix("arbor-"))
        elif args.command == "observe":
            tracer = Tracer(args.run_dir)
            if args.observe_command == "trace":
                span = {"type": args.type, "model": args.model,
                        "token_in": args.token_in, "token_out": args.token_out,
                        "latency_ms": args.latency_ms, "event": args.event}
                tracer.trace(span)
                result = {"status": "traced", "count": len(tracer.spans())}
            elif args.observe_command == "spans":
                result = {"spans": tracer.spans()}
            else:
                result = {"privacy_clean": tracer.privacy_scan()}
        elif args.command == "experiment" and args.experiment_command == "init":
            result = initialize(args.project)
        elif args.command == "experiment" and args.experiment_command == "scan":
            root = args.project.resolve()
            if not root.is_dir():
                raise ProtocolError("项目目录不存在")
            # Read-only, bounded, explicit-file ingestion. No environment or hidden-file scanning.
            files = []
            for path in root.iterdir():
                if path.is_file() and path.suffix.lower() in {".py", ".md", ".tex", ".json"}:
                    files.append({"path": path.name, "bytes": path.stat().st_size})
            result = {"project": str(root), "files": sorted(files, key=lambda f: f["path"]),
                      "adapter": "registered_python_mse", "ready_for_init": (root / "experiment.json").is_file(),
                      "scope": "顶层文件清单；不自动推断实验语义"}
        elif args.command == "experiment" and args.experiment_command == "evaluate":
            if args.evaluate_command == "a3":
                result = run_a3(args.case_dir)
            else:
                evaluator = Evaluator(args.project_dir)
                if args.evaluate_command == "replay":
                    result = evaluator.replay_check()
                elif args.evaluate_command == "ablation":
                    result = evaluator.ablation(remove=args.remove)
                else:
                    result = evaluator.acceptance_report()
        elif args.command == "experiment" and args.experiment_command == "archive":
            experiment = Experiment(args.project)
            result = experiment.archive_events(Path(args.project) / ".popper" / "archive")
        elif args.command == "experiment" and args.experiment_command == "isolation":
            result = isolation_status(args.project)
        else:
            experiment = Experiment(args.project)
            if args.experiment_command == "search":
                if bool(args.base_url) != bool(args.model):
                    raise ProtocolError("--base-url 和 --model 必须同时提供")
                _reject_dual_exec_mode(args)
                proposer = make_proposer(args.base_url, args.model) if args.model else None
                result = experiment.search(trusted_local=args.trusted_local,
                                           sandboxed=args.sandbox, proposer=proposer)
            elif args.experiment_command == "confirm":
                _reject_dual_exec_mode(args)
                result = experiment.confirm(args.trusted_local, args.sandbox)
            elif args.experiment_command == "recover":
                if not args.runner_stopped:
                    raise ProtocolError("先停止原执行进程，再使用 --runner-stopped")
                result = experiment.recover()
            else:
                result = getattr(experiment,
                                 args.experiment_command
                                 if args.experiment_command != "status" else "state")()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ProtocolError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Popper: {error}", file=sys.stderr)
        return 2
    finally:
        if experiment:
            experiment.close()
        if research_controller:
            research_controller.close()
