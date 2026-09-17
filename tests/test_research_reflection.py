import copy
import unittest
from unittest.mock import Mock

from popper.core import ProtocolError
from popper.research.models import DeepSeekResearchPolicy
from popper.research.reflection import build_reflection_context, parse_reflection


def reflection_fixture(value=0.55, *, direction="max", available=2, remaining=True):
    baseline = {"study_id": "S-one", "observation_id": "OBS-baseline",
                "run_id": "RUN-baseline", "scorer_id": "accuracy-v1", "value": 0.6,
                "scope": "dev", "unit": "accuracy", "uncertainty": 0.01,
                "artifact_id": "/private/evaluation.json", "artifact_sha256": "a" * 64,
                "selector": "mean", "holdout_path": "/private/test.json"}
    observation = {**baseline, "observation_id": "OBS-candidate",
                   "run_id": "RUN-candidate", "value": value}
    current = {"study_id": "S-one", "hypothesis_id": "H-current", "version": 1,
               "status": "inconclusive", "mechanism": "Regularization reduces variance.",
               "applicability": "The registered development split.",
               "predictions": ["Accuracy should improve."],
               "falsification": ["No meaningful improvement."],
               "alternatives": ["Optimization failure."],
               "config": {"penalty": 1.0}, "design_id": "D-current"}
    next_candidate = {**current, "hypothesis_id": "H-next", "version": 2,
                      "parent_version": 1, "status": "untested", "design_id": "D-next",
                      "config": {"penalty": 0.1}}
    raw = {"study_id": "S-one", "study": {"question": "Does regularization help?"},
           "candidates": [current] + ([next_candidate] if remaining else []),
           "runs": [{"study_id": "S-one", "run_id": "RUN-baseline", "design_id": "D-control"},
                    {"study_id": "S-one", "run_id": "RUN-candidate", "design_id": "D-current"}],
           "observations": [baseline, observation], "budget": {"available": available},
           "metric": {"name": "accuracy", "direction": direction},
           "min_meaningful_effect": 0.02}
    return raw, baseline, observation


_DEFAULT_REVISION = object()


def response(action="add_control", next_id="H-next", revision=_DEFAULT_REVISION):
    if revision is _DEFAULT_REVISION:
        revision = ({"predictions": ["Weaker regularization should improve accuracy.",
                                     "If optimization failed, weakening penalty should not help."]}
                    if action == "add_control" else None)
    return {"action": action, "rationale": " The development evidence misses the threshold. ",
            "alternative_explanation": " Excess regularization may suppress useful features. ",
            "next_hypothesis_id": next_id, "revision": revision,
            "evidence_refs": ["OBS-baseline", "OBS-candidate"]}


class ReflectionContextTests(unittest.TestCase):
    def test_preregistered_slice_contrast_requests_independent_boundary_confirmation(self):
        raw, baseline, observed = reflection_fixture(value=0.605)
        baseline["hypothesis_id"] = "H-control-fixture"
        observed["hypothesis_id"] = "H-current"
        slice_rows = [
            {**baseline, "observation_id": "OBS-b-s0", "scope": "dev:slice:s0",
             "value": 0.50},
            {**observed, "observation_id": "OBS-c-s0", "scope": "dev:slice:s0",
             "value": 0.60},
            {**baseline, "observation_id": "OBS-b-s1", "scope": "dev:slice:s1",
             "value": 0.62},
            {**observed, "observation_id": "OBS-c-s1", "scope": "dev:slice:s1",
             "value": 0.615},
        ]
        raw["observations"].extend(slice_rows)
        context = build_reflection_context(raw, baseline, observed, "H-current")
        proposed = response("request_scope_boundary_confirmation", None)
        proposed["evidence_refs"] = context["evidence_refs"]

        self.assertTrue(context["boundary_detected"])
        self.assertEqual(["request_scope_boundary_confirmation"], context["allowed_actions"])
        self.assertEqual(6, len(context["evidence_refs"]))
        self.assertEqual("request_scope_boundary_confirmation",
                         parse_reflection(proposed, context)["action"])

    def test_effect_direction_and_allowed_actions(self):
        for value, direction, expected, actions in [
                (0.7, "max", 0.1, ["request_confirmation"]),
                (0.5, "max", -0.1, ["add_control"]),
                (0.605, "max", 0.005, ["add_control"]),
                (0.5, "min", 0.1, ["request_confirmation"]),
                (0.7, "min", -0.1, ["add_control"])]:
            with self.subTest(value=value, direction=direction):
                raw, baseline, observed = reflection_fixture(value, direction=direction)
                context = build_reflection_context(raw, baseline, observed, "H-current")
                self.assertAlmostEqual(expected, context["effect"])
                self.assertEqual(actions, context["allowed_actions"])
                self.assertEqual(2, context["remaining_candidates"][0]["version"])

    def test_no_budget_or_control_allows_only_stop(self):
        for kwargs in [{"available": 0}, {"available": 0.99}, {"remaining": False}]:
            with self.subTest(kwargs=kwargs):
                raw, baseline, observed = reflection_fixture(**kwargs)
                context = build_reflection_context(raw, baseline, observed, "H-current")
                self.assertEqual(["stop"], context["allowed_actions"])
                self.assertEqual("stop", parse_reflection(response("stop", None), context)["action"])

    def test_only_same_study_dev_evidence_and_whitelisted_fields(self):
        raw, baseline, observed = reflection_fixture()
        raw["observations"].extend([
            {**observed, "study_id": "S-other", "observation_id": "OBS-outside"},
            {**observed, "scope": "confirmation", "observation_id": "OBS-holdout"},
            {**observed, "scope": "test", "observation_id": "OBS-test"}])
        raw["candidates"].append({**raw["candidates"][1], "study_id": "S-other",
                                  "hypothesis_id": "H-other"})
        context = build_reflection_context(raw, baseline, observed, "H-current")
        self.assertEqual(["OBS-baseline", "OBS-candidate"],
                         [row["observation_id"] for row in context["observations"]])
        self.assertEqual(["H-next"], [row["hypothesis_id"] for row in context["remaining_candidates"]])
        self.assertNotIn("artifact_id", context["baseline"])
        self.assertNotIn("holdout_path", context["baseline"])
        context["remaining_candidates"][0]["config"]["penalty"] = 99
        self.assertEqual(0.1, raw["candidates"][1]["config"]["penalty"])

    def test_forged_cross_study_holdout_and_wrong_design_comparisons_rejected(self):
        for update in [{"value": 999}, {"study_id": "S-other"},
                       {"scope": "confirmation"}, {"observation_id": "OBS-unknown"},
                       {"run_id": "RUN-baseline"}]:
            with self.subTest(update=update):
                raw, baseline, observed = reflection_fixture()
                altered = {**observed, **update}
                with self.assertRaises(ProtocolError):
                    build_reflection_context(raw, baseline, altered, "H-current")
        raw, baseline, observed = reflection_fixture()
        raw["runs"][1]["design_id"] = "D-other"
        with self.assertRaisesRegex(ProtocolError, "design"):
            build_reflection_context(raw, baseline, observed, "H-current")

    def test_previously_observed_design_is_not_remaining(self):
        raw, baseline, observed = reflection_fixture()
        prior = {**observed, "observation_id": "OBS-prior", "run_id": "RUN-prior"}
        raw["observations"].append(prior)
        raw["runs"].append({"study_id": "S-one", "run_id": "RUN-prior", "design_id": "D-next"})
        context = build_reflection_context(raw, baseline, observed, "H-current")
        self.assertEqual([], context["remaining_candidates"])
        self.assertEqual(["stop"], context["allowed_actions"])

    def test_invalid_kernel_numbers_and_unknown_hypothesis_rejected(self):
        for value in [True, float("nan"), float("inf"), "0.9"]:
            with self.subTest(value=value):
                raw, baseline, observed = reflection_fixture(value)
                with self.assertRaises(ProtocolError):
                    build_reflection_context(raw, baseline, observed, "H-current")
        raw, baseline, observed = reflection_fixture()
        with self.assertRaises(ProtocolError):
            build_reflection_context(raw, baseline, observed, "H-outsider")


class ReflectionResponseTests(unittest.TestCase):
    def setUp(self):
        raw, baseline, observed = reflection_fixture()
        self.context = build_reflection_context(raw, baseline, observed, "H-current")

    def test_valid_revision_is_normalized_without_mutating_input(self):
        proposed = response(revision={"mechanism": " Weaker regularization preserves signal. ",
                                      "predictions": [" The registered control should improve. ",
                                                      " Optimization failure predicts no improvement. "]})
        original = copy.deepcopy(proposed)
        normalized = parse_reflection(proposed, self.context)
        self.assertEqual(original, proposed)
        self.assertEqual("Weaker regularization preserves signal.", normalized["revision"]["mechanism"])
        self.assertEqual(["The registered control should improve.",
                          "Optimization failure predicts no improvement."],
                         normalized["revision"]["predictions"])
        self.assertEqual("The development evidence misses the threshold.", normalized["rationale"])

    def test_positive_confirmation_needs_no_revision_or_next_target(self):
        raw, baseline, observed = reflection_fixture(0.7)
        context = build_reflection_context(raw, baseline, observed, "H-current")
        normalized = parse_reflection(response("request_confirmation", None), context)
        self.assertEqual("request_confirmation", normalized["action"])
        for proposed in [response("request_confirmation", "H-current"),
                         response("request_confirmation", None, {"mechanism": "changed"}),
                         response("add_control")]:
            with self.subTest(proposed=proposed), self.assertRaises(ProtocolError):
                parse_reflection(proposed, context)

    def test_missing_extra_and_fabricated_measurement_fields_rejected(self):
        for field in ["effect", "value", "p_value", "threshold", "holdout", "config", "version"]:
            proposed = response()
            proposed[field] = 0.99
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                parse_reflection(proposed, self.context)
        for field in list(response()):
            proposed = response()
            del proposed[field]
            with self.subTest(missing=field), self.assertRaises(ProtocolError):
                parse_reflection(proposed, self.context)

    def test_no_unknown_tested_or_whitespace_target_ids(self):
        for next_id in [None, "H-current", "H-other", " H-next", ["H-next"], 1]:
            with self.subTest(next_id=next_id), self.assertRaises(ProtocolError):
                parse_reflection(response(next_id=next_id), self.context)

    def test_both_exact_evidence_refs_are_required(self):
        for refs in [[], ["OBS-baseline"], ["OBS-baseline", "OBS-holdout"],
                     ["OBS-baseline", "OBS-outside"], ["OBS-baseline", " OBS-candidate"],
                     ["OBS-baseline", "OBS-candidate", "OBS-candidate"],
                     ["OBS-baseline", 1], "OBS-baseline", None]:
            proposed = response()
            proposed["evidence_refs"] = refs
            with self.subTest(refs=refs), self.assertRaises(ProtocolError):
                parse_reflection(proposed, self.context)
        proposed = response()
        proposed["evidence_refs"].reverse()
        self.assertEqual(self.context["evidence_refs"],
                         parse_reflection(proposed, self.context)["evidence_refs"])

    def test_negative_and_near_zero_cannot_request_confirmation(self):
        for value in [0.5, 0.605]:
            raw, baseline, observed = reflection_fixture(value)
            context = build_reflection_context(raw, baseline, observed, "H-current")
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                parse_reflection(response("request_confirmation", None), context)

    def test_revisions_reject_ids_configs_numeric_values_and_bad_text(self):
        for revision in [None, {}, "new explanation", {"hypothesis_id": "H-other"},
                         {"version": 3}, {"config": {"penalty": 999}}, {"value": 1.0},
                         {"holdout": "test"}, {"mechanism": 1}, {"mechanism": "  "},
                         {"applicability": []}, {"predictions": []},
                         {"falsification": "not an array"}, {"alternatives": [" "]},
                         {"predictions": ["Only one prediction."]},
                         {"predictions": ["One prediction.", " one PREDICTION. "]},
                         {"predictions": [False]}, {"mechanism": "x" * 4001},
                         {"predictions": ["x"] * 17}, {"alternatives": ["x" * 2001]}]:
            with self.subTest(revision=str(revision)[:100]), self.assertRaises(ProtocolError):
                parse_reflection(response(revision=revision), self.context)

    def test_revision_other_fields_are_validated_even_with_valid_predictions(self):
        for field, value in [("mechanism", " "), ("applicability", []),
                             ("alternatives", []), ("falsification", [1]),
                             ("config", {"penalty": 100}), ("effect", 1.0),
                             ("study_id", "S-other"), ("hypothesis_id", "H-other")]:
            proposed = response()
            proposed["revision"][field] = value
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                parse_reflection(proposed, self.context)

    def test_revision_accepts_all_five_scientific_fields(self):
        proposed = response()
        proposed["revision"].update({"mechanism": " A weaker penalty preserves useful signal. ",
                                     "applicability": " Registered development observations. ",
                                     "falsification": [" No improvement from weaker penalty. "],
                                     "alternatives": [" Optimization failure. "]})
        normalized = parse_reflection(proposed, self.context)
        self.assertEqual(5, len(normalized["revision"]))
        self.assertEqual(["Optimization failure."], normalized["revision"]["alternatives"])

    def test_response_text_types_length_and_non_json_are_rejected(self):
        for field in ["rationale", "alternative_explanation"]:
            for value in [None, 1, False, [], {}, "  ", "x" * 4001]:
                proposed = response()
                proposed[field] = value
                with self.subTest(field=field, value=str(value)[:30]), self.assertRaises(ProtocolError):
                    parse_reflection(proposed, self.context)
        for proposed in [None, [], "{}", {**response(), "revision": {"mechanism": object()}}]:
            with self.subTest(proposed=str(proposed)[:60]), self.assertRaises(ProtocolError):
                parse_reflection(proposed, self.context)

    def test_forged_allowed_actions_do_not_override_effect_gate(self):
        self.context["allowed_actions"] = ["request_confirmation", "stop"]
        with self.assertRaises(ProtocolError):
            parse_reflection(response("request_confirmation", None), self.context)

    def test_supported_effect_cannot_stop_before_confirmation(self):
        raw, baseline, observed = reflection_fixture(0.7)
        context = build_reflection_context(raw, baseline, observed, "H-current")
        self.assertEqual(["request_confirmation"], context["allowed_actions"])
        with self.assertRaisesRegex(ProtocolError, "action"):
            parse_reflection(response("stop", None), context)
        accepted = parse_reflection(response("request_confirmation", None), context)
        self.assertEqual("request_confirmation", accepted["action"])


class DeepSeekReflectionCorrectionTests(unittest.TestCase):
    def setUp(self):
        raw, baseline, observed = reflection_fixture()
        self.context = build_reflection_context(raw, baseline, observed, "H-current")
        self.original_context = copy.deepcopy(self.context)
        self.policy = object.__new__(DeepSeekResearchPolicy)

    def test_one_correction_receives_validation_error_and_original_evidence(self):
        invalid = response()
        invalid["revision"]["config"] = {"penalty": 999}
        valid = response()
        self.policy._call = Mock(side_effect=[invalid, valid])

        normalized = self.policy.reflect(self.context)

        self.assertEqual(parse_reflection(valid, self.original_context), normalized)
        self.assertEqual(2, self.policy._call.call_count)
        first_system, first_payload = self.policy._call.call_args_list[0].args
        correction_system, correction_payload = self.policy._call.call_args_list[1].args
        self.assertEqual(first_system, correction_system)
        self.assertEqual(self.original_context, first_payload)
        self.assertEqual(self.original_context, correction_payload["research_context"])
        self.assertEqual(invalid, correction_payload["previous_invalid_response"])
        self.assertIn("revision", correction_payload["validation_error"])
        self.assertTrue(correction_payload["correction_request"].strip())
        self.assertEqual(self.original_context, self.context)
        self.assertEqual({"penalty": 0.1}, self.context["remaining_candidates"][0]["config"])

    def test_repeated_invalid_response_stops_after_two_calls_without_context_changes(self):
        invalid = response()
        invalid["revision"]["config"] = {"penalty": 999}
        self.policy._call = Mock(side_effect=[invalid, copy.deepcopy(invalid)])

        with self.assertRaisesRegex(ProtocolError, "revision"):
            self.policy.reflect(self.context)

        self.assertEqual(2, self.policy._call.call_count)
        correction_payload = self.policy._call.call_args_list[1].args[1]
        self.assertEqual(self.original_context, correction_payload["research_context"])
        self.assertIn("revision", correction_payload["validation_error"])
        self.assertEqual(self.original_context, self.context)


if __name__ == "__main__":
    unittest.main()
