"""GPU/CPU worker 的不可变提交与回执契约。"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

from ...core import ProtocolError, digest, portable_path


# Job manifest 终态集合；任一终态都可被 collect() 校验（无需再执行）。
TERMINAL_JOB_STATUSES = frozenset({
    "succeeded", "implementation_failed", "infrastructure_failed", "cancelled"})

# worker 工作区布局：输入制品放在 inputs/，候选输出放在 outputs/。
INPUT_DIR = "inputs/"
OUTPUT_DIR = "outputs/"


def job_invocation(pack, seed):
    """按 worker 工作区布局解析域包声明的调用契约 → (args, outputs)。

    控制器、holdout runner 与 bundle 校验都从这里取，避免出现第二份 CLI 字面量。
    """
    invocation = pack.invocation()
    values = {role: INPUT_DIR + basename for role, basename in invocation.inputs}
    values["prediction"] = OUTPUT_DIR + invocation.prediction_name(seed)
    values["seed"] = str(seed)
    return invocation.render(values), (values["prediction"],)


# 输入角色 → InputArtifact.role 标签（唯一来源）：角色是调用契约的一部分，
# 标签只用于审计展示，二者必须一起从声明派生，避免出现第二份字面量。
INPUT_ROLE_LABELS = {"train": "training_data",
                     "inputs": "development_features_without_labels",
                     "config": "frozen_intervention"}


def staged_input_artifacts(invocation, sources):
    """按声明的输入角色与落位名构造 InputArtifact tuple（顺序 = 声明顺序）。

    sources: {role: (源路径, sha256)}，只对声明的角色取值。
    声明了角色却没有源 → ProtocolError（绝不静默落位一个候选收不到的文件）。
    有源但未声明 → 忽略（未声明的输入不该出现在工作区）。
    """
    artifacts = []
    for role, basename in invocation.inputs:
        if role not in INPUT_ROLE_LABELS:
            raise ProtocolError(f"未支持的调用输入角色: {role!r}")
        if role not in sources:
            raise ProtocolError(f"调用输入角色 {role!r} 缺少落位来源")
        source, sha256 = sources[role]
        if not isinstance(sha256, str) or not sha256:
            raise ProtocolError(f"调用输入角色 {role!r} 的来源必须绑定 SHA-256")
        artifacts.append(InputArtifact(str(source), basename, sha256, INPUT_ROLE_LABELS[role]))
    return tuple(artifacts)


@runtime_checkable
class Worker(Protocol):
    """统一 worker 接口：本地与远程后端共享的传输无关契约。

    控制器只依赖这里的方法，绝不触碰某个后端的文件系统布局（如 LocalWorker.root）；
    于是本地执行与 `RemoteWorker`（REST 指向盒子上跑的 worker 服务端）可无感互换。
    """

    def submit(self, spec: "JobSpec"): ...
    def launch(self, spec: "JobSpec"): ...
    def poll(self, job_id: str): ...
    def collect(self, job_id: str): ...
    def cancel(self, job_id: str): ...
    def failure_context(self, job_id: str, max_bytes: int = 8192): ...
    def reap_stale(self, job_ids=None, stale_seconds: float = 20.0): ...
    def list_jobs(self): ...
    def list_receipts(self): ...
    def fetch_artifact(self, job_id: str, name: str, destination): ...
    def read_manifest(self, job_id: str): ...
    def read_workspace_file(self, job_id: str, name: str): ...


@dataclass(frozen=True)
class InputArtifact:
    source: str
    target: str
    sha256: str
    role: str

    def __post_init__(self):
        normalized = portable_path(self.target)
        if normalized is None:
            raise ProtocolError("InputArtifact target 必须是安全相对路径")
        # 存归一后的形式：worker 拿它拼 workspace，两侧必须是同一个位置
        object.__setattr__(self, "target", normalized)
        if len(self.sha256) != 64 or not self.role:
            raise ProtocolError("InputArtifact 必须绑定 SHA-256 与 role")


@dataclass(frozen=True)
class JobSpec:
    idempotency_key: str
    revision_id: str
    revision_manifest_sha256: str
    design_id: str
    entrypoint: str
    args: tuple = field(default_factory=tuple)
    inputs: tuple = field(default_factory=tuple)
    outputs: tuple = field(default_factory=tuple)
    timeout_seconds: float = 300.0
    mem_limit_mb: int | None = None
    cpu_time_seconds: int | None = None
    max_processes: int = 4
    gpu_count: int = 0
    require_edit_coverage: bool = False
    sandboxed: bool = True

    def __post_init__(self):
        if type(self.require_edit_coverage) is not bool:
            raise ProtocolError("require_edit_coverage 必须为布尔值")
        if type(self.sandboxed) is not bool:
            raise ProtocolError("JobSpec sandboxed 必须为布尔值")
        entry = portable_path(self.entrypoint)
        if (not self.idempotency_key or not self.revision_id or not self.design_id
                or entry is None or PurePosixPath(entry).suffix != ".py"):
            raise ProtocolError("JobSpec 身份或 entrypoint 不合法")
        object.__setattr__(self, "entrypoint", entry)
        if len(self.revision_manifest_sha256) != 64:
            raise ProtocolError("JobSpec 必须绑定 revision manifest SHA-256")
        if (isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 86400):
            raise ProtocolError("JobSpec timeout_seconds 必须在 (0,86400]")
        if not all(isinstance(v, str) for v in self.args):
            raise ProtocolError("JobSpec args 必须是字符串 tuple")
        if not all(isinstance(item, InputArtifact) for item in self.inputs):
            raise ProtocolError("JobSpec inputs 必须是 InputArtifact tuple")
        outputs = []
        for output in self.outputs:
            normalized = portable_path(output)
            if normalized is None:
                raise ProtocolError("JobSpec output 必须是安全相对路径")
            outputs.append(normalized)
        object.__setattr__(self, "outputs", tuple(outputs))
        if len(set(self.outputs)) != len(self.outputs):
            raise ProtocolError("JobSpec outputs 不能重复")
        if self.require_edit_coverage and any(
                output == "outputs/_popper_execution.json" for output in self.outputs):
            raise ProtocolError("JobSpec outputs 不能占用执行轨迹保留路径")
        if (type(self.max_processes) is not int or self.max_processes < 1
                or type(self.gpu_count) is not int or self.gpu_count < 0):
            raise ProtocolError("JobSpec 资源数量不合法")
        for value, name in ((self.mem_limit_mb, "mem_limit_mb"),
                            (self.cpu_time_seconds, "cpu_time_seconds")):
            if value is not None and (type(value) is not int or value < 1):
                raise ProtocolError(f"JobSpec {name} 必须是正整数或 null")

    def payload(self):
        value = asdict(self)
        value["args"] = list(self.args)
        value["inputs"] = [asdict(item) for item in self.inputs]
        value["outputs"] = list(self.outputs)
        return value

    @classmethod
    def from_payload(cls, value):
        """从 job.json 的 spec 载荷重建 JobSpec（detached supervisor 使用）。"""
        value = dict(value)
        value["args"] = tuple(value.get("args", []))
        value["inputs"] = tuple(InputArtifact(**item) for item in value.get("inputs", []))
        value["outputs"] = tuple(value.get("outputs", []))
        return cls(**value)

    @property
    def job_id(self):
        # The idempotency key, rather than the whole payload, owns the job identity.
        # submit() then rejects reuse of that identity with a different payload.
        return "JOB-" + digest({"idempotency_key": self.idempotency_key})[:24]


@dataclass(frozen=True)
class WorkerReceipt:
    job_id: str
    status: str
    revision_id: str
    revision_manifest_sha256: str
    artifacts: dict
    elapsed_seconds: float
    execution_backend: str
    error_type: str | None = None

    def payload(self):
        return asdict(self)
