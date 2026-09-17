"""领域语料种子/增量初始化（I-4)。

把一组方法学级/工程级的占位种子记录批量加入本地语料库，用于：
  - 验证语料库规模（N≥200）与增量机制；
  - 作为可增量、可后续标定的种子语料起点。

约束：种子只描述方法论/工程级观察，**不编造真实论文的精确数字结论**；
`novelty_tag` 一律 None（未标定）。重复 seed 幂等（依赖 Corpus.add 的 record_id 去重）。
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from .corpus import VALID_TYPES, Corpus, ProtocolError

# -- 主题域：围绕 Popper 自研的领域（ML 科研方法论 / 实验推进 / 查新 /
#    拒稿风险 / 证据系统 / 编排）等，做方法学级观察。 ---------------------
_SUBJECTS = (
    "AutoEP 类工具",
    "实验推进 Agent",
    "Hook 检索层",
    "查新（FPR/反证）基线",
    "拒稿风险预检器",
    "证据链审计器",
    "领域语料增量机制",
    "谱系图查新器",
    "可复现包校验器",
    "claim 校验器",
)

_OBSERVATIONS = (
    "缺少统一的实验结果登记契约",
    "未把中断状态纳入持久化索引",
    "依赖单一启发式而缺少可量化判定基线",
    "缺少对子图级失败的可恢复语义",
    "未区分确定性校验与需人工判定的环节",
    "缺少跨工具可交换的证据格式",
    "回放粒度不足以支撑离线复算",
    "未把 novelty 标定与语料版本解耦",
    "缺少受控语料下的反证幻觉率基线",
    "依赖内部静态启发式而非可审计规则",
    "缺少对增量语料去重的显式约束",
    "未把实验元数据纳入事件溯源",
    "缺少正则化层的可选依赖回退",
    "未披露忽略的判定分支与适用边界",
    "缺少对查新结果可测试性的定义",
    "依赖实验发起时的环境快照而非可复现清单",
    "缺少对中断后恢复的一致性检查",
    "未把门禁判定与验收判据显式对齐",
    "缺少对多来源判定方向一致性的统计",
    "依赖隐式顺序而非显式拓扑声明",
)

_MAX_BUILTIN = len(_SUBJECTS) * len(_OBSERVATIONS)  # 200


def _builtin_seeds(count):
    """确定性生成方法学级种子记录（count 不超过内置模板组合数 200）。"""
    count = max(0, min(int(count), _MAX_BUILTIN))
    seeds = []
    for i, combo in enumerate(itertools.product(_SUBJECTS, _OBSERVATIONS)):
        if i >= count:
            break
        subject, observation = combo
        # literature_id 形如 arXiv:20xx.NNNNN，可被 _LITERATURE_ID_PATTERN 解析。
        seeds.append({
            "type": VALID_TYPES[i % len(VALID_TYPES)],
            "literature_id": f"arXiv:{2000 + i % 26}.{str(i).zfill(5)}",
            "description": f"{subject}：{observation}",
            "novelty_tag": None,
        })
    return seeds


def _load_seed_file(seed_file):
    """读取外部种子 JSON：支持 {"records":[...]} 或直接是记录列表。"""
    path = Path(seed_file)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not records:
        raise ProtocolError(f"种子文件 {path} 需是非空记录列表或含 records 的对象")
    return records


def seed(corpus_dir, count=200, seed_file=None):
    """把种子记录批量加入本地语料库（幂等）。

    返回 {"status", "inserted", "target", "count", "by_type"}。
    """
    seeds = _load_seed_file(seed_file) if seed_file is not None else _builtin_seeds(count)
    corpus = Corpus(corpus_dir)
    corpus.initialize()
    inserted = 0
    for item in seeds:
        if not isinstance(item, dict):
            raise ProtocolError("种子记录必须是对象")
        result = corpus.add(item["type"], item["literature_id"],
                            item["description"], item.get("novelty_tag"),
                            None, item.get("support_level"))
        if result["status"] == "added":
            inserted += 1
    stats = corpus.stats()
    return {"status": "seeded", "inserted": inserted, "target": len(seeds),
            "count": stats["count"], "by_type": stats["by_type"]}