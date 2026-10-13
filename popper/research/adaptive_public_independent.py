"""Six UCI regression tasks that are not the locked formal public set.

Sample identities are hashes of the numeric feature rows. independence_verified
stays false: public rows are not treated as independent analysis units.
"""
from __future__ import annotations

import csv
import io
import hashlib
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

from ..core import ProtocolError, digest, file_hash, write_json

PUBLIC_INDEPENDENT = (
    ("concrete", 165, "concrete+compressive+strength", "compressive_strength"),
    ("yacht", 243, "yacht+hydrodynamics", "residuary_resistance"),
    ("energy", 242, "energy+efficiency", "heating_load"),
    ("qsar-aquatic", 505, "qsar+aquatic+toxicity", "lc50"),
    ("servo", 87, "servo", "rise_time"),
    ("forest-fires", 162, "forest+fires", "burned_area"),
)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SERVO_LEVELS = ("A", "B", "C", "D", "E")


def dataset_url(number, slug):
    return f"https://archive.ics.uci.edu/static/public/{number}/{slug}.zip"


def download_zip(number, slug):
    url = dataset_url(number, slug)
    request = urllib.request.Request(url, headers={"User-Agent": "PopperResearch/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read(20_000_001)
    if len(raw) > 20_000_000:
        raise ProtocolError("Dataset download exceeds preparation limit")
    return url, raw, zipfile.ZipFile(io.BytesIO(raw))


def _numeric_rows(rows):
    values = []
    for row in rows:
        if row is None:
            continue
        cells = list(row)
        while cells and (cells[-1] is None or str(cells[-1]).strip() == ""):
            cells.pop()
        if not cells:
            continue
        try:
            values.append([float(cell) for cell in cells])
        except (TypeError, ValueError):
            continue
    if not values:
        raise ProtocolError("Public table has no numeric rows")
    return values


def parse_concrete_table(rows):
    numeric = _numeric_rows(rows)
    if any(len(row) < 9 for row in numeric):
        raise ProtocolError("Concrete rows need 8 ingredients/age columns and a strength")
    matrix = np.asarray([row[:9] for row in numeric], dtype=float)
    names = ["cement", "slag", "fly_ash", "water", "superplasticizer",
             "coarse_aggregate", "fine_aggregate", "age"]
    return matrix[:, :8], matrix[:, 8], names


def parse_yacht_text(text):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        rows.append([float(part) for part in line.split()])
    matrix = np.asarray(rows, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] < 7:
        raise ProtocolError("Yacht rows need 6 hull/speed columns and a resistance")
    names = ["longitudinal_center", "prismatic", "length_displacement",
             "beam_draught", "length_beam", "froude"]
    return matrix[:, :6], matrix[:, 6], names


def parse_energy_table(rows):
    numeric = _numeric_rows(rows)
    if any(len(row) < 9 for row in numeric):
        raise ProtocolError("Energy rows need 8 building columns and a heating load")
    matrix = np.asarray([row[:9] for row in numeric], dtype=float)
    names = ["relative_compactness", "surface_area", "wall_area", "roof_area",
             "overall_height", "orientation", "glazing_area", "glazing_distribution"]
    return matrix[:, :8], matrix[:, 8], names


def parse_qsar_text(text):
    sample = text.strip().splitlines()[0]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(delimiter)]
        try:
            rows.append([float(part) for part in parts])
        except ValueError:
            continue
    matrix = np.asarray(rows, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] < 9:
        raise ProtocolError("QSAR rows need 8 descriptors and an LC50")
    names = ["tpsa", "saacc", "h050", "mlogp", "rdchi", "gats1p", "nN", "c040"]
    return matrix[:, :8], matrix[:, 8], names


def _indicators(value, levels, label):
    text = str(value).strip()
    if text not in levels:
        raise ProtocolError(f"Unexpected {label} level {text}")
    return [float(text == level) for level in levels]


def parse_servo_text(text):
    features, targets = [], []
    for line in text.splitlines():
        if not line.strip() or line.lower().startswith("index"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            pgain, vgain, target = float(parts[2]), float(parts[3]), float(parts[4])
        except ValueError:
            continue
        features.append(_indicators(parts[0], _SERVO_LEVELS, "servo motor")
                        + _indicators(parts[1], _SERVO_LEVELS, "servo screw")
                        + [pgain, vgain])
        targets.append(target)
    if not features:
        raise ProtocolError("Servo file has no data rows")
    names = [f"motor_{level}" for level in _SERVO_LEVELS] + [f"screw_{level}" for level in _SERVO_LEVELS] + ["pgain", "vgain"]
    return np.asarray(features, dtype=float), np.asarray(targets, dtype=float), names


def parse_forest_text(text):
    reader = csv.DictReader(io.StringIO(text))
    features, targets = [], []
    for row in reader:
        month = row["month"].strip().lower()
        day = row["day"].strip().lower()
        if month not in _MONTHS or day not in _DAYS:
            raise ProtocolError("Forest-fires month or day is outside the registered levels")
        numeric = [float(row[name]) for name in ("X", "Y", "FFMC", "DMC", "DC", "ISI", "temp", "RH", "wind", "rain")]
        features.append([float(row["X"]), float(row["Y"])]
                        + [float(month == name) for name in _MONTHS]
                        + [float(day == name) for name in _DAYS]
                        + numeric[2:])
        targets.append(float(row["area"]))
    if not features:
        raise ProtocolError("Forest-fires file has no data rows")
    names = ["X", "Y"] + [f"month_{name}" for name in _MONTHS] + [f"day_{name}" for name in _DAYS] + [
        "FFMC", "DMC", "DC", "ISI", "temp", "RH", "wind", "rain"]
    return np.asarray(features, dtype=float), np.asarray(targets, dtype=float), names


def _sheet_rows_xlsx(payload):
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(payload), data_only=False)
    return list(book.worksheets[0].iter_rows(values_only=True))


def _sheet_rows_xls(payload):
    import xlrd
    book = xlrd.open_workbook(file_contents=payload)
    sheet = book.sheet_by_index(0)
    return [tuple(sheet.row_values(i)) for i in range(sheet.nrows)]


def _member(archive, suffix):
    return archive.read(next(name for name in archive.namelist() if name.endswith(suffix)))


def parse_archive(name, archive):
    if name == "concrete":
        return parse_concrete_table(_sheet_rows_xls(_member(archive, ".xls")))
    if name == "yacht":
        return parse_yacht_text(_member(archive, ".data").decode("utf-8", errors="replace"))
    if name == "energy":
        return parse_energy_table(_sheet_rows_xlsx(_member(archive, ".xlsx")))
    if name == "qsar-aquatic":
        return parse_qsar_text(_member(archive, ".csv").decode("utf-8", errors="replace"))
    if name == "servo":
        return parse_servo_text(_member(archive, "servo.data").decode("utf-8", errors="replace"))
    if name == "forest-fires":
        return parse_forest_text(_member(archive, ".csv").decode("utf-8", errors="replace"))
    raise ProtocolError("Unknown independent public dataset")


def load_public(name):
    spec = next(item for item in PUBLIC_INDEPENDENT if item[0] == name)
    url, raw, archive = download_zip(spec[1], spec[2])
    features, target, names = parse_archive(name, archive)
    source = {
        "kind": "public",
        "url": url,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "dataset_id": spec[1],
        "pretraining_exposure": "unknown",
        "target_column": spec[3],
    }
    return features, target, names, source


def materialize_task(directory, task_id, features, target, feature_names, source):
    """Split like the existing public tasks: group by the full feature row."""
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "prepared.json").exists():
        from ..core import read_json
        prepared = read_json(root / "prepared.json")
        for split, sha in prepared["data_hashes"].items():
            if file_hash(root / (split + ".json")) != sha:
                raise ProtocolError("Prepared data changed")
        return prepared
    import secrets
    seed = secrets.randbits(63)
    rng = np.random.default_rng(seed)
    X = np.asarray(features, dtype=float)
    y = np.asarray(target, dtype=float)
    groups = {}
    for index, row in enumerate(X):
        normalized = [None if not np.isfinite(value) else float(value) for value in row]
        groups[str(index)] = digest(normalized)
    identities = np.array(sorted(set(groups.values())))
    order = rng.permutation(identities)
    cut_a, cut_b = int(.6 * len(order)), int(.8 * len(order))
    owners = {group: split for split, values in (
        ("train", order[:cut_a]), ("dev", order[cut_a:cut_b]), ("confirmation", order[cut_b:])) for group in values}
    indices = {split: [i for i in range(len(X)) if owners[groups[str(i)]] == split]
               for split in ("train", "dev", "confirmation")}
    medians = np.nanmedian(X[indices["train"]], axis=0)
    X = np.where(np.isfinite(X), X, medians)
    if not np.all(np.isfinite(X)) or not np.all(np.isfinite(y)):
        raise ProtocolError("Dataset contains an unfillable column or invalid target")
    for split, ids in indices.items():
        write_json(root / (split + ".json"),
                   [{"id": str(i), "features": X[i].tolist(), "y": float(y[i])} for i in ids])
    train = X[indices["train"]]
    correlations = []
    for column in train.T:
        correlations.append(float(np.corrcoef(column, y[indices["train"]])[0, 1]) if column.std() > 0 else 0.)
    card = {
        "rows_train": len(train),
        "feature_count": int(X.shape[1]),
        "feature_means_train": train.mean(axis=0).tolist(),
        "feature_std_train": train.std(axis=0).tolist(),
        "target_column": source["target_column"],
        "source": source,
        "scope": "predictive performance; mechanisms remain unverified",
        "correlations_train": correlations,
    }
    data = {
        "task_id": task_id,
        "target_column": source["target_column"],
        "feature_names": feature_names,
        "source": source,
        "groups": groups,
        "data_hashes": {split: file_hash(root / (split + ".json")) for split in indices},
        "independence_verified": False,
        "sampling": {"design": "feature_row_hash"},
        "card": card,
        "materials": [{"id": "dataset-card", "text": str(card)}],
        "bootstrap_seed": int(rng.integers(0, 2**32)),
    }
    write_json(root / "oracle-private.json", {"seed": seed, "kind": None})
    write_json(root / "prepared.json", data)
    return data
