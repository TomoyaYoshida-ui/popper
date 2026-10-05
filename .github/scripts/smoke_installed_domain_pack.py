"""Prove an external entry-point pack works with an independently installed wheel."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_installed_domain_pack.py <target with Popper + external pack>")
    installed = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="popper-wheel-domain-") as temporary:
        root = Path(temporary)
        sys.path.insert(0, str(installed))
        os.environ["POPPER_DOMAIN_PACKS"] = "external-order"
        import popper
        from popper.core import EVALUATORS, Experiment, initialize

        if not Path(popper.__file__).resolve().is_relative_to(installed):
            raise SystemExit("External domain smoke imported checkout instead of installed wheel")
        if "example-external-order-v1" not in EVALUATORS:
            raise SystemExit("Installed wheel did not discover selected external entry point")
        identity = EVALUATORS["example-external-order-v1"]
        if not identity.get("implementation_distribution_sha256"):
            raise SystemExit("External wheel payload was not bound to evaluator identity")
        import external_order_pack
        if not Path(external_order_pack.__file__).resolve().is_relative_to(installed):
            raise SystemExit("External domain smoke imported example source instead of its wheel")
        project = root / "project"
        project.mkdir()
        example = Path(popper.__file__).resolve().parent / "demo_templates" / "ode-convergence"
        for name in ("experiment.json", "model.py", "train.json", "dev.json", "test.json"):
            shutil.copyfile(example / name, project / name)
        spec_path = project / "experiment.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["metric"]["name"] = "external_convergence_order"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        initialize(project)
        experiment = Experiment(project)
        try:
            experiment.search(True)
            experiment.freeze()
            experiment.confirm(True)
            replay = experiment.replay()
            if not replay["claim_recomputed"] or replay["runs_recomputed"] < 4:
                raise SystemExit(f"Installed external domain could not replay claim: {replay}")
        finally:
            experiment.close()
    print("Installed wheel: selected third-party domain pack completed and replayed non-ML claim OK")


if __name__ == "__main__":
    main()
