"""Offline release gate for the frozen Street/WDBC paper reproduction.

Copies only registered inputs and the pinned dataset into a fresh task. The
historical output directory is never reused, and the fixture cannot download
data silently because the expected input file is present and hash-checked.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from popper.core import ProtocolError
from popper.reproduction import (ReproductionTask, initialize_reproduction,
                                 inspect_reproduction, reproduction_research_projection)


SOURCE = Path(__file__).resolve().parents[2] / "reproductions" / "street-1993-wdbc"
INPUTS = ("reproduction.json", "paper.json", "protocol.json", "reproduce.py",
          "verify.py", "source/wdbc.data")
REQUIRED_NODES = {"study", "external_source", "reproduction_protocol",
                  "code_revision", "run", "artifact", "observation", "decision"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"Frozen reproduction release gate failed: {message}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="popper-frozen-reproduction-") as temporary:
        root = Path(temporary)
        for name in INPUTS:
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / name, destination)
        protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
        dataset = root / "source" / "wdbc.data"
        require(hashlib.sha256(dataset.read_bytes()).hexdigest()
                == protocol["dataset"]["expected_sha256"], "pinned dataset hash")
        require(inspect_reproduction(root)["status"] == "ready", "readiness")
        initialize_reproduction(root)
        task = ReproductionTask(root)
        result = task.run(trusted_local=True)
        require(result["phase"] == "verified", "execution and independent verification")
        require(result["assessment"]["outcome"] == "supports_reproduction",
                "frozen claim assessment")
        require(task.audit()["phase"] == "verified", "output and input audit")
        require(task.event_history()["integrity"]["ok"], "reproduction event chain")
        projection = reproduction_research_projection(root)
        graph = projection["experiment_graph"]
        require(graph["integrity"]["ok"], "research graph integrity")
        require(REQUIRED_NODES <= {node["kind"] for node in graph["nodes"]},
                "research graph coverage")
        # Prove that the audit reads the actual saved output, not only a stored flag.
        output = root / "results.json"
        with output.open("ab") as handle:
            handle.write(b"\n")
        try:
            task.audit()
        except ProtocolError:
            pass
        else:
            raise SystemExit("Frozen reproduction release gate failed: tamper not detected")
        print(json.dumps({"gate": "frozen_reproduction", "status": "passed",
                          "claim_id": protocol["target_claim"]["id"],
                          "outcome": result["assessment"]["outcome"],
                          "observed_accuracy": result["assessment"]["observed_value"],
                          "result_field": result["assessment"]["result_field"],
                          "graph_nodes": len(graph["nodes"]),
                          "tamper_detected": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
