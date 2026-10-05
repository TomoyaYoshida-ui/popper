"""一次批准的自治包络：限定开发集实验可以自动进行到哪里。

包络只放行开发集上的实现、修复和执行。独立确认、对外提交和包络外的文件写入
始终需要单独审批。包络不是科研事实，Research Kernel 仍是唯一裁决来源。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core import ProtocolError, canonical, digest, read_json, write_json

SCHEMA = "1.0"
ALLOWED_ACTIONS = frozenset({"implement", "repair", "execute_dev"})
DEV_TOOLS = frozenset({
    "research.create_hypothesis", "research.create_design", "research.freeze_design",
    "research.execute_design", "research.study_action", "research.revision_create",
    "research.revision_execute", "research.revision_commit", "research.revision_run",
    "workspace.file_replace",
})


def build_envelope(study_id, *, max_dev_runs, max_model_tokens, max_wall_clock_minutes,
                   code_files, state_version, allowed_actions=None, approved_at=None):
    """规范化包络并计算稳定摘要。摘要不包含自身。"""
    actions = tuple(sorted(allowed_actions or ALLOWED_ACTIONS))
    if not isinstance(study_id, str) or not study_id.startswith("S-"):
        raise ProtocolError("自治包络必须绑定已登记的 Study")
    if type(max_dev_runs) is not int or not 1 <= max_dev_runs <= 100:
        raise ProtocolError("开发集运行上限须为 1 到 100 的整数")
    if type(max_model_tokens) is not int or not 1 <= max_model_tokens <= 100_000_000:
        raise ProtocolError("模型 token 上限不合法")
    if type(max_wall_clock_minutes) is not int or not 1 <= max_wall_clock_minutes <= 24 * 60:
        raise ProtocolError("运行时长上限须为 1 到 1440 分钟")
    if type(state_version) is not int or state_version < 1:
        raise ProtocolError("自治包络必须绑定研究状态版本")
    if (not isinstance(code_files, (list, tuple)) or not code_files
            or any(not isinstance(name, str) or name.startswith(("/", "\\")) or ".." in Path(name).parts
                   or not name.endswith(".py") for name in code_files)):
        raise ProtocolError("可修改文件必须是项目内的相对 Python 路径")
    if any(action not in ALLOWED_ACTIONS for action in actions) or "confirm" in actions:
        raise ProtocolError("自治包络不能包含独立确认")
    body = {
        "schema_version": SCHEMA, "study_id": study_id, "state_version": state_version,
        "max_dev_runs": max_dev_runs, "max_model_tokens": max_model_tokens,
        "max_wall_clock_minutes": max_wall_clock_minutes,
        "code_files": tuple(sorted(set(code_files))), "allowed_actions": actions,
        "approved_at": approved_at or datetime.now(timezone.utc).isoformat(),
    }
    return {**body, "envelope_sha256": digest(body)}


def save_envelope(run_dir, envelope):
    path = Path(run_dir) / "envelope.json"
    current = build_envelope(
        envelope["study_id"], max_dev_runs=envelope["max_dev_runs"],
        max_model_tokens=envelope["max_model_tokens"],
        max_wall_clock_minutes=envelope["max_wall_clock_minutes"],
        code_files=envelope["code_files"], state_version=envelope["state_version"],
        allowed_actions=envelope["allowed_actions"], approved_at=envelope["approved_at"])
    if current["envelope_sha256"] != envelope["envelope_sha256"]:
        raise ProtocolError("自治包络摘要与内容不一致")
    write_json(path, current)
    return current


def load_envelope(run_dir):
    path = Path(run_dir) / "envelope.json"
    if not path.is_file():
        return None
    return read_json(path)


def write_usage(run_dir, *, dev_runs_used, tokens_used=0):
    """记录已消耗的开发运行。用量不进入包络摘要，避免每次执行都改写批准内容。"""
    if type(dev_runs_used) is not int or dev_runs_used < 0:
        raise ProtocolError("开发运行用量必须是非负整数")
    if type(tokens_used) is not int or tokens_used < 0:
        raise ProtocolError("token 用量必须是非负整数")
    write_json(Path(run_dir) / "envelope-usage.json",
               {"dev_runs_used": dev_runs_used, "tokens_used": tokens_used})


def read_usage(run_dir):
    path = Path(run_dir) / "envelope-usage.json"
    if not path.is_file():
        return {"dev_runs_used": 0, "tokens_used": 0}
    usage = read_json(path)
    return {"dev_runs_used": usage.get("dev_runs_used", 0),
            "tokens_used": usage.get("tokens_used", 0)}


def assert_active(envelope, *, dev_runs_used, state_version, tokens_used=0, now=None):
    """执行下一步之前核对包络仍有效。超限、过期或研究版本变化都拒绝。"""
    if envelope is None:
        raise ProtocolError("没有生效的自治包络")
    expected = build_envelope(
        envelope["study_id"], max_dev_runs=envelope["max_dev_runs"],
        max_model_tokens=envelope["max_model_tokens"],
        max_wall_clock_minutes=envelope["max_wall_clock_minutes"],
        code_files=envelope["code_files"], state_version=envelope["state_version"],
        allowed_actions=envelope["allowed_actions"], approved_at=envelope["approved_at"])
    if expected["envelope_sha256"] != envelope["envelope_sha256"]:
        raise ProtocolError("自治包络内容与摘要不一致")
    if state_version != envelope["state_version"]:
        raise ProtocolError("研究状态版本已变化，自治包络失效")
    if dev_runs_used >= envelope["max_dev_runs"]:
        raise ProtocolError("已达到自治包络的开发集运行上限")
    if tokens_used > envelope["max_model_tokens"]:
        raise ProtocolError("已超过自治包络的模型 token 上限")
    approved = datetime.fromisoformat(envelope["approved_at"])
    current = now or datetime.now(timezone.utc)
    if current > approved + timedelta(minutes=envelope["max_wall_clock_minutes"]):
        raise ProtocolError("自治包络已超过批准的运行时长")
    return envelope


def covers_tool(envelope, tool_name, arguments):
    """包络能否自动批准这个工具。确认类操作永远不能。"""
    if not isinstance(arguments, dict):
        return False
    if "confirm" in tool_name or tool_name not in DEV_TOOLS:
        return False
    if "confirm" in canonical(arguments):
        return False
    if tool_name == "research.study_action" and arguments.get("action") != "run":
        return False
    if tool_name == "workspace.file_replace" and arguments.get("path") not in envelope["code_files"]:
        return False
    return True


def covering_envelope(root, tool_name, arguments, *, now=None):
    """在工作区已登记的研究运行里找一份仍有效、且覆盖该工具的包络。"""
    base = Path(root) / ".popper" / "research-runs"
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*/envelope.json")):
        try:
            envelope = read_json(path)
            usage = read_usage(path.parent)
            assert_active(envelope, dev_runs_used=usage["dev_runs_used"],
                          tokens_used=usage["tokens_used"],
                          state_version=envelope["state_version"], now=now)
        except (ProtocolError, OSError, ValueError, KeyError):
            continue
        if covers_tool(envelope, tool_name, arguments):
            return envelope
    return None
