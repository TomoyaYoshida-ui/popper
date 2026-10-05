"""Serve retained development evidence locally without a model client.

Refuse every active persistent queue before Workstation construction. This is a
UI-only acceptance environment, not an experiment or a model continuation.
"""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from popper.core import ProtocolError, read_json
from popper.server import Workstation
from popper.sessions import AgentSessionStore


def check_quiet(project, research):
    registry = project / ".popper/workspaces.json"
    if registry.exists():
        roots = read_json(registry)["roots"]
        if any(Path(row["root"]).resolve() != project for row in roots):
            raise ProtocolError("UI acceptance must not recover another registered project")
    with closing(AgentSessionStore.readonly(project)) as store:
        if not store.verify_turn_events()["ok"] or not store.verify_tool_events()["ok"]:
            raise ProtocolError("session integrity failed")
        for session in store.list_sessions():
            if any(row["status"] in {"queued", "running"} for row in store.agent_jobs(session["session_id"])):
                raise ProtocolError("active model queue; do not start UI acceptance")
            if any(row["status"] in {"approved", "running", "pending_approval"}
                   for row in store.tool_calls(session["session_id"])):
                raise ProtocolError("unsettled tools; do not start UI acceptance")
    for root in (project / ".popper", research):
        for database in root.glob("*.sqlite"):
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                for table in tables:
                    quoted = '"' + table.replace('"', '""') + '"'
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(" + quoted + ")")}
                    if "status" in columns and "jobs" in table:
                        active = connection.execute("SELECT COUNT(*) FROM " + quoted + " WHERE status IN ('queued','running')").fetchone()[0]
                        if active:
                            raise ProtocolError("active persistent execution queue: " + database.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    project, research = args.project.resolve(), args.research.resolve()
    check_quiet(project, research)
    server = Workstation(("127.0.0.1", args.port), project, trusted_local=True,
                         research_runs=[research])
    server.environment_label = "临时证据验收 · 未配置模型"
    print("UI acceptance ready; no model client; queues checked", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
