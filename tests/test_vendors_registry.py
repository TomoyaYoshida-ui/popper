"""VendorRegistry 契约的仓库内验证：用合成 fixture 跑通登记、指纹校验与越界拒绝。

为什么要有这个文件：真实语料用例（tests/test_vendors.py）只有在「上游仓库与本项目同级
放置」的开发机上才跑得动——`integrations/vendors.json` 的 `source_root` 写的是
`../../AI-Research-SKILLs-main` 这类仓库外相对路径，干净克隆和 CI runner 上那些目录不存在。
如果 vendor 层只有那一组用例，那么这台机器之外**一条都没跑过**，CI 绿灯纯属侥幸。

这里把与外部语料无关的部分全部钉住：注册表 schema、指纹不匹配、许可证/SKILL/入口未锁定、
路径越界、目录缺失。真实语料那部分继续由 test_vendors.py 覆盖，缺语料时显式跳过并说明布局要求。
"""
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from popper.capabilities import vendor_corpus_status
from popper.core import ProtocolError, file_hash
from popper.vendors import VendorRegistry


def make_workspace(root, *, component_id="alpha", source_root=None, kind="executable",
                   license_file="LICENSE", skill_file="SKILL.md", entrypoint="main.py",
                   drop_from_lock=(), extra_keys=None, break_hash=False,
                   create_component=True):
    """造一个「上游仓库与项目仓库同级」的最小布局，返回 (项目根, 注册表路径)。"""
    project_root = Path(root) / "repo"
    registry_dir = project_root / "integrations"
    registry_dir.mkdir(parents=True, exist_ok=True)
    component_dir = Path(root) / "components" / component_id
    if create_component:
        component_dir.mkdir(parents=True, exist_ok=True)
        (component_dir / "LICENSE").write_text("MIT synthetic license\n", encoding="utf-8")
        (component_dir / "SKILL.md").write_text("# synthetic skill\n", encoding="utf-8")
        (component_dir / "main.py").write_text("print('synthetic')\n", encoding="utf-8")
    locked = {}
    if create_component:
        for relative in (license_file, skill_file, entrypoint):
            if relative in drop_from_lock:
                continue
            locked[relative] = str(file_hash(component_dir / relative))
    if break_hash and locked:
        locked[sorted(locked)[0]] = "0" * 64
    entry = {"kind": kind, "name": component_id.title(),
             "source_root": source_root or f"../../components/{component_id}",
             "license": "MIT", "license_file": license_file, "skill_file": skill_file,
             "capabilities": ["synthetic.capability"], "sha256": locked}
    if kind == "executable":
        entry["entrypoint"] = entrypoint
    if extra_keys:
        entry.update(extra_keys)
    registry_path = registry_dir / "vendors.json"
    registry_path.write_text(json.dumps({"schema_version": "1.1",
                                         "components": {component_id: entry}},
                                        ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return project_root, registry_path


class SyntheticRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def registry(self, **kwargs):
        project_root, registry_path = make_workspace(self.root, **kwargs)
        return VendorRegistry(project_root=project_root, registry_path=registry_path)

    def test_clean_synthetic_registry_is_verified(self):
        report = self.registry().inspect()
        self.assertEqual("verified", report["status"])
        self.assertEqual(1, len(report["components"]))
        component = report["components"][0]
        self.assertEqual(3, component["files_verified"])
        self.assertTrue(Path(component["entrypoint"]).is_file())

    def test_fingerprint_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "指纹不匹配"):
            self.registry(break_hash=True).inspect()

    def test_unpinned_license_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "未锁定许可证或 SKILL"):
            self.registry(drop_from_lock=("LICENSE",)).inspect()

    def test_unpinned_skill_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "未锁定许可证或 SKILL"):
            self.registry(drop_from_lock=("SKILL.md",)).inspect()

    def test_unpinned_entrypoint_is_rejected_for_executable(self):
        with self.assertRaisesRegex(ProtocolError, "未锁定入口"):
            self.registry(drop_from_lock=("main.py",)).inspect()

    def test_missing_required_key_is_rejected(self):
        project_root, registry_path = make_workspace(self.root)
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        del data["components"]["alpha"]["license"]
        registry_path.write_text(json.dumps(data), encoding="utf-8")
        registry = VendorRegistry(project_root=project_root, registry_path=registry_path)
        with self.assertRaisesRegex(ProtocolError, "登记不完整"):
            registry.component("alpha")

    def test_unregistered_extra_key_is_rejected(self):
        """键集必须精确相等：偷偷多一个字段（比如 auto_trust）同样算契约不完整。"""
        with self.assertRaisesRegex(ProtocolError, "登记不完整"):
            self.registry(extra_keys={"auto_trust": True}).component("alpha")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "登记不完整"):
            self.registry(kind="library").component("alpha")

    def test_unsupported_license_is_rejected(self):
        project_root, registry_path = make_workspace(self.root)
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        data["components"]["alpha"]["license"] = "GPL-3.0"
        registry_path.write_text(json.dumps(data), encoding="utf-8")
        registry = VendorRegistry(project_root=project_root, registry_path=registry_path)
        with self.assertRaisesRegex(ProtocolError, "登记不完整"):
            registry.component("alpha")

    def test_missing_component_directory_is_rejected(self):
        """这就是干净克隆 / CI 上真实语料的处境：登记在、目录不在，必须显式报错。"""
        registry = self.registry(create_component=False)
        with self.assertRaisesRegex(ProtocolError, "目录缺失或越界"):
            registry.component("alpha")
        ok, detail = vendor_corpus_status(project_root=registry.project_root,
                                          registry_path=registry.registry_path)
        self.assertFalse(ok)
        self.assertIn("目录缺失或越界", detail)

    def test_source_root_pointing_outside_the_workspace_is_rejected(self):
        """目录真实存在但在工作区之外：越界分支必须和「不存在」一样被拒。"""
        outside = self.root.parent / f"popper-escape-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        outside.mkdir(parents=True, exist_ok=True)
        try:
            relative = "../../" + outside.name
            registry = self.registry(source_root=relative)
            with self.assertRaisesRegex(ProtocolError, "目录缺失或越界"):
                registry.component("alpha")
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_resolve_rejects_path_outside_component(self):
        registry = self.registry()
        with self.assertRaisesRegex(ProtocolError, "文件缺失或越界"):
            registry.resolve("alpha", "../LICENSE")
        with self.assertRaisesRegex(ProtocolError, "文件缺失或越界"):
            registry.resolve("alpha", "nope.py")

    def test_protocol_component_needs_no_entrypoint(self):
        """kind=protocol 只交付 SKILL，不提供可执行入口：入口不该成为必需字段。"""
        registry = self.registry(kind="protocol")
        component, _ = registry.component("alpha")
        self.assertNotIn("entrypoint", component)
        report = registry.inspect()
        self.assertEqual("verified", report["status"])
        self.assertIsNone(report["components"][0]["entrypoint"])

    def test_unknown_component_id_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "未知开源组件"):
            self.registry().component("does-not-exist")


if __name__ == "__main__":
    unittest.main()
