import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, initialize
from popper.research.dev_threshold import first_candidate_past_dev_threshold
from popper.research.search_session import prepare_search_project, run_search


class MultiFeatureSearchTests(unittest.TestCase):
    def test_prepare_uses_feature_vectors_and_keeps_splits_disjoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "table.csv"
            data.write_text("a,b,y\n0,1,1\n1,0,2\n2,1,5\n3,0,10\n4,1,17\n5,0,26\n6,1,37\n7,0,50\n",
                            encoding="utf-8")
            baseline = root / "baseline.py"
            baseline.write_text("print('baseline')\n", encoding="utf-8")
            project = root / "project"
            prepared = prepare_search_project(project, data=data, target="y", metric="mae", baseline=baseline)
            self.assertFalse(set(prepared["holdout_ids"]) & set(prepared["dev_ids"]))
            spec = __import__("json").loads((project / "experiment.json").read_text(encoding="utf-8"))
            row = __import__("json").loads((project / "train.json").read_text(encoding="utf-8"))[0]
            self.assertEqual("vector-mae-v1", spec["evaluator_id"])
            self.assertEqual(2, len(row["features"]))


class LocalSearchHarnessTests(unittest.TestCase):
    def test_first_proposal_fails_and_second_fits_a_line(self):
        from popper.harness.local_search import LocalSearchHarness
        harness = LocalSearchHarness()
        files = [{"path": "model.py", "sha256": "abc", "content": "print(1)\n"}]
        first = harness.propose_revision("search", {}, {}, files)
        second = harness.propose_revision("search", {}, {}, files)
        self.assertIn("search probe", first["edits"][0].replacement)
        self.assertIn("slope", second["edits"][0].replacement)
        self.assertNotEqual(first["edits"][0].replacement, second["edits"][0].replacement)


class DevThresholdTests(unittest.TestCase):
    def test_threshold_selects_a_candidate_without_a_support_status(self):
        manifest = {"metric": {"direction": "min"}, "min_meaningful_effect": 0.05,
                    "control_hypothesis_id": "H-control",
                    "candidate_hypothesis_ids": ["H-a", "H-b"]}
        observations = [
            {"hypothesis_id": "H-control", "scope": "dev", "value": 1.0},
            {"hypothesis_id": "H-a", "scope": "dev", "value": 0.99},
            {"hypothesis_id": "H-b", "scope": "dev", "value": 0.9},
        ]
        self.assertEqual("H-b", first_candidate_past_dev_threshold(manifest, observations))

    def test_small_change_is_not_a_candidate(self):
        manifest = {"metric": {"direction": "min"}, "min_meaningful_effect": 0.05,
                    "control_hypothesis_id": "H-control",
                    "candidate_hypothesis_ids": ["H-a"]}
        observations = [
            {"hypothesis_id": "H-control", "scope": "dev", "value": 1.0},
            {"hypothesis_id": "H-a", "scope": "dev", "value": 0.99},
        ]
        self.assertIsNone(first_candidate_past_dev_threshold(manifest, observations))


class SearchSessionTests(unittest.TestCase):
    def test_run_search_does_not_reflect_or_read_next_action(self):
        class Controller:
            def __init__(self):
                self.kwargs = None

            def reflect(self, *_args, **_kwargs):
                raise AssertionError("reflect")

            def run(self, **kwargs):
                self.kwargs = kwargs
                return {"phase": "researching"}

        controller = Controller()
        result = run_search(controller, trusted_local=True, sandboxed=False, max_steps=8)
        self.assertEqual("researching", result["phase"])
        self.assertEqual({"trusted_local": True, "sandboxed": False, "max_steps": 8,
                          "auto_confirm": False, "autonomous_code": True, "search": True},
                         controller.kwargs)
        self.assertNotIn("next_action", controller.kwargs)
        with self.assertRaises(ProtocolError):
            run_search(controller, trusted_local=True, sandboxed=True)

    def test_prepare_seals_holdout_out_of_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "table.csv"
            data.write_text("x,y\n0,1\n1,2\n2,3\n3,4\n4,5\n5,6\n6,7\n7,8\n", encoding="utf-8")
            baseline = root / "baseline.py"
            baseline.write_text("print('baseline')\n", encoding="utf-8")
            project = root / "project"
            prepared = prepare_search_project(
                project, data=data, target="y", metric="mae", baseline=baseline)
            protocol = __import__('json').loads((project / 'search-protocol.json').read_text(encoding='utf-8'))
            spec = __import__('json').loads((project / 'experiment.json').read_text(encoding='utf-8'))
            self.assertEqual(['x'], protocol['features'])
            self.assertNotIn('population_transport', protocol)
            self.assertEqual('0.25', protocol['holdout_ratio'])
            self.assertEqual([0, 1, 2, 3, 4], protocol['split_seeds'])
            self.assertEqual([11], spec['seeds'])
            initialize(project)
            self.assertFalse(set(prepared["holdout_ids"]) & set(prepared["dev_ids"]))
            dev = (project / "dev.json").read_text(encoding="utf-8")
            for holdout_id in prepared["holdout_ids"]:
                self.assertNotIn(f'"{holdout_id}"', dev)


def _vector_rows(columns, values, outcomes):
    return [{"id": f"r{index}", "y": outcome,
             "features": [column[index] for column in values]}
            for index, outcome in enumerate(outcomes)]


class ClaimCardTests(unittest.TestCase):
    def test_card_uses_the_declared_claim_and_ignores_other_columns(self):
        from popper.research.search_session import (
            build_claim_card, confirm_claim_card, declared_claim, lock_claim_card,
            record_split_confirmation)
        columns = ["part", "whole", "span"]
        claim = declared_claim(["part", "whole"], relation="ratio")
        dev = _vector_rows(columns, [[1, 2, 3, 4], [1, 1, 1, 1], [9, 9, 9, 1]], [1, 2, 3, 4])
        dev.append({"id": "zero", "y": 9, "features": [5, 0, 3]})
        holdout = _vector_rows(columns, [[1, 2, 3, 4], [1, 1, 1, 1], [1, 8, 3, 1]], [1, 2, 3, 4])
        self.assertIsNone(build_claim_card(["Cement", "Water", "Age"], dev, None))
        card = build_claim_card(columns, dev, claim)
        self.assertEqual("ratio:part/whole", card["feature"])
        self.assertEqual("part/whole", card["label"])
        self.assertEqual("positive", card["direction"])
        self.assertEqual("search_set", card["direction_source"])
        self.assertEqual(1, card["search_rows_discarded"])
        self.assertNotIn("candidates", card)
        confirmed = confirm_claim_card(card, holdout, columns)
        self.assertEqual("ratio:part/whole", confirmed["feature"])
        self.assertEqual("positive", confirmed["direction"])
        decoy = declared_claim("span")
        other = build_claim_card(columns, holdout, decoy)
        self.assertEqual("span", other["feature"])
        self.assertNotEqual(other["feature"], card["feature"])
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "search-protocol.json").write_text(
                '{"schema":"popper.search-protocol/v1","features":["part","whole","span"],'
                '"metric":{"name":"mae","direction":"min"},"claim":'
                + __import__("json").dumps(claim) + "}", encoding="utf-8")
            (project / "dev.json").write_text(__import__("json").dumps(dev), encoding="utf-8")
            (project / "test.json").write_text("{not-json", encoding="utf-8")
            locked = lock_claim_card(project)
            self.assertEqual("ratio:part/whole", locked["feature"])
            again = lock_claim_card(project)
            self.assertEqual(locked, again)
            self.assertNotIn("Cement", (project / "claim-card.json").read_text(encoding="utf-8"))

            class Controller:
                manifest = {"min_meaningful_effect": 0.05}
                run_dir = project / "run"

            controller = Controller()
            controller.project = project
            (project / "experiment.json").write_text('{"evaluator_id":"vector-mae-v1"}', encoding="utf-8")
            (project / "train.json").write_text("[]", encoding="utf-8")
            (project / "test.json").write_text("{still-not-json", encoding="utf-8")
            before = (project / "claim-card.json").read_bytes()
            with self.assertRaises(Exception):
                record_split_confirmation(controller, sandboxed=False, trusted_local=True)
            self.assertEqual(before, (project / "claim-card.json").read_bytes())

    def test_declared_direction_is_not_rewritten_and_short_holdout_has_no_interval(self):
        from popper.research.search_session import build_claim_card, confirm_claim_card, declared_claim
        columns = ["dose"]
        dev = _vector_rows(columns, [[1, 2, 3, 4, 5]], [1, 2, 3, 4, 5])
        locked = declared_claim("dose")
        card = build_claim_card(columns, dev, locked)
        self.assertEqual("dose", card["feature"])
        self.assertEqual("positive", card["direction"])
        opposite = _vector_rows(columns, [list(range(1, 31))], [-float(value) for value in range(1, 31)])
        refuted = confirm_claim_card(card, opposite, columns)
        self.assertEqual("positive", refuted["direction"])
        self.assertEqual("refuted", refuted["scientific_status"])
        self.assertLess(refuted["ci95"][1], 0)
        supported = confirm_claim_card(card, dev, columns)
        self.assertEqual("supported", supported["scientific_status"])
        self.assertGreater(supported["ci95"][0], 0)
        short = confirm_claim_card(card, dev[:1], columns)
        self.assertIsNone(short["ci95"])
        self.assertEqual("inconclusive", short["scientific_status"])
        self.assertEqual(1, short["n_rows"])
        forced = build_claim_card(columns, dev, declared_claim("dose", "negative"))
        self.assertEqual("negative", forced["direction"])
        self.assertEqual("declared", forced["direction_source"])
        self.assertGreater(forced["search_slope"], 0)
        self.assertEqual("refuted", confirm_claim_card(forced, dev, columns)["scientific_status"])


class PopulationTransportTests(unittest.TestCase):
    def test_declared_cut_keeps_the_claim_and_names_the_confirmation_population(self):
        from popper.research.claim_projection import project_claim
        from popper.research.search_session import (
            build_claim_card, confirm_claim_card, declared_claim, declared_transport,
            lock_claim_card, prepare_search_project)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "table.csv"
            data.write_text(
                "part,span,batch,y\n"
                "1,1,1,1\n2,1,2,2\n3,1,3,3\n4,1,4,4\n5,1,5,5\n6,1,6,6\n"
                "1,10,20,1\n2,20,30,2\n0,30,40,0\n"
                "1,1,,9\n1,1,old,9\n",
                encoding="utf-8")
            baseline = root / "baseline.py"
            baseline.write_text("print('baseline')\n", encoding="utf-8")
            project = root / "project"
            claim_input = declared_claim("part")
            prepare_search_project(project, data=data, target="y", metric="mae", baseline=baseline,
                                   transport=declared_transport("batch", "10"), claim=claim_input)
            protocol = __import__("json").loads((project / "search-protocol.json").read_text(encoding="utf-8"))
            self.assertEqual("batch", protocol["population_transport"]["column"])
            self.assertEqual({"operator": "le", "threshold": 10}, protocol["population_transport"]["search"])
            self.assertEqual({"operator": "gt", "threshold": 10}, protocol["population_transport"]["confirmation"])
            self.assertEqual(2, protocol["population_transport"]["rows_discarded"])
            self.assertEqual("part", protocol["claim"]["feature"])
            self.assertNotIn("holdout_ratio", protocol)
            self.assertNotIn("Cement", (project / "search-protocol.json").read_text(encoding="utf-8"))
            self.assertFalse((project / "claim-card.json").exists())
            features = protocol["features"]
            batch_index = features.index("batch")

            def batches(name):
                rows = __import__("json").loads((project / name).read_text(encoding="utf-8"))
                return rows, [row["features"][batch_index] for row in rows]

            train, train_batch = batches("train.json")
            dev, dev_batch = batches("dev.json")
            holdout, holdout_batch = batches("test.json")
            self.assertTrue(train and dev and holdout)
            self.assertTrue(all(value <= 10 for value in train_batch + dev_batch))
            self.assertTrue(all(value > 10 for value in holdout_batch))
            self.assertEqual(3, len(holdout))
            self.assertEqual({20, 30, 40}, set(holdout_batch))
            locked = lock_claim_card(project)
            again = lock_claim_card(project)
            self.assertEqual(locked, again)
            self.assertEqual("part", locked["feature"])
            self.assertEqual("positive", locked["direction"])
            self.assertEqual(10, locked["population_transport"]["search"]["threshold"])
            other = build_claim_card(features, holdout, declared_claim("span"))
            self.assertEqual("span", other["feature"])
            self.assertNotEqual(other["feature"], locked["feature"])
            confirmed = confirm_claim_card(locked, holdout, features)
            self.assertEqual("part", confirmed["feature"])
            self.assertEqual("positive", confirmed["direction"])

            columns = ["part", "span", "batch"]
            search = [{"id": f"s{index}", "y": value, "features": [value, 1, 7]}
                      for index, value in enumerate(range(1, 9), start=1)]
            card = build_claim_card(columns, search, declared_claim("part"))
            self.assertEqual("part", card["feature"])
            self.assertEqual("positive", card["direction"])
            noisy = [{"id": f"n{index}", "y": 100 if index % 2 == 0 else -100,
                      "features": [index, 56, 20]} for index in range(1, 31)]
            transport_confirmation = confirm_claim_card(card, noisy, columns)
            self.assertLess(transport_confirmation["ci95"][0], 0)
            self.assertGreater(transport_confirmation["ci95"][1], 0)
            self.assertEqual("inconclusive", transport_confirmation["scientific_status"])
            same_population = confirm_claim_card(card, search, columns)
            self.assertEqual("supported", same_population["scientific_status"])
            self.assertGreater(same_population["ci95"][0], 0)
            claim = {"study_id": "S-test", "hypothesis_id": "H-candidate", "hypothesis_version": 1,
                     "observation_refs": ["OBS-control", "OBS-candidate"], "metric": "mae",
                     "oriented_delta": 9, "min_meaningful_effect": 0.05, "status": "supported_in_scope"}
            judgment = {"study_id": "S-test", "hypothesis_id": "H-candidate", "hypothesis_version": 1,
                        "scope": "confirmation", "observation_refs": claim["observation_refs"],
                        "judgment_id": "J-test", "new_judgment": "inconclusive", "evidence": {}}
            observations = [
                {"observation_id": "OBS-dev-base", "hypothesis_id": "H-control", "scope": "dev", "value": 13},
                {"observation_id": "OBS-dev-cand", "hypothesis_id": "H-candidate", "scope": "dev", "value": 4},
                {"observation_id": "OBS-control", "hypothesis_id": "H-control", "scope": "confirmation", "value": 100},
                {"observation_id": "OBS-candidate", "hypothesis_id": "H-candidate", "scope": "confirmation", "value": 1},
            ]
            projected = project_claim(claim, [judgment], observations, split_confirmation={
                "unit": "confirmation_row", "baseline": 100, "candidate": 1,
                "same_population_ci95": same_population["ci95"],
                "population_transport": protocol["population_transport"],
                "claim_assertion": transport_confirmation,
            })
            self.assertEqual("inconclusive", projected["scientific_status"])
            text = projected["compiled_sentence"]
            self.assertIn("这是预先声明的人群迁移确认：确认人群为 batch > 10，不是同一人群的随机留出", text)
            self.assertTrue(text.endswith("这不建立机制。"))
            self.assertNotIn("断言获得独立证据支持", text)
            self.assertIn("确认集基线 MAE 100、候选 MAE 1", text)
            self.assertNotIn("龄期", text)


class SearchNodeClaimTests(unittest.TestCase):
    def _table(self, root, claim=None):
        root.mkdir(parents=True, exist_ok=True)
        data = root / "table.csv"
        lines = ["dose,span,y"]
        for index in range(1, 13):
            lines.append(f"{index},{index % 3},{index}")
        data.write_text("\n".join(lines) + "\n", encoding="utf-8")
        baseline = root / "baseline.py"
        baseline.write_text("print('baseline')\n", encoding="utf-8")
        project = root / "project"
        prepare_search_project(project, data=data, target="y", metric="mae", baseline=baseline,
                               claim=claim)
        return project

    def test_scripted_search_keeps_the_first_valid_claim(self):
        from popper.harness.base import parse_revision_proposal
        from popper.research.search_session import (
            accept_search_claim, confirm_claim_card, declared_claim, lock_claim_card)

        class Harness:
            def __init__(self):
                self.pending = (
                    {"columns": ["missing"]},
                    {"columns": ["dose"]},
                    {"columns": ["span"], "direction": "negative"},
                )

            def propose_revision(self, *_args, **_kwargs):
                claim = self.pending[0]
                self.pending = self.pending[1:]
                return {"edits": [], "rationale": "交出断言", "claim": claim}

        with tempfile.TemporaryDirectory() as tmp:
            project = self._table(Path(tmp))
            harness = Harness()
            sealed = (project / "test.json").read_bytes()
            (project / "test.json").write_text("{not-json", encoding="utf-8")
            missed = harness.propose_revision()
            self.assertIsNone(accept_search_claim(project, missed["claim"]))
            proposed = parse_revision_proposal(harness.propose_revision())
            locked = accept_search_claim(project, proposed["claim"])
            self.assertEqual("dose", locked["feature"])
            self.assertEqual("search_node", locked["origin"])
            self.assertEqual("search_set", locked["direction_source"])
            self.assertEqual("positive", locked["direction"])
            later = harness.propose_revision()
            self.assertEqual(locked, accept_search_claim(project, later["claim"]))
            (project / "test.json").write_bytes(sealed)
            features = ["dose", "span"]
            holdout = __import__("json").loads((project / "test.json").read_text(encoding="utf-8"))
            confirmed = confirm_claim_card(locked, holdout, features)
            self.assertEqual("dose", confirmed["feature"])
            self.assertEqual("positive", confirmed["direction"])
            self.assertIsNone(lock_claim_card(project / "empty"))

            declared = self._table(Path(tmp) / "declared", declared_claim("dose"))
            self.assertIsNone(accept_search_claim(declared, {"columns": ["span"], "direction": "negative"}))
            self.assertFalse((declared / "claim-card.json").exists())
            protocol_card = lock_claim_card(declared)
            self.assertEqual("dose", protocol_card["feature"])
            self.assertNotEqual("search_node", protocol_card.get("origin"))

            bare = self._table(Path(tmp) / "bare")
            self.assertIsNone(lock_claim_card(bare))
            empty = confirm_claim_card(None, [], ["dose"])
            self.assertEqual("inconclusive", empty["scientific_status"])
            self.assertEqual("没有断言卡", empty["interval_reason"])
