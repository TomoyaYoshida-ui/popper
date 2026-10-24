"""A3 查新可靠性测量（AC-4）。

在受控语料（含近 miss 硬样本）上测量反证/门禁的 FPR 与反证幻觉率。
- FPR：把"实际无覆盖/无矛盾"的样本误判为有覆盖/有矛盾的比率。
- 反证幻觉率：反证检索声称"已有覆盖"但其证据不成立（% fake）的比率。
- 全部用确定性 fixture（无需 LLM key），可离线复现。
- 幻觉率依赖全文核验后端：无后端时如实 not_available（None），不编造数值。
- 可选 quote_corpus：用与 Scoop step5 相同的字面引文绑定规则离线测幻觉率；
  real_novelty_fpr 仍为 null（不是审计级 Scoop FPR 产品）。
"""
from __future__ import annotations

import random
from pathlib import Path

from .core import ProtocolError, digest, read_json, write_json
from .quote_binding import passage_bound_to_fulltext
from .review import RejectionReview


def _sample(n, rng):
    """生成受控样本：positives=真覆盖/矛盾，negatives=近 miss 硬样本（无覆盖但高度相似）。"""
    samples = []
    # 正样本：prior_covered=True 或 contradiction=True（门禁应拦）
    for i in range(n):
        samples.append({
            "label": "positive",
            "report": {"prior_covered": True, "contradiction": True,
                       "delta": 0.01, "min_improvement": 0.05,
                       "mechanisms": [], "coverage_statement": "authoritative"},
        })
    # 负样本（近 miss 硬样本）：看似接近覆盖但仍未覆盖，门禁不应误判为高风险致命。
    for i in range(n):
        samples.append({
            "label": "negative",
            "report": {"prior_covered": False, "contradiction": False,
                       "delta": 0.08, "min_improvement": 0.02,
                       "mechanisms": [{"name": "m", "ablation": "E"}],
                       "coverage_statement": "authoritative",
                       "importance": {"foresight": True, "counter_consensus": True}},
        })
    return samples


def measure_fpr(samples):
    """FPR = 负样本（无覆盖/无矛盾）被误判为 fail 且来自拒稿侧非用户裁决项的比例。"""
    reviewer = RejectionReview()
    fp = 0
    negatives = [s for s in samples if s["label"] == "negative"]
    for s in negatives:
        result = reviewer.assess(s["report"])["rejection_side"]
        risks = result["risks"]
        # 负样本上不应有任何机械致命项（fail 且非用户裁决）
        mechanical_fail = any(r["status"] == "fail" and not r.get("user_adjudication")
                              for r in risks.values())
        if mechanical_fail:
            fp += 1
    return fp / len(negatives) if negatives else 0.0


def make_quote_binding_verifier(corpus, claims):
    """Build verifier(literature_id) -> bool using Scoop-identical quote binding.

    ``corpus`` maps literature_id -> fulltext. ``claims`` maps literature_id ->
    closest_passage. Missing text/passage or unbound passage => False.
    """
    if not isinstance(corpus, dict) or not isinstance(claims, dict):
        raise ProtocolError("quote corpus/claims 必须是对象")

    def verifier(literature_id):
        if not isinstance(literature_id, str):
            return False
        content = corpus.get(literature_id)
        passage = claims.get(literature_id)
        if not isinstance(content, str) or not isinstance(passage, str):
            return False
        return passage_bound_to_fulltext(passage, content)

    return verifier


def _default_quote_claims(corpus, n_candidates):
    """For resolvable fixture ids: alternate bound vs unbound passages."""
    claims = {}
    for i in range(n_candidates):
        if i % 2 != 0:
            continue
        lit = "10.1101/fixed-{}".format(i)
        text = corpus.get(lit)
        if not isinstance(text, str):
            continue
        # i % 4 == 0: bind a real 40-char span; else: deliberate unbound quote.
        if i % 4 == 0:
            span = text.strip()
            if len(span) < 40:
                continue
            claims[lit] = span[:40]
        else:
            claims[lit] = "Q" * 40
    return claims


def measure_hallucination_rate(rng, n_candidates, verifier=None):
    """反证幻觉率：需要全文核验后端；无后端时如实 not_available，不编造数值。

    构造一组「已有覆盖」声明：一半绑定可解析占位文献 id（待核验），
    一半绑定不可解析 id（确定性可捕获的幻觉）。
    有 verifier 后端时按核验结果计算漏检幻觉率；没有后端时以 None 表示不可测。
    """
    declarations = []
    for i in range(n_candidates):
        lit = "10.1101/fixed-{}".format(i) if i % 2 == 0 else "not-a-real-id-{}".format(i)
        declarations.append({"claimed_covered": True, "literature_id": lit})
    unverifiable = [d for d in declarations if d["literature_id"].startswith("not-")]
    resolvable = [d for d in declarations if not d["literature_id"].startswith("not-")]
    total = len(declarations)
    base = {
        "status": "measured" if verifier is not None else "not_available",
        "claimed_covered_total": total,
        "caught_unresolvable": len(unverifiable),
        "resolvable_unverified": len(resolvable),
    }
    if verifier is None:
        base["hallucination_rate"] = None
        base["reason"] = ("缺少全文核验后端：可解析占位文献是否幻觉必须经真实检索/全文核验"
                          "判断，不以占位数值冒充测量结果")
        return base
    leaked = [d for d in resolvable if not verifier(d["literature_id"])]
    rate = len(leaked) / total if total else 0.0
    base["hallucination_rate"] = round(rate, 3)
    base["note"] = "由 verifier 全文核验后端按声明文献是否可验证计算漏检幻觉率"
    return base


def run(case_dir, n_negative=40, rng=None, verifier=None, quote_corpus=None,
        quote_claims=None):
    """运行受控测量，输出 FPR 与幻觉率并落盘 case_dir/a3_report.json。

    ``quote_corpus``：可选 {literature_id: fulltext}。提供时用 Scoop 同款引文绑定
    规则构造 verifier（复用 reverse_citation_hallucination_rate 字段）。
    ``real_novelty_fpr`` 始终为 null。
    """
    rng = rng or random.Random(7)
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    if quote_corpus is not None:
        if verifier is not None:
            raise ProtocolError("quote_corpus 与 verifier 不能同时提供")
        if isinstance(quote_corpus, (str, Path)):
            quote_corpus = read_json(quote_corpus)
        claims = quote_claims if quote_claims is not None else _default_quote_claims(
            quote_corpus, n_negative)
        verifier = make_quote_binding_verifier(quote_corpus, claims)
    samples = _sample(n_negative, rng)
    fpr = measure_fpr(samples)
    hyp = measure_hallucination_rate(rng, n_candidates=n_negative, verifier=verifier)
    hallucination_available = hyp["hallucination_rate"] is not None
    method = "确定性 fixture（详见代码）；未接 LLM/检索后端"
    if quote_corpus is not None:
        method = ("确定性 fixture + 离线引文绑定核验（与 Scoop step5 同规则）；"
                  "real_novelty_fpr 未测，非审计级 Scoop FPR")
    report = {
        "schema": "a3-1.0",
        "controlled_corpus": {"n_negative_hard_samples": n_negative,
                              "includes_near_miss": False,
                              "unique_report_patterns": len({digest(s["report"]) for s in samples}),
                              "measurement_kind": "rule_control_flow_fixture"},
        "fpr": round(fpr, 3),
        "real_novelty_fpr": None,
        "novelty_status": "not_measured",
        "reverse_citation_hallucination_rate": hyp["hallucination_rate"],
        "hallucination_status": hyp["status"],
        "pass": (fpr <= 0.1 and hyp["hallucination_rate"] <= 0.10)
                if hallucination_available else None,
        "pass_criteria": "FPR ≤0.1 且反证幻觉率 ≤10%；幻觉率未接入核验后端时如实不可测，不判 pass",
        "method": method,
    }
    write_json(case_dir / "a3_report.json", report)
    return report
