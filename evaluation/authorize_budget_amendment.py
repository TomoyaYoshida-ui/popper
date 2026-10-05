"""Record the human's 2026-10-03 token authorization without changing science."""
from contextlib import closing
from pathlib import Path

from popper.core import digest, read_json, write_json
from popper.research.agent_evaluation_budget import AgentEvaluationBudget


if __name__ == "__main__":
    root = Path(__file__).parent / "runs/agent-oscillator-2026-10-01-v3/formal-campaign-qwen-v2"
    manifest = read_json(root / "campaign.json")
    with closing(AgentEvaluationBudget(manifest["budget_ledger"])) as ledger:
        evidence = lambda: digest({name: [list(row) for row in ledger.connection.execute(f"SELECT * FROM {name} ORDER BY 1")]
                                  for name in ("requests", "aggregate_usage", "aggregate_members")})
        before = evidence()
        policy = ledger.amend_policy("human-token-ceiling-2026-10-03-400-million",
            {"max_requests": 60, "max_tokens": 400000},
            {"max_requests": 60, "max_tokens": 400000000},
            {"actor": "user", "text": "4个亿的tocten额度", "date": "2026-10-03",
             "scope": "cumulative model tokens; request cap unchanged; prior charges retained"})
        assert before == evidence(), "Existing charge evidence must never be rewritten"
        receipt = {"effective_policy": policy, "status": ledger.status(),
                   "request_and_usage_evidence_sha256": before,
                   "scientific_protocol_unchanged": True,
                   "source": "explicit human message in this thread; not Agent authorization"}
        target = root / "budget-authorization-400-million-v1.json"
        if target.exists():
            assert read_json(target) == receipt
        else:
            write_json(target, receipt)
        print(receipt)
