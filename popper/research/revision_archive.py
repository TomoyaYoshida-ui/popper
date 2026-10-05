"""Application Worker decorator preserving the core replay artifact contract.

Generated Revision predictions are fetched into the research run. Core replay
also needs those exact predictions in its active core run before complete_run
freezes the artifact manifest. Copy only receipt-verified predictions, never
rewrite results/events or infer a score from candidate output.
"""
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from ..core import ProtocolError, digest, evaluator_pack, file_hash, read_json


class RevisionArchiveWorker:
    def __init__(self, worker, run_dir):
        self.delegate = worker
        self.run_dir = Path(run_dir).resolve()

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def fetch_artifact(self, job_id, name, destination):
        result = self.delegate.fetch_artifact(job_id, name, destination)
        destination = Path(destination).resolve()
        if not destination.is_relative_to(self.run_dir / "predictions"):
            return result
        research = read_json(self.run_dir / "research.json")
        spec = self.delegate.read_manifest(job_id)["spec"]
        revision = self.delegate.revisions.verify(spec["revision_id"])
        config = research["configs"].get(revision["identity"].get("hypothesis_id"))
        if config is None or name not in spec["outputs"]:
            raise ProtocolError("Revision replay archive ownership mismatch")
        project = Path(research["project"]).resolve()
        with closing(sqlite3.connect(f"{(project / '.popper/state.db').as_uri()}?mode=ro", uri=True)) as db:
            runs = db.execute("SELECT id FROM runs WHERE status='running' AND split='dev' AND config_hash=?",
                              (digest(config),)).fetchall()
        if len(runs) != 1:
            raise ProtocolError("Revision replay archive requires exactly one matching active core run")
        basename = destination.name
        pack = evaluator_pack(research["scorer_id"])
        allowed = {pack.invocation().prediction_name(seed) for seed in research["seeds"]}
        if basename not in allowed or Path(name).name != basename:
            raise ProtocolError("Revision replay prediction filename mismatch")
        target = project / ".popper/runs" / runs[0][0] / basename
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = file_hash(destination)
        if target.exists():
            if file_hash(target) != expected:
                raise ProtocolError("existing replay prediction cannot be overwritten")
        else:
            shutil.copyfile(destination, target)
        if file_hash(target) != expected:
            raise ProtocolError("Revision replay artifact copy failed verification")
        return result
