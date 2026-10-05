"""Verify the public cross-domain benchmark using an independently installed wheel."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_installed_benchmark.py <pip --target directory>")
    installed = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(installed))
    import popper
    from popper.research.benchmark import (run_cross_domain_benchmark,
                                           verify_cross_domain_benchmark)

    if not Path(popper.__file__).resolve().is_relative_to(installed):
        raise SystemExit("Benchmark smoke imported checkout instead of installed wheel")
    with tempfile.TemporaryDirectory(prefix="popper-wheel-benchmark-") as temporary:
        output = Path(temporary) / "benchmark"
        result = run_cross_domain_benchmark(output, trusted_local=True)
        if result["status"] != "passed" or result["passed"] != 2:
            raise SystemExit(f"Installed cross-domain benchmark failed: {result}")
        if not verify_cross_domain_benchmark(output)["ok"]:
            raise SystemExit("Installed cross-domain benchmark verification failed")
    print("Installed wheel ODE + throughput benchmark and verification OK")


if __name__ == "__main__":
    main()
