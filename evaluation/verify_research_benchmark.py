"""Offline verification of frozen cohort identities, receipts and usage bounds."""
from __future__ import annotations

import argparse
from pathlib import Path

from popper.core import ProtocolError, digest, file_hash, read_json, write_json
from popper.research.confirmation_contracts import verify_envelope
from evaluation.run_research_benchmark import validate_protocol


def verify(out):
    out = Path(out)
    report = read_json(out / "report.json")
    validate_protocol(report["protocol"])
    planned = {r["id"] for r in report["protocol"]["cells"]}
    actual = [r["id"] for r in report["cells"]]
    if len(actual) != len(set(actual)) or set(actual) != planned or len(actual) != 54:
        raise ProtocolError("报告单元与冻结协议不一致")
    roster = read_json(out / "finalists.json")
    if digest(roster) != report["roster_sha256"]:
        raise ProtocolError("冻结候选清单已变")
    verified = 0
    for cell in report["cells"]:
        if cell["finalist"] != roster["finalists"][cell["id"]]:
            raise ProtocolError("报告候选与冻结清单不一致")
        if cell.get("confirmation_status") != "succeeded":
            continue
        receipt = read_json(out / "cells" / cell["id"] / "confirmation.json")
        payload = verify_envelope(receipt, report["public_key"])
        if (payload != cell["confirmation"] or payload["selection_sha256"] != digest(cell["finalist"])
                or payload["protocol_sha256"] != report["protocol"]["protocol_sha256"]
                or payload["roster_sha256"] != report["roster_sha256"]
                or payload["confirmation_consumed_once"] is not True):
            raise ProtocolError("确认回执身份与报告不一致")
        verified += 1
    requests = report["usage"]["attempts"]
    accounted = sum(r["reserved_tokens"] if r["used_tokens"] is None else r["used_tokens"] for r in requests)
    usage = report["usage"]["global"]
    if (len(requests) != usage["calls"] or accounted != usage["accounted_tokens"]
            or usage["calls"] > 600 or accounted > 3000000):
        raise ProtocolError("模型账本或上限不一致")
    if verified != report["confirmations_succeeded"]:
        raise ProtocolError("确认计数不一致")
    return {"ok": True, "planned_cells": 54, "verified_confirmations": verified,
            "calls": usage["calls"], "accounted_tokens": accounted,
            "known_used_tokens": sum(r["used_tokens"] or 0 for r in requests),
            "unknown_attempts": sum(r["status"] == "unknown" for r in requests),
            "report_sha256": file_hash(out / "report.json")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    print(verify(args.out))
