"""Isolated structured bridge into pinned vendor Python APIs."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run_source_workers(vendor_search, sources, queries, start_year, end_year,
                       max_results, parallel, timeout_seconds=60):
    """Forwarding layer over the pinned vendor worker functions.

    Vendor modules stay untouched: `_start_worker` / `_collect_worker` /
    `_terminate_workers` are called as ordinary functions instead of being
    replaced on the vendor module, and the per-source wall-clock deadline lives
    in a local table rather than on the process object.
    """
    gathered = {}
    with tempfile.TemporaryDirectory(prefix="popper_paper_search_") as temp_dir:
        work_dir = Path(temp_dir)
        states = {}
        deadlines = {}
        try:
            for source in sources:
                try:
                    states[source] = vendor_search._start_worker(
                        source, queries, start_year, end_year, max_results, work_dir)
                except OSError as exc:
                    print(f"[{source}] worker could not start: {exc}", file=sys.stderr)
                    gathered[source] = []
                    continue
                deadlines[source] = time.monotonic() + timeout_seconds
                if not parallel:
                    gathered[source] = _collect_with_deadline(
                        vendor_search, source, states[source], deadlines[source])
            if parallel:
                # Every process is already running; collecting in requested order
                # does not serialize the network work.
                for source in states:
                    gathered[source] = _collect_with_deadline(
                        vendor_search, source, states[source], deadlines[source])
        except BaseException:
            vendor_search._terminate_workers([state[0] for state in states.values()])
            raise
        finally:
            vendor_search._terminate_workers([state[0] for state in states.values()])
    return {source: gathered.get(source, []) for source in sources}


def _collect_with_deadline(vendor_search, source, state, deadline):
    process = state[0]
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        vendor_search._terminate_workers([process])
        print(f"[{source}] source deadline exceeded; retaining other sources", file=sys.stderr)
    return vendor_search._collect_worker(source, *state)


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "paper-search":
        raise SystemExit("usage: vendor_worker paper-search SCRIPTS_DIR")
    scripts = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(scripts))
    request = json.load(sys.stdin)
    import search_papers as vendor_search
    from _http_runtime import validate_environment
    from postprocess import dedup, rank
    sources = list(dict.fromkeys(request["sources"]))
    validate_environment(sources)
    raw = run_source_workers(
        vendor_search, sources, request["queries"], request["start_year"],
        request["end_year"], request["max_papers"], request["parallel"])
    counts = {source: len(papers) for source, papers in raw.items()}
    merged = dedup(raw)
    ranked, dropped = rank(merged, request["queries"], request.get("min_score"))
    json.dump({"raw_by_source": raw, "source_counts": counts, "papers": ranked,
               "duplicate_count": sum(counts.values()) - len(merged),
               "dropped_count": dropped}, sys.stdout, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    main()
