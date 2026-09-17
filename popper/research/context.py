"""从权威研究存储构建模型与确定性策略共用的最小上下文。"""
from __future__ import annotations


def build_context(store, study_id, manifest):
    study = store.get("study", study_id)
    hypotheses = [h for h in store.list("hypothesis") if h["study_id"] == study_id]
    designs = [d for d in store.list("design") if d.get("study_id") == study_id]
    runs = [r for r in store.list("run") if r.get("study_id") == study_id]
    observations = [o for o in store.list("observation") if o.get("study_id") == study_id]
    decisions = [d for d in store.list("decision") if d.get("study_id") == study_id]
    candidate_ids = set(manifest["candidate_hypothesis_ids"])
    candidates = []
    for hypothesis in hypotheses:
        if hypothesis["hypothesis_id"] not in candidate_ids:
            continue
        design = next(d for d in designs if d["hypothesis_id"] == hypothesis["hypothesis_id"]
                      and d["hypothesis_version"] == hypothesis["version"])
        candidates.append({"hypothesis_id": hypothesis["hypothesis_id"],
                           "status": hypothesis["status"],
                           "version": hypothesis["version"],
                           "mechanism": hypothesis["mechanism"],
                           "applicability": hypothesis["applicability"],
                           "predictions": hypothesis["predictions"],
                           "falsification": hypothesis["falsification"],
                           "alternatives": hypothesis["alternatives"],
                           "config": manifest["configs"][hypothesis["hypothesis_id"]],
                           "design_id": design["design_id"]})
    # 账本与研究库共用连接，不拥有资源，因此不需要关闭。
    budget = store.budget(study_id).balance(study["family_id"])
    return {"study_id": study_id, "study": study, "candidates": candidates, "runs": runs,
            "observations": observations, "decisions": decisions,
            "reflections": [r for r in store.list("reflection") if r["study_id"] == study_id],
            "budget": budget,
            "metric": manifest["metric"],
            "min_meaningful_effect": manifest["min_meaningful_effect"],
            "capability_mode": manifest["capability_mode"]}
