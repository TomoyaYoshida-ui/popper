"""Bind generated hypotheses to the controller's supported experiment contract."""
import re
from .core import ProtocolError


def contract_from_spec(spec):
    return {"metric": spec["metric"], "min_improvement": spec["min_improvement"],
            "baseline": spec["baseline"], "seeds": spec["seeds"],
            "evaluation": "fixed train/dev/test split; descriptive threshold, no significance claim"}


def validate_candidate(candidate, contract):
    if not isinstance(candidate, dict) or any(
            not isinstance(candidate.get(k), str) or not candidate[k].strip()
            for k in ("title", "core_mechanism", "falsification_prediction")):
        raise ProtocolError("候选必须包含标题、机制与可证伪预测")
    if candidate.get("evaluation_contract") != contract:
        raise ProtocolError("候选 evaluation_contract 与冻结实验协议不一致")
    # Conservative guard complements the structured binding; it is not a semantic proof.
    # 只拦截「自报统计主张」的措辞。具体指标名不再枚举：指标合法性已由
    # evaluation_contract 结构化绑定，反向禁词表枚举指标名只会把新指标（f1/mae/roc_auc）
    # 误判为不合规。
    prediction = candidate["falsification_prediction"]
    forbidden = (r"显著|置信区间|区间估计|统计检验|\bp\s*[<>=]|p-value|confidence interval|"
                 r"bootstrap|significan")
    if re.search(forbidden, prediction, flags=re.I):
        raise ProtocolError("可证伪预测不得自行声明统计显著性或区间估计")
