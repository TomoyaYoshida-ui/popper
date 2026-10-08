"""数据接入夹具。不调用模型，不下载数据，不写验收报告。"""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from popper.cli import main
from popper.core import (ProtocolError, canonical, digest, evaluator_pack, file_hash, initialize,
                         read_json, write_json)
from popper.research.contracts import Study
from popper.research import intake as intake_mod
from popper.research.controller import ResearchController
from popper.research.intake import (MAX_COLUMNS, MAX_KMER_K, MAX_ROWS, IntakeLog, adapters,
                                    approve_adapter, import_intake_events, intake, link_parent_run,
                                    register_adapter)
from popper.research.recipe import approve_recipe, compile_recipe, propose_recipe
from popper.research.recipe_runner import (MAX_COLUMNS as RUNNER_COLUMNS, MAX_KMER_K as RUNNER_K,
                                           run_recipe, run_workspace)
from popper.research.store import ResearchStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "data-intake"


def _ratios():
    return {"train_ratio": "0.600000", "dev_ratio": "0.200000", "holdout_ratio": "0.200000"}


def _write(path, text):
    Path(path).write_text(text, encoding="utf-8")
    return path


class IntakeTests(unittest.TestCase):
    def test_unknown_extension_magic_and_csv_conversion_are_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "notes.xyz", "a,b\n1,2\n3,4\n5,6\n")
            with self.assertRaises(ProtocolError) as unknown:
                intake(root, raw="notes.xyz", split_strategy="random", seed=1,
                       source_note="fixture", target_column="b", **_ratios())
            self.assertEqual(str(unknown.exception), "未知扩展名，不能猜测格式")
            _write(root / "data.csv", "PAR1,not-really\n1,2\n3,4\n5,6\n")
            with self.assertRaises(ProtocolError) as magic:
                intake(root, raw="data.csv", split_strategy="random", seed=1,
                       source_note="fixture", target_column="b", **_ratios())
            self.assertEqual(str(magic.exception), "文件魔数与已登记格式不一致")
            _write(root / "ok.csv", "a,b\n1,0\n2,1\n3,0\n")
            with self.assertRaises(ProtocolError) as converted:
                intake(root, raw="ok.csv", split_strategy="random", seed=1,
                       source_note="fixture", target_column="b",
                       convert_unknown_to_csv=True, **_ratios())
            self.assertEqual(str(converted.exception), "不能把未知文件转换成 CSV 再读取")
            _write(root / "table.parquet", "PAR1-not-a-real-parquet")
            with self.assertRaises(ProtocolError) as parquet:
                intake(root, raw="table.parquet", split_strategy="random", seed=1,
                       source_note="fixture", **_ratios())
            self.assertEqual(str(parquet.exception), "当前环境不能读取 Parquet")
        ids = {item["id"] for item in adapters()}
        self.assertEqual(ids, {"csv", "parquet", "json", "fasta"})
        self.assertFalse(ids & {"cif", "poscar", "pdb", "sdf", "fastq"})

    def test_seal_happens_before_label_stats(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "table.csv", "\ufeffage,income,label\n1,1.0,0\n2,,1\n3,3.0,0\n4,4.0,1\n")
            seen = {}
            before = intake_mod.label_stats_calls

            def checkpoint():
                seen["calls"] = intake_mod.label_stats_calls
                seen["card"] = (root / ".popper-intake" / "data-card.json").exists()

            intake_mod.seal_checkpoints.append(checkpoint)
            try:
                intake(root, raw="table.csv", split_strategy="random", seed=3,
                       source_note="fixture", target_column="label", **_ratios())
            finally:
                intake_mod.seal_checkpoints.remove(checkpoint)
            self.assertEqual(seen["calls"], before)
            self.assertFalse(seen["card"])
            self.assertEqual(intake_mod.label_stats_calls, before + 1)
            log = IntakeLog(root / ".popper-intake" / "intake.sqlite")
            try:
                events = log.events()
            finally:
                log.close()
            kinds = [item["event_type"] for item in events]
            self.assertEqual(kinds[:3], ["dataset_hashed", "holdout_sealed", "data_card_written"])
            self.assertLess(events[0]["seq"], events[1]["seq"])
            self.assertLess(events[1]["seq"], events[2]["seq"])
            card = read_json(root / ".popper-intake" / "data-card.json")
            self.assertEqual(card["label_stats"]["scope"], "train_dev")
            self.assertNotIn("distribution", card["holdout"])

    def test_unknown_recipe_op_is_rejected(self):
        with TemporaryDirectory() as tmp:
            root = self._seed(tmp)
            recipe = root / "bad.json"
            _write(recipe, json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [{"op": "pca", "column": "income"}],
            }))
            with self.assertRaises(ProtocolError) as caught:
                propose_recipe(root, recipe)
            self.assertEqual(str(caught.exception), "配方含词汇表外操作: pca")

    def test_median_fit_ignores_dev_and_holdout(self):
        recipe = {
            "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
            "steps": [{"op": "impute_median", "column": "income"}],
        }
        train = [
            {"id": "t1", "values": {"income": 1, "label": 0}},
            {"id": "t2", "values": {"income": 3, "label": 1}},
            {"id": "t3", "values": {"income": None, "label": 0}},
        ]
        dev = [{"id": "d1", "values": {"income": None, "label": 1}}]
        holdout = [{"id": "h1", "values": {"income": None, "label": 0}}]
        types = {"income": "number", "label": "integer"}
        fit, _train, dev_out, holdout_out = run_recipe(recipe, train, dev, holdout, types)
        dev[0]["values"]["income"] = 100
        holdout[0]["values"]["income"] = -1000
        refit, _train2, _dev2, _hold2 = run_recipe(recipe, train, dev, holdout, types)
        self.assertEqual(fit["steps"][0]["p50"], 2)
        self.assertEqual(refit["steps"][0]["p50"], fit["steps"][0]["p50"])
        self.assertEqual(dev_out[0]["values"]["income"], 2)
        self.assertEqual(holdout_out[0]["values"]["income"], 2)

    def test_holdout_rows_stay_fixed_and_resplit_is_rejected(self):
        with TemporaryDirectory() as tmp:
            root = self._seed(tmp)
            seal = read_json(root / ".popper-intake" / "holdout-seal.json")
            before_ids = list(seal["holdout_ids"])
            before_labels = [row["values"]["label"] for row in seal["holdout_rows"]]
            before_set = seal["row_set_sha256"]
            before_hash = file_hash(root / ".popper-intake" / "holdout-seal.json")
            recipe = root / "recipe.json"
            _write(recipe, json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [
                    {"op": "impute_median", "column": "income"},
                    {"op": "one_hot", "column": "city"},
                    {"op": "select_columns", "columns": ["age", "income"]},
                ],
            }, ensure_ascii=False))
            proposed = propose_recipe(root, recipe)
            approved = approve_recipe(root, proposed["proposal_id"])
            compiled = compile_recipe(root, approved["proposal_id"], "binary-accuracy-v1",
                                      executor=run_workspace)
            seal_after = read_json(root / ".popper-intake" / "holdout-seal.json")
            self.assertEqual(seal_after["holdout_ids"], before_ids)
            self.assertEqual([row["values"]["label"] for row in seal_after["holdout_rows"]],
                             before_labels)
            self.assertEqual(seal_after["row_set_sha256"], before_set)
            self.assertEqual(compiled["holdout_row_set_sha256"], before_set)
            self.assertEqual(file_hash(root / ".popper-intake" / "holdout-seal.json"), before_hash)
            holdout = read_json(root / ".popper-intake" / "compiled" / proposed["recipe_sha256"]
                                / "holdout.json")
            self.assertEqual([row["id"] for row in holdout], before_ids)
            self.assertEqual([row["label"] for row in holdout], before_labels)
            pack = evaluator_pack("binary-accuracy-v1")
            sets = [read_json(root / ".popper-intake" / "compiled" / proposed["recipe_sha256"] / name)
                    for name in ("train.json", "dev.json", "holdout.json")]
            for split, rows in zip(("train", "dev", "test"), sets):
                pack.validate_rows(rows, split)
            pack.validate_splits(sets)
            log = IntakeLog(root / ".popper-intake" / "intake.sqlite")
            try:
                self.assertTrue(all(item["kind"] == "dataset" for item in log.events()))
                self.assertEqual(log.count("holdout_sealed"), 1)
            finally:
                log.close()
            with self.assertRaises(ProtocolError) as resplit:
                intake(root, raw="table.csv", split_strategy="random", seed=99,
                       source_note="fixture", target_column="label", **_ratios())
            self.assertEqual(str(resplit.exception), "这份研究已经封存确认集；重新切分必须新建 Study")
            log = IntakeLog(root / ".popper-intake" / "intake.sqlite")
            try:
                self.assertEqual(log.count("holdout_sealed"), 1)
            finally:
                log.close()

    def test_group_split_keeps_each_group_intact(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = ["family,age,label"]
            for index, family in enumerate(("f1", "f1", "f1", "f2", "f2", "f2", "f3", "f3", "f3")):
                lines.append(f"{family},{index},{index % 2}")
            _write(root / "groups.csv", "\n".join(lines) + "\n")
            intake(root, raw="groups.csv", split_strategy="group", seed=1, column="family",
                   source_note="fixture", target_column="label",
                   train_ratio="0.333333", dev_ratio="0.333333", holdout_ratio="0.333334")
            seal = read_json(root / ".popper-intake" / "holdout-seal.json")
            families = {f"r{index:06d}": family for index, family in enumerate(
                ("f1", "f1", "f1", "f2", "f2", "f2", "f3", "f3", "f3"))}
            seen = {}
            for name in ("train_ids", "dev_ids", "holdout_ids"):
                for row_id in seal[name]:
                    seen.setdefault(families[row_id], set()).add(name)
            self.assertTrue(all(len(places) == 1 for places in seen.values()))
            self.assertEqual(set(seen), {"f1", "f2", "f3"})

    def test_json_and_fasta_parse_without_becoming_csv(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "rows.json", json.dumps([
                {"age": 1, "city": "a", "label": 0},
                {"age": None, "city": "b", "label": 1},
                {"age": 3, "city": "a", "label": 0},
            ]))
            intake(root, raw="rows.json", split_strategy="random", seed=1,
                   source_note="json fixture", target_column="label",
                   train_ratio="0.333334", dev_ratio="0.333333", holdout_ratio="0.333333")
            card = read_json(root / ".popper-intake" / "data-card.json")
            self.assertEqual(card["schema"], "popper.data-card/v1")
            fasta = root / "sample.fasta"
            _write(fasta, ">s1\nACDEFGHIKLMN\n>s2\nACDEFGHIKLMN\n>s3\nACDEFGHIKLMN\n")
            other = Path(tmp) / "fasta-proj"
            other.mkdir()
            fasta_dest = other / "sample.fasta"
            fasta_dest.write_text(fasta.read_text(encoding="utf-8"), encoding="utf-8")
            intake(other, raw="sample.fasta", split_strategy="random", seed=1,
                   source_note="fasta fixture",
                   train_ratio="0.333334", dev_ratio="0.333333", holdout_ratio="0.333333")
            fasta_card = read_json(other / ".popper-intake" / "data-card.json")
            sequence = next(item for item in fasta_card["columns"] if item["name"] == "sequence")
            self.assertEqual(sequence["type"], "sequence")
            self.assertEqual(sequence["sequence"]["alphabet"], "protein")
            self.assertIsNone(fasta_card["label_stats"])

    def test_import_into_research_store_rejects_unknown_event_types(self):
        with TemporaryDirectory() as tmp:
            root = self._seed(tmp)
            store = ResearchStore(Path(tmp) / "run" / "research.sqlite")
            try:
                import_intake_events(store, root)
                store.create_study(Study("S-test", "F-test", "问题", "范围", budget_cap=4))
                report = store.verify()
                self.assertTrue(report["ok"], report.get("reason"))
                kinds = [row[0] for row in store._unsafe_conn.execute("SELECT kind FROM events")]
                self.assertIn("dataset", kinds)
                self.assertNotIn("observation", kinds)
                self.assertNotIn("claim", kinds)
            finally:
                store.close()
            fresh = ResearchStore(Path(tmp) / "other" / "research.sqlite")
            try:
                def _bad():
                    fresh._append("dataset", "entity", "not_a_dataset_event",
                                  {"raw_sha256": "x"}, "research_store")
                fresh._transaction(_bad)
                report = fresh.verify()
                self.assertFalse(report["ok"])
                self.assertIn("未知事件类型", report["reason"])
            finally:
                fresh.close()

    def test_runner_limits_match_intake_constants(self):
        self.assertEqual(RUNNER_COLUMNS, MAX_COLUMNS)
        self.assertEqual(RUNNER_K, MAX_KMER_K)

    def test_fixture_cases_and_seal_order(self):
        with TemporaryDirectory() as tmp:
            root = self._copy_case(tmp, "csv-ok")
            result = intake(root, raw="table.csv", split_strategy="random", seed=1,
                            source_note="csv-ok", target_column="label", **_ratios())
            events = self._events(root)
            kinds = [item["event_type"] for item in events]
            hashed, sealed, card = (kinds.index(name) for name in (
                "dataset_hashed", "holdout_sealed", "data_card_written"))
            self.assertLess(events[hashed]["seq"], events[sealed]["seq"])
            self.assertLess(events[sealed]["seq"], events[card]["seq"])
            self.assertEqual(result["holdout_sealed_seq"], events[sealed]["seq"])
            seal_before = file_hash(root / ".popper-intake" / "holdout-seal.json")
            proposed = propose_recipe(root, root / "recipe.json")
            approved = approve_recipe(root, proposed["proposal_id"])
            compile_recipe(root, approved["proposal_id"], "binary-accuracy-v1",
                           executor=run_workspace)
            self.assertEqual(file_hash(root / ".popper-intake" / "holdout-seal.json"), seal_before)
            self.assertEqual(sum(item["event_type"] == "holdout_sealed" for item in self._events(root)), 1)

    def test_remaining_fixture_rejections(self):
        cases = {
            "csv-no-header": ("table.csv", "CSV 缺少表头"),
            "csv-empty": ("empty.csv", "原始文件为空"),
            "csv-unknown-column": ("mixed.csv", "列无法推断类型: note"),
            "parquet-missing-extra": ("table.parquet", "当前环境不能读取 Parquet"),
            "json-not-array": ("object.json", "JSON 顶层必须是对象数组"),
            "fasta-empty-record": ("empty.fasta", "序列文件含空序列"),
        }
        for case, (name, sentence) in cases.items():
            with TemporaryDirectory() as tmp:
                root = self._copy_case(tmp, case)
                with self.assertRaises(ProtocolError) as caught:
                    intake(root, raw=name, split_strategy="random", seed=1,
                           source_note="fixture", **_ratios())
                self.assertEqual(str(caught.exception), sentence, case)
                self.assertFalse((root / ".popper-intake" / "holdout-seal.json").exists())
        with TemporaryDirectory() as tmp:
            root = self._copy_case(tmp, "csv-over-rows")
            text = (root / "rows.csv").read_text(encoding="utf-8")
            data_rows = len([line for line in text.splitlines() if line]) - 1
            original = intake_mod.MAX_ROWS
            intake_mod.MAX_ROWS = data_rows - 1
            try:
                with self.assertRaises(ProtocolError) as caught:
                    intake(root, raw="rows.csv", split_strategy="random", seed=1,
                           source_note="fixture", target_column="label", **_ratios())
                self.assertEqual(str(caught.exception), f"行数超过 {intake_mod.MAX_ROWS}")
                self.assertNotEqual(intake_mod.MAX_ROWS, MAX_ROWS)
            finally:
                intake_mod.MAX_ROWS = original

    def _copy_case(self, tmp, case):
        root = Path(tmp)
        for path in (FIXTURES / case).iterdir():
            if path.is_file():
                (root / path.name).write_bytes(path.read_bytes())
        return root

    def _events(self, root):
        log = IntakeLog(root / ".popper-intake" / "intake.sqlite")
        try:
            return log.events()
        finally:
            log.close()

    def test_new_recipe_ops_fit_only_on_train_and_reject_target_leak(self):
        recipe = {
            "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
            "steps": [
                {"op": "ordinal", "column": "band", "categories": ["low", "high"]},
                {"op": "select_missing_rate", "max_missing_rate": 0.5},
                {"op": "select_variance", "min_variance": 0},
            ],
        }
        train = [
            {"id": "t1", "values": {"band": "low", "x": 1, "quiet": 4, "note": "a",
                                    "hole": None, "label": 0}},
            {"id": "t2", "values": {"band": "high", "x": 3, "quiet": 4, "note": None,
                                    "hole": None, "label": 1}},
        ]
        dev = [{"id": "d1", "values": {"band": "low", "x": 9, "quiet": 4, "note": None,
                                       "hole": None, "label": 0}}]
        holdout = [{"id": "h1", "values": {"band": "high", "x": 8, "quiet": 100, "note": "z",
                                           "hole": 5, "label": 1}}]
        types = {"band": "categorical", "x": "integer", "quiet": "integer", "note": "categorical",
                 "hole": "integer", "label": "integer"}
        fit, train_out, _dev_out, holdout_out = run_recipe(recipe, train, dev, holdout, types)
        holdout[0]["values"]["x"] = 0
        holdout[0]["values"]["hole"] = 0
        holdout[0]["values"]["note"] = None
        refit, _, _, _ = run_recipe(recipe, train, dev, holdout, types)
        self.assertEqual(fit["steps"][0]["op"], "select_missing_rate")
        self.assertEqual(fit["steps"][0]["columns"], refit["steps"][0]["columns"])
        self.assertEqual(fit["steps"][1]["columns"], refit["steps"][1]["columns"])
        self.assertNotIn("label", fit["steps"][0]["columns"])
        self.assertNotIn("hole", fit["steps"][0]["columns"])
        self.assertNotIn("quiet", fit["steps"][1]["columns"])
        self.assertEqual(train_out[0]["values"]["band"], 0)
        self.assertEqual(holdout_out[0]["values"]["band"], 1)
        self.assertNotIn("hole", holdout_out[0]["values"])
        self.assertEqual([row["id"] for row in holdout_out], ["h1"])
        narrowed = json.loads(json.dumps(recipe))
        narrowed["steps"][0]["categories"] = ["low"]
        with self.assertRaises(RuntimeError) as unseen:
            run_recipe(narrowed, train, dev, holdout, types)
        self.assertEqual(str(unseen.exception), "序数编码遇到未声明的取值: band")
        leaked = json.loads(json.dumps(recipe))
        leaked["steps"][0]["column"] = "label"
        with self.assertRaises(RuntimeError) as target:
            run_recipe(leaked, train, dev, holdout, types)
        self.assertEqual(str(target.exception), "目标列禁止该变换: ordinal")
        missing = [
            {"id": "t1", "values": {"x": None, "label": 0}},
            {"id": "t2", "values": {"x": 1, "label": 1}},
        ]
        with self.assertRaises(RuntimeError) as variance:
            run_recipe({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [{"op": "select_variance", "min_variance": 0}],
            }, missing, missing[:1], missing[:1], {"x": "number", "label": "integer"})
        self.assertEqual(str(variance.exception), "方差选择要求列已无缺失")

    def test_sequence_ops_use_train_vocabulary_only(self):
        train = [{"id": "t1", "values": {"seq": "ACACACACACAC", "label": 0}}]
        dev = [{"id": "d1", "values": {"seq": "GGGGGGGGGGGG", "label": 1}}]
        holdout = [{"id": "h1", "values": {"seq": "TTTTTTTTTTTT", "label": 0}}]
        types = {"seq": "sequence", "label": "integer"}

        def once(steps):
            return run_recipe({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": steps,
            }, train, dev, holdout, types)

        length_fit, length_out, _, _ = once([
            {"op": "length", "column": "seq", "output": "seq_len"}])
        self.assertEqual(length_fit["steps"], [])
        self.assertEqual(length_out[0]["values"]["seq_len"], 12)
        self.assertIn("seq", length_out[0]["values"])
        kmer_fit, kmer_out, _, kmer_hold = once([{"op": "kmer", "column": "seq", "k": 2}])
        self.assertEqual(kmer_fit["steps"][0]["vocabulary"], ["AC", "CA"])
        self.assertNotIn("seq", kmer_out[0]["values"])
        self.assertGreater(kmer_hold[0]["values"]["seq____other__"], 0)
        comp_fit, comp_out, _, comp_hold = once([{"op": "composition", "column": "seq"}])
        self.assertEqual(comp_fit["steps"][0]["letters"], ["A", "C"])
        self.assertIn("seq", comp_out[0]["values"])
        self.assertGreater(comp_hold[0]["values"]["seq____other__"], 0)
        dev[0]["values"]["seq"] = "AAAAAAAAAAAAAAAA"
        refit, _, _, _ = once([{"op": "kmer", "column": "seq", "k": 2}])
        self.assertEqual(refit["steps"][0]["vocabulary"], ["AC", "CA"])
        wide = [{"id": "t1", "values": {**{f"c{i}": 1 for i in range(127)},
                                        "seq": "ACGTACGTACGT", "label": 0}}]
        wide_types = {f"c{i}": "integer" for i in range(127)}
        wide_types.update(types)
        with self.assertRaises(RuntimeError) as overflow:
            run_recipe({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [{"op": "kmer", "column": "seq", "k": 1}],
            }, wide, wide, wide, wide_types)
        self.assertEqual(str(overflow.exception), "k-mer 展开后列数超过上限")
        with TemporaryDirectory() as tmp:
            root = self._seed(tmp)
            _write(root / "k.json", json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [{"op": "kmer", "column": "city", "k": 7}],
            }))
            with self.assertRaises(ProtocolError) as k_range:
                propose_recipe(root, root / "k.json")
            self.assertEqual(str(k_range.exception), f"k-mer 的 k 必须是 1 到 {MAX_KMER_K} 的整数")
            _write(root / "target.json", json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [{"op": "one_hot", "column": "label"}],
            }))
            with self.assertRaises(ProtocolError) as target:
                propose_recipe(root, root / "target.json")
            self.assertEqual(str(target.exception), "目标列禁止该变换: one_hot")
            _write(root / "extra.json", json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [{"op": "composition", "column": "city", "output": "nope"}],
            }))
            with self.assertRaises(ProtocolError) as fields:
                propose_recipe(root, root / "extra.json")
            self.assertEqual(str(fields.exception), "配方步骤含未声明字段")

    def test_one_hot_levels_ignore_dev_and_holdout(self):
        recipe = {
            "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
            "steps": [{"op": "one_hot", "column": "city"}],
        }
        train = [
            {"id": "t1", "values": {"city": "a", "label": 0}},
            {"id": "t2", "values": {"city": "b", "label": 1}},
        ]
        dev = [{"id": "d1", "values": {"city": "zzz", "label": 0}}]
        holdout = [{"id": "h1", "values": {"city": "qqq", "label": 1}}]
        types = {"city": "categorical", "label": "integer"}
        fit, _, dev_out, holdout_out = run_recipe(recipe, train, dev, holdout, types)
        self.assertEqual(fit["steps"][0]["levels"], ["a", "b"])
        self.assertEqual(dev_out[0]["values"]["city____other__"], 1)
        self.assertEqual(holdout_out[0]["values"]["city____other__"], 1)
        dev[0]["values"]["city"] = "mmm"
        refit, _, _, _ = run_recipe(recipe, train, dev, holdout, types)
        self.assertEqual(refit["steps"][0]["levels"], fit["steps"][0]["levels"])

    def test_unapproved_adapter_is_rejected_and_second_batch_is_not_parsed(self):
        import io
        from contextlib import redirect_stdout
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "stub.txt", "not a parser (\n")
            _write(root / "notes.fx", "FX1\nnot,a,table\n")
            _write(root / "mol.cif", "CIF\nstub\n")
            with redirect_stdout(io.StringIO()):
                code = main(["research", "adapter", "register", str(root),
                             "--id", "fixturefx", "--suffix", ".fx", "--magic", "FX1",
                             "--record-shape", "table", "--implementation", "stub.txt"])
            self.assertEqual(code, 0)
            with self.assertRaises(ProtocolError) as pending:
                intake(root, raw="notes.fx", split_strategy="random", seed=1,
                       source_note="fixture", **_ratios())
            self.assertEqual(str(pending.exception), "适配器尚未批准")
            self.assertFalse((root / ".popper-intake" / "holdout-seal.json").exists())
            with redirect_stdout(io.StringIO()):
                code = main(["research", "adapter", "approve", str(root),
                             "--id", "fixturefx", "--approver", "person"])
            self.assertEqual(code, 0)
            with self.assertRaises(ProtocolError) as unread:
                intake(root, raw="notes.fx", split_strategy="random", seed=1,
                       source_note="fixture", **_ratios())
            self.assertEqual(str(unread.exception), "适配器未实现，不能读取")
            self.assertFalse((root / ".popper-intake" / "holdout-seal.json").exists())
            with self.assertRaises(ProtocolError) as unknown:
                intake(root, raw="mol.cif", split_strategy="random", seed=1,
                       source_note="fixture", **_ratios())
            self.assertEqual(str(unknown.exception), "未知扩展名，不能猜测格式")
            register_adapter(root, adapter_id="cif", suffixes=[".cif"], magic="CIF",
                             record_shape="structure", implementation="stub.txt")
            approve_adapter(root, adapter_id="cif", approver="person")
            with self.assertRaises(ProtocolError) as cif:
                intake(root, raw="mol.cif", split_strategy="random", seed=1,
                       source_note="fixture", **_ratios())
            self.assertEqual(str(cif.exception), "适配器未实现，不能读取")
            self.assertEqual({item["id"] for item in adapters()}, {"csv", "parquet", "json", "fasta"})
            _write(root / "table.csv", "age,label\n1,0\n2,1\n3,0\n")
            intake(root, raw="table.csv", split_strategy="random", seed=1,
                   source_note="after adapter", target_column="label",
                   train_ratio="0.333334", dev_ratio="0.333333", holdout_ratio="0.333333")
            kinds = [item["event_type"] for item in self._events(root)]
            self.assertLess(kinds.index("dataset_hashed"), kinds.index("holdout_sealed"))
            self.assertLess(kinds.index("holdout_sealed"), kinds.index("data_card_written"))
            self.assertEqual(kinds.count("holdout_sealed"), 1)
            store = ResearchStore(root / "run" / "research.sqlite")
            try:
                import_intake_events(store, root)
                report = store.verify()
                self.assertTrue(report["ok"], report.get("reason"))
                copied = [row[0] for row in store._unsafe_conn.execute(
                    "SELECT event_type FROM events")]
                self.assertIn("adapter_approved", copied)
                kinds_copied = [row[0] for row in store._unsafe_conn.execute(
                    "SELECT kind FROM events")]
                self.assertNotIn("observation", kinds_copied)
                self.assertNotIn("claim", kinds_copied)
            finally:
                store.close()

    def test_frozen_study_cannot_compile(self):
        with TemporaryDirectory() as tmp:
            root = self._seed(tmp)
            recipe = root / "recipe.json"
            _write(recipe, json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [
                    {"op": "impute_median", "column": "income"},
                    {"op": "ordinal", "column": "city", "categories": ["a", "b"]},
                ],
            }))
            proposed = propose_recipe(root, recipe)
            approved = approve_recipe(root, proposed["proposal_id"])
            before = file_hash(root / ".popper-intake" / "holdout-seal.json")
            compiled = compile_recipe(root, approved["proposal_id"], "binary-accuracy-v1",
                                      executor=self._workspace_guard(root))
            identity_path = (root / ".popper-intake" / "compiled" / proposed["recipe_sha256"]
                             / "input-identity.json")
            identity = read_json(identity_path)
            self.assertEqual(digest(identity), compiled["input_identity_sha256"])
            train_path = identity_path.with_name("train.json")
            raw = bytearray(train_path.read_bytes())
            raw[-1] ^= 1
            train_path.write_bytes(raw)
            changed = dict(identity)
            changed["compiled_sha256"] = dict(identity["compiled_sha256"])
            changed["compiled_sha256"]["train"] = file_hash(train_path)
            self.assertNotEqual(digest(changed), compiled["input_identity_sha256"])
            self.assertEqual(read_json(root / ".popper-intake" / "holdout-seal.json")["row_set_sha256"],
                             compiled["holdout_row_set_sha256"])
            home = root / ".popper"
            home.mkdir()
            db = sqlite3.connect(home / "state.db")
            db.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("INSERT INTO state VALUES (1, ?)", (canonical({"phase": "frozen"}),))
            db.commit()
            db.close()
            with self.assertRaises(ProtocolError) as frozen:
                compile_recipe(root, approved["proposal_id"], "binary-accuracy-v1",
                               executor=run_workspace)
            self.assertEqual(str(frozen.exception), "协议已冻结，不能再编译新配方")
            self.assertEqual(file_hash(root / ".popper-intake" / "holdout-seal.json"), before)
            self.assertEqual(sum(item["event_type"] == "recipe_compiled"
                                 for item in self._events(root)), 1)

    def test_unbudgeted_compiles_count_toward_study_budget_floor(self):
        with TemporaryDirectory() as tmp:
            root = self._seed(tmp)
            recipe = root / "recipe.json"
            _write(recipe, json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [
                    {"op": "impute_median", "column": "income"},
                    {"op": "ordinal", "column": "city", "categories": ["a", "b"]},
                ],
            }))
            proposed = propose_recipe(root, recipe)
            approved = approve_recipe(root, proposed["proposal_id"])
            compile_recipe(root, approved["proposal_id"], "binary-accuracy-v1",
                           executor=run_workspace)
            first = [item for item in self._events(root) if item["event_type"] == "recipe_compiled"]
            self.assertEqual(first[0]["payload"]["budget_reserved"], False)
            _write(root / "model.py", "def main():\n    return None\n")
            write_json(root / "train.json", [{"id": "a", "x": 1, "y": 1}])
            write_json(root / "dev.json", [{"id": "b", "x": 2, "y": 2}])
            write_json(root / "test.json", [{"id": "c", "x": 3, "y": 3}])
            write_json(root / "experiment.json", {
                "name": "budget-floor",
                "objective": "未入账编译计入 Study 预算下限",
                "entrypoint": "model.py", "code_files": ["model.py"],
                "train": "train.json", "dev": "dev.json", "test": "test.json",
                "baseline": {"degree": 1}, "candidates": [{"degree": 0}],
                "metric": {"name": "mse", "direction": "min"}, "seeds": [1],
                "budget": 1, "timeout_seconds": 20, "min_improvement": 0.1,
            })
            initialize(root)
            run = root / "study-run"
            with self.assertRaises(ProtocolError) as low:
                ResearchController.initialize(root, run, budget_cap=4)
            self.assertEqual(str(low.exception), "未入账编译次数超过 Study 预算下限")
            ResearchController.initialize(root, run, budget_cap=5)
            compile_recipe(root, approved["proposal_id"], "binary-accuracy-v1",
                           executor=run_workspace)
            compiled = [item for item in self._events(root)
                        if item["event_type"] == "recipe_compiled"]
            self.assertEqual(len(compiled), 2)
            self.assertEqual(compiled[1]["payload"]["budget_reserved"], True)
            manifest = read_json(run / "research.json")
            store = ResearchStore(run / "research.sqlite")
            try:
                balance = store.budget(manifest["study_id"]).balance(manifest["family_id"])
            finally:
                store.close()
            self.assertEqual(balance["reserved"], 1.0)

    def test_followup_holdout_rejects_parent_split_overlap(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / "parent"
            parent.mkdir()
            _write(parent / "model.py", "def main():\n    return None\n")
            write_json(parent / "train.json", [{"id": "p-train", "x": 1, "y": 2}])
            write_json(parent / "dev.json", [{"id": "p-dev", "x": 3, "y": 4}])
            write_json(parent / "test.json", [{"id": "p-test", "x": 5, "y": 6}])
            write_json(parent / "experiment.json", {
                "name": "parent",
                "objective": "父研究划分",
                "entrypoint": "model.py", "code_files": ["model.py"],
                "train": "train.json", "dev": "dev.json", "test": "test.json",
                "baseline": {"degree": 1}, "candidates": [{"degree": 0}],
                "metric": {"name": "mse", "direction": "min"}, "seeds": [1],
                "budget": 1, "timeout_seconds": 20, "min_improvement": 0.1,
            })
            initialize(parent)
            run = base / "parent-run"
            ResearchController.initialize(parent, run, budget_cap=4)
            child = base / "child"
            child.mkdir()
            link_parent_run(child, run)
            ratios = {"train_ratio": "0.333334", "dev_ratio": "0.333333",
                      "holdout_ratio": "0.333333"}

            def _lists(train, dev, holdout):
                write_json(child / "lists.json",
                           {"train": [train], "dev": [dev], "holdout": [holdout]})

            def _seal(text):
                _write(child / "rows.csv", text)
                return intake(child, raw="rows.csv", split_strategy="id_list", seed=1,
                              id_column="id", id_lists="lists.json", source_note="followup",
                              target_column="y", **ratios)

            before = intake_mod.label_stats_calls
            _lists("fresh-d", "fresh-h", "p-train")
            with self.assertRaises(ProtocolError) as ident:
                _seal("id,x,y\np-train,7,8\nfresh-d,9,10\nfresh-h,11,12\n")
            self.assertEqual(str(ident.exception), "新确认集与父实验数据存在样本重叠")
            self.assertFalse((child / ".popper-intake" / "holdout-seal.json").exists())
            self.assertEqual(intake_mod.label_stats_calls, before)
            _lists("fresh-t", "fresh-d", "fresh-h")
            with self.assertRaises(ProtocolError) as content:
                _seal("id,x,y\nfresh-t,7,8\nfresh-d,9,10\nfresh-h,1,2\n")
            self.assertEqual(str(content.exception), "新确认集与父实验数据存在样本重叠")
            self.assertEqual(intake_mod.label_stats_calls, before)
            seen = {}

            def checkpoint():
                seen["calls"] = intake_mod.label_stats_calls
                seen["card"] = (child / ".popper-intake" / "data-card.json").exists()

            intake_mod.seal_checkpoints.append(checkpoint)
            try:
                _seal("id,x,y\nfresh-t,7,8\nfresh-d,9,10\nfresh-h,11,12\n")
            finally:
                intake_mod.seal_checkpoints.remove(checkpoint)
            self.assertEqual(seen["calls"], before)
            self.assertFalse(seen["card"])
            kinds = [item["event_type"] for item in self._events(child)]
            self.assertLess(kinds.index("dataset_hashed"), kinds.index("holdout_sealed"))
            self.assertLess(kinds.index("holdout_sealed"), kinds.index("data_card_written"))
            self.assertEqual(kinds.count("holdout_sealed"), 1)

    def test_feature_drop_rows_leave_workspace_before_runner(self):
        ratios = {"train_ratio": "0.500000", "dev_ratio": "0.333333", "holdout_ratio": "0.166667"}
        table = ("age,x,y,label\n"
                 "1,1.0,10,0\n2,,20,1\n3,3.0,30,0\n"
                 "4,4.0,40,1\n5,,50,0\n6,6.0,60,1\n")
        missing = {"r000001", "r000004"}

        def _seal(root, seed):
            _write(root / "rows.csv", table)
            return intake(root, raw="rows.csv", split_strategy="random", seed=seed,
                          source_note="fixture", target_column="label", **ratios)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seal(root, 2)
            seal = read_json(root / ".popper-intake" / "holdout-seal.json")
            sealed_holdout = list(seal["holdout_ids"])
            self.assertFalse(set(sealed_holdout) & missing)
            expected_train = [row_id for row_id in seal["train_ids"] if row_id not in missing]
            expected_dev = [row_id for row_id in seal["dev_ids"] if row_id not in missing]
            self.assertLess(len(expected_train), len(seal["train_ids"]))
            self.assertLess(len(expected_dev), len(seal["dev_ids"]))
            recipe = root / "recipe.json"
            _write(recipe, json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [{"op": "drop_rows", "column": "x", "predicate": "missing"}],
            }))
            proposed = propose_recipe(root, recipe)
            approved = approve_recipe(root, proposed["proposal_id"])
            compile_recipe(root, approved["proposal_id"], "binary-accuracy-v1",
                           executor=run_workspace)
            compiled_dir = root / ".popper-intake" / "compiled" / proposed["recipe_sha256"]
            train = read_json(compiled_dir / "train.json")
            dev = read_json(compiled_dir / "dev.json")
            holdout = read_json(compiled_dir / "holdout.json")
            self.assertEqual([row["id"] for row in train], expected_train)
            self.assertEqual([row["id"] for row in dev], expected_dev)
            self.assertEqual([row["id"] for row in holdout], sealed_holdout)
            workspace = root / ".popper-intake" / "workspaces" / proposed["recipe_sha256"]
            workspace_train = [json.loads(line)["id"] for line in
                               (workspace / "inputs" / "train.jsonl").read_text(encoding="utf-8").splitlines()]
            workspace_dev = [json.loads(line)["id"] for line in
                             (workspace / "inputs" / "dev.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(workspace_train, expected_train)
            self.assertEqual(workspace_dev, expected_dev)
            self.assertFalse(set(workspace_train + workspace_dev) & missing)
            handed = read_json(workspace / "inputs" / "recipe.json")
            self.assertFalse(any(step.get("op") == "drop_rows" for step in handed["steps"]))
            fit = read_json(compiled_dir / "recipe_fit.json")
            self.assertEqual(fit["dropped_train_ids"],
                             [row_id for row_id in seal["train_ids"] if row_id in missing])
            self.assertEqual(fit["dropped_dev_ids"],
                             [row_id for row_id in seal["dev_ids"] if row_id in missing])
            self.assertEqual(read_json(root / ".popper-intake" / "holdout-seal.json")["holdout_ids"],
                             sealed_holdout)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seal(root, 0)
            seal_path = root / ".popper-intake" / "holdout-seal.json"
            sealed_holdout = list(read_json(seal_path)["holdout_ids"])
            self.assertTrue(set(sealed_holdout) & missing)
            before = file_hash(seal_path)
            recipe = root / "recipe.json"
            _write(recipe, json.dumps({
                "schema": "popper.recipe/v1", "target_column": "label", "row_atom": "row",
                "steps": [{"op": "drop_rows", "column": "x", "predicate": "missing"}],
            }))
            proposed = propose_recipe(root, recipe)
            approved = approve_recipe(root, proposed["proposal_id"])
            with self.assertRaises(ProtocolError) as rejected:
                compile_recipe(root, approved["proposal_id"], "binary-accuracy-v1",
                               executor=run_workspace)
            self.assertEqual(str(rejected.exception), "确认集行集合不可变")
            self.assertEqual(read_json(seal_path)["holdout_ids"], sealed_holdout)
            self.assertEqual(file_hash(seal_path), before)
            self.assertFalse((root / ".popper-intake" / "compiled" / proposed["recipe_sha256"]).exists())

    def _workspace_guard(self, root):
        seal = read_json(root / ".popper-intake" / "holdout-seal.json")
        holdout_ids = list(seal["holdout_ids"])

        def execute(workspace):
            files = {path.relative_to(workspace).as_posix()
                     for path in workspace.rglob("*") if path.is_file()}
            self.assertEqual(files, {
                "code/recipe_runner.py", "inputs/recipe.json", "inputs/train.jsonl",
                "inputs/dev.jsonl", "inputs/columns.json",
            })
            blob = "".join(path.read_text(encoding="utf-8")
                           for path in (workspace / "inputs").iterdir())
            for row_id in holdout_ids:
                self.assertNotIn(row_id, blob)
            columns = read_json(workspace / "inputs" / "columns.json")
            self.assertNotIn("label", {item["name"] for item in columns["columns"]})
            for name in ("train.jsonl", "dev.jsonl"):
                for line in (workspace / "inputs" / name).read_text(encoding="utf-8").splitlines():
                    self.assertNotIn("label", json.loads(line)["values"])
            run_workspace(workspace)

        return execute

    def _seed(self, tmp):
        root = Path(tmp)
        _write(root / "table.csv", "\ufeffage,income,city,label\n"
               "20,1.5,a,0\n21,,b,1\n22,2.5,a,0\n30,3.0,b,1\n"
               "31,4.0,a,0\n32,,b,1\n40,1.0,a,0\n41,2.0,b,1\n42,3.5,a,0\n")
        intake(root, raw="table.csv", split_strategy="random", seed=7,
               source_note="fixture", target_column="label", **_ratios())
        return root


if __name__ == "__main__":
    unittest.main()
