"""Signed, one-use holdout scoring; this service never runs candidate code.

The local implementation is engineering verification under one OS account.
Deploying it under a separate account, with a private database and signing key,
is an external requirement for label confidentiality.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

from ..core import (EVALUATORS, ProtocolError, canonical, digest, evaluator_pack,
                    model_inputs, number, score)
from .confirmation_contracts import (load_private_key, public_key_b64, sign_payload,
                                     validate_contract, validate_submission, verify_envelope,
                                     confirmation_service_hash)
from .evaluation_service import scoring_code_hash
from .schema import migrate_holdout


def _rows_from_bytes(content, evaluator_id):
    """Validate and score the same snapshot instead of reopening a mutable path."""
    rows = json.loads(content.decode("utf-8-sig"))
    evaluator_pack(evaluator_id).validate_rows(rows, None)
    return rows


class HoldoutService:
    def __init__(self, root, signing_key_path=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._key = load_private_key(
            Path(signing_key_path) if signing_key_path else self.root / "signing-key.pem",
            create=signing_key_path is None)
        self.public_key = public_key_b64(self._key)
        self._db = sqlite3.connect(self.root / "holdout.sqlite", timeout=30)
        self._db.row_factory = sqlite3.Row
        # 私有标签库刻意与 research.sqlite 分离：合并会让研究侧连接可直接读到
        # held-out 标签。这里只统一迁移入口与版本，不合并物理文件。
        migrate_holdout(self._db)

    def close(self):
        self._db.close()

    def register(self, *, dataset_id, dataset_version, evaluation_group, train_path,
                 dev_path, holdout_path, evaluator_id, seeds, min_effect, runtime_id,
                 runner_public_key, allowed_backend="windows_low_integrity_engineering",
                 analysis_slices=()):
        for value in (dataset_id, dataset_version, evaluation_group, runtime_id):
            if not isinstance(value, str) or not value.strip():
                raise ProtocolError("服务登记身份必须是非空字符串")
        if (not isinstance(seeds, list) or not seeds
                or any(type(seed) is not int for seed in seeds)
                or len(set(seeds)) != len(seeds)):
            raise ProtocolError("确认 seeds 必须是无重复的非空整数列表")
        if not number(min_effect) or min_effect <= 0:
            raise ProtocolError("确认 min_effect 必须是正的有限数值")
        snapshots = {name: Path(path).read_bytes() for name, path in
                     (("train", train_path), ("dev", dev_path), ("holdout", holdout_path))}
        pack = evaluator_pack(evaluator_id)
        splits = {name: _rows_from_bytes(content, evaluator_id)
                  for name, content in snapshots.items()}
        pack.validate_splits(splits.values())
        for left, right in (("train", "dev"), ("train", "holdout"), ("dev", "holdout")):
            if {row["id"] for row in splits[left]} & {row["id"] for row in splits[right]}:
                raise ProtocolError(f"{left}/{right} 数据 id 重叠")
            if ({pack.row_identity(row) for row in splits[left]}
                    & {pack.row_identity(row) for row in splits[right]}):
                raise ProtocolError(f"{left}/{right} 存在改名后重复样本")
        labels, rows = snapshots["holdout"], splits["holdout"]
        analysis_slices = list(analysis_slices)
        if analysis_slices:
            from .evaluation_service import _analysis_slices, _slice_rows
            _analysis_slices(analysis_slices, evaluator_id)
            for item in analysis_slices:
                _slice_rows(rows, item["rule"])
        payload = {
            "schema_version": "2.0" if analysis_slices else "1.0",
            "kind": "holdout_contract",
            "issuer_id": digest(self.public_key)[:24], "dataset_id": dataset_id,
            "dataset_version": dataset_version, "evaluation_group": evaluation_group,
            "train_sha256": hashlib.sha256(snapshots["train"]).hexdigest(),
            "dev_sha256": hashlib.sha256(snapshots["dev"]).hexdigest(),
            "holdout_sha256": hashlib.sha256(labels).hexdigest(),
            "features_sha256": digest(model_inputs(rows, evaluator_id)),
            "evaluator_id": evaluator_id, "evaluator_hash": digest(EVALUATORS[evaluator_id]),
            "scoring_code_sha256": scoring_code_hash(), "seeds": seeds,
            "service_code_sha256": confirmation_service_hash(),
            "min_effect": min_effect, "runtime_id": runtime_id,
            "runner_public_key": runner_public_key, "allowed_backend": allowed_backend,
            "trust": "engineering_same_account"}
        if analysis_slices:
            payload["analysis_slices"] = analysis_slices
        payload["contract_id"] = "HC-" + digest(payload)[:24]
        envelope = sign_payload(payload, self._key)
        validate_contract(envelope, self.public_key)
        consumption_key = digest({"dataset_id": dataset_id, "dataset_version": dataset_version,
                                  "evaluation_group": evaluation_group})
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            old = self._db.execute("SELECT envelope FROM contracts WHERE consumption_key=?",
                                   (consumption_key,)).fetchone()
            if old:
                if json.loads(old["envelope"]) != envelope:
                    raise ProtocolError("已登记的数据版本与评估组不能替换确认合同")
                return json.loads(old["envelope"])
            self._db.execute("INSERT INTO contracts VALUES(?,?,?,?)",
                             (payload["contract_id"], consumption_key, canonical(envelope), labels))
        return envelope

    def _registered_contract(self, contract_id):
        record = self._db.execute("SELECT * FROM contracts WHERE contract_id=?",
                                  (contract_id,)).fetchone()
        if record is None:
            raise ProtocolError("确认合同未由本服务登记")
        envelope = json.loads(record["envelope"])
        payload = validate_contract(envelope, self.public_key)
        return record, envelope, payload

    def _contract(self, contract_id):
        record, envelope, payload = self._registered_contract(contract_id)
        self._verify_implementation(payload)
        labels = bytes(record["labels"])
        if hashlib.sha256(labels).hexdigest() != payload["holdout_sha256"]:
            raise ProtocolError("服务私有确认数据摘要变化")
        rows = _rows_from_bytes(labels, payload["evaluator_id"])
        if digest(model_inputs(rows, payload["evaluator_id"])) != payload["features_sha256"]:
            raise ProtocolError("服务确认特征摘要变化")
        return record, envelope, payload, rows

    @staticmethod
    def _verify_implementation(payload):
        if payload["scoring_code_sha256"] != scoring_code_hash():
            raise ProtocolError("确认评分实现与登记合同不同")
        if payload["service_code_sha256"] != confirmation_service_hash():
            raise ProtocolError("确认服务实现与登记合同不同")

    def begin(self, submission_manifest):
        submission_manifest = json.loads(canonical(submission_manifest))
        identity = validate_submission(submission_manifest)
        record, envelope, contract, _ = self._contract(identity["contract_id"])
        validate_submission(submission_manifest, contract)
        if identity["contract_sha256"] != digest(envelope):
            raise ProtocolError("提交未绑定本服务签名的确认合同")
        submission_hash = digest(submission_manifest)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            old = self._db.execute("SELECT * FROM tickets WHERE consumption_key=?",
                                   (record["consumption_key"],)).fetchone()
            if old:
                ticket = json.loads(old["envelope"])
                if ticket["payload"]["submission_sha256"] != submission_hash:
                    raise ProtocolError("该数据版本与评估组已消费，不能换研究族或候选重试")
                if json.loads(old["submission"]) != submission_manifest:
                    raise ProtocolError("确认提交身份冲突")
                return ticket
            ticket = sign_payload({
                "schema_version": "1.0", "kind": "holdout_ticket",
                "ticket_id": "TKT-" + digest({"contract_id": contract["contract_id"],
                    "submission_id": submission_manifest["submission_id"]})[:24],
                "contract_id": contract["contract_id"], "contract_sha256": digest(envelope),
                "submission_id": submission_manifest["submission_id"],
                "submission_sha256": submission_hash,
                "consumption_key": record["consumption_key"],
                "features_sha256": contract["features_sha256"],
                "issued_at": datetime.now(timezone.utc).isoformat()}, self._key)
            self._db.execute("""INSERT INTO tickets(
                ticket_id,contract_id,consumption_key,submission,envelope,status)
                VALUES(?,?,?,?,?,'issued')""", (ticket["payload"]["ticket_id"],
                contract["contract_id"], record["consumption_key"], canonical(submission_manifest),
                canonical(ticket)))
        return ticket

    def _ticket(self, ticket):
        payload = verify_envelope(ticket, self.public_key)
        if payload.get("kind") != "holdout_ticket" or not isinstance(payload.get("ticket_id"), str):
            raise ProtocolError("不是本服务签发的确认票据")
        row = self._db.execute("SELECT * FROM tickets WHERE ticket_id=?",
                               (payload["ticket_id"],)).fetchone()
        if row is None or json.loads(row["envelope"]) != ticket:
            raise ProtocolError("确认票据未消费登记或内容变化")
        return row, payload

    def features(self, ticket):
        row, payload = self._ticket(ticket)
        if row["status"] != "issued":
            raise ProtocolError("仅 issued 确认票据可以获取无标签特征")
        _, _, contract, rows = self._contract(payload["contract_id"])
        return model_inputs(rows, contract["evaluator_id"])

    @staticmethod
    def _validate_runner(receipt, contract, ticket, identity, predictions):
        payload = verify_envelope(receipt, contract["runner_public_key"])
        required = {"schema_version", "kind", "ticket_id", "contract_id", "submission_id",
                    "submission_sha256", "runtime_id", "backend", "status", "code_hashes",
                    "predictions_sha256", "error_type"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ProtocolError("确认 runner 回执字段不完整或含未知字段")
        expected = {"schema_version": "1.0", "kind": "confirmation_runner_receipt",
                    "ticket_id": ticket["ticket_id"], "contract_id": ticket["contract_id"],
                    "submission_id": ticket["submission_id"],
                    "submission_sha256": ticket["submission_sha256"],
                    "runtime_id": contract["runtime_id"], "backend": contract["allowed_backend"],
                    "code_hashes": {name: identity[name]["files"] for name in ("control", "candidate")},
                    "predictions_sha256": digest(predictions)}
        for key, value in expected.items():
            if payload[key] != value:
                raise ProtocolError(f"确认 runner 回执身份不匹配: {key}")
        if payload["status"] not in {"succeeded", "failed"}:
            raise ProtocolError("确认 runner 回执状态非法")
        if ((payload["status"] == "succeeded" and payload["error_type"] is not None)
                or (payload["status"] == "failed" and
                    (not isinstance(payload["error_type"], str) or not payload["error_type"]))):
            raise ProtocolError("确认 runner 回执错误状态不一致")
        return payload

    @staticmethod
    def _score_predictions(predictions, contract, rows):
        if not isinstance(predictions, dict) or set(predictions) != {"control", "candidate"}:
            raise ProtocolError("确认预测必须包含且仅包含 control/candidate")
        scored = {}
        for arm in ("control", "candidate"):
            items = predictions[arm]
            if not isinstance(items, list):
                raise ProtocolError("确认预测 seed 清单必须是列表")
            by_seed = {}
            for item in items:
                if (not isinstance(item, dict) or set(item) != {"seed", "rows"}
                        or type(item["seed"]) is not int or item["seed"] in by_seed):
                    raise ProtocolError("确认预测 seed 清单格式错误或有重复")
                by_seed[item["seed"]] = item["rows"]
            if set(by_seed) != set(contract["seeds"]):
                raise ProtocolError("确认预测必须完整覆盖全部登记 seed")
            points = [{"seed": seed, "value": score(rows, by_seed[seed], contract["evaluator_id"])}
                      for seed in contract["seeds"]]
            values = [point["value"] for point in points]
            scored[arm] = {"per_seed": points, "mean": statistics.mean(values),
                           "std": statistics.stdev(values) if len(values) > 1 else 0.0}
            if contract["schema_version"] == "2.0":
                from .evaluation_service import _slice_rows, _slice_score
                scored[arm]["slices"] = []
                for registered in contract["analysis_slices"]:
                    selected = _slice_rows(rows, registered["rule"])
                    slice_points = [{"seed": seed,
                                     "value": _slice_score(selected, by_seed[seed],
                                                           contract["evaluator_id"])}
                                    for seed in contract["seeds"]]
                    raw = [point["value"] for point in slice_points]
                    scored[arm]["slices"].append({
                        "slice_id": registered["slice_id"], "n": len(selected),
                        "per_seed": slice_points, "mean": statistics.mean(raw),
                        "std": statistics.stdev(raw) if len(raw) > 1 else 0.0})
        return scored

    def _result(self, ticket, receipt_hash, *, scored=None, error_type=None,
                confirmation_kind=None):
        # Failure receipts must remain possible even when private labels or the
        # running service implementation failed their integrity check.
        _, _, contract = self._registered_contract(ticket["contract_id"])
        effect = None
        passed = None
        if scored is not None:
            effect = scored["candidate"]["mean"] - scored["control"]["mean"]
            if EVALUATORS[contract["evaluator_id"]]["metric"]["direction"] == "min":
                effect = -effect
            if not number(effect):
                raise ProtocolError("确认效应必须是有限数值")
            passed = effect >= contract["min_effect"]
            if (contract["schema_version"] == "2.0"
                    and confirmation_kind == "scope_boundary"):
                direction = EVALUATORS[contract["evaluator_id"]]["metric"]["direction"]
                slice_effects = []
                for registered in contract["analysis_slices"]:
                    index = next(i for i, item in enumerate(scored["control"]["slices"])
                                 if item["slice_id"] == registered["slice_id"])
                    raw = (scored["candidate"]["slices"][index]["mean"]
                           - scored["control"]["slices"][index]["mean"])
                    slice_effects.append(raw if direction == "max" else -raw)
                passed = (abs(effect) < contract["min_effect"] and len(slice_effects) >= 2
                          and max(slice_effects) >= contract["min_effect"]
                          and min(slice_effects) < contract["min_effect"])
        payload = {"schema_version": contract["schema_version"], "kind": "holdout_result",
            "ticket_id": ticket["ticket_id"], "contract_id": ticket["contract_id"],
            "contract_sha256": ticket["contract_sha256"],
            "submission_id": ticket["submission_id"],
            "submission_sha256": ticket["submission_sha256"],
            "runner_receipt_sha256": receipt_hash if receipt_hash is not None else digest(None),
            "scoring_code_sha256": contract["scoring_code_sha256"],
            "service_code_sha256": contract["service_code_sha256"],
            "status": "succeeded" if scored is not None else "failed",
            "control": scored["control"] if scored is not None else None,
            "candidate": scored["candidate"] if scored is not None else None,
            "effect": effect, "passed": passed,
            "error_type": error_type, "trust": "engineering_same_account"}
        if contract["schema_version"] == "2.0":
            payload["confirmation_kind"] = confirmation_kind
        return sign_payload(payload, self._key)

    def complete(self, ticket, runner_receipt, predictions):
        # Freeze caller-owned objects before validating their digests or scoring.
        ticket = json.loads(canonical(ticket))
        runner_receipt = json.loads(canonical(runner_receipt))
        predictions = json.loads(canonical(predictions))
        receipt_hash, prediction_hash = digest(runner_receipt), digest(predictions)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row, payload = self._ticket(ticket)
            if row["status"] in {"succeeded", "failed"}:
                self._verify_implementation(self._registered_contract(payload["contract_id"])[2])
                if (row["runner_receipt_sha256"] != receipt_hash
                        or row["predictions_sha256"] != prediction_hash):
                    raise ProtocolError("确认终态不能更换 runner 回执或预测")
                return json.loads(row["result"])
            if row["status"] != "issued":
                raise ProtocolError("确认评分仍在执行或已中断；只能显式终结，不能重算")
            self._db.execute("""UPDATE tickets SET status='scoring',runner_receipt_sha256=?,
                predictions_sha256=? WHERE ticket_id=?""", (receipt_hash, prediction_hash, payload["ticket_id"]))
        submission = json.loads(row["submission"])
        _, _, registered_contract = self._registered_contract(payload["contract_id"])
        identity = validate_submission(submission, registered_contract)
        confirmation_kind = identity.get("confirmation_kind")
        try:
            _, envelope, contract, rows = self._contract(payload["contract_id"])
            identity = validate_submission(submission, contract)
            if (identity["contract_sha256"] != digest(envelope)
                    or digest(submission) != payload["submission_sha256"]):
                raise ProtocolError("保存的确认提交身份变化")
            runner = self._validate_runner(runner_receipt, contract, payload, identity, predictions)
            if runner["status"] == "failed":
                result = self._result(payload, receipt_hash, error_type="RunnerFailed",
                                      confirmation_kind=confirmation_kind)
            else:
                scored = self._score_predictions(predictions, contract, rows)
                result = self._result(payload, receipt_hash, scored=scored,
                                      confirmation_kind=confirmation_kind)
        except Exception as error:
            # No error text or labels leave the service. Input, runner and scorer
            # failures all consume the existing ticket and return one final receipt.
            result = self._result(payload, receipt_hash, error_type=type(error).__name__,
                                  confirmation_kind=confirmation_kind)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self._db.execute("SELECT status FROM tickets WHERE ticket_id=?",
                                       (payload["ticket_id"],)).fetchone()
            if current["status"] != "scoring":
                raise ProtocolError("确认票据已被显式终结，不能覆盖终态")
            self._db.execute("UPDATE tickets SET status=?,result=? WHERE ticket_id=?",
                             (result["payload"]["status"], canonical(result), payload["ticket_id"]))
        return result

    def fail_interrupted(self, ticket):
        """An operator may finalize an abandoned ticket, never resume its scoring.

        The caller must establish that its runner/scorer is no longer active.
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row, payload = self._ticket(ticket)
            if row["status"] in {"succeeded", "failed"}:
                return json.loads(row["result"])
            result = self._result(payload, row["runner_receipt_sha256"],
                error_type="InterruptedBeforeReceipt" if row["runner_receipt_sha256"] is None else "Interrupted")
            self._db.execute("UPDATE tickets SET status='failed',result=? WHERE ticket_id=?",
                             (canonical(result), payload["ticket_id"]))
        return result
