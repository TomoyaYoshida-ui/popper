"""Application tool schemas; no research-state ownership."""
import re

from .core import ProtocolError, portable_path
from .identity import EntityKind, parse_id


REVISION_RISKS = {"research.revision_preview": "disclose", "research.revision_create": "write",
                  "research.revision_execute": "write", "research.revision_job": "disclose",
                  "research.revision_commit": "write", "research.revision_run": "write"}
ALIASES = {name.replace(".", "_"): name for name in REVISION_RISKS}
SPEC_FIELDS = {"study_id", "expected_state_version", "hypothesis_id", "design_id",
               "path", "expected_sha256", "replacement"}
SPEC_COMMON_FIELDS = SPEC_FIELDS - {"replacement"}


def validate_edit(spec):
    """Validate source input without silently choosing one editing format."""
    if ("replacement" in spec) == ("edits" in spec):
        raise ProtocolError("Revision requires exactly one of replacement or edits")
    if "replacement" in spec:
        if not isinstance(spec["replacement"], str) or len(spec["replacement"].encode("utf-8")) > 65536:
            raise ProtocolError("Revision replacement exceeds 64 KiB")
        return
    edits = spec["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= 20:
        raise ProtocolError("Revision SEARCH/REPLACE requires 1 to 20 edits")
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != {"search", "replace"}
                or not isinstance(edit["search"], str) or not edit["search"]
                or not isinstance(edit["replace"], str)):
            raise ProtocolError("invalid Revision SEARCH/REPLACE edit")
    if sum(len(value.encode("utf-8")) for edit in edits for value in edit.values()) > 65536:
        raise ProtocolError("Revision SEARCH/REPLACE exceeds 64 KiB")


def validate(tool, args):
    if not isinstance(args, dict):
        raise ProtocolError("Revision arguments must be an object")
    if tool in {"research.revision_preview", "research.revision_create"}:
        validate_edit(args)
        fields = SPEC_COMMON_FIELDS | ({"replacement"} if "replacement" in args else {"edits"})
        fields |= {"approval_sha256"} if tool.endswith("create") else set()
        if (set(args) not in (fields, fields | {"parent_revision_id"})
                or type(args.get("expected_state_version")) is not int or args["expected_state_version"] < 1):
            raise ProtocolError("invalid Revision proposal/version")
        if "parent_revision_id" in args:
            parse_id(args["parent_revision_id"], EntityKind.REVISION)
        parse_id(args["study_id"], EntityKind.STUDY)
        parse_id(args["hypothesis_id"], EntityKind.HYPOTHESIS)
        parse_id(args["design_id"], EntityKind.DESIGN)
        if not isinstance(args["path"], str) or len(args["path"]) > 512 or portable_path(args["path"]) != args["path"]:
            raise ProtocolError("invalid Revision path")
        hashes = ["expected_sha256"] + (["approval_sha256"] if tool.endswith("create") else [])
    elif tool in {"research.revision_commit", "research.revision_run"}:
        reference = "preview_call_id" if tool.endswith("commit") else "create_call_id"
        if set(args) != {"study_id", reference}:
            raise ProtocolError("invalid Revision reference arguments")
        parse_id(args["study_id"], EntityKind.STUDY)
        parse_id(args[reference], EntityKind.TOOL_CALL)
        hashes = []
    elif tool == "research.revision_execute":
        if set(args) != {"study_id", "revision_id", "manifest_sha256", "expected_state_version"}:
            raise ProtocolError("invalid Revision execution arguments")
        parse_id(args["study_id"], EntityKind.STUDY)
        parse_id(args["revision_id"], EntityKind.REVISION)
        if type(args["expected_state_version"]) is not int or args["expected_state_version"] < 1:
            raise ProtocolError("invalid Study version")
        hashes = ["manifest_sha256"]
    elif tool == "research.revision_job":
        if set(args) != {"study_id", "job_id"} or not isinstance(args["job_id"], str) or not re.fullmatch(r"JOBR-[0-9a-f]{24}", args["job_id"]):
            raise ProtocolError("invalid Revision Job arguments")
        parse_id(args["study_id"], EntityKind.STUDY)
        hashes = []
    else:
        raise ProtocolError("unknown Revision tool")
    for field in hashes:
        if not isinstance(args[field], str) or not re.fullmatch(r"[0-9a-f]{64}", args[field]):
            raise ProtocolError("invalid Revision digest")
    return args


def specification(args):
    fields = SPEC_COMMON_FIELDS | ({"replacement"} if "replacement" in args else {"edits"})
    fields |= {"parent_revision_id"} if "parent_revision_id" in args else set()
    return {field: args[field] for field in fields}


def _schema(properties):
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}


_ID = {"type": "string", "minLength": 1}
_SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_VERSION = {"type": "integer", "minimum": 1}
_SPEC = {"study_id": _ID, "expected_state_version": _VERSION, "hypothesis_id": _ID,
         "design_id": _ID, "path": _ID, "expected_sha256": _SHA,
         "replacement": {"type": "string", "maxLength": 65536},
         "edits": {"type": "array", "minItems": 1, "maxItems": 20,
                   "items": _schema({"search": {"type": "string", "minLength": 1},
                                     "replace": {"type": "string"}})}}
TOOL_DEFINITIONS = []
LEGACY_TOOL_DEFINITIONS = []
for name, description, properties in (
    ("research_revision_preview", "预览不可变源码 Revision 差异；replacement 全文或 edits 精确 SEARCH/REPLACE 二选一，最多20项，每项须唯一匹配，禁止模糊匹配。须批准读取；不修改文件、不执行实验。后续以本工具 call_id 提议 revision_commit。", _SPEC),
    ("research_revision_commit", "引用本会话已完成 revision_preview 的 call_id 提议创建不可变 Agent Revision；服务器绑定完整源码、版本及摘要，仍须独立创建审批，不执行实验。",
     {"study_id": _ID, "preview_call_id": _ID}),
    ("research_revision_run", "引用本会话已完成 revision_commit（或旧 revision_create）的 call_id 提议执行不可变 Revision；仍须独立执行审批，开发失败消耗预算，返回入队 Job 不等于完成。",
     {"study_id": _ID, "create_call_id": _ID}),
    ("research_revision_create", "按已审查的提案摘要创建 Agent 作者的不可变 Revision；须独立审批，不覆盖冻结源码、不执行实验。",
     {**_SPEC, "approval_sha256": _SHA}),
    ("research_revision_execute", "提议执行不可变 Revision，须再次审批；消耗开发预算，失败同样计入。结果是 Job 入队，不代表实验成功。",
     {"study_id": _ID, "revision_id": _ID, "manifest_sha256": _SHA, "expected_state_version": _VERSION}),
    ("research_revision_job", "读取指定 Study 的 Revision Job 真实状态及开发 Observation；批准后才向模型披露，不运行实验。",
     {"study_id": _ID, "job_id": _ID}),
):
    definitions = LEGACY_TOOL_DEFINITIONS if name in {"research_revision_create", "research_revision_execute"} else TOOL_DEFINITIONS
    definitions.append({"type": "function", "function": {
        "name": name, "description": description, "parameters": _schema(properties)}})
    if name in {"research_revision_preview", "research_revision_create"}:
        parameters = definitions[-1]["function"]["parameters"]
        parameters["required"] = sorted(SPEC_COMMON_FIELDS | ({"approval_sha256"} if name.endswith("create") else set()))
        parameters["oneOf"] = [{"required": ["replacement"], "not": {"required": ["edits"]}},
                               {"required": ["edits"], "not": {"required": ["replacement"]}}]
        parameters["properties"] = {
            **properties, "parent_revision_id": {**_ID, "description": "仅修复终态失败的同一设计 Revision 时提供；不是成功后调参入口。"}}
