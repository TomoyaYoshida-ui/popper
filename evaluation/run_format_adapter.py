"""格式适配器登记验收。证明批准并绑定解析器后可以画像和封存。

不调用模型。不实现 CIF、POSCAR、PDB、SDF、FASTQ。
"""
from __future__ import annotations

import json
from pathlib import Path

from popper.core import ProtocolError, write_json
from popper.research.intake import (
    IntakeLog, approve_adapter, bind_adapter_parser, intake, register_adapter,
    unbind_adapter_parser,
)
from popper.research.metrics import registry

REPO = Path(__file__).resolve().parents[1]
CASE = "fixture-tab"
PROJECT = REPO / "evaluation" / "data" / "format-adapter" / CASE
REPORT = REPO / "evaluation" / "runs" / "format-adapter" / CASE / "report.json"
NOTE = "研究问题涉及材料学与生物分子学，学科名称不是拒绝条件"


def _parse_tab(raw):
    lines = [line for line in raw.decode("utf-8").splitlines() if line]
    header = lines[1].split("|")
    return header, [dict(zip(header, line.split("|"))) for line in lines[2:]]


def _ratios():
    return {"train_ratio": "0.333334", "dev_ratio": "0.333333", "holdout_ratio": "0.333333"}


def main():
    if PROJECT.exists():
        import shutil
        shutil.rmtree(PROJECT)
    PROJECT.mkdir(parents=True)
    (PROJECT / "stub.txt").write_text("not a parser\n", encoding="utf-8")
    (PROJECT / "notes.tab").write_text("TAB1\nage|label\n1|0\n2|1\n3|0\n", encoding="utf-8")
    (PROJECT / "notes.xyz").write_text("TAB1\nage|label\n1|0\n2|1\n3|0\n", encoding="utf-8")
    register_adapter(PROJECT, adapter_id="fixturetab", suffixes=[".tab"], magic="TAB1",
                     record_shape="table", implementation="stub.txt")
    try:
        intake(PROJECT, raw="notes.tab", split_strategy="random", seed=1,
               source_note=NOTE, target_column="label", **_ratios())
    except ProtocolError as exc:
        if str(exc) != "适配器尚未批准":
            raise
    else:
        raise SystemExit("未批准的适配器没有被拒绝")
    approve_adapter(PROJECT, adapter_id="fixturetab", approver="person")
    try:
        intake(PROJECT, raw="notes.xyz", split_strategy="random", seed=1,
               source_note=NOTE, target_column="label", **_ratios())
    except ProtocolError as exc:
        unregistered = str(exc) == "未知扩展名，不能猜测格式"
    else:
        unregistered = False
    if not unregistered:
        raise SystemExit("未注册扩展名没有被拒绝")
    bind_adapter_parser(PROJECT, "fixturetab", _parse_tab)
    try:
        intake(PROJECT, raw="notes.tab", split_strategy="random", seed=1,
               source_note=NOTE, target_column="label", format="fixturetab", **_ratios())
    finally:
        unbind_adapter_parser(PROJECT, "fixturetab")
    log = IntakeLog(PROJECT / ".popper-intake" / "intake.sqlite")
    try:
        events = log.events()
    finally:
        log.close()
    kinds = [item["event_type"] for item in events]
    if "observation" in kinds or "claim" in kinds:
        raise SystemExit("适配器事件链里出现了 Observation 或 Claim")
    approved = [item for item in events if item["event_type"] == "adapter_approved"
                and item["payload"]["adapter_id"] == "fixturetab"]
    hashed = next(item for item in events if item["event_type"] == "dataset_hashed")
    sealed = next(item for item in events if item["event_type"] == "holdout_sealed")
    card_event = next(item for item in events if item["event_type"] == "data_card_written")
    card = json.loads((PROJECT / ".popper-intake" / "data-card.json").read_text(encoding="utf-8"))
    if "metric" in card or hashed["payload"]["format"] == "csv":
        raise SystemExit("适配器被降级成了 CSV，或数据卡里出现了指标")
    if not {"mse-v1", "mae-v1", "binary-accuracy-v1", "binary-f1-v1",
            "multiclass-macro-f1-v1"} <= set(registry()):
        raise SystemExit("指标登记表里没有五个表格指标")
    report = {
        "schema": "popper.format-adapter-report/v1",
        "case": CASE,
        "milestone": "format-adapter",
        "status": "passed",
        "model_called": False,
        "adapter_id": "fixturetab",
        "record_shape": approved[-1]["payload"]["record_shape"],
        "approved_once": len(approved) == 1,
        "profile_sha256": card_event["payload"]["data_card_sha256"],
        "seal_sha256": sealed["payload"]["seal_sha256"],
        "unregistered_rejected": True,
        "converted_to_csv": False,
        "discipline_allowlist": False,
        "metric_from_registry": "metric" not in card and card_event["seq"] > sealed["seq"],
    }
    if not all(report[key] is True for key in (
            "approved_once", "unregistered_rejected", "metric_from_registry")):
        raise SystemExit("格式适配器报告没有满足完成判据")
    if report["converted_to_csv"] or report["discipline_allowlist"] or report["model_called"]:
        raise SystemExit("格式适配器报告把未完成的事写成了完成")
    write_json(REPORT, report)
    print(f"写入 {REPORT}")


if __name__ == "__main__":
    main()
