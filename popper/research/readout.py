"""研究库只读投影（工作台假设环）。

工作台是研究库的旁观者：绝不写、也不做迁移（ResearchStore 构造时会补齐 DDL、
控制器还会对账预算）。这里以 SQLite URI 只读模式直读 snapshots / events /
study_versions / families 四张表，实体形状与 ResearchStore.get()/list() 的
语义一致（payload + status）。读取失败按 run 目录显式返回原因，不静默吞掉。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..core import canonical, digest, file_hash, read_json
from ..identity import EntityKind, parse_id
from .controller import research_lifecycle_phase

# snapshots 里与研究工作台相关的实体类型；顺序即 API 投影顺序。
_ENTITY_KINDS = ("study", "hypothesis", "design", "run", "observation", "decision",
                 "claim", "reflection", 'judgment')
_REF_KINDS = {
    "study": EntityKind.STUDY,
    "hypothesis": EntityKind.HYPOTHESIS,
    "design": EntityKind.DESIGN,
    "run": EntityKind.RUN,
    "observation": EntityKind.OBSERVATION,
    "decision": EntityKind.DECISION,
    "claim": EntityKind.CLAIM,
}


def controller_routing_readiness(run_dir, state, design):
    """Read-only audit of today's manifest-bound Controller route.

    This is not an execution authorization: source inputs, evaluator binding,
    sandbox and immutable CodeRevision still need their own checks at run time.
    """
    reasons = []
    try:
        manifest = json.loads((Path(run_dir) / "research.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {"controller_routing_ready": False,
                "execution_ready": False, "reasons": ["manifest_unreadable"],
                "registered_design_route_ready": False,
                "registered_design_reasons": ["manifest_unreadable"],
                "registered_candidate_match": None,
                "not_checked": ["source_inputs", "evaluator_binding", "sandbox"]}
    hypothesis = next((item for item in state.get("hypotheses") or []
                       if item.get("hypothesis_id") == design.get("hypothesis_id")), None)
    try:
        scope = json.loads(state["study"]["scope"])
        if scope["manifest_sha256"] != file_hash(Path(run_dir) / "research.json"):
            reasons.append("manifest_identity_mismatch")
    except (KeyError, TypeError, ValueError, OSError):
        reasons.append("manifest_identity_unverifiable")
    if state.get("study", {}).get("status") != "active":
        reasons.append("study_not_active")
    if design.get("status") != "frozen":
        reasons.append("design_not_frozen")
    if hypothesis is None:
        reasons.append("hypothesis_missing")
    elif hypothesis.get("version") != design.get("hypothesis_version"):
        reasons.append("hypothesis_version_mismatch")
    hypothesis_id = design.get("hypothesis_id")
    version = design.get("hypothesis_version")
    if not isinstance(hypothesis_id, str) or type(version) is not int:
        reasons.append("controller_design_identity_missing")
    else:
        expected_id = "D-" + hypothesis_id.removeprefix("H-") + (
            f"-v{version}" if version > 1 else "")
        if design.get("design_id") != expected_id:
            reasons.append("controller_design_id_unmapped")
    configs = manifest.get("configs")
    if not isinstance(configs, dict) or hypothesis_id not in configs:
        reasons.append("controller_config_missing")
    elif list(design.get("interventions") or []) != [canonical(configs[hypothesis_id])]:
        reasons.append("intervention_config_mismatch")
    registered_candidate_match = None
    completed_dev_configs = set()
    core_phase = None
    if isinstance(configs, dict):
        try:
            project = Path(manifest["project"])
            core_db = project / ".popper" / "state.db"
            with closing(sqlite3.connect(
                    f"{core_db.resolve().as_uri()}?mode=ro", uri=True)) as db:
                row = db.execute("SELECT data FROM state WHERE id=1").fetchone()
                core_state = json.loads(row[0]) if row else {}
                spec = core_state["spec"]
                core_phase = core_state.get("phase")
                completed_dev_configs = {digest(json.loads(result)["config"])
                                         for (result,) in db.execute(
                                             "SELECT result FROM runs WHERE split='dev' "
                                             "AND status='completed'")}
            registered = {digest(c) for c in [spec["baseline"], *spec["candidates"]]}
            if hypothesis_id in configs and digest(configs[hypothesis_id]) not in registered:
                reasons.append("core_candidate_unregistered")
            interventions = design.get("interventions") or []
            if len(interventions) == 1:
                registered_candidate_match = next((candidate_id for candidate_id in
                    manifest.get("candidate_hypothesis_ids", [])
                    if candidate_id in configs
                    and digest(configs[candidate_id]) in registered
                    and interventions[0] == canonical(configs[candidate_id])), None)
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
            reasons.append("core_registration_unverifiable")
    control_id = manifest.get("control_hypothesis_id")
    if (not isinstance(configs, dict) or control_id not in configs
            or design.get("control") != canonical(configs[control_id])
            or design.get("baseline") != canonical(configs[control_id])):
        reasons.append("control_config_mismatch")
    metric = manifest.get("metric")
    if not isinstance(metric, dict):
        metric = {}
    if (design.get("metric") != metric.get("name")
            or design.get("metric_direction") != metric.get("direction")
            or design.get("scorer_id") != manifest.get("scorer_id")):
        reasons.append("scoring_binding_mismatch")
    if (design.get("analysis_unit") != manifest.get("repeat_unit", "train_seed")
            or list(design.get("seeds") or []) != list(
                manifest.get("repeat_values", manifest.get("seeds", [])))):
        reasons.append("repeat_protocol_mismatch")
    if "dev" not in (design.get("splits") or []):
        reasons.append("development_scope_missing")
    budget = state.get("budget") or {}
    available = budget.get("available")
    if isinstance(available, (int, float)) and not isinstance(available, bool) and available < 1:
        reasons.append("development_budget_insufficient")
    registered_reasons = [reason for reason in reasons if reason not in {
        "controller_design_id_unmapped", "controller_config_missing",
        "intervention_config_mismatch"}]
    if registered_candidate_match is None:
        registered_reasons.append("registered_candidate_missing")
    if core_phase != "searching":
        registered_reasons.append("development_search_closed")
    if ((Path(run_dir) / "external-confirmation" / "bundle").exists() or any(
            row.get("action") == "freeze_external_confirmation"
            for row in state.get("decisions") or [])):
        registered_reasons.append("external_confirmation_frozen")
    if design.get("min_meaningful_effect") != manifest.get("min_meaningful_effect"):
        registered_reasons.append("minimum_effect_mismatch")
    own_run = any(row.get("design_id") == design.get("design_id")
                  for row in state.get("runs") or [])
    own_observation = any(row.get("run_id") in {
        run.get("run_id") for run in state.get("runs") or []
        if run.get("design_id") == design.get("design_id")}
        for row in state.get("observations") or [])
    if (hypothesis is not None and hypothesis.get("status") != "untested"
            and not own_observation):
        registered_reasons.append("hypothesis_already_tested")
    if (registered_candidate_match is not None and not own_run
            and digest(configs[registered_candidate_match]) in completed_dev_configs):
        registered_reasons.append("candidate_evidence_already_used")
    return {"controller_routing_ready": not reasons,
            "execution_ready": False, "reasons": reasons,
            "registered_candidate_match": registered_candidate_match,
            "registered_design_route_ready": not registered_reasons,
            "registered_design_reasons": registered_reasons,
            "not_checked": ["source_inputs", "evaluator_binding", "sandbox"]}


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
        return _project(conn, study_id, manifest, run_dir)
    except sqlite3.Error as error:
        return {"ok": False, "reason": f"研究库读取失败: {error}"}
    finally:
        conn.close()


def _project(conn, study_id, manifest, run_dir) -> dict:
    entities = {kind: [] for kind in _ENTITY_KINDS}
    study = None
    for kind, entity_id, status, payload, seq in conn.execute(
            "SELECT kind, entity_id, status, payload, updated_seq FROM snapshots "
            "ORDER BY updated_seq, entity_id"):
        if kind not in entities:
            continue
        data = json.loads(payload)
        if kind != "study" and data.get("study_id") != study_id:
            continue
        if kind == "study" and entity_id != study_id:
            continue
        data["status"] = status
        data["entity_id"] = entity_id
        if kind in _REF_KINDS:
            data["entity_ref"] = parse_id(entity_id, _REF_KINDS[kind]).as_dict()
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
        revisions.append({"hypothesis_id": entity_id,
                          "hypothesis_ref": parse_id(
                              entity_id, EntityKind.HYPOTHESIS).as_dict(),
                          "version": data.get("version"),
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
    graph = _experiment_graph(run_dir, entities, study_id)
    from .claim_projection import project_claim
    seeds = manifest.get("repeat_values", manifest.get("seeds", []))
    split_path = Path(run_dir) / "split-confirmation.json"
    split_confirmation = read_json(split_path) if split_path.is_file() else None
    entities['claim'] = [project_claim(row, entities['judgment'], entities['observation'], seeds,
                                       split_confirmation)
                         for row in entities['claim']]
    claim_card = _claim_card_view(manifest, split_confirmation)
    lifecycle = _lifecycle_projection(manifest, study, entities["decision"], run_dir)
    projected = {"ok": True,
            **lifecycle,
            "manifest": {"study_id": study_id,
                         "project": manifest.get("project"),
                         "study_ref": parse_id(study_id, EntityKind.STUDY).as_dict(),
                         "capability_mode": manifest.get("capability_mode"),
                         "policy": manifest.get("policy"),
                         "followup_lineage": manifest.get("followup_lineage"),
                         "metric": manifest.get("metric"),
                         "scorer_id": manifest.get("scorer_id"),
                         "repeat_unit": manifest.get("repeat_unit", "train_seed"),
                         "repeat_values": manifest.get("repeat_values", manifest.get("seeds", [])),
                         "min_meaningful_effect": manifest.get("min_meaningful_effect")},
            "study": study, "state_version": state_version,
            "hypotheses": entities["hypothesis"], "revisions": revisions,
            "designs": entities["design"], "runs": entities["run"],
            "observations": entities["observation"],
            "decisions": entities["decision"],
            "claims": entities["claim"],
            "reflections": entities["reflection"],
            'judgments': entities['judgment'],
            "budget": budget, "events_total": events_total,
            "experiment_graph": graph, "claim_card": claim_card}
    from ..demo import summarize_research_status
    from .resume_context import project_resume_context
    integrity = (graph or {}).get("integrity") if isinstance(graph, dict) else {}
    if not isinstance(integrity, dict):
        integrity = {}
    projected["resume"] = project_resume_context(
        run_dir,
        phase=projected.get("lifecycle_phase") or projected.get("phase"),
        integrity_ok=bool(integrity.get("ok")),
    )
    projected["status_brief"] = summarize_research_status(projected)
    return projected


def _claim_card_view(manifest, split_confirmation):
    """断言卡来自搜索集锁文件；区间和状态来自确认收据，不在这里重选特征。"""
    card = None
    project = manifest.get("project")
    if project:
        path = Path(project) / "claim-card.json"
        if path.is_file():
            try:
                card = read_json(path)
            except (OSError, ValueError):
                card = None
    assertion = split_confirmation.get("claim_assertion") if isinstance(split_confirmation, dict) else None
    if not card and not assertion:
        return None
    transport = None
    if isinstance(card, dict) and isinstance(card.get("population_transport"), dict):
        transport = card.get("population_transport")
    elif isinstance(split_confirmation, dict) and isinstance(split_confirmation.get("population_transport"), dict):
        transport = split_confirmation.get("population_transport")
    elif project:
        protocol_path = Path(project) / "search-protocol.json"
        if protocol_path.is_file():
            try:
                loaded = read_json(protocol_path).get("population_transport")
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                transport = loaded
    view = {
        "feature": (card or {}).get("feature") or (assertion or {}).get("feature"),
        "label": (card or {}).get("label") or (assertion or {}).get("label"),
        "direction": (card or {}).get("direction") or (assertion or {}).get("direction"),
        "contrast_definition": (card or {}).get("contrast_definition"),
        "failure_condition": (card or {}).get("failure_condition"),
        "search_association": (card or {}).get("search_association"),
        "search_rows_discarded": (card or {}).get("search_rows_discarded"),
        "ci95": None if not isinstance(assertion, dict) else assertion.get("ci95"),
        "scientific_status": None if not isinstance(assertion, dict) else assertion.get("scientific_status"),
        "n_rows": None if not isinstance(assertion, dict) else assertion.get("n_rows"),
    }
    if isinstance(transport, dict):
        view["population_transport"] = {
            "column": transport.get("column"),
            "search": transport.get("search"),
            "confirmation": transport.get("confirmation"),
            "rows_discarded": transport.get("rows_discarded"),
        }
    return view


def _lifecycle_projection(manifest, study, decisions, run_dir):
    """Read the core snapshot without opening Experiment/Controller writers."""
    if manifest.get('planning_protocol') == 'research-runtime/v2':
        return {'phase': 'concluded' if study['status'] == 'concluded' else 'researching',
                'core_phase': None, 'lifecycle_reason': None}
    core_phase = None
    reason = None
    try:
        core_path = Path(manifest["project"]) / ".popper" / "state.db"
        with closing(sqlite3.connect(
                f"{core_path.resolve().as_uri()}?mode=ro", uri=True)) as core:
            row = core.execute("SELECT data FROM state WHERE id=1").fetchone()
        snapshot = json.loads(row[0]) if row else {}
        core_phase = snapshot.get("phase")
        if not isinstance(core_phase, str):
            raise ValueError("实验阶段缺失")
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        reason = "无法只读核对底层实验阶段；请检查项目文件后重新载入。不会推测执行或确认资格。"
    external_submitted = any(row.get("action") == "freeze_external_confirmation"
                             for row in decisions)
    external_frozen = (external_submitted or
                       (Path(run_dir) / "external-confirmation" / "bundle").exists())
    phase = research_lifecycle_phase(
        study["status"], core_phase, decisions,
        external_frozen=external_frozen, external_submitted=external_submitted)
    if reason is None:
        reason = {
            "confirming": "独立确认已经开始；一次性资源已消费，请查看作业状态，不会自动重试。",
            "confirmation_failed": "独立确认失败且一次性资源已消费；请核对失败记录，不得重新消费确认资源。",
            "external_confirmation_pending": "已冻结外部签名确认提交，请等待并核验外部确认结果。",
            "external_preparation_incomplete": "外部签名确认提交准备中断；请检查冻结提交，不使用本地确认替代。",
        }.get(phase)
    result = {"lifecycle_phase": phase, "lifecycle_reason": reason,
              "confirmation_mode": "signed_external" if manifest.get(
                  "external_confirmation") else "local"}
    if core_phase is not None:
        result["confirmation_resource_consumed"] = core_phase in {
            "confirming", "confirmation_failed", "completed"}
    return result


def _experiment_graph(run_dir, entities, study_id):
    """Build a deterministic, read-only scientific provenance graph.

    Code revisions live in the content-addressed artifact store while the other
    entities live in research.sqlite.  This projection joins those two
    authorities and reports broken bindings explicitly; it never repairs or
    invents provenance during a read.
    """
    nodes = []
    edges = []
    issues = []

    def node(kind, entity_id, **data):
        nodes.append({"kind": kind, "id": entity_id, **data})

    def edge(source_kind, source_id, target_kind, target_id, relation):
        edges.append({"source": {"kind": source_kind, "id": source_id},
                      "target": {"kind": target_kind, "id": target_id},
                      "relation": relation})

    hypotheses = {row["entity_id"]: row for row in entities["hypothesis"]}
    designs = {row["entity_id"]: row for row in entities["design"]}
    runs = {row["entity_id"]: row for row in entities["run"]}
    observations = {row["entity_id"]: row for row in entities["observation"]}

    for entity_id, row in hypotheses.items():
        node("hypothesis", entity_id, status=row["status"], version=row.get("version"))
    for entity_id, row in designs.items():
        node("design", entity_id, status=row["status"],
             hypothesis_version=row.get("hypothesis_version"))
        hypothesis_id = row.get("hypothesis_id")
        if hypothesis_id in hypotheses:
            edge("hypothesis", hypothesis_id, "design", entity_id, "designed_as")
        else:
            issues.append({"code": "design_hypothesis_missing", "entity_id": entity_id,
                           "reference": hypothesis_id})

    revision_root = run_dir / "revisions"
    revision_manifests = {}
    if revision_root.is_dir():
        for path in sorted(revision_root.glob("REV-*/revision.json")):
            revision_id = path.parent.name
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                identity = manifest.get("identity")
                if (manifest.get("revision_id") != revision_id
                        or manifest.get("status") != "immutable"
                        or "REV-" + digest(identity)[:24] != revision_id):
                    raise ValueError("manifest identity mismatch")
                code_root = path.parent / "code"
                actual_files = {
                    str(item.relative_to(code_root)).replace("\\", "/"): file_hash(item)
                    for item in sorted(code_root.rglob("*.py"))}
                if actual_files != manifest.get("files"):
                    raise ValueError("code file hashes mismatch")
                revision_manifests[revision_id] = (manifest, file_hash(path))
                source_control = identity.get("source_control")
                node("code_revision", revision_id, status="immutable",
                     manifest_sha256=file_hash(path), files=len(actual_files),
                     source_control=source_control or {"kind": "legacy_unbound"})
                if source_control is None:
                    issues.append({"code": "revision_source_control_unbound",
                                   "entity_id": revision_id})
                elif not isinstance(source_control, dict):
                    issues.append({"code": "revision_source_control_invalid",
                                   "entity_id": revision_id})
                elif source_control.get("kind") == "git":
                    commit = source_control.get("head_commit")
                    status_sha = source_control.get("source_status_sha256")
                    if (not isinstance(commit, str) or len(commit) != 40
                            or any(ch not in "0123456789abcdef" for ch in commit)
                            or not isinstance(status_sha, str) or len(status_sha) != 64
                            or any(ch not in "0123456789abcdef" for ch in status_sha)):
                        issues.append({"code": "revision_git_anchor_invalid",
                                       "entity_id": revision_id})
                elif source_control != {"kind": "unversioned"}:
                    issues.append({"code": "revision_source_control_invalid",
                                   "entity_id": revision_id})
            except (OSError, ValueError, TypeError) as error:
                issues.append({"code": "revision_integrity_failed",
                               "entity_id": revision_id, "detail": str(error)})

    for revision_id, (revision, _) in revision_manifests.items():
        identity = revision["identity"]
        design_id = identity.get("design_id")
        if design_id in designs:
            edge("design", design_id, "code_revision", revision_id, "implemented_by")
        else:
            issues.append({"code": "revision_design_missing", "entity_id": revision_id,
                           "reference": design_id})
        parent_id = identity.get("parent_revision_id")
        if parent_id:
            if parent_id in revision_manifests:
                edge("code_revision", parent_id, "code_revision", revision_id, "parent_of")
            else:
                issues.append({"code": "revision_parent_missing", "entity_id": revision_id,
                               "reference": parent_id})

    for entity_id, row in runs.items():
        node("run", entity_id, status=row["status"])
        design_id = row.get("design_id")
        if design_id in designs:
            edge("design", design_id, "run", entity_id, "scheduled_as")
        else:
            issues.append({"code": "run_design_missing", "entity_id": entity_id,
                           "reference": design_id})
        revision_id = row.get("revision_id")
        expected_sha = row.get("revision_manifest_sha256")
        external_source = row.get("external_source")
        if revision_id is None and external_source is not None:
            required = {"kind", "submission_id", "submission_sha256", "role",
                        "files_sha256"}
            if (not isinstance(external_source, dict)
                    or set(external_source) != required
                    or external_source.get("kind") != "signed_confirmation_bundle"
                    or not isinstance(external_source.get("submission_id"), str)
                    or not external_source["submission_id"].startswith("SUB-")
                    or external_source.get("role") not in {"control", "candidate"}
                    or not all(isinstance(value, str) and len(value) == 64
                               and all(ch in "0123456789abcdef" for ch in value)
                               for value in ((external_source.get("submission_sha256"),
                                              external_source.get("files_sha256"))
                                             if isinstance(external_source, dict) else ()))):
                issues.append({"code": "run_external_source_invalid",
                               "entity_id": entity_id})
            else:
                source_id = (f"{external_source['submission_id']}:"
                             f"{external_source['role']}")
                if not any(item["kind"] == "external_source" and item["id"] == source_id
                           for item in nodes):
                    node("external_source", source_id, status="verified",
                         submission_sha256=external_source["submission_sha256"],
                         files_sha256=external_source["files_sha256"])
                    edge("design", design_id, "external_source", source_id,
                         "exported_as")
                edge("external_source", source_id, "run", entity_id, "executed_as")
        elif revision_id is None:
            issues.append({"code": "run_revision_unbound", "entity_id": entity_id})
        elif revision_id not in revision_manifests:
            issues.append({"code": "run_revision_missing", "entity_id": entity_id,
                           "reference": revision_id})
        else:
            revision, actual_sha = revision_manifests[revision_id]
            if expected_sha != actual_sha:
                issues.append({"code": "run_revision_hash_mismatch", "entity_id": entity_id,
                               "reference": revision_id})
            elif revision["identity"].get("design_id") != design_id:
                issues.append({"code": "run_revision_design_mismatch", "entity_id": entity_id,
                               "reference": revision_id})
            else:
                edge("code_revision", revision_id, "run", entity_id, "executed_as")

    for entity_id, row in observations.items():
        node("observation", entity_id, status=row["status"], scope=row.get("scope"))
        run_id = row.get("run_id")
        if run_id in runs:
            edge("run", run_id, "observation", entity_id, "evaluated_as")
        else:
            issues.append({"code": "observation_run_missing", "entity_id": entity_id,
                           "reference": run_id})

    for row in entities["decision"]:
        entity_id = row["entity_id"]
        node("decision", entity_id, status=row["status"], action=row.get("action"))
        for observation_id in row.get("observation_refs", []):
            if observation_id in observations:
                edge("observation", observation_id, "decision", entity_id, "informed")
            else:
                issues.append({"code": "decision_observation_missing",
                               "entity_id": entity_id, "reference": observation_id})

    decisions = {row["entity_id"]: row for row in entities["decision"]}
    for row in entities["claim"]:
        claim_id = row["entity_id"]
        node("claim", claim_id, status=row["status"], scope=row.get("scope"),
             oriented_delta=row.get("oriented_delta"))
        decision_id = row.get("decision_id")
        design_id = row.get("design_id")
        if decision_id in decisions:
            edge("decision", decision_id, "claim", claim_id, "concluded_as")
        else:
            issues.append({"code": "claim_decision_missing", "entity_id": claim_id,
                           "reference": decision_id})
        if design_id in designs:
            edge("design", design_id, "claim", claim_id, "tested_in_scope")
        else:
            issues.append({"code": "claim_design_missing", "entity_id": claim_id,
                           "reference": design_id})
        refs = row.get("observation_refs")
        if not isinstance(refs, list) or len(refs) < 2:
            issues.append({"code": "claim_evidence_shape_invalid", "entity_id": claim_id})
            continue
        for observation_id in refs:
            if observation_id in observations:
                edge("observation", observation_id, "claim", claim_id, "supports_scope")
            else:
                issues.append({"code": "claim_observation_missing", "entity_id": claim_id,
                               "reference": observation_id})
        if all(ref in observations for ref in refs) and design_id in designs:
            design = designs[design_id]
            direction = design.get("metric_direction")
            baseline, candidate = (observations[ref]["value"] for ref in refs[:2])
            oriented = baseline - candidate if direction == "min" else candidate - baseline
            threshold = design["min_meaningful_effect"]
            if row.get("scope") == "signed_external_scope_boundary":
                slices = row.get("slice_effects")
                pairs = list(zip(refs[2::2], refs[3::2]))
                if (not isinstance(slices, list) or len(slices) < 2
                        or len(pairs) != len(slices) or len(refs) != 2 + 2 * len(slices)):
                    issues.append({"code": "claim_boundary_evidence_shape_invalid",
                                   "entity_id": claim_id})
                    continue
                recomputed = []
                for b_ref, c_ref in pairs:
                    if b_ref not in observations or c_ref not in observations:
                        break
                    b_obs, c_obs = observations[b_ref], observations[c_ref]
                    raw = c_obs["value"] - b_obs["value"]
                    recomputed.append({"slice_id": b_obs["scope"].removeprefix(
                        "confirmation:slice:"),
                        "oriented_delta": -raw if direction == "min" else raw})
                if recomputed != slices:
                    issues.append({"code": "claim_boundary_evidence_mismatch",
                                   "entity_id": claim_id})
                    continue
                passed = (abs(oriented) < threshold
                          and max(item["oriented_delta"] for item in recomputed) >= threshold
                          and min(item["oriented_delta"] for item in recomputed) < threshold)
                expected_status = ("boundary_supported_in_scope" if passed else
                                   "boundary_not_supported_in_scope")
            else:
                expected_status = ("supported_in_scope" if oriented >= threshold else
                                   "not_supported_in_scope")
            if (direction not in {"min", "max"}
                    or row.get("oriented_delta") != oriented
                    or row.get("status") != expected_status
                    or row.get("min_meaningful_effect") != design["min_meaningful_effect"]):
                issues.append({"code": "claim_evidence_mismatch", "entity_id": claim_id})

    nodes.sort(key=lambda item: (item["kind"], item["id"]))
    edges.sort(key=lambda item: (item["source"]["kind"], item["source"]["id"],
                                 item["relation"], item["target"]["kind"],
                                 item["target"]["id"]))
    issues.sort(key=lambda item: (item["code"], item["entity_id"],
                                  str(item.get("reference", ""))))
    return {"schema_version": "1.0", "study_id": study_id,
            "nodes": nodes, "edges": edges,
            "integrity": {"ok": not issues, "issues": issues}}
