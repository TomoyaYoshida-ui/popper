"""Bounded scientific designs with frozen predictions and executable interventions."""
from __future__ import annotations

from ..core import ProtocolError, canonical, digest
from .contracts import ExperimentDesign, Hypothesis
from .controller import _design_id
from .prediction import (evaluate_prediction, render_prediction, research_contract,
                         validate_prediction)

FIELDS = {"schema", "action", "mechanism", "config", "intervention", "contrasts",
          "prediction_contract", "evidence_refs", "rationale", "parent_hypothesis_id"}


def validate_experiment_proposal(value, manifest, context):
    if not isinstance(value, dict) or set(value) != FIELDS or value["schema"] != "popper.experiment-proposal/v2":
        raise ProtocolError("自主实验提案字段或版本不合法")
    if value["action"] not in {"new_candidate", "add_control", "repair", "stop", "request_confirmation"}:
        raise ProtocolError("自主实验动作不合法")
    contract = validate_prediction(value["prediction_contract"], evaluator_id=manifest["scorer_id"],
        threshold=manifest["min_meaningful_effect"], scope=manifest["prediction_contract"]["scope"])
    if contract != manifest["prediction_contract"]:
        raise ProtocolError("自主实验不能改变冻结预测")
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        raise ProtocolError("实验必须说明可审计理由")
    visible = {row["observation_id"] for row in context["observations"]
               if row["scope"] == "dev" or row["scope"].startswith("dev:slice:")}
    refs = value["evidence_refs"]
    if (not isinstance(refs, list) or len(refs) != len(set(refs)) or not set(refs) <= visible):
        raise ProtocolError("实验只能引用当前研究的开发证据")
    parent = value["parent_hypothesis_id"]
    if parent is not None and parent not in {r["hypothesis_id"] for r in context["candidates"]}:
        raise ProtocolError("实验父假设不存在")
    if value["action"] in {"stop", "request_confirmation"}:
        return dict(value)
    if (not isinstance(value["config"], dict) or not value["config"]
            or len(canonical(value["config"]).encode()) > 16000):
        raise ProtocolError("实验执行配置不合法")
    if not isinstance(value["mechanism"], str) or not value["mechanism"].strip():
        raise ProtocolError("实验必须给出待验证机制")
    intervention = value["intervention"]
    if (not isinstance(intervention, dict) or set(intervention) != {"kind", "parameters"}
            or intervention["kind"] not in {"feature", "model", "algorithm", "ablation", "control", "repair"}
            or not isinstance(intervention["parameters"], dict) or not intervention["parameters"]):
        raise ProtocolError("新设计必须声明实际干预")
    contrasts = value["contrasts"]
    if (not isinstance(contrasts, list) or len(contrasts) != 2
            or any(not isinstance(r, dict) or set(r) != {"explanation", "expected_direction"}
                   or not isinstance(r["explanation"], str) or not r["explanation"].strip() for r in contrasts)
            or {r["expected_direction"] for r in contrasts} != {"gte", "lt"}):
        raise ProtocolError("新设计必须给出方向不同的两条竞争预测")
    if value["config"] == manifest["configs"][manifest["control_hypothesis_id"]]:
        raise ProtocolError("候选配置不能冒充冻结基线")
    fingerprint = digest({"config": value["config"], "intervention": intervention})
    if any(row.get("proposal_fingerprint") == fingerprint for row in context.get("designs", [])):
        raise ProtocolError("重复实验设计，未执行")
    return {**value, "proposal_fingerprint": fingerprint}


def register_experiment(controller, value, context):
    value = validate_experiment_proposal({k: v for k, v in value.items() if k != "proposal_fingerprint"},
                                        controller.manifest, context)
    if value["action"] in {"stop", "request_confirmation"}:
        return None
    from .envelope import assert_active, load_envelope, read_usage
    envelope = load_envelope(controller.run_dir)
    usage = read_usage(controller.run_dir)
    assert_active(envelope, dev_runs_used=usage["dev_runs_used"], tokens_used=usage["tokens_used"],
        state_version=context["study"]["state_version"],
        contract_sha256=research_contract(controller.manifest))
    if "design_dev" not in envelope["allowed_actions"]:
        raise ProtocolError("包络没有授权新增实验设计")
    hypothesis_id = "H-auto-" + value["proposal_fingerprint"][:16]
    design_id = _design_id(hypothesis_id)
    ledger = controller.store.budget(controller.manifest["study_id"])
    with controller.store.transaction():
        controller.store.record_envelope_approval(envelope)
        if ledger.balance(controller.manifest["family_id"])["available"] < 3:
            raise ProtocolError("开发预算不足，必须保留两次确认计分额度")
        reservation = ledger.reserve(controller.manifest["family_id"], "DESIGN-" + design_id,
                                     1.0, kind="design")
        controller.store.add_hypothesis(Hypothesis(hypothesis_id, controller.manifest["study_id"],
            1, None, value["mechanism"], controller.manifest["prediction_contract"]["scope"],
            (render_prediction(value["prediction_contract"]),),
            ("仅裁定注册预测，不裁定机制胜负",), tuple(r["explanation"] for r in value["contrasts"])))
        baseline = canonical(controller.manifest["configs"][controller.manifest["control_hypothesis_id"]])
        controller.store.create_design(ExperimentDesign(design_id, hypothesis_id,
            interventions=(canonical(value["config"]),), control=baseline, baseline=baseline,
            metric=controller.manifest["metric"]["name"], scorer_id=controller.manifest["scorer_id"],
            metric_direction=controller.manifest["metric"]["direction"],
            analysis_unit=controller.manifest["repeat_unit"], splits=("dev", "confirmation"),
            seeds=tuple(controller.manifest["repeat_values"]),
            min_meaningful_effect=controller.manifest["min_meaningful_effect"],
            prediction_contract=value["prediction_contract"], execution_config=value["config"],
            proposal_fingerprint=value["proposal_fingerprint"], proposal=value))
        controller.store.freeze_design(design_id)
    controller.manifest["configs"][hypothesis_id] = value["config"]
    controller.manifest["candidate_hypothesis_ids"].append(hypothesis_id)
    controller.exp.authorize_candidate(value["config"], envelope)
    return {"hypothesis_id": hypothesis_id, "design_id": design_id, "reservation": reservation}


def run_autonomous(controller, baseline, max_steps, should_cancel=None):
    from .context import build_context
    from .envelope import load_envelope, read_usage, write_usage, assert_active
    from .actions import STOP, REQUEST_CONFIRMATION, ActionProposal
    from .contracts import HypothesisStatus, StudyStatus
    from ..core import read_json, write_json
    if not hasattr(controller.policy, "propose_experiment"):
        raise ProtocolError("自主设计需要支持结构化实验提案的策略")
    limit = 4 if max_steps is None else max_steps
    envelope = load_envelope(controller.run_dir)
    journal = controller.run_dir / "autonomous-progress.json"
    records = read_json(journal)["attempts"] if journal.is_file() else []
    for step in range(len(records), limit):
        if should_cancel and should_cancel():
            raise ProtocolError("研究执行已取消")
        context = build_context(controller.store, controller.manifest["study_id"], controller.manifest)
        context["designs"] = controller.store.list("design")
        context["prediction_contract"] = controller.manifest["prediction_contract"]
        context["attempts"] = records
        context["registered_configs"] = list(controller.manifest["configs"].values())
        usage = read_usage(controller.run_dir)
        assert_active(envelope, dev_runs_used=step, state_version=context["study"]["state_version"],
                      tokens_used=usage["tokens_used"], contract_sha256=research_contract(controller.manifest))
        record = {"step": step + 1, "status": "failed", "observation_id": None}
        registration = None
        try:
            proposal = validate_experiment_proposal(controller.policy.propose_experiment(context),
                                                   controller.manifest, context)
            if proposal["action"] in {"stop", "request_confirmation"}:
                if proposal["action"] == "request_confirmation" and not any(
                        r["status"] == "supported_in_scope" for r in context["candidates"]):
                    raise ProtocolError("未达到开发阈值，不能请求确认")
                controller._record(ActionProposal(STOP if proposal["action"] == "stop" else REQUEST_CONFIRMATION,
                    proposal["rationale"], next((r["hypothesis_id"] for r in context["candidates"]
                        if r["status"] == "supported_in_scope"), None), source="autonomous_design"))
                if proposal["action"] == "stop":
                    controller.store.set_study_status(controller.manifest["study_id"], StudyStatus.CONCLUDED)
                break
            registration = register_experiment(controller, proposal, context)
            # Persist the attempt before any model or worker side effect; interrupted attempts remain visible.
            record.update(registration={k: v for k, v in registration.items() if k != "reservation"},
                          status="running")
            records.append(record)
            write_json(journal, {"schema": "popper.autonomous-progress/v2", "attempts": records})
            result = controller.implement(registration["hypothesis_id"], search_node=True,
                should_cancel=should_cancel, design_reservation=registration["reservation"])
            observation = result["observation"]
            effect = observation["value"] - baseline["value"]
            if controller.manifest["metric"]["direction"] == "min":
                effect = -effect
            judgment = evaluate_prediction(controller.manifest["prediction_contract"], effect,
                                          repeat_unit=controller.manifest["repeat_unit"])
            controller.store.set_hypothesis_status(registration["hypothesis_id"], 1,
                HypothesisStatus.SUPPORTED_IN_SCOPE if judgment["status"] == "met" else HypothesisStatus.INCONCLUSIVE)
            record.update(status="succeeded", observation_id=observation["observation_id"],
                          prediction_result=judgment)
        except ProtocolError as error:
            record.update(status="failed", error_type=type(error).__name__, reason=str(error))
            if not records or records[-1] is not record:
                records.append(record)
        finally:
            if registration:
                controller.store.budget(controller.manifest["study_id"]).settle(
                    registration["reservation"]["reservation_id"], 1.0)
            tokens = getattr(controller.harness, "tokens_used", 0)
            tokens = tokens() if callable(tokens) else tokens
            write_usage(controller.run_dir, dev_runs_used=len(records), tokens_used=int(tokens))
            write_json(journal, {"schema": "popper.autonomous-progress/v2", "attempts": records})
    return controller.status()
