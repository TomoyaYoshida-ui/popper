"""Boundary Claims mean heterogeneity, not an overall positive effect."""
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, canonical, digest, file_hash, write_json
from popper.research.store import _signed_claim_from_evidence


class SignedBoundaryClaimTests(unittest.TestCase):
    def test_boundary_claim_uses_global_and_preregistered_slice_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "result.json"
            baseline = {"study_id": "S-boundary", "design_id": "D-control",
                        "status": "frozen", "control": {"mode": "base"},
                        "interventions": [{"mode": "base"}], "metric": "accuracy",
                        "metric_direction": "max", "scorer_id": "accuracy-v1",
                        "min_meaningful_effect": 0.02,
                        "hypothesis_id": "H-control", "hypothesis_version": 1}
            candidate = {**baseline, "design_id": "D-candidate",
                         "interventions": [{"mode": "candidate"}],
                         "hypothesis_id": "H-candidate"}
            values = [("confirmation", 0.6, 0.605),
                      ("confirmation:slice:s0", 0.5, 0.6),
                      ("confirmation:slice:s1", 0.62, 0.615)]
            result = {"status": "succeeded", "confirmation_kind": "scope_boundary",
                      "passed": True, "effect": values[0][2] - values[0][1],
                      "trust": "engineering_same_account",
                      "control": {"mean": values[0][1], "slices": [
                          {"slice_id": "s0", "mean": values[1][1]},
                          {"slice_id": "s1", "mean": values[2][1]}]},
                      "candidate": {"mean": values[0][2], "slices": [
                          {"slice_id": "s0", "mean": values[1][2]},
                          {"slice_id": "s1", "mean": values[2][2]}]}}
            receipt = {"payload": result, "signature": "fixture-only"}
            write_json(receipt_path, receipt)
            observations, designs, runs = [], [], []
            for scope, control_value, candidate_value in values:
                for role, value, design in (("control", control_value, baseline),
                                            ("candidate", candidate_value, candidate)):
                    index = len(observations)
                    observations.append({
                        "observation_id": f"OBS-{index}", "study_id": "S-boundary",
                        "design_id": design["design_id"], "run_id": f"RUN-{role}",
                        "scorer_id": "accuracy-v1", "unit": "accuracy",
                        "scope": scope, "value": value,
                        "artifact_id": str(receipt_path),
                        "artifact_sha256": file_hash(receipt_path)})
                    designs.append(design)
                    runs.append({"run_id": f"RUN-{role}", "study_id": "S-boundary",
                                 "design_id": design["design_id"],
                                 "status": "succeeded", "external_source": {
                                     "kind": "signed_confirmation_bundle", "role": role,
                                     "submission_id": "SUB-fixture",
                                     "submission_sha256": "a" * 64}})
            decision = {"study_id": "S-boundary", "actor": "external_confirmation_controller",
                        "action": "conclude_scope_boundary",
                        "observation_refs": [obs["observation_id"] for obs in observations],
                        "rationale": canonical({"external_receipt_sha256": digest(receipt),
                                                "status": "succeeded", "passed": True,
                                                "effect": result["effect"],
                                                "confirmation_kind": "scope_boundary",
                                                "trust": result["trust"]})}
            payload, status = _signed_claim_from_evidence(
                "S-boundary", "DEC-boundary", decision,
                observations, designs, runs, receipt)
            self.assertEqual("boundary_supported_in_scope", status)
            self.assertEqual("signed_external_scope_boundary", payload["scope"])
            self.assertLess(payload["oriented_delta"], payload["min_meaningful_effect"])
            self.assertEqual(["s0", "s1"],
                             [row["slice_id"] for row in payload["slice_effects"]])

            # Re-labelling an unconfirmed boundary as confirmed cannot pass.
            altered = dict(receipt)
            altered["payload"] = {**result, "passed": False}
            write_json(receipt_path, altered)
            for obs in observations:
                obs["artifact_sha256"] = file_hash(receipt_path)
            with self.assertRaises(ProtocolError):
                _signed_claim_from_evidence("S-boundary", "DEC-boundary", decision,
                                            observations, designs, runs, altered)
