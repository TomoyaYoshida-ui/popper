"""可选能力探测：缺哪个 extra 就明说缺哪个，不让调用方撞 ImportError。

放在生产代码里而不是测试目录，有两个原因：

1. 这些判定本身是产品语义的一部分。CLI 与工作台需要回答"独立确认为什么不可用"，
   答案必须是「confirmation extra 未安装：pip install -e ".[confirmation]"」，
   而不是一个裸的 ImportError 堆栈。
2. 测试树有两处（`tests/` 与 `evaluation/blind_pilot/tests/`），各自跑在依赖完备程度
   不同的环境里。同一份探测逻辑复制两遍必然漂移，共享一个入口才跳得一致。

对应纪律：核心零依赖 job 里跳过用例是**显式跳过并写明原因**，不是静默降级——
装齐 extras 的 job 仍然逐条真跑，覆盖不丢。
"""
from __future__ import annotations

import importlib.util

#: pyproject.toml `[project.optional-dependencies]` 的 extra 名 -> 该 extra 的关键导入名。
#: 新增 extra 时必须同步这里，否则探测会假报"可用"。
EXTRA_IMPORTS = {
    "ml": ("sklearn",),
    "research": ("requests", "pypdf"),
    "orchestration": ("langgraph",),
    "confirmation": ("cryptography",),
}


def missing_imports(extra):
    """返回该 extra 尚未安装的导入名；全部就绪时返回空元组。未知 extra 直接报错。"""
    try:
        modules = EXTRA_IMPORTS[extra]
    except KeyError:
        raise KeyError(f"未知 extra: {extra}（已知：{', '.join(sorted(EXTRA_IMPORTS))}）") from None
    return tuple(name for name in modules if importlib.util.find_spec(name) is None)


def extra_available(extra):
    return not missing_imports(extra)


def install_hint(extra):
    return f'pip install -e ".[{extra}]"'


def vendor_corpus_status(project_root=None, registry_path=None):
    """真实开源语料是否可用：返回 (ok, detail)。

    `integrations/vendors.json` 的 `source_root` 写的是「与项目仓库同级」的相对路径
    （local-first 布局：上游仓库与本项目并列放在同一个工作目录）。干净克隆、CI runner
    上都没有这些兄弟目录，此时所有真实语料用例必须显式跳过并说明布局要求——
    注册表逻辑本身由仓库内的合成 fixture 用例（tests/test_vendors_registry.py）覆盖。

    两个参数只用于测试：不传就是产品默认布局（仓库内 integrations/ + 仓库同级为工作区）。
    """
    try:
        from .vendors import VendorRegistry

        registry = VendorRegistry(project_root=project_root, registry_path=registry_path)
        for component_id in sorted(registry.registry["components"]):
            registry.component(component_id)
    except Exception as error:  # 任何登记/布局问题都算不可用，细节原样带出
        return False, f"{type(error).__name__}: {error}"
    return True, "verified"
