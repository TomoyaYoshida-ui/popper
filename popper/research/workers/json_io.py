"""Bounded reads of atomically replaced worker metadata, not job retries."""
from __future__ import annotations

import os
import time

from ...core import read_json as _read_json

_WINDOWS = os.name == "nt"
_SHARING_DELAYS = (0.0, 0.05, 0.1, 0.2, 0.4, 0.8)


def read_job_json(path):
    # Windows replacement/indexer handles can briefly deny even a read. Keep
    # the original JSON/integrity checks and never resubmit or mutate a job.
    for index, delay in enumerate(_SHARING_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return _read_json(path)
        except PermissionError:
            if not _WINDOWS or index == len(_SHARING_DELAYS) - 1:
                raise
