import json
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, write_json
from popper.workspace import Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = Workspace(self.root)
        self.ws.seed_if_empty("默认研究任务", "searching")

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_first_and_idempotent(self):
        fresh = Workspace(self.root / "_fresh")
        self.assertTrue(fresh.is_empty())
        self.assertTrue(fresh.seed_if_empty("真实项目名", "searching"))
        data = fresh.read()
        self.assertEqual("真实项目名", data["folders"][0]["name"])
        self.assertEqual(1, len(data["folders"][0]["tasks"]))
        self.assertEqual("推进实验搜索", data["folders"][0]["tasks"][0]["title"])
        self.assertEqual("searching", data["meta"]["seed"]["phase"])
        self.assertEqual([], data["deliverables"])
        # 幂等：第二次不重播
        self.assertFalse(fresh.seed_if_empty("别的名字", "completed"))
        self.assertEqual(1, len(fresh.read()["folders"]))

    def test_folder_crud_and_persistence(self):
        data = self.ws.read()
        self.ws.folder_create(data, "工作区 A")
        fid = data["folders"][-1]["fid"]
        self.ws.folder_rename(data, fid, "工作区 A2")
        self.ws.save(data)
        reloaded = self.ws.read()
        self.assertEqual("工作区 A2", reloaded["folders"][-1]["name"])
        self.ws.folder_remove(data, fid)
        self.ws.save(data)
        self.assertNotIn("工作区 A2", [f["name"] for f in self.ws.read()["folders"]])

    def test_quest_counter_global_across_folders(self):
        data = self.ws.read()
        for name in ("A", "B"):
            self.ws.folder_create(data, name)
        fa, fb = data["folders"][0]["fid"], data["folders"][1]["fid"]
        for _ in range(3):
            self.ws.task_create(data, fa)
        self.ws.task_create(data, fb)  # 计数器跨文件夹递增
        self.assertEqual("新 Quest 1", data["folders"][0]["tasks"][1]["title"])
        self.assertEqual("新 Quest 4", data["folders"][1]["tasks"][0]["title"])

    def test_task_rename_toggle_remove_move(self):
        data = self.ws.read()
        seed_fid = data["folders"][0]["fid"]  # 种子文件夹（含 1 个种子任务）
        self.ws.task_create(data, seed_fid, "自定义标题")
        quid = data["folders"][0]["tasks"][1]["quid"]  # 文件夹[0]里第 2 个(新建)任务
        self.ws.task_rename(data, seed_fid, quid, "改后标题")
        self.ws.task_toggle(data, seed_fid, quid, True)
        self.ws.folder_create(data, "目标")
        to_fid = data["folders"][1]["fid"]
        self.ws.task_move(data, seed_fid, quid, to_fid)
        self.ws.save(data)
        d = self.ws.read()
        self.assertEqual("改后标题", d["folders"][1]["tasks"][0]["title"])
        self.assertTrue(d["folders"][1]["tasks"][0]["done"])
        self.assertEqual(1, len(d["folders"][0]["tasks"]))  # 原文件夹只剩种子任务
        self.assertEqual(1, len(d["folders"][1]["tasks"]))  # 目标文件夹收到被移任务

    def test_missing_targets_raise_protocol_error(self):
        data = self.ws.read()
        with self.assertRaisesRegex(ProtocolError, "文件夹不存在"):
            self.ws.task_create(data, "nope")
        with self.assertRaisesRegex(ProtocolError, "Quest 不存在"):
            self.ws.task_rename(data, data["folders"][0]["fid"], "nope", "x")

    def test_corrupt_workspace_raises(self):
        write_json(self.ws.path, "not-a-dict")
        with self.assertRaises(ProtocolError):
            self.ws.read()

    def test_save_is_atomic_no_tmp_left(self):
        data = self.ws.read()
        self.ws.save(data)
        leftovers = list(self.root.joinpath(".popper").glob("*.tmp"))
        self.assertEqual([], leftovers)

    def test_deliverable_registration_and_traversal_guard(self):
        data = self.ws.read()
        item = self.ws.register_deliverable(data, "manuscript.md", "md", "nature", 42)
        self.ws.save(data)
        (self.root / "deliverables").mkdir(parents=True, exist_ok=True)
        (self.root / "deliverables" / "manuscript.md").write_text("x", encoding="utf-8")
        path = self.ws.deliverable_path(item["id"])
        self.assertEqual("manuscript.md", path.name)
        # 篡改登记文件名越界 → 拒绝
        reloaded = self.ws.read()
        reloaded["deliverables"][0]["filename"] = "../../../../secret.txt"
        self.ws.save(reloaded)
        with self.assertRaises(ProtocolError):
            self.ws.deliverable_path(item["id"])
        # 未登记 id → 拒绝
        with self.assertRaisesRegex(ProtocolError, "产物不存在"):
            self.ws.deliverable_path("d_forged")

    def test_write_deliverable_guards(self):
        """写回只允许覆写 deliverables 内已存在的 md/tex/json，防穿越/防新建/防扩名。"""
        data = self.ws.read()
        item = self.ws.register_deliverable(data, "manuscript.md", "md", "nature", 42)
        self.ws.save(data)
        deliverables = self.root / "deliverables"
        deliverables.mkdir(parents=True, exist_ok=True)
        (deliverables / "manuscript.md").write_text("旧内容", encoding="utf-8")

        # 合法写回：覆写已存在 md → 内容更新
        path = self.ws.write_deliverable(item["id"], "# 新内容")
        self.assertEqual("manuscript.md", path.name)
        self.assertEqual("# 新内容", (deliverables / "manuscript.md").read_text(encoding="utf-8"))

        # 中途：注册第二个产物用于后续负例
        data = self.ws.read()
        extern = self.ws.register_deliverable(data, "external.md", "md", "nature", 7)
        self.ws.save(data)

        # 越界（写回用 .md 后缀保证先通过扩展名门，再触发白名单目录校验）
        reloaded = self.ws.read()
        reloaded["deliverables"][0]["filename"] = "../secret.md"
        self.ws.save(reloaded)
        with self.assertRaisesRegex(ProtocolError, "产物路径越界"):
            self.ws.write_deliverable(item["id"], "x")

        # 非存在文件（已登记但文件缺失）→ 拒绝新建
        with self.assertRaisesRegex(ProtocolError, "仅允许覆写已生成的物化文件"):
            self.ws.write_deliverable(extern["id"], "x")

        # 错误扩展名（docx 不可写回文本）→ 拒绝
        data = self.ws.read()
        docx = self.ws.register_deliverable(data, "poster.docx", "docx", None, 9)
        deliverables2 = self.ws.ensure_deliverables_dir()
        (deliverables2 / "poster.docx").write_text("binary", encoding="utf-8")
        self.ws.save(data)
        with self.assertRaisesRegex(ProtocolError, "仅支持写回文本稿件"):
            self.ws.write_deliverable(docx["id"], "x")


if __name__ == "__main__":
    unittest.main()