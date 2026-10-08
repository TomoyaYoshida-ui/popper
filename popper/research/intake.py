"""数据接入：格式适配器、确认集封存、数据卡。

配方编译在 recipe.py。本模块不写 Observation，也不写 Claim。
标签分布只在 holdout_sealed 提交成功之后计算。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from ..core import ProtocolError, canonical, digest, file_hash, read_json, write_json, is_safe_relative
from ..eventlog import GENESIS_HASH, chained_body
from .recipe_runner import quantiles

MAX_SOURCE_BYTES = 32 * 1024 * 1024  # 33554432
MAX_ROWS = 50_000
MAX_COLUMNS = 128
MAX_CELL_CHARS = 4_096
MAX_FASTA_SEQUENCE_CHARS = 65_536
MAX_CATEGORY_LEVELS = 256
MAX_STRATA = 64
MAX_KMER_K = 6
MAX_RECIPE_STEPS = 32
MAX_UNBUDGETED_COMPILES = 8
STUDY_BUDGET_FLOOR = 4
TOP_CATEGORIES = 20
SEED_MAX = 2_147_483_647
MISSING_TOKENS = ("", "na", "n/a", "null", "none", "nan", "?")

STRATEGIES = ("random", "stratified", "group", "temporal", "id_list")
COLUMN_STRATEGIES = ("stratified", "group", "temporal")
TEXT_FORMATS = ("csv", "json", "fasta")
TABLE_EVALUATORS = (
    "mse-v1", "mae-v1", "binary-accuracy-v1", "binary-f1-v1", "multiclass-macro-f1-v1",
)

EVENT_PAYLOAD_KEYS = {
    "dataset_hashed": {"raw_sha256", "byte_length", "format", "source_note_sha256"},
    "holdout_sealed": {"seal_sha256", "split_declaration_sha256", "n_train", "n_dev",
                       "n_holdout", "row_set_sha256"},
    "data_card_written": {"data_card_sha256", "seal_sha256"},
    "recipe_proposed": {"proposal_id", "recipe_sha256"},
    "recipe_approved": {"proposal_id", "recipe_sha256"},
    "recipe_compiled": {"input_identity_sha256", "seal_sha256", "holdout_row_set_sha256",
                        "recipe_sha256", "recipe_fit_sha256", "receipt_sha256",
                        "budget_reserved"},
    "adapter_registered": {"adapter_id", "suffixes_sha256", "magic", "record_shape",
                           "implementation_sha256"},
    "adapter_approved": {"adapter_id", "implementation_sha256", "approver", "record_shape"},
    "problem_proposed": {"proposal_id", "problem_sha256", "split_declaration_sha256"},
    "problem_approved": {"proposal_id", "problem_sha256", "experiment_sha256"},
    "metric_registered": {"metric_id", "implementation_sha256", "fixture_sha256", "approver"},
}

_INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
_NUM_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?)?$")
_SEQ_RE = re.compile(r"^[A-Za-z]+$")
_INT64_MIN, _INT64_MAX = -2**63, 2**63 - 1

# 内置适配器。第二批 CIF / POSCAR / PDB / SDF / FASTQ 不在这里，也没有解析器。
RECORD_SHAPES = ("table", "sequence", "structure", "document", "measurement_request")
SECOND_BATCH_IDS = frozenset({"cif", "poscar", "pdb", "sdf", "fastq"})
_ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ADAPTER_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,16}$")
_PARSERS = frozenset({"csv", "json", "fasta", "parquet"})
BUILTIN_ADAPTERS = (
    {"id": "csv", "suffixes": (".csv",), "record_shape": "table", "magic": "utf-8-not-parquet"},
    {"id": "parquet", "suffixes": (".parquet", ".pq"), "record_shape": "table", "magic": "PAR1"},
    {"id": "json", "suffixes": (".json",), "record_shape": "table", "magic": "json-array"},
    {"id": "fasta", "suffixes": (".fasta", ".fa", ".faa", ".fna"),
     "record_shape": "sequence", "magic": "fasta-gt"},
)

label_stats_calls = 0
seal_checkpoints = []
# 进程内解析器。实现文件只取哈希，不执行。第二批 id 不能绑定。
_BOUND_PARSERS = {}


def adapters():
    """已登记的内置适配器。不包含第二批结构或测序格式，也不包含未批准的登记。"""
    return tuple(dict(item) for item in BUILTIN_ADAPTERS)


def _registry_path(project):
    return Path(project) / ".popper-intake" / "adapter-registry.json"


def load_adapter_registry(project):
    path = _registry_path(project)
    if not path.is_file():
        return []
    data = read_json(path)
    if (not isinstance(data, dict) or set(data) != {"schema", "adapters"}
            or data.get("schema") != "popper.adapter-registry/v1"
            or not isinstance(data.get("adapters"), list)):
        raise ProtocolError("适配器声明含未声明字段")
    return data["adapters"]


def _write_registry(project, entries):
    write_json(_registry_path(project), {
        "schema": "popper.adapter-registry/v1",
        "adapters": entries,
    })


def _registry_by_id(entries, adapter_id):
    for item in entries:
        if item.get("id") == adapter_id:
            return item
    return None


def _registry_by_suffix(entries, suffix):
    for item in entries:
        if suffix in item.get("suffixes", ()):
            return item
    return None


def _validate_adapter_declaration(adapter_id, suffixes, magic, record_shape, entries):
    if not isinstance(adapter_id, str) or not _ADAPTER_ID_RE.fullmatch(adapter_id):
        raise ProtocolError("适配器声明含未声明字段")
    if _adapter_by_id(adapter_id) is not None:
        raise ProtocolError("适配器声明含未声明字段")
    if (not isinstance(suffixes, (list, tuple)) or not suffixes
            or len(set(suffixes)) != len(suffixes)):
        raise ProtocolError("适配器声明含未声明字段")
    normalized = []
    for suffix in suffixes:
        if type(suffix) is not str:
            raise ProtocolError("适配器声明含未声明字段")
        token = suffix.lower()
        if not _ADAPTER_SUFFIX_RE.fullmatch(token) or _adapter_by_suffix(token) is not None:
            raise ProtocolError("适配器声明含未声明字段")
        if any(token in item.get("suffixes", ()) and item.get("id") != adapter_id for item in entries):
            raise ProtocolError("适配器声明含未声明字段")
        normalized.append(token)
    if (type(magic) is not str or not magic or len(magic) > 64 or "\n" in magic
            or record_shape not in RECORD_SHAPES):
        raise ProtocolError("适配器声明含未声明字段")
    return normalized


def register_adapter(project, *, adapter_id, suffixes, magic, record_shape, implementation):
    """登记一个适配器。只记录声明和实现文件哈希，不执行该文件，也不解析第二批格式。"""
    project = _project_dir(project)
    entries = load_adapter_registry(project)
    normalized = _validate_adapter_declaration(
        adapter_id, suffixes, magic, record_shape, entries)
    impl = _inside(project, implementation)
    implementation_sha = file_hash(impl)
    body = {
        "id": adapter_id,
        "suffixes": normalized,
        "magic": magic,
        "record_shape": record_shape,
        "implementation_sha256": implementation_sha,
        "status": "registered",
        "approver": None,
    }
    existing = _registry_by_id(entries, adapter_id)
    payload = {
        "adapter_id": adapter_id,
        "suffixes_sha256": digest(normalized),
        "magic": magic,
        "record_shape": record_shape,
        "implementation_sha256": implementation_sha,
    }
    if existing is not None:
        comparable = {key: existing.get(key) for key in body if key not in {"status", "approver"}}
        current = {key: body[key] for key in comparable}
        if comparable != current:
            raise ProtocolError("适配器声明含未声明字段")
        return {"adapter_id": adapter_id, "implementation_sha256": implementation_sha,
                "status": existing["status"]}
    entries.append(body)
    _write_registry(project, entries)
    log = IntakeLog(project / ".popper-intake" / "intake.sqlite")
    try:
        log.append(adapter_id, "adapter_registered", payload)
        log.commit()
    finally:
        log.close()
    return {"adapter_id": adapter_id, "implementation_sha256": implementation_sha,
            "status": "registered"}


def bind_adapter_parser(project, adapter_id, parser):
    """把解析函数绑到已登记的表或序列适配器。不执行实现文件，也不解析第二批格式。"""
    project = _project_dir(project)
    existing = _registry_by_id(load_adapter_registry(project), adapter_id)
    if existing is None:
        raise ProtocolError("未登记格式不能读取")
    if (adapter_id in SECOND_BATCH_IDS or existing.get("record_shape") not in {"table", "sequence"}
            or not callable(parser)):
        raise ProtocolError("适配器未实现，不能读取")
    _BOUND_PARSERS[(str(project), adapter_id)] = parser
    return {"adapter_id": adapter_id, "bound": True}


def unbind_adapter_parser(project, adapter_id):
    project = _project_dir(project)
    _BOUND_PARSERS.pop((str(project), adapter_id), None)


def approve_adapter(project, *, adapter_id, approver):
    """批准一次。对后续同格式文件有效，不按 Study 重复批准。没有解析器时仍不能读取。"""
    project = _project_dir(project)
    if type(approver) is not str or not approver.strip() or "\n" in approver or len(approver) > 200:
        raise ProtocolError("适配器声明含未声明字段")
    entries = load_adapter_registry(project)
    existing = _registry_by_id(entries, adapter_id)
    if existing is None:
        raise ProtocolError("未登记格式不能读取")
    if existing.get("status") == "approved":
        if existing.get("approver") != approver:
            raise ProtocolError("适配器声明含未声明字段")
        return {"adapter_id": adapter_id,
                "implementation_sha256": existing["implementation_sha256"],
                "status": "approved", "approver": approver}
    existing["status"] = "approved"
    existing["approver"] = approver
    _write_registry(project, entries)
    log = IntakeLog(project / ".popper-intake" / "intake.sqlite")
    try:
        log.append(adapter_id, "adapter_approved", {
            "adapter_id": adapter_id,
            "implementation_sha256": existing["implementation_sha256"],
            "approver": approver,
            "record_shape": existing["record_shape"],
        })
        log.commit()
    finally:
        log.close()
    return {"adapter_id": adapter_id,
            "implementation_sha256": existing["implementation_sha256"],
            "status": "approved", "approver": approver}


def _adapter_by_id(adapter_id):
    for item in BUILTIN_ADAPTERS:
        if item["id"] == adapter_id:
            return item
    return None


def _adapter_by_suffix(suffix):
    for item in BUILTIN_ADAPTERS:
        if suffix in item["suffixes"]:
            return item
    return None


def note_seal_point():
    for callback in list(seal_checkpoints):
        callback()


class IntakeLog:
    """接入事件链。表形状与研究库 events 相同，previous 键走 chained_body。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "kind TEXT NOT NULL, entity_id TEXT NOT NULL,"
            "event_type TEXT NOT NULL, payload TEXT NOT NULL,"
            "writer TEXT NOT NULL,"
            "previous_hash TEXT NOT NULL, hash TEXT NOT NULL)")
        self._conn.commit()
        self.holdout_committed = self.event("holdout_sealed") is not None

    def close(self):
        self._conn.close()

    def events(self):
        rows = self._conn.execute(
            "SELECT seq, kind, entity_id, event_type, payload, writer, previous_hash, hash "
            "FROM events ORDER BY seq").fetchall()
        return [{"seq": row[0], "kind": row[1], "entity_id": row[2], "event_type": row[3],
                 "payload": json.loads(row[4]), "writer": row[5],
                 "previous_hash": row[6], "hash": row[7]} for row in rows]

    def event(self, event_type):
        found = [item for item in self.events() if item["event_type"] == event_type]
        return found[-1] if found else None

    def count(self, event_type):
        return sum(1 for item in self.events() if item["event_type"] == event_type)

    def append(self, entity_id, event_type, payload):
        if event_type not in EVENT_PAYLOAD_KEYS or set(payload) != EVENT_PAYLOAD_KEYS[event_type]:
            raise ProtocolError("接入事件链与封存件不一致")
        row = self._conn.execute(
            "SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row[0] if row else GENESIS_HASH
        _body, event_hash = chained_body(
            {"kind": "dataset", "entity_id": entity_id, "event_type": event_type,
             "payload": payload, "writer": "research_store"},
            previous, previous_key="previous")
        self._conn.execute(
            "INSERT INTO events(kind, entity_id, event_type, payload, writer,"
            " previous_hash, hash) VALUES(?,?,?,?,?,?,?)",
            ("dataset", entity_id, event_type,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             "research_store", previous, event_hash))
        return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def commit(self):
        self._conn.commit()
        self.holdout_committed = self.event("holdout_sealed") is not None


def _project_dir(project):
    path = Path(project)
    if not path.exists():
        raise ProtocolError("项目目录不存在")
    if not path.is_dir():
        raise ProtocolError("项目路径必须是目录")
    return path.resolve()


def _inside(project, relative, *, missing_message="原始文件不存在"):
    raw = Path(relative)
    if not is_safe_relative(str(relative)):
        raise ProtocolError("原始文件必须位于项目目录之内")
    path = (project / raw).resolve()
    if not path.is_relative_to(project):
        raise ProtocolError("原始文件必须位于项目目录之内")
    if not path.is_file():
        raise ProtocolError(missing_message)
    return path


def _decode_utf8(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProtocolError("原始文件不是 UTF-8 或 UTF-8-SIG") from None


def _magic_ok(adapter_id, raw):
    if adapter_id == "parquet":
        return raw.startswith(b"PAR1")
    if raw.startswith(b"PAR1"):
        return False
    text = _decode_utf8(raw)
    stripped = text.lstrip()
    if adapter_id == "csv":
        return True
    if adapter_id == "json":
        return stripped.startswith("[") or stripped.startswith("{")
    if adapter_id == "fasta":
        return stripped.startswith(">")
    return False


def _parquet_reader():
    try:
        import pyarrow.parquet as parquet
    except ImportError:
        return None
    return parquet


def _custom_magic_ok(magic, raw):
    if raw.startswith(magic.encode("utf-8")):
        return True
    try:
        text = _decode_utf8(raw)
    except ProtocolError:
        return False
    return text.lstrip().startswith(magic)


def resolve_adapter(path, format_id, raw, *, convert_unknown_to_csv=False, registry=None):
    """按后缀和格式 id 找已登记适配器。未批准、未知扩展名、未知魔数都直接拒绝。"""
    if convert_unknown_to_csv:
        raise ProtocolError("不能把未知文件转换成 CSV 再读取")
    entries = list(registry or [])
    suffix = Path(path).suffix.lower()
    by_suffix = _adapter_by_suffix(suffix)
    registered = None if by_suffix is not None else _registry_by_suffix(entries, suffix)
    if by_suffix is None and registered is None:
        raise ProtocolError("未知扩展名，不能猜测格式")
    if registered is not None and registered.get("status") != "approved":
        raise ProtocolError("适配器尚未批准")
    if format_id is not None:
        chosen = _adapter_by_id(format_id)
        declared = None if chosen is not None else _registry_by_id(entries, format_id)
        if chosen is None and declared is None:
            raise ProtocolError("未登记格式不能读取")
        if declared is not None and declared.get("status") != "approved":
            raise ProtocolError("适配器尚未批准")
        if chosen is None:
            chosen = declared
        expected = by_suffix["id"] if by_suffix is not None else registered["id"]
        if chosen["id"] != expected:
            raise ProtocolError("声明格式与文件后缀不一致")
    else:
        chosen = by_suffix if by_suffix is not None else registered
    if chosen["id"] in _PARSERS:
        if not _magic_ok(chosen["id"], raw):
            raise ProtocolError("文件魔数与已登记格式不一致")
    elif not _custom_magic_ok(chosen.get("magic", ""), raw):
        raise ProtocolError("文件魔数与已登记格式不一致")
    return chosen


def _is_missing_token(text):
    return text.strip().lower() in MISSING_TOKENS


def _kind_and_value(value):
    """把标量收成规范值，并给出推断用的种类。嵌套值返回 nested。"""
    if value is None:
        return "missing", None
    if isinstance(value, bool):
        return "bool", "true" if value else "false"
    if isinstance(value, int):
        if _INT64_MIN <= value <= _INT64_MAX:
            return "int", value
        return "bad", value
    if isinstance(value, float):
        if math.isfinite(value):
            return "float", value
        return "bad", value
    if isinstance(value, str):
        if _is_missing_token(value):
            return "missing", None
        lowered = value.strip().lower()
        if lowered in {"true", "false"}:
            return "bool", lowered
        if _INT_RE.fullmatch(value.strip()):
            number = int(value.strip())
            if _INT64_MIN <= number <= _INT64_MAX:
                return "int", number
            return "bad", value
        if _NUM_RE.fullmatch(value.strip()):
            number = float(value.strip())
            if math.isfinite(number):
                return "float", number
        if _real_datetime(value.strip()):
            return "datetime", value.strip()
        if _SEQ_RE.fullmatch(value.strip()):
            return "sequence", value.strip()
        return "text", value
    return "nested", value


def _real_datetime(text):
    if not _DT_RE.fullmatch(text):
        return False
    try:
        datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return False
    if len(text) == 10:
        return True
    hour, minute, second = int(text[11:13]), int(text[14:16]), int(text[17:19])
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def classify_alphabet(letters):
    dna, rna = set("ACGTN"), set("ACGUN")
    protein = set("ACDEFGHIKLMNPQRSTVWYX")
    if letters <= dna:
        return "dna"
    if letters <= rna:
        return "rna"
    if letters <= protein and not letters <= dna and not letters <= rna:
        return "protein"
    return None


def infer_column(values, *, fasta_sequence=False):
    """类型推断。全部缺失返回 None，调用方拒绝。推断失败返回 'reject'。"""
    present = [value for value in values if value is not None]
    if not present:
        return None
    kinds = []
    for value in present:
        kind, _stored = _kind_and_value(value) if not _already_canonical(value) else (
            _canonical_kind(value), value)
        kinds.append(kind)
    if any(kind in {"nested", "bad"} for kind in kinds):
        return "reject"
    if all(kind == "bool" for kind in kinds):
        return "categorical"
    if all(kind == "int" for kind in kinds):
        return "integer"
    if all(kind in {"int", "float"} for kind in kinds):
        return "number"
    if all(kind == "datetime" for kind in kinds):
        return "datetime"
    if all(kind == "sequence" for kind in kinds):
        letters, lengths = set(), []
        for value in present:
            text = str(value).upper()
            letters.update(text)
            lengths.append(len(str(value)))
        alphabet = classify_alphabet(letters)
        if alphabet and (fasta_sequence or (quantiles(lengths) or {}).get("p50", 0) >= 12):
            return "sequence"
        if fasta_sequence:
            return "reject-alphabet"
    numeric = {"int", "float"}
    if any(kind in numeric for kind in kinds) and any(kind not in numeric for kind in kinds):
        return "reject"
    if any(kind == "datetime" for kind in kinds) and any(kind in numeric for kind in kinds):
        return "reject"
    distinct = {canonical(value) for value in present}
    if len(distinct) <= MAX_CATEGORY_LEVELS:
        return "categorical"
    return "text"


def _already_canonical(value):
    return value is None or isinstance(value, (bool, int, float, str))


def _canonical_kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float" if math.isfinite(value) else "bad"
    if isinstance(value, str):
        if value in {"true", "false"}:
            return "bool"
        if _INT_RE.fullmatch(value):
            return "int"
        if _NUM_RE.fullmatch(value):
            return "float"
        if _real_datetime(value):
            return "datetime"
        if _SEQ_RE.fullmatch(value):
            return "sequence"
        return "text"
    return "nested"


def _store_scalar(value):
    _kind, stored = _kind_and_value(value)
    if _kind == "nested":
        return "nested"
    if _kind == "bad":
        return "bad"
    return stored


def _header_is_data(header):
    """表头每个单元格都是整数或有限数时，视为没有表头。内核不起列名。"""
    def data_like(text):
        if _INT_RE.fullmatch(text):
            return True
        if not _NUM_RE.fullmatch(text):
            return False
        try:
            return math.isfinite(float(text))
        except ValueError:
            return False
    return bool(header) and all(data_like(name) for name in header)


def _parse_csv(text):
    if ";" in text and "," not in text or "\t" in text and "," not in text:
        raise ProtocolError("CSV 只接受逗号分隔")
    records = _csv_records(text)
    if not records:
        raise ProtocolError("CSV 缺少表头")
    header = [name.strip() for name in records[0]]
    if (not header or any(not name or "\n" in name or len(name) > 128 for name in header)
            or len(header) != len(set(header))):
        raise ProtocolError("CSV 表头为空或重复")
    if _header_is_data(header):
        raise ProtocolError("CSV 缺少表头")
    body = records[1:]
    if not body:
        raise ProtocolError("原始文件为空")
    rows = []
    for record in body:
        if len(record) != len(header):
            raise ProtocolError("CSV 只接受逗号分隔")
        values = {}
        for name, field in zip(header, record):
            stored = _store_scalar(field)
            if stored == "nested":
                raise ProtocolError(f"列无法推断类型: {name}")
            if stored == "bad":
                raise ProtocolError(f"列无法推断类型: {name}")
            values[name] = stored
        rows.append(values)
    return header, rows


def _csv_records(text):
    rows, fields, buf = [], [], []
    quoted = False
    field_quoted = False
    index = 0
    length = len(text)

    def flush_field():
        text_value = "".join(buf)
        fields.append(text_value if field_quoted else text_value.strip())
        buf.clear()

    def flush_row():
        nonlocal field_quoted
        if buf or fields or field_quoted:
            flush_field()
        if fields:
            rows.append(list(fields))
        fields.clear()
        field_quoted = False

    while index < length:
        char = text[index]
        if quoted:
            if char == '"':
                if index + 1 < length and text[index + 1] == '"':
                    buf.append('"')
                    index += 2
                    continue
                quoted = False
                index += 1
                continue
            buf.append(char)
            index += 1
            continue
        if char == '"':
            quoted = True
            field_quoted = True
            index += 1
            continue
        if char == ",":
            flush_field()
            field_quoted = False
            index += 1
            continue
        if char in "\n\r":
            if char == "\r" and index + 1 < length and text[index + 1] == "\n":
                index += 1
            flush_row()
            index += 1
            continue
        buf.append(char)
        index += 1
    if quoted:
        raise ProtocolError("CSV 缺少表头")
    if buf or fields:
        flush_row()
    return rows


def _parse_json(text):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise ProtocolError("JSON 顶层必须是对象数组") from None
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        if isinstance(payload, list) and payload and any(not isinstance(item, dict) for item in payload):
            raise ProtocolError("JSON 数组元素必须是对象")
        raise ProtocolError("JSON 顶层必须是对象数组")
    if not payload:
        raise ProtocolError("原始文件为空")
    columns = []
    for item in payload:
        for key in item:
            if key not in columns:
                columns.append(key)
    rows = []
    for item in payload:
        values = {}
        for name in columns:
            if name not in item or item[name] is None:
                values[name] = None
                continue
            if isinstance(item[name], (dict, list)):
                raise ProtocolError(f"JSON 含无法推断的嵌套值: {name}")
            stored = _store_scalar(item[name])
            if stored in {"nested", "bad"}:
                raise ProtocolError(f"列无法推断类型: {name}")
            values[name] = stored
        rows.append(values)
    return columns, rows


def _parse_fasta(text):
    records = []
    name, chunks = None, []

    def flush():
        if name is None:
            return
        if name == "":
            raise ProtocolError("序列标识为空")
        sequence = "".join(chunks).upper()
        if sequence == "":
            raise ProtocolError("序列文件含空序列")
        if len(sequence) > MAX_FASTA_SEQUENCE_CHARS:
            raise ProtocolError(f"序列超过 {MAX_FASTA_SEQUENCE_CHARS} 字符")
        records.append((name, sequence))

    for line in text.splitlines():
        if line.startswith(">"):
            flush()
            header = line[1:].strip()
            name = header.split()[0] if header else ""
            if name == "":
                raise ProtocolError("序列标识为空")
            chunks = []
        elif name is None:
            if line.strip():
                raise ProtocolError("文件魔数与已登记格式不一致")
        else:
            chunks.append(re.sub(r"\s+", "", line))
    flush()
    if not records:
        raise ProtocolError("序列文件没有记录")
    seen = set()
    rows = []
    for ident, sequence in records:
        if ident in seen:
            raise ProtocolError(f"序列标识重复: {ident}")
        seen.add(ident)
        rows.append({"id": ident, "sequence": sequence})
    return ["id", "sequence"], rows


def _parse_parquet(path):
    reader = _parquet_reader()
    if reader is None:
        raise ProtocolError("当前环境不能读取 Parquet")
    try:
        table = reader.read_table(path)
    except Exception as exc:
        raise ProtocolError("当前环境不能读取 Parquet") from exc
    columns = list(table.column_names)
    if not columns or table.num_rows == 0:
        raise ProtocolError("原始文件为空")
    rows = []
    data = table.to_pydict()
    for index in range(table.num_rows):
        values = {}
        for name in columns:
            value = data[name][index]
            if isinstance(value, (dict, list)):
                raise ProtocolError(f"Parquet 含无法展开的嵌套列: {name}")
            stored = _store_scalar(value)
            if stored in {"nested", "bad"}:
                raise ProtocolError(f"Parquet 含无法展开的嵌套列: {name}")
            values[name] = stored
        rows.append(values)
    return columns, rows


def _coerce_parser_table(parsed):
    if not isinstance(parsed, tuple) or len(parsed) != 2:
        raise ProtocolError("适配器未实现，不能读取")
    columns, rows = parsed
    if (not isinstance(columns, list) or not columns
            or any(type(name) is not str or not name for name in columns)
            or len(set(columns)) != len(columns)
            or not isinstance(rows, list) or not rows):
        raise ProtocolError("适配器未实现，不能读取")
    coerced = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(columns):
            raise ProtocolError("适配器未实现，不能读取")
        values = {}
        for name in columns:
            stored = _store_scalar(row[name])
            if stored in {"nested", "bad"}:
                raise ProtocolError(f"列无法推断类型: {name}")
            values[name] = stored
        coerced.append(values)
    return columns, coerced


def parse_bytes(path, adapter, raw, *, project=None):
    shape = adapter.get("record_shape")
    if adapter["id"] in SECOND_BATCH_IDS or shape not in {"table", "sequence"}:
        raise ProtocolError("适配器未实现，不能读取")
    if adapter["id"] == "parquet":
        if _parquet_reader() is None:
            raise ProtocolError("当前环境不能读取 Parquet")
        columns, rows = _parse_parquet(path)
    elif adapter["id"] in _PARSERS:
        text = _decode_utf8(raw)
        if adapter["id"] == "csv":
            columns, rows = _parse_csv(text)
        elif adapter["id"] == "json":
            columns, rows = _parse_json(text)
        elif adapter["id"] == "fasta":
            columns, rows = _parse_fasta(text)
        else:
            raise ProtocolError("适配器未实现，不能读取")
    else:
        key = None if project is None else (str(Path(project).resolve()), adapter["id"])
        parser = None if key is None else _BOUND_PARSERS.get(key)
        if parser is None:
            raise ProtocolError("适配器未实现，不能读取")
        try:
            parsed = parser(raw)
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("适配器未实现，不能读取") from None
        columns, rows = _coerce_parser_table(parsed)
    if len(rows) > MAX_ROWS:
        raise ProtocolError(f"行数超过 {MAX_ROWS}")
    if len(columns) > MAX_COLUMNS:
        raise ProtocolError(f"列数超过 {MAX_COLUMNS}")
    types = {}
    fasta = adapter["id"] == "fasta"
    for name in columns:
        values = [row[name] for row in rows]
        inferred = infer_column(values, fasta_sequence=(fasta and name == "sequence"))
        if inferred is None:
            raise ProtocolError(f"列全为缺失，无法推断类型: {name}")
        if inferred == "reject-alphabet":
            raise ProtocolError("序列字母表无法推断")
        if inferred == "reject":
            raise ProtocolError(f"列无法推断类型: {name}")
        limit = MAX_FASTA_SEQUENCE_CHARS if inferred == "sequence" else MAX_CELL_CHARS
        for value in values:
            if isinstance(value, str) and len(value) > limit:
                if inferred == "sequence":
                    raise ProtocolError(f"序列超过 {MAX_FASTA_SEQUENCE_CHARS} 字符")
                raise ProtocolError(f"单元格超过 {MAX_CELL_CHARS} 字符")
        types[name] = inferred
    return columns, rows, types


def _ratio(text):
    if isinstance(text, bool) or not isinstance(text, (str, int)):
        raise ProtocolError("切分比例最多 6 位小数")
    token = str(text)
    if not re.fullmatch(r"\d+(\.\d+)?", token):
        raise ProtocolError("切分比例必须为正且和为 1")
    fraction = token.split(".")[1] if "." in token else ""
    if len(fraction) > 6:
        raise ProtocolError("切分比例最多 6 位小数")
    number = Decimal(token)
    if number <= 0:
        raise ProtocolError("切分比例必须为正且和为 1")
    return f"{number:.6f}"


def normalize_ratios(train, dev, holdout):
    ratios = {"train": _ratio(train), "dev": _ratio(dev), "holdout": _ratio(holdout)}
    if sum(Decimal(value) for value in ratios.values()) != Decimal("1.000000"):
        raise ProtocolError("切分比例必须为正且和为 1")
    return ratios


def largest_remainder(count, ratios):
    names = ("holdout", "dev", "train")
    raw = [Decimal(ratios[name]) * count for name in names]
    base = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in raw]
    left = count - sum(base)
    order = sorted(range(3), key=lambda index: (-(raw[index] - base[index]), index))
    for index in order[:left]:
        base[index] += 1
    return dict(zip(names, base))


def _perm_key(seed, token):
    hashed = hashlib.sha256(f"{seed}:{token}".encode("utf-8")).hexdigest()
    return hashed, token


def _assign_ids(rows, id_column):
    seen = set()
    for index, row in enumerate(rows):
        if id_column is None:
            row_id = f"r{index:06d}"
        else:
            value = row["values"][id_column]
            if not (isinstance(value, str) or type(value) is int) or value == "":
                raise ProtocolError("id 列必须是无缺失的字符串或整数")
            row_id = value if isinstance(value, str) else str(value)
        if row_id in seen:
            raise ProtocolError("id 列取值重复" if id_column else "id 列取值重复")
        seen.add(row_id)
        row["id"] = row_id


def split_rows(rows, declaration, ratios, id_lists=None):
    count = len(rows)
    if count < 3:
        raise ProtocolError("行数不足以切成训练、开发、确认三份")
    quotas = largest_remainder(count, ratios)
    if any(quotas[name] <= 0 for name in quotas):
        raise ProtocolError("切分后某一份为空")
    strategy, seed = declaration["strategy"], declaration["seed"]
    column = declaration["column"]
    order = list(range(count))
    if strategy == "random":
        order.sort(key=lambda index: _perm_key(seed, str(index)))
        groups = {
            "holdout": order[:quotas["holdout"]],
            "dev": order[quotas["holdout"]:quotas["holdout"] + quotas["dev"]],
            "train": order[quotas["holdout"] + quotas["dev"]:],
        }
    elif strategy == "stratified":
        groups = _split_stratified(rows, column, seed, ratios, quotas)
    elif strategy == "group":
        groups = _split_group(rows, column, seed, quotas)
    elif strategy == "temporal":
        groups = _split_temporal(rows, column, quotas)
    else:
        groups = _split_id_list(rows, id_lists, quotas)
    if any(len(groups[name]) == 0 for name in ("train", "dev", "holdout")):
        raise ProtocolError("切分后某一份为空")
    return {name: [rows[index] for index in groups[name]] for name in ("train", "dev", "holdout")}


def _split_stratified(rows, column, seed, ratios, quotas):
    buckets = {}
    for index, row in enumerate(rows):
        buckets.setdefault(canonical(row["values"][column]), []).append(index)
    if len(buckets) > MAX_STRATA:
        raise ProtocolError(f"分层列基数超过 {MAX_STRATA}")
    if any(len(indexes) < 3 for indexes in buckets.values()):
        raise ProtocolError("分层列的某个取值不足以分到三份")
    assigned = {"holdout": [], "dev": [], "train": []}
    for indexes in buckets.values():
        indexes = sorted(indexes, key=lambda index: _perm_key(seed, str(index)))
        local = largest_remainder(len(indexes), ratios)
        cursor = 0
        for name in ("holdout", "dev", "train"):
            assigned[name].extend(indexes[cursor:cursor + local[name]])
            cursor += local[name]
    del quotas
    return assigned


def _split_group(rows, column, seed, quotas):
    buckets, tokens = {}, []
    for index, row in enumerate(rows):
        if row["values"][column] is None:
            raise ProtocolError("分组列含缺失，不能整组切分")
        token = canonical(row["values"][column])
        if token not in buckets:
            tokens.append(token)
            buckets[token] = []
        buckets[token].append(index)
    largest = max(quotas.values())
    if any(len(indexes) > largest for indexes in buckets.values()):
        raise ProtocolError("单个分组大于最大一份的行数配额")
    tokens.sort(key=lambda token: _perm_key(seed, token))
    counts = {name: 0 for name in ("holdout", "dev", "train")}
    assigned = {name: [] for name in counts}
    names = ("holdout", "dev", "train")
    for token in tokens:
        indexes = buckets[token]
        size = len(indexes)
        choices = [name for name in names if counts[name] + size <= quotas[name]]
        if not choices:
            raise ProtocolError("分组无法在不拆开的前提下满足比例")
        choice = min(choices, key=lambda name: (counts[name] / quotas[name], names.index(name)))
        assigned[choice].extend(indexes)
        counts[choice] += size
    return assigned


def _split_temporal(rows, column, quotas):
    if any(row["values"][column] is None for row in rows):
        raise ProtocolError("时间列含缺失或无法排序")
    order = sorted(range(len(rows)), key=lambda index: (rows[index]["values"][column], index))
    train_n, dev_n = quotas["train"], quotas["dev"]
    return {
        "train": order[:train_n],
        "dev": order[train_n:train_n + dev_n],
        "holdout": order[train_n + dev_n:],
    }


def _split_id_list(rows, lists, quotas):
    if not isinstance(lists, dict) or set(lists) != {"train", "dev", "holdout"}:
        raise ProtocolError("id 清单与数据行不能一一对应")
    by_id = {row["id"]: index for index, row in enumerate(rows)}
    assigned, seen = {}, set()
    for name in ("train", "dev", "holdout"):
        values = lists[name]
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ProtocolError("id 清单与数据行不能一一对应")
        if len(values) != quotas[name]:
            raise ProtocolError("id 清单的份数与声明比例不一致")
        indexes = []
        for item in values:
            if item in seen or item not in by_id:
                raise ProtocolError("id 清单与数据行不能一一对应")
            seen.add(item)
            indexes.append(by_id[item])
        assigned[name] = indexes
    if seen != set(by_id):
        raise ProtocolError("id 清单与数据行不能一一对应")
    return assigned


def _row_set_sha(holdout_rows):
    ordered = sorted(holdout_rows, key=lambda row: row["id"])
    return digest({"ids": [row["id"] for row in ordered],
                   "rows": [row["values"] for row in ordered]})


def _declaration_core(strategy, seed, ratios, column, id_column, id_lists_sha, target, source_note):
    if strategy not in STRATEGIES:
        raise ProtocolError("该切分策略必须声明列")
    if type(seed) is not int or not 0 <= seed <= SEED_MAX:
        raise ProtocolError(f"随机种子必须是 0 到 {SEED_MAX} 的整数")
    if not isinstance(source_note, str) or not 1 <= len(source_note) <= 500 or "\n" in source_note or "\r" in source_note:
        raise ProtocolError("来源说明必须是一行 1 到 500 字的文字")
    needs_column = strategy in COLUMN_STRATEGIES
    if needs_column and not isinstance(column, str):
        raise ProtocolError("该切分策略必须声明列")
    if not needs_column and column is not None:
        raise ProtocolError("该切分策略必须声明列")
    return {
        "strategy": strategy,
        "seed": seed,
        "ratios": ratios,
        "column": column if needs_column else None,
        "id_column": id_column,
        "id_lists_sha256": id_lists_sha,
        "target_column": target,
        "source_note": source_note,
    }


def _prepare_rows(columns, raw_rows, types, declaration):
    rows = [{"id": None, "values": {name: raw[name] for name in columns}} for raw in raw_rows]
    column = declaration["column"]
    if declaration["strategy"] in COLUMN_STRATEGIES and column not in columns:
        raise ProtocolError("该切分策略必须声明列")
    if declaration["strategy"] == "temporal" and types[column] not in {"datetime", "integer", "number"}:
        raise ProtocolError("时间列含缺失或无法排序")
    id_column = declaration["id_column"]
    if id_column is not None and id_column not in columns:
        raise ProtocolError("id 列必须是无缺失的字符串或整数")
    target = declaration["target_column"]
    if target is not None and target not in columns:
        raise ProtocolError(f"目标列不存在: {target}")
    _assign_ids(rows, id_column)
    return rows


def _sequence_profile(values):
    present = [value for value in values if isinstance(value, str)]
    letters = sorted({character for value in present for character in value.upper()})
    alphabet = classify_alphabet(set(letters))
    return {
        "alphabet": alphabet,
        "letters": "".join(letters),
        "length_quantiles": quantiles([len(value) for value in present]),
    }


def _column_stats(name, column_type, train_dev, target):
    values = [row["values"][name] for row in train_dev]
    missing = sum(value is None for value in values)
    total = len(train_dev)
    is_target = name == target
    present = [value for value in values if value is not None]
    entry = {
        "name": name,
        "type": column_type,
        "non_null_count": len(present),
        "missing_rate": missing / total,
        "cardinality": None if is_target else len({canonical(value) for value in present}),
        "quantiles": None,
        "top_categories": None,
        "sequence": None,
    }
    if is_target:
        return entry
    if column_type in {"integer", "number"}:
        entry["quantiles"] = quantiles(present)
    if column_type == "categorical":
        counts = Counter(canonical(value) for value in present)
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_CATEGORIES]
        decoded = []
        for key, count in ranked:
            value = next(item for item in present if canonical(item) == key)
            decoded.append({"value": value, "count": count})
        entry["top_categories"] = decoded
    if column_type == "sequence":
        entry["sequence"] = _sequence_profile(present)
    return entry


def compute_label_stats(log, columns, types, train_dev, target):
    """训练 + 开发上的标签分布和关联。封存提交前调用会拒绝，并且计入计数器。"""
    global label_stats_calls
    label_stats_calls += 1
    if log is None or not log.holdout_committed:
        raise ProtocolError("封存完成前不能计算标签分布或特征与标签的相关")
    total = len(train_dev)
    labels = [row["values"][target] for row in train_dev]
    present = [value for value in labels if value is not None]
    counts = Counter(canonical(value) for value in present)
    distribution = []
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        value = next(item for item in present if canonical(item) == key)
        distribution.append({"value": value, "count": count, "rate": count / total})
    associations = []
    for name in columns:
        if name == target:
            continue
        pairs = [(row["values"][name], row["values"][target]) for row in train_dev
                 if row["values"][name] is not None and row["values"][target] is not None]
        associations.append(_associate(name, types[name], types[target], pairs))
    return {
        "target_column": target,
        "scope": "train_dev",
        "distribution": distribution,
        "associations": associations,
    }


def _associate(name, feature_type, target_type, pairs):
    numeric = {"integer", "number"}
    method, value = None, None
    if feature_type in {"sequence", "text", "datetime"} or target_type in {"sequence", "text", "datetime"}:
        method = None
    elif feature_type in numeric and target_type in numeric:
        method = "pearson"
        value = _pearson([item[0] for item in pairs], [item[1] for item in pairs])
    elif ((feature_type == "categorical" and target_type in numeric)
          or (target_type == "categorical" and feature_type in numeric)):
        method = "correlation_ratio"
        if feature_type == "categorical":
            value = _correlation_ratio([item[0] for item in pairs], [item[1] for item in pairs])
        else:
            value = _correlation_ratio([item[1] for item in pairs], [item[0] for item in pairs])
    elif feature_type == "categorical" and target_type == "categorical":
        method = "cramers_v"
        value = _cramers_v([item[0] for item in pairs], [item[1] for item in pairs])
    return {"feature": name, "method": method, "n_pairs": len(pairs), "value": value}


def _pearson(left, right):
    count = len(left)
    if count < 3:
        return None
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    var_left = sum((item - mean_left) ** 2 for item in left) / count
    var_right = sum((item - mean_right) ** 2 for item in right) / count
    if var_left == 0 or var_right == 0:
        return None
    dot = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    norm_left = math.sqrt(sum((item - mean_left) ** 2 for item in left))
    norm_right = math.sqrt(sum((item - mean_right) ** 2 for item in right))
    value = dot / (norm_left * norm_right)
    if not math.isfinite(value):
        return None
    return value


def _correlation_ratio(categories, numbers):
    count = len(numbers)
    if count == 0:
        return None
    mean = sum(numbers) / count
    total = sum((item - mean) ** 2 for item in numbers)
    if total == 0:
        return None
    groups = {}
    for category, number in zip(categories, numbers):
        groups.setdefault(canonical(category), []).append(number)
    within = 0
    for values in groups.values():
        center = sum(values) / len(values)
        within += sum((item - center) ** 2 for item in values)
    return 1 - within / total


def _cramers_v(left, right):
    count = len(left)
    if count == 0:
        return None
    table = {}
    for a, b in zip(left, right):
        table.setdefault(canonical(a), Counter())[canonical(b)] += 1
    row_keys, col_keys = list(table), sorted({key for counts in table.values() for key in counts})
    row_sums = {key: sum(table[key].values()) for key in row_keys}
    col_sums = {key: sum(table[row][key] for row in row_keys) for key in col_keys}
    freedom = min(len(row_keys) - 1, len(col_keys) - 1)
    if freedom <= 0:
        return None
    chi2 = 0
    for row in row_keys:
        for col in col_keys:
            expected = row_sums[row] * col_sums[col] / count
            observed = table[row][col]
            if expected:
                chi2 += (observed - expected) ** 2 / expected
    return math.sqrt(chi2 / (count * freedom))


def build_data_card(raw_sha, seal_sha, columns, types, parts, target, label_stats):
    train_dev = parts["train"] + parts["dev"]
    entries = [_column_stats(name, types[name], train_dev, target) for name in columns]
    duplicate = 0
    seen = Counter(canonical(row["values"]) for row in train_dev)
    duplicate = sum(count - 1 for count in seen.values() if count > 1)
    id_like = []
    for entry in entries:
        if (entry["name"] != target and entry["missing_rate"] == 0
                and entry["cardinality"] == len(train_dev)):
            id_like.append(entry["name"])
    if target is not None and label_stats is not None:
        target_entry = next(entry for entry in entries if entry["name"] == target)
        if (target_entry["missing_rate"] == 0
                and len(label_stats["distribution"]) == len(train_dev)):
            id_like.append(target)
    targets = [entry["name"] for entry in entries
               if entry["type"] in {"integer", "number", "categorical"}
               and entry["name"] not in id_like]
    feature_types = {entry["name"]: entry["type"] for entry in entries if entry["name"] != target}
    return {
        "schema": "popper.data-card/v1",
        "raw_sha256": raw_sha,
        "seal_sha256": seal_sha,
        "profile_scope": "train_dev",
        "n_rows": len(train_dev) + len(parts["holdout"]),
        "n_train": len(parts["train"]),
        "n_dev": len(parts["dev"]),
        "n_holdout": len(parts["holdout"]),
        "n_columns": len(columns),
        "duplicate_row_count": duplicate,
        "id_column": None,
        "target_column": target,
        "columns": entries,
        "candidate_id_columns": id_like,
        "candidate_target_columns": targets,
        "holdout": {"n_rows": len(parts["holdout"]), "feature_types": feature_types},
        "label_stats": label_stats,
    }


def _recover(directory, log):
    seal = directory / "holdout-seal.json"
    sealed = log.event("holdout_sealed")
    if seal.is_file() and sealed is None:
        seal.unlink()
        return
    if sealed is not None:
        if not seal.is_file() or file_hash(seal) != sealed["payload"]["seal_sha256"]:
            raise ProtocolError("接入事件链与封存件不一致")


def _write_card(log, directory, entity_id, raw_sha, seal_sha, columns, types, parts,
                declaration, target):
    stats = None
    if target is not None:
        stats = compute_label_stats(log, columns, types, parts["train"] + parts["dev"], target)
    card = build_data_card(raw_sha, seal_sha, columns, types, parts, target, stats)
    card["id_column"] = declaration["id_column"]
    card_sha = digest(card)
    write_json(directory / "data-card.json", card)
    seq = log.append(entity_id, "data_card_written",
                     {"data_card_sha256": card_sha, "seal_sha256": seal_sha})
    log.commit()
    return card_sha, seq


def unbudgeted_compile_count(project):
    """recipe_compiled 里 budget_reserved 为 false 的次数。没有接入库时是 0。"""
    path = Path(project) / ".popper-intake" / "intake.sqlite"
    if not path.is_file():
        return 0
    log = IntakeLog(path)
    try:
        return _unbudgeted_in(log)
    finally:
        log.close()


def _unbudgeted_in(log):
    return sum(1 for item in log.events()
               if item["event_type"] == "recipe_compiled"
               and item["payload"].get("budget_reserved") is False)


def assert_study_budget_floor(project, budget_cap):
    """未入账编译次数计入 Study 预算下限。超出则拒绝。"""
    extra = unbudgeted_compile_count(project)
    if extra and float(budget_cap) < STUDY_BUDGET_FLOOR + extra:
        raise ProtocolError("未入账编译次数超过 Study 预算下限")


def record_study_budget(project, *, budget_cap, family_id, study_id, run_dir):
    """让后续编译能读到 Study 预算。只在接入目录已经存在时由 research init 调用。"""
    project = _project_dir(project)
    directory = project / ".popper-intake"
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "study-budget.json", {
        "schema": "popper.intake-study-budget/v1",
        "budget_cap": float(budget_cap),
        "family_id": family_id,
        "study_id": study_id,
        "run_dir": str(Path(run_dir).resolve()),
    })


def load_study_budget(project):
    path = Path(project) / ".popper-intake" / "study-budget.json"
    if not path.is_file():
        return None
    data = read_json(path)
    required = {"schema", "budget_cap", "family_id", "study_id", "run_dir"}
    if (not isinstance(data, dict) or set(data) != required
            or data.get("schema") != "popper.intake-study-budget/v1"
            or not isinstance(data.get("study_id"), str) or not data["study_id"]
            or not isinstance(data.get("family_id"), str) or not data["family_id"]
            or not isinstance(data.get("run_dir"), str) or not data["run_dir"]):
        raise ProtocolError("Study 预算账本无法读取")
    cap = data["budget_cap"]
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap):
        raise ProtocolError("Study 预算账本无法读取")
    return data


def link_parent_run(project, parent_run_dir):
    """后继项目记下父研究运行目录。封存时用它复用 followup 的重叠检查。"""
    project = _project_dir(project)
    parent = Path(parent_run_dir).resolve()
    if not (parent / "research.json").is_file() or not (parent / "research.sqlite").is_file():
        raise ProtocolError("父研究未初始化")
    directory = project / ".popper-intake"
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "parent-run.json", {
        "schema": "popper.intake-parent/v1",
        "parent_run_dir": str(parent),
    })


def _holdout_as_pack_rows(holdout, pack):
    fields = tuple(pack.input_fields) + (pack.target_field,)
    rows = []
    for row in holdout:
        values = row["values"]
        built = {"id": row["id"]}
        if all(name in values for name in fields):
            for name in fields:
                built[name] = values[name]
        rows.append(built)
    return rows


def _reject_followup_holdout_overlap(project, holdout_rows):
    path = Path(project) / ".popper-intake" / "parent-run.json"
    if not path.is_file():
        return
    link = read_json(path)
    if (not isinstance(link, dict) or set(link) != {"schema", "parent_run_dir"}
            or link.get("schema") != "popper.intake-parent/v1"
            or not isinstance(link.get("parent_run_dir"), str)):
        raise ProtocolError("父研究未初始化")
    from .followup import assert_new_confirmation_disjoint, parent_split_rows
    pack, prior_rows = parent_split_rows(link["parent_run_dir"])
    assert_new_confirmation_disjoint(prior_rows, _holdout_as_pack_rows(holdout_rows, pack), pack)


def intake(project, *, raw, split_strategy, seed, train_ratio, dev_ratio, holdout_ratio,
           source_note, format=None, column=None, id_column=None, id_lists=None,
           target_column=None, convert_unknown_to_csv=False):
    """哈希原始文件、切分、封存，提交 holdout_sealed 之后才写数据卡。"""
    project = _project_dir(project)
    directory = project / ".popper-intake"
    directory.mkdir(parents=True, exist_ok=True)
    log = IntakeLog(directory / "intake.sqlite")
    try:
        _recover(directory, log)
        path = _inside(project, raw)
        raw_bytes = path.read_bytes()
        raw_sha = hashlib.sha256(raw_bytes).hexdigest()
        if len(raw_bytes) == 0:
            raise ProtocolError("原始文件为空")
        if len(raw_bytes) > MAX_SOURCE_BYTES:
            raise ProtocolError(f"原始文件超过 {MAX_SOURCE_BYTES} 字节")
        adapter = resolve_adapter(path, format, raw_bytes,
                                  convert_unknown_to_csv=convert_unknown_to_csv,
                                  registry=load_adapter_registry(project))
        ratios = normalize_ratios(train_ratio, dev_ratio, holdout_ratio)
        lists_sha = None
        parsed_lists = None
        if split_strategy == "id_list":
            if id_lists is None:
                raise ProtocolError("id 清单与数据行不能一一对应")
            list_path = _inside(project, id_lists)
            lists_sha = file_hash(list_path)
            parsed_lists = read_json(list_path)
        elif id_lists is not None:
            raise ProtocolError("id 清单与数据行不能一一对应")
        declaration = _declaration_core(
            split_strategy, seed, ratios, column, id_column, lists_sha, target_column, source_note)
        columns, raw_rows, types = parse_bytes(path, adapter, raw_bytes, project=project)
        rows = _prepare_rows(columns, raw_rows, types, declaration)
        parts = split_rows(rows, declaration, ratios, parsed_lists)
        declaration_sha = digest(declaration)
        sealed = log.event("holdout_sealed")
        if sealed is not None:
            return _reuse_or_reject(
                log, directory, project, raw_sha, declaration, declaration_sha, sealed,
                columns, types, parts, adapter)
        _reject_followup_holdout_overlap(project, parts["holdout"])
        seal_body = {
            "schema": "popper.holdout-seal/v1",
            "raw_sha256": raw_sha,
            "split_declaration_sha256": declaration_sha,
            "split_declaration": declaration,
            "id_column": id_column,
            "train_ids": [row["id"] for row in parts["train"]],
            "dev_ids": [row["id"] for row in parts["dev"]],
            "holdout_ids": [row["id"] for row in parts["holdout"]],
            "holdout_rows": [{"id": row["id"], "values": row["values"]} for row in parts["holdout"]],
            "row_set_sha256": _row_set_sha(parts["holdout"]),
        }
        payload = (canonical(seal_body) + "\n").encode("utf-8")
        seal_sha = hashlib.sha256(payload).hexdigest()
        temporary = directory / "holdout-seal.json.tmp"
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        log.append(raw_sha, "dataset_hashed", {
            "raw_sha256": raw_sha,
            "byte_length": len(raw_bytes),
            "format": adapter["id"],
            "source_note_sha256": digest(source_note),
        })
        sealed_seq = log.append(raw_sha, "holdout_sealed", {
            "seal_sha256": seal_sha,
            "split_declaration_sha256": declaration_sha,
            "n_train": len(parts["train"]),
            "n_dev": len(parts["dev"]),
            "n_holdout": len(parts["holdout"]),
            "row_set_sha256": seal_body["row_set_sha256"],
        })
        os.replace(temporary, directory / "holdout-seal.json")
        note_seal_point()
        log.commit()
        card_sha, _card_seq = _write_card(
            log, directory, raw_sha, raw_sha, seal_sha, columns, types, parts,
            declaration, target_column)
        return {
            "intake_dir": ".popper-intake",
            "raw_sha256": raw_sha,
            "seal_sha256": seal_sha,
            "data_card_sha256": card_sha,
            "holdout_sealed_seq": sealed_seq,
        }
    finally:
        log.close()


def _idempotent(raw_sha, sealed, card):
    return {
        "intake_dir": ".popper-intake",
        "raw_sha256": raw_sha,
        "seal_sha256": sealed["payload"]["seal_sha256"],
        "data_card_sha256": digest(card),
        "holdout_sealed_seq": sealed["seq"],
    }


def _reuse_or_reject(log, directory, project, raw_sha, declaration, declaration_sha, sealed,
                     columns, types, parts, adapter):
    del project, adapter, declaration_sha
    seal = read_json(directory / "holdout-seal.json")
    saved = seal["split_declaration"]
    if seal["raw_sha256"] != raw_sha:
        raise ProtocolError("这份研究已经封存确认集；重新切分必须新建 Study")
    card_path = directory / "data-card.json"
    card = read_json(card_path) if card_path.is_file() else None
    saved_target = card["target_column"] if card is not None else saved.get("target_column")
    saved_rest = {key: value for key, value in saved.items() if key != "target_column"}
    new_rest = {key: value for key, value in declaration.items() if key != "target_column"}
    if saved_rest != new_rest:
        raise ProtocolError("这份研究已经封存确认集；重新切分必须新建 Study")
    if card is not None and declaration["target_column"] == saved_target:
        return _idempotent(raw_sha, sealed, card)
    if saved_target is not None and declaration["target_column"] != saved_target:
        raise ProtocolError("目标列已经写入数据卡；更改目标列必须新建 Study")
    card_sha, _seq = _write_card(
        log, directory, raw_sha, raw_sha, sealed["payload"]["seal_sha256"], columns, types,
        parts, declaration, declaration["target_column"])
    del card_sha
    return _idempotent(raw_sha, sealed, read_json(card_path))


def import_intake_events(store, project):
    """把接入事件按 seq 抄进研究库并 verify。没有接入库时什么都不做。"""
    path = Path(project) / ".popper-intake" / "intake.sqlite"
    if not path.is_file():
        return
    log = IntakeLog(path)
    try:
        copied = log.events()
    finally:
        log.close()
    if not copied:
        return

    def _do():
        for item in copied:
            store._append("dataset", item["entity_id"], item["event_type"],
                          item["payload"], "research_store")
        report = store.verify()
        if not report["ok"]:
            raise ProtocolError("接入事件链与封存件不一致")

    store._transaction(_do)


def load_intake(project):
    project = _project_dir(project)
    directory = project / ".popper-intake"
    seal_path = directory / "holdout-seal.json"
    card_path = directory / "data-card.json"
    if not seal_path.is_file() or not card_path.is_file():
        raise ProtocolError("接入事件链与封存件不一致")
    return read_json(seal_path), read_json(card_path), directory
