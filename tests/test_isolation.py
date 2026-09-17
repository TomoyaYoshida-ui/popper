"""安全边界 / 隔离状态自检测试。"""
import os
import unittest

from popper import sandbox
from popper.isolation import isolation_status


class IsolationTests(unittest.TestCase):
    def test_status_contains_required_items_and_counts(self):
        report = isolation_status(".")
        self.assertIn("items", report)
        self.assertIn("summary", report)
        items = {item["id"]: item for item in report["items"]}
        for item_id in ("os_sandbox_filesystem_scope", "network_default_off",
                        "http_proxy_interception", "low_privilege_process",
                        "command_interception", "resource_limits",
                        "process_tree_termination", "secrets_excluded_from_logs",
                        "heldout_sealed_channel", "independent_scoring_process",
                        "local_first_no_training",
                        "single_manuscript_no_batch"):
            self.assertIn(item_id, items)
            self.assertIsInstance(items[item_id]["implemented"], bool)
            self.assertIsInstance(items[item_id]["note"], str)
        self.assertEqual(len(report["items"]), report["summary"]["total"])
        self.assertEqual(sum(1 for item in report["items"] if item["implemented"]),
                         report["summary"]["implemented"])

    def test_sandbox_items_are_honestly_unimplemented(self):
        report = isolation_status(".")
        items = {item["id"]: item for item in report["items"]}
        # 写作用域 / 代理拦截 / 命令拦截 / 资源限额 / 低权限进程随后端探活动态翻转；
        # 内核级断网仅提权环境可用。
        self.assertEqual(items["os_sandbox_filesystem_scope"]["implemented"], sandbox.available())
        self.assertEqual(items["http_proxy_interception"]["implemented"], sandbox.available())
        self.assertEqual(items["command_interception"]["implemented"], sandbox.available())
        self.assertEqual(items["resource_limits"]["implemented"], sandbox.job_limits_available())
        self.assertEqual(items["low_privilege_process"]["implemented"], sandbox.available())
        self.assertEqual(items["network_default_off"]["implemented"], sandbox.netblock_available())
        # 整树终止取决于内核机制是否可用（Windows 看 Job Object，POSIX 看进程组），不再硬编码 True。
        self.assertEqual(items["process_tree_termination"]["implemented"],
                         sandbox.process_tree_termination_available())

    def test_integrity_and_honesty_items_are_implemented(self):
        report = isolation_status(".")
        items = {item["id"]: item for item in report["items"]}
        for item_id in ("secrets_excluded_from_logs",
                        "independent_scoring_process", "local_first_no_training",
                        "single_manuscript_no_batch"):
            self.assertTrue(items[item_id]["implemented"], item_id)
        # 保留集通道的诚实性由内核禁读能力决定，不再是无条件 False。
        self.assertEqual(items["heldout_sealed_channel"]["implemented"],
                         sandbox.seal_read_available())
        if os.name == "nt":
            self.assertTrue(sandbox.seal_read_available())


if __name__ == "__main__":
    unittest.main()
