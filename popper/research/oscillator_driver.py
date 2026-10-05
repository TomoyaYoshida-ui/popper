"""Candidate-side wrapper. This file is copied, not imported with the host.

The only trusted results are those accepted and retained by the host broker.
"""
import json
import os
import runpy
import sys

from oscillator_client import RHSClient


def main():
    solver = runpy.run_path(sys.argv[1])["solve"]
    with open(sys.argv[2], encoding="utf-8") as handle:
        rows = json.load(handle)
    with open(sys.argv[3], encoding="utf-8") as handle:
        config = json.load(handle)
    client = RHSClient(os.environ["POPPER_RHS_PIPE"], os.environ["POPPER_RHS_CAPABILITY"],
                       os.environ["POPPER_RHS_JOB"], timeout=15)
    try:
        for row in rows:
            client.begin(row["id"])
            states = solver(client.rhs, [row["q0"], row["v0"]], row["times"], row["budget"], config)
            client.finish(states)
    finally:
        client.close()


if __name__ == "__main__":
    main()
