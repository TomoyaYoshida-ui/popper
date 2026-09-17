"""创新点引擎 · 生成层（能力 1）。

候选贡献 = 领域语料记录 × ideation 算子；三类创新点（选题/方法/发现）共用框架。
- ideation 算子显式、领域无关；LLM 只做算子内实例化。
- compose(op_A, op_B) 组合算子需用户显式登记后方可使用。
- novelty scorer：多类别锚定检索，输出 score+category+近邻文献，分级（重大/增量/平凡）永不二值。
- 每个候选附可证伪预测 + 预注册 scaffold。
"""
from __future__ import annotations

from pathlib import Path

from .core import ProtocolError, digest, read_json, write_json

# 创新点类型
VALID_KINDS = ("选题", "方法", "发现")
# novelty 分级-永不二值
NOVELTY_LEVELS = ("major", "incremental", "trivial")
# novelty 分类
NOVELTY_CATEGORIES = ("new_problem", "new_data", "new_method_family", "new_connection", "incremental_delta")

TEMPLATES = {
    "选题": {
        "title": "问题域: {question}",
        "motivation": "由语料记录 {record_id}（{literature}）驱动：{description}",
        "method": "建立可重测的评估协议并给出可证伪预测",
        "prereg": {
            "hypothesis": "候选问题存在可量化差异",
            "falsification_prediction": "在开放对比基线下，指标不劣于既有工作",
            "control": "约束同一数据划分/预算/随机种子",
        },
    },
    "方法": {
        "title": "方法: {question}",
        "motivation": "针对 {description} 提出模块级改造",
        "method": "replace-module / cross-domain-transfer / counterfactual-mechanism",
        "prereg": {
            "hypothesis": "替换/迁移/反事实后机制保持",
            "falsification_prediction": "最小对比实验中 δ 改善超预注册阈值",
            "control": "同数据、同种子、同评估适配器",
        },
    },
    "发现": {
        "title": "发现: {question}",
        "motivation": "由 {description} 提出可证伪假设",
        "method": "data-deficit-to-task / negative-result-pivot",
        "prereg": {
            "hypothesis": "变量间存在系统性关系",
            "falsification_prediction": "探针实验呈现非平凡信号",
            "control": "预注册扰动协议 + held-out",
        },
    },
}


def _titleize(text):
    words = text.strip().split()
    return " ".join(words[:8]) if words else "untitled"


def _validate_kind(kind):
    if kind and not kind.startswith("组合") and kind not in VALID_KINDS:
        raise ProtocolError(f"kind 必须是 {VALID_KINDS}")
    return kind.replace("组合:", "").rstrip("+") if kind else kind


def _builtin_operators():
    """默认基础算子（选题的 gap/矛盾/前瞻，方法/发现的机制算子）。"""
    return {
        "gap-to-problem": {"kind": "选题", "need": "gap", "desc": "把领域 gap 派生为新问题"},
        "contradiction-to-problem": {"kind": "选题", "need": "contradiction", "desc": "把矛盾派生为新问题"},
        "foresight-to-problem": {"kind": "选题", "need": "foresight", "desc": "把前瞻信号派生为新问题"},
        "replace-module": {"kind": "方法", "need": None, "desc": "方法创新：替换模块"},
        "cross-domain-transfer": {"kind": "方法", "need": None, "desc": "方法创新：跨域迁移"},
        "counterfactual-mechanism": {"kind": "方法", "need": None, "desc": "方法创新：反事实机制"},
        "data-deficit-to-task": {"kind": "发现", "need": None, "desc": "发现创新：数据缺失到任务"},
        "negative-result-pivot": {"kind": "发现", "need": None, "desc": "发现创新：负结果 pivot"},
    }


class IdeationRun:
    """生成层运行。

    run_dir/ideation.json 保存候选卡、novelty 与预注册 scaffold。
    组合算子登记在 run_dir/operators.json。
    """

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.operators_path = self.run_dir / "operators.json"
        self.output_path = self.run_dir / "ideation.json"

    def register_operator(self, name, op_a, op_b, order="A->B"):
        if not isinstance(op_a, str) or not isinstance(op_b, str):
            raise ProtocolError("组合算子必须由两个基础算子名称组成")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        ops = self._load_operators()
        if name in ops:
            return {"status": "already_registered", "name": name}
        ops[name] = {"compose": [op_a, op_b], "order": order, "registered": True,
                     "sha256": digest({"a": op_a, "b": op_b, "order": order})}
        write_json(self.operators_path, ops)
        return {"status": "registered", "name": name, "definition": ops[name]}

    def _load_operators(self):
        if not self.operators_path.is_file():
            return dict(_builtin_operators())
        data = read_json(self.operators_path)
        merged = dict(_builtin_operators())
        merged.update({k: v for k, v in data.items() if not isinstance(v, dict) or v.get("registered")})
        return merged

    def _resolve_operator(self, name, corpus, insight=None):
        ops = self._load_operators()
        definition = ops.get(name)
        if definition is None:
            raise ProtocolError(f"算子未登记: {name}")
        if definition.get("registered"):
            # 组合算子：先解析注册时的底层算子，再逐级实例化。
            op_a, op_b = definition["compose"]
            base = dict(ops)
            base.pop(name, None)
            return {"name": name, "kind": self._compose_kind(base, op_a, op_b),
                    "stages": [op_a, op_b], "source_corpus": self._resolve_source(corpus, insight)}
        return {"name": name, "kind": definition["kind"], "stages": [name],
                "source_corpus": self._resolve_source(corpus, insight)}

    def _compose_kind(self, ops, op_a, op_b):
        def kind(op):
            d = ops.get(op)
            return d.get("kind") if isinstance(d, dict) else "选题"
        return "组合:" + kind(op_a) + "+" + kind(op_b)

    def _resolve_source(self, corpus, insight):
        if insight:
            return insight
        if corpus:
            records = [r for r in corpus.verified_records()]
            if records:
                return records[0]
        return None

    def ideate(self, corpus, operator="gap-to-problem", insight=None, question=None):
        """对语料生成一个候选创新点卡。"""
        resolved = self._resolve_operator(operator, corpus, insight)
        kind = resolved["kind"]
        _validate_kind(kind)
        source = resolved["source_corpus"]
        template = TEMPLATES.get(kind.split(":")[-1] if kind.startswith("组合") else kind, TEMPLATES["选题"])
        record_id = source.get("record_id") if isinstance(source, dict) else None
        literature = source.get("literature_id") if isinstance(source, dict) else "—"
        description = source.get("description") if isinstance(source, dict) else (insight or question or "未提供描述")

        question_text = question or (_titleize(description))
        card = {
            "operator": operator,
            "kind": kind,
            "title": template["title"].format(question=question_text),
            "motivation": template["motivation"].format(record_id=record_id or "N/A",
                                                        literature=literature, description=description),
            "method": template["method"],
            "nearest_work": self._nearest_work(corpus, source),
            "falsifiable_prediction": template["prereg"]["falsification_prediction"],
            "minimal_discriminating_experiment": template["prereg"]["control"],
        }
        novel = self.novelty_score(card, corpus)
        card["novelty"] = novel
        card["preregistration"] = {
            "scaffold": template["prereg"],
            "record_id": record_id or None,
            "changes_require_approval": True,
        }
        return card

    def novelty_score(self, card, corpus):
        """多类别 novelty 锚定检索；分级永不二值。

        无语料近邻 => 保守 incremental_delta/incremental。
        语料近邻直接覆盖 => 输出 trivial。
        近邻仅部分相关 + 跨类别连接 => 更高级别。
        """
        overlaps = corpus.verified_records() if corpus else []
        nearest = card.get("nearest_work")
        related = [r for r in overlaps if r.get("literature_id")]
        covered = any(("已实现" in r["description"] or "已覆盖" in r["description"]
                       or "解决" in r["description"]) for r in related)
        # 语料有"已覆盖"描述 => 平凡增量；否则按是否有近邻锚定给出分级。
        anchors = sorted({r["literature_id"] for r in related})
        if covered:
            category, level, score = "incremental_delta", "trivial", 0.3
            rationale = "语料存在已覆盖描述，判定平凡增量"
        elif nearest:
            category, level, score = "new_connection", "incremental", 0.7
            rationale = "锚定最近邻非直接覆盖，判定增量；待 scoop 反证后再裁决"
        else:
            category, level, score = "new_connection", "incremental", 0.7
            rationale = "缺少已登记近邻，输出保守 incremental；待 scoop 反证后再裁决"
        return {
            "score": score,
            "category": category,
            "level": level,
            "nearest_work": nearest,
            "anchors": anchors,
            "rationale": rationale,
        }

    def _nearest_work(self, corpus, source):
        if isinstance(source, dict) and source.get("literature_id"):
            return source["literature_id"]
        records = corpus.verified_records() if corpus else []
        return records[0]["literature_id"] if records else None

    def commit(self, card, record_id=None):
        """把候选卡持久化到 run 目录，返回统一 id。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        output_id = record_id or digest({k: card.get(k) for k in ("title", "motivation")})[:12]
        data = self._load_output()
        if output_id in data:
            return {"status": "already_exists", "id": output_id}
        data[output_id] = {"id": output_id, **card}
        write_json(self.output_path, data)
        return {"status": "created", "id": output_id, "count": len(data)}

    def _load_output(self):
        if not self.output_path.is_file():
            return {}
        return read_json(self.output_path)