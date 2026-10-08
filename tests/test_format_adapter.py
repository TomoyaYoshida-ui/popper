"""已批准且绑定解析器的适配器可以画像并封存。不实现第二批科学格式。"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from popper.core import ProtocolError, read_json
from popper.research import intake as intake_mod
from popper.research.intake import (IntakeLog, adapters, approve_adapter, bind_adapter_parser,
                                    intake, register_adapter, unbind_adapter_parser)


def _ratios():
    return {"train_ratio": "0.333334", "dev_ratio": "0.333333", "holdout_ratio": "0.333333"}


def _parse_tab(raw):
    lines = [line for line in raw.decode("utf-8").splitlines() if line]
    header = lines[1].split("|")
    return header, [dict(zip(header, line.split("|"))) for line in lines[2:]]


class FormatAdapterTests(unittest.TestCase):
    def test_approved_parser_seals_before_label_stats(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stub.txt").write_text("not a parser\n", encoding="utf-8")
            (root / "notes.tab").write_text(
                "TAB1\nage|label\n1|0\n2|1\n3|0\n", encoding="utf-8")
            (root / "mol.cif").write_text("CIF\nstub\n", encoding="utf-8")
            register_adapter(root, adapter_id="fixturetab", suffixes=[".tab"], magic="TAB1",
                             record_shape="table", implementation="stub.txt")
            with self.assertRaises(ProtocolError) as pending:
                intake(root, raw="notes.tab", split_strategy="random", seed=1,
                       source_note="fixture", target_column="label", **_ratios())
            self.assertEqual(str(pending.exception), "适配器尚未批准")
            self.assertFalse((root / ".popper-intake" / "holdout-seal.json").exists())
            approve_adapter(root, adapter_id="fixturetab", approver="person")
            with self.assertRaises(ProtocolError) as unread:
                intake(root, raw="notes.tab", split_strategy="random", seed=1,
                       source_note="fixture", target_column="label", **_ratios())
            self.assertEqual(str(unread.exception), "适配器未实现，不能读取")
            register_adapter(root, adapter_id="cif", suffixes=[".cif"], magic="CIF",
                             record_shape="structure", implementation="stub.txt")
            approve_adapter(root, adapter_id="cif", approver="person")
            with self.assertRaises(ProtocolError) as bound:
                bind_adapter_parser(root, "cif", _parse_tab)
            self.assertEqual(str(bound.exception), "适配器未实现，不能读取")
            with self.assertRaises(ProtocolError) as cif:
                intake(root, raw="mol.cif", split_strategy="random", seed=1,
                       source_note="fixture", **_ratios())
            self.assertEqual(str(cif.exception), "适配器未实现，不能读取")
            (root / "notes.xyz").write_text("TAB1\nage|label\n1|0\n2|1\n3|0\n", encoding="utf-8")
            with self.assertRaises(ProtocolError) as unknown:
                intake(root, raw="notes.xyz", split_strategy="random", seed=1,
                       source_note="fixture", **_ratios())
            self.assertEqual(str(unknown.exception), "未知扩展名，不能猜测格式")
            before = intake_mod.label_stats_calls
            seen = {}

            def checkpoint():
                seen["calls"] = intake_mod.label_stats_calls
                seen["card"] = (root / ".popper-intake" / "data-card.json").exists()

            bind_adapter_parser(root, "fixturetab", _parse_tab)
            intake_mod.seal_checkpoints.append(checkpoint)
            try:
                intake(root, raw="notes.tab", split_strategy="random", seed=1,
                       source_note="fixture", target_column="label", **_ratios())
            finally:
                intake_mod.seal_checkpoints.remove(checkpoint)
                unbind_adapter_parser(root, "fixturetab")
            self.assertEqual(seen["calls"], before)
            self.assertFalse(seen["card"])
            self.assertEqual(intake_mod.label_stats_calls, before + 1)
            log = IntakeLog(root / ".popper-intake" / "intake.sqlite")
            try:
                events = log.events()
            finally:
                log.close()
            kinds = [item["event_type"] for item in events]
            self.assertLess(kinds.index("dataset_hashed"), kinds.index("holdout_sealed"))
            self.assertLess(kinds.index("holdout_sealed"), kinds.index("data_card_written"))
            hashed = events[kinds.index("dataset_hashed")]
            sealed = events[kinds.index("holdout_sealed")]
            card_event = events[kinds.index("data_card_written")]
            self.assertLess(hashed["seq"], sealed["seq"])
            self.assertLess(sealed["seq"], card_event["seq"])
            self.assertNotIn("distribution", sealed["payload"])
            card = read_json(root / ".popper-intake" / "data-card.json")
            self.assertEqual(set(card["holdout"]), {"n_rows", "feature_types"})
            self.assertEqual(card["label_stats"]["scope"], "train_dev")
            self.assertTrue(all(item["kind"] == "dataset" for item in events))
            self.assertNotIn("observation", kinds)
            self.assertNotIn("claim", kinds)

    def test_discipline_names_are_not_an_entry_allowlist(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = "研究问题涉及材料学、生物分子学和天文学，学科名称不是拒绝条件"
            (root / "stub.txt").write_text("not a parser\n", encoding="utf-8")
            (root / "mol.cif").write_text("CIF\nstub\n", encoding="utf-8")
            (root / "cell.poscar").write_text("POSCAR stub\n", encoding="utf-8")
            (root / "fold.pdb").write_text("PDB stub\n", encoding="utf-8")
            (root / "mol.sdf").write_text("SDF stub\n", encoding="utf-8")
            (root / "reads.fastq").write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            for name in ("mol.cif", "cell.poscar", "fold.pdb", "mol.sdf", "reads.fastq"):
                with self.assertRaises(ProtocolError) as unknown:
                    intake(root, raw=name, split_strategy="random", seed=1,
                           source_note=note, **_ratios())
                self.assertEqual(str(unknown.exception), "未知扩展名，不能猜测格式")
            (root / "notes.tab").write_text("TAB1\nage|label\n1|0\n2|1\n3|0\n", encoding="utf-8")
            register_adapter(root, adapter_id="fixturetab", suffixes=[".tab"], magic="TAB1",
                             record_shape="table", implementation="stub.txt")
            with self.assertRaises(ProtocolError) as pending:
                intake(root, raw="notes.tab", split_strategy="random", seed=1,
                       source_note=note, target_column="label", **_ratios())
            self.assertEqual(str(pending.exception), "适配器尚未批准")
            approve_adapter(root, adapter_id="fixturetab", approver="person")
            with self.assertRaises(ProtocolError) as unread:
                intake(root, raw="notes.tab", split_strategy="random", seed=1,
                       source_note=note, target_column="label", **_ratios())
            self.assertEqual(str(unread.exception), "适配器未实现，不能读取")
            self.assertFalse({item["id"] for item in adapters()}
                             & {"cif", "poscar", "pdb", "sdf", "fastq"})
            self.assertFalse((root / ".popper-intake" / "holdout-seal.json").exists())
            (root / "table.csv").write_text("age,label\n1,0\n2,1\n3,0\n", encoding="utf-8")
            intake(root, raw="table.csv", split_strategy="random", seed=1,
                   source_note=note, target_column="label", **_ratios())
            seal = read_json(root / ".popper-intake" / "holdout-seal.json")
            self.assertEqual(seal["split_declaration"]["source_note"], note)
            self.assertTrue(seal["holdout_ids"])


if __name__ == "__main__":
    unittest.main()
