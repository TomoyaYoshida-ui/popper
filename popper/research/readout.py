"""研究库只读投影（工作台假设环）。

工作台是研究库的旁观者：绝不写、也不做迁移（ResearchStore 构造时会补齐 DDL、
控制器还会对账预算）。这里以 SQLite URI 只读模式直读 snapshots / events /
study_versions / families 四张表，实体形状与 ResearchStore.get()/list() 的
语义一致（payload + status）。读取失败按 run 目录显式返回原因，不静默吞掉。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# snapshots 里与假设环相关的实体类型；顺序即前端展示顺序。
_ENTITY_KINDS = ("study", "hypothesis", "observation", "decision", "reflection")


def read_research_state(run_dir) -> dict:
    """读取一个研究 run 目录的真实状态投影。

    返回 {"ok": True, ...}（study/hypotheses/revisions/observations/
    decisions/reflections/budget/state_version/events_total）或
    {"ok": False, "reason": ...}。调用方把逐目录结果原样放进 /api/research。
    """
    run_dir = Path(run_dir)
    manifest_path = run_dir / "research.json"
    if not manifest_path.is_file():
        return {"ok": False,
                "reason": "缺少 research.json（该目录尚未执行 popper research init）"}
    db_path = run_dir / "research.sqlite"
    if not db_path.is_file():
        return {"ok": False, "reason": "缺少 research.sqlite（研究库尚未建立）"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"ok": False, "reason": f"research.json 不可读: {error}"}
    study_id = manifest.get("study_id")
    if not study_id:
        return {"ok": False, "reason": "research.json 缺少 study_id"}
    # file URI 只读打开：不存在、被独占锁或损坏都在这里显式失败，绝不退化成可写。
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro",
                               uri=True, timeout=5.0)
    except sqlite3.Error as error:
        return {"ok": False, "reason": f"研究库无法只读打开: {error}"}
    try:
        return _project(conn, study_id, manifest)
    except sqlite3.Error as error:
        return {"ok": False, "reason": f"研究库读取失败: {error}"}
    finally:
        conn.close()


def _project(conn, study_id, manifest) -> dict:
    entities = {kind: [] for kind in _ENTITY_KINDS}
    study = None
    for kind, entity_id, status, payload, seq in conn.execute(
            "SELECT kind, entity_id, status, payload, updated_seq FROM snapshots "
            "ORDER BY updated_seq, entity_id"):
        # design / run 是控制器内部编排实体，不属于假设环投影的五类。
        if kind not in entities and kind != "study":
            continue
        data = json.loads(payload)
        if kind != "study" and data.get("study_id") != study_id:
            continue
        if kind == "study" and entity_id != study_id:
            continue
        data["status"] = status
        data["entity_id"] = entity_id
        if kind == "study":
            study = data
        else:
            entities[kind].append(data)
    if study is None:
        return {"ok": False, "reason": f"研究库中不存在 study: {study_id}"}

    # 假设修订链：只有 created/revised 携带版本信息（status 事件只是状态转移）。
    revisions = []
    for seq, entity_id, event_type, payload in conn.execute(
            "SELECT seq, entity_id, event_type, payload FROM events "
            "WHERE kind = 'hypothesis' AND event_type IN ('created', 'revised') "
            "ORDER BY seq"):
        data = json.loads(payload)
        if data.get("study_id") != study_id:
            continue
        revisions.append({"hypothesis_id": entity_id, "version": data.get("version"),
                          "parent_version": data.get("parent_version"),
                          "origin": event_type, "seq": seq})

    row = conn.execute(
        "SELECT version FROM study_versions WHERE study_id = ?", (study_id,)).fetchone()
    state_version = row[0] if row else None

    budget = None
    family_id = study.get("family_id")
    row = conn.execute(
        "SELECT cap, reserved, spent FROM families WHERE family_id = ?",
        (family_id,)).fetchone()
    if row is not None:
        cap, reserved, spent = row
        budget = {"family_id": family_id, "cap": cap, "reserved": reserved,
                  "spent": spent, "available": cap - reserved - spent}

    events_total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {"ok": True,
            "manifest": {"study_id": study_id,
                         "capability_mode": manifest.get("capability_mode"),
                         "policy": manifest.get("policy"),
                         "metric": manifest.get("metric"),
                         "min_meaningful_effect": manifest.get("min_meaningful_effect")},
            "study": study, "state_version": state_version,
            "hypotheses": entities["hypothesis"], "revisions": revisions,
            "observations": entities["observation"],
            "decisions": entities["decision"],
            "reflections": entities["reflection"],
            "budget": budget, "events_total": events_total}
