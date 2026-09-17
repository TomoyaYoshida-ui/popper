"""只读的安全边界 / 隔离状态自检，诚实标注已实现项与未实现项。

执行边界采用 OS 原生沙箱策略（对齐 Codex/Trae 的本地模式），不引入容器：
文件写作用域限定实验工作区、网络默认阻断、低权限进程、高危命令拦截与越界审批。
本地执行路径不把 Docker 容器作为组成部分；容器仅保留给未来 BYOK 云算力或
第三方仓库复现（B2.5）场景。

清单只回答「有没有」，具体机制措辞跟着选中的沙箱后端走（Windows 低完整性 /
Linux bubblewrap），无后端平台上用平台中立措辞并在 `backend` 字段如实返 None。
"""
from __future__ import annotations

from pathlib import Path

from . import sandbox


def isolation_status(project_dir):
    Path(project_dir)
    # 平台相关条目的措辞由当前选中的沙箱后端提供（capability_notes）：默认文本只说
    # 中立事实，否则 Linux 上会打印只有 Windows 才成立的机制描述。
    notes = sandbox.capability_notes()
    defaults = [
        {
            "id": "os_sandbox_filesystem_scope",
            "implemented": sandbox.available(),
            "note": "OS 原生沙箱把候选的文件写作用域限定在运行工作区，其余目录只读；"
                    "读除保留集禁读外不受限（具体机制见当前后端）",
        },
        {"id": "network_default_off", "implemented": sandbox.netblock_available(),
         "note": "内核级断网可用时才声明为已实现；不可用时仅保留尽力而为的拦截，"
                 "不承诺断网（具体前置条件见当前后端）"},
        {
            "id": "http_proxy_interception",
            "implemented": sandbox.available(),
            "note": "候选进程环境变量代理指向死端口：遵循代理的 HTTP(S) 客户端被拦，"
                    "裸 socket 与 DNS 不受限（已有内核级断网时这层只是纵深防御）",
        },
        {"id": "low_privilege_process", "implemented": sandbox.available(),
         "note": "候选跑在降权进程/隔离 namespace 里（不是用户主会话同级进程），"
                 "具体降权手段由当前后端提供"},
        {
            "id": "command_interception",
            "implemented": sandbox.available(),
            "note": "Python 层命令拦截（sitecustomize 注入）：高危命令直接拒绝；"
                    "可被原生 API 绕过，非内核级边界",
        },
        {"id": "resource_limits", "implemented": sandbox.job_limits_available(),
         "note": "内核强制的资源限额（内存 / CPU 时间 / 进程数）；限额参数无法强制时"
                 "直接报错而不静默丢弃"},
        {"id": "process_tree_termination",
         "implemented": sandbox.process_tree_termination_available(),
         "note": "整棵进程树能被内核级终止（不只是尽力而为的遍历杀）；"
                 "内核机制不可用时该条目为 False，不硬编码通过"},
        {"id": "secrets_excluded_from_logs", "implemented": True,
         "note": "proposer/scoop 保证密钥不入事件与日志"},
        {"id": "heldout_sealed_channel", "implemented": sandbox.seal_read_available(),
         "note": "保留集（最终测试数据）读隔离：内核层阻止候选进程读出真实数据，"
                 "控制器与用户不受影响，窗口结束即恢复；不可用时 --sandbox 对测试划分"
                 "直接报错，不静默降级。账户级/主机级隔离仍需外部评估服务"},
        {"id": "independent_scoring_process", "implemented": True,
         "note": "Research Controller 通过单独进程从哈希绑定的逐样本预测重算指标；"
                 "当前仍与控制器使用同一 OS 账户"},
        {"id": "local_first_no_training", "implemented": True,
         "note": "本地优先，不将用户数据用于训练"},
        {"id": "single_manuscript_no_batch", "implemented": True,
         "note": "单次单稿件、无批量生成，由 materialize 防线保证"},
    ]
    items = [{**item, "note": notes.get(item["id"], item["note"])} for item in defaults]
    implemented = sum(1 for item in items if item["implemented"])
    return {"items": items,
            "summary": {"implemented": implemented, "total": len(items)},
            "backend": sandbox.selected_backend_name()}
