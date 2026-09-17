"""分离式 supervisor：在独立进程拥有本地 job 的执行生命周期。

控制器只负责 submit/poll/collect；真正的执行（沙箱、产物、回执）由 supervisor
进程承载。控制器崩溃后，已 detach 的 supervisor 继续把 job 推进到终态，重新挂载
的控制器通过 job.json manifest 重连，而不是重跑。

supervisor 内跑两个守护线程（镜像 OpenResearch 的「日志 tail + 状态轮询」双线程）：
- 心跳：周期性刷新 supervisor.json 的单调时间戳，供 reap_stale 判活；
- 取消观察：轮询 cancel_requested 标记，命中即读 sandbox.pid 杀整棵进程树。

入口 `supervise_job(root, job_id[, revisions_root])` 复用了 LocalWorker 的
`_execute_job` 公共体，保证分离执行与同步测试路径产出字节级一致。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from ... import sandbox
from ...core import read_json, write_json
from .base import TERMINAL_JOB_STATUSES, JobSpec
from .local import LocalWorker

_HEARTBEAT_SECONDS = 5.0


def _heartbeat(job_dir, done):
    """周期刷新 supervisor.json 心跳；done 置位即退出。"""
    while not done.is_set():
        write_json(job_dir / "supervisor.json",
                   {"pid": os.getpid(), "heartbeat": time.time()})
        done.wait(_HEARTBEAT_SECONDS)


def _watch_cancel(job_dir, done):
    """轮询 cancel_requested；命中即按 sandbox.pid 杀任务树后退出。"""
    while not done.is_set():
        if not (job_dir / "cancel_requested").is_file():
            time.sleep(0.2)
            continue
        pid_file = job_dir / "sandbox.pid"
        if pid_file.is_file():
            sandbox.terminate_tree(read_json(pid_file)["pid"])
            break
        time.sleep(0.2)


def supervise_job(root, job_id, revisions_root=None):
    """读取 manifest、重建 spec、执行并落终态；返回 {job_id, status}。"""
    root = Path(root).resolve()
    revisions_root = (Path(revisions_root).resolve() if revisions_root
                      else root.parent / "revisions")
    worker = LocalWorker(root, revisions_root)
    job_dir = (worker.root / job_id).resolve()
    if not job_dir.is_relative_to(worker.root):
        raise ValueError("job_id 越界")
    manifest_path = job_dir / "job.json"
    manifest = read_json(manifest_path)
    if manifest["status"] in TERMINAL_JOB_STATUSES:
        return {"job_id": job_id, "status": manifest["status"]}
    if (job_dir / "cancel_requested").is_file():
        worker._finalize_manual(job_dir, manifest, "cancelled", "Cancelled")
        return {"job_id": job_id, "status": "cancelled"}
    spec = JobSpec.from_payload(manifest["spec"])
    done = threading.Event()
    heart = threading.Thread(target=_heartbeat, args=(job_dir, done), daemon=True)
    watch = threading.Thread(target=_watch_cancel, args=(job_dir, done), daemon=True)
    heart.start()
    watch.start()
    try:
        worker._execute_job(spec, manifest)
    finally:
        done.set()
        heart.join(timeout=2)
        watch.join(timeout=2)
    return {"job_id": job_id, "status": read_json(manifest_path)["status"]}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (2, 3):
        raise SystemExit(
            "usage: python -m popper.research.workers.supervisor "
            "<jobs_root> <job_id> [<revisions_root>]")
    root, job_id = args[0], args[1]
    revisions_root = args[2] if len(args) == 3 else None
    result = supervise_job(root, job_id, revisions_root)
    # stdout 已被 spawn 端重定向到 supervisor.log；仅输出终态供排查。
    print(result["status"])
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())