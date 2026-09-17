"""Conservative changed-statement coverage evidence, not a scientific mechanism test.

Line events cannot observe every real edit: deleting a statement, changing an
``if`` condition or a definition header, and editing comments or formatting all
leave the set of changed statements empty. Such a change is therefore reported as
``ambiguous`` coverage rather than rejected, and ``passed`` stays true for it. The
assessment still blocks the two cases where it has positive evidence against the
candidate: detected changed statements that never ran (``none``) and an incomplete
trace (``unknown``)."""
from __future__ import annotations

import ast
from collections import Counter

from ..core import ProtocolError


# Coverage values that mean at least one detected changed statement actually ran.
EXECUTED_COVERAGE = frozenset({"complete", "partial"})
# Coverage values that carry positive evidence against the candidate.
BLOCKING_COVERAGE = frozenset({"none", "unknown"})


def changed_statement_lines(before, after):
    # Container/definition events do not establish execution of their bodies.
    excluded = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If,
                ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
                ast.Try, ast.Import, ast.ImportFrom, ast.Pass)

    def statements(source):
        tree = ast.parse(source)
        # Statements sharing a line with a container header are the container's own
        # event, not proof that a body executed.
        containers = {node.lineno for node in ast.walk(tree)
                      if isinstance(node, excluded) or type(node).__name__ in {"Match", "TryStar"}}
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt) or isinstance(node, excluded):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            # Match/TryStar are containers on Python versions that expose them.
            if type(node).__name__ in {"Match", "TryStar"}:
                continue
            if node.lineno in containers:
                continue
            yield ast.dump(node, include_attributes=False), node.lineno

    original = Counter(key for key, _ in statements(before))
    changed = []
    for key, line in statements(after):
        if original[key]:
            original[key] -= 1
        else:
            changed.append(line)
    return sorted(set(changed))


def assess_execution(changes, trace):
    if (not isinstance(trace, dict) or trace.get("schema_version") != "1.0"
            or type(trace.get("completed")) is not bool
            or not isinstance(trace.get("lines"), dict)):
        raise ProtocolError("执行轨迹格式错误")
    for name, lines in trace["lines"].items():
        if (not isinstance(name, str) or not isinstance(lines, list)
                or any(type(line) is not int or line < 1 for line in lines)):
            raise ProtocolError("执行轨迹行号格式错误")
    files = {name: {"changed_lines": lines,
                    "executed_changed_lines": sorted(set(lines) & set(trace["lines"].get(name, [])))}
             for name, lines in changes.items() if lines}
    uncovered = sorted(name for name, row in files.items() if not row["executed_changed_lines"])
    if not trace["completed"]:
        reason, coverage = "trace_incomplete", "unknown"
    elif not files:
        # Nothing to check: either the revision really changed no statement, or it
        # only deleted statements, edited a condition or definition header, or
        # touched comments and formatting. Line events cannot tell those apart, so
        # the assessment reports evidence instead of rejecting the candidate.
        reason, coverage = "no_executable_change", "ambiguous"
    elif not uncovered:
        reason, coverage = "executed_changed_code", "complete"
    elif len(uncovered) == len(files):
        reason, coverage = "changed_code_not_executed", "none"
    else:
        reason, coverage = "partially_executed_changed_code", "partial"
    return {"passed": coverage not in BLOCKING_COVERAGE, "reason": reason,
            "coverage": coverage, "uncovered_files": uncovered,
            "files": files, "trust": "in_process_trace",
            "scientific_mechanism_verified": False}


# Copied outside the candidate's immutable code tree, before lowering integrity.
# The trace lives in the candidate process: it is diagnostic evidence, not an
# adversarially secure proof or proof that a changed value influenced predictions.
PROBE_SOURCE = r'''
import json
import os
import runpy
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
entrypoint = sys.argv[2]
target = Path(sys.argv[3])
arguments = sys.argv[4:]
seen = {}
cache = {}
completed = False

def trace(frame, event, arg):
    if event in ("call", "line"):
        filename = frame.f_code.co_filename
        if filename not in cache:
            path = Path(filename).resolve()
            cache[filename] = path.relative_to(root).as_posix() if path.is_relative_to(root) else None
        name = cache[filename]
        if name is None:
            return None
        if event == "line":
            seen.setdefault(name, set()).add(frame.f_lineno)
    return trace

sys.argv = [str(root / entrypoint), *arguments]
sys.path.insert(0, str(root))
sys.path.insert(0, str((root / entrypoint).parent))
sys.settrace(trace)
try:
    try:
        runpy.run_path(str(root / entrypoint), run_name="__main__")
        completed = True
    except SystemExit as error:
        completed = error.code is None or error.code == 0
        raise
finally:
    intact = sys.gettrace() is trace
    sys.settrace(None)
    target.write_text(json.dumps({"schema_version": "1.0", "completed": completed and intact,
        "lines": {name: sorted(lines) for name, lines in seen.items()}}), encoding="utf-8")
'''
