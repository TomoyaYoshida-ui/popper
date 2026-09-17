"""Bounded live verification of one previously downloaded paper; no new search."""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from popper.core import file_hash
from popper.scoop import _verify_fulltext, make_json_client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-trial", type=Path, required=True)
    parser.add_argument("--paper-id", required=True)
    args = parser.parse_args()
    source = args.source_trial.resolve() / "model-calls.json"
    records = json.loads(source.read_text(encoding="utf-8"))
    original = next(record for record in records
                    if record["system"].startswith("Verify")
                    and record["payload"]["paper"]["paper_id"] == args.paper_id)
    payload = original["payload"]
    extracted = next((path for path in (source.parent / "scoop" / "papers").rglob("*.txt")
                      if path.stem == args.paper_id
                      and path.read_text(encoding="utf-8", errors="replace").strip()[:60000]
                      == payload["text"]), None)
    if extracted is None or not extracted.with_suffix(".pdf").is_file():
        raise RuntimeError("Stored model input does not match a downloaded PDF/text artifact")
    folder = ROOT / "integrations" / "runs" / (
        "quote-verification-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    folder.mkdir(parents=True)
    print("TRIAL=" + str(folder), flush=True)
    key = os.environ.get("POPPER_API_KEY", "")

    def save(name, value):
        text = json.dumps(value, ensure_ascii=False, indent=2)
        if key:
            text = text.replace(key, "[REDACTED]")
        (folder / name).write_text(text + "\n", encoding="utf-8")

    hashes = {str(path.relative_to(ROOT)): file_hash(path)
              for path in (ROOT / "popper").glob("*.py")}
    save("protocol.json", {
        "scope": "One real model verification of previously extracted text, not a complete autonomous campaign.",
        "source": str(source), "source_sha256": file_hash(source),
        "text_path": str(extracted), "text_sha256": file_hash(extracted),
        "pdf_sha256": file_hash(extracted.with_suffix(".pdf")),
        "source_hashes": hashes, "paper_id": args.paper_id,
        "model": "deepseek-flash", "max_logical_calls": 2, "max_physical_attempts": 4,
        "manual_quote_or_candidate_supplied": False,
    })
    calls = []
    started = time.monotonic()
    outcome = {}
    try:
        client = make_json_client("https://api.deepseek.com", "deepseek-flash",
                                  diagnostics_dir=folder / "model-diagnostics")

        def observed(prompt, request):
            if len(calls) >= 2:
                raise RuntimeError("Quote verification call budget exhausted")
            record = {"index": len(calls) + 1, "system": prompt, "payload": request}
            calls.append(record)
            save("model-calls.json", calls)
            print("MODEL_CALL=" + str(len(calls)), flush=True)
            result = client(prompt, request)
            record["response"] = result
            save("model-calls.json", calls)
            return result

        verified = _verify_fulltext(observed, payload["candidate"], payload["paper"], payload["text"])
        literal = " ".join(verified["closest_passage"].split()) in " ".join(payload["text"].split())
        outcome = {"status": "passed" if literal else "failed", "literal_quote_verified": literal,
                   "verified": verified}
    except Exception as error:
        outcome = {"status": "failed", "error_type": type(error).__name__, "message": str(error)}
    finally:
        outcome.update({
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "logical_calls": len(calls),
            "physical_attempts": len(list((folder / "model-diagnostics").glob("*.json"))),
            "source_unchanged": all(file_hash(ROOT / name) == value for name, value in hashes.items()),
            "upstream_unchanged": file_hash(source) == json.loads(
                (folder / "protocol.json").read_text(encoding="utf-8"))["source_sha256"],
        })
        save("outcome.json", outcome)
        print(json.dumps({k: v for k, v in outcome.items() if k != "verified"}, ensure_ascii=False), flush=True)
    return 0 if outcome["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
