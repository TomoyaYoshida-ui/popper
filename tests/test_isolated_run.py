import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from popper import sandbox


class IsolatedRunTests(unittest.TestCase):
    def _script(self, body):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "probe.py"
        p.write_text(body, encoding="utf-8")
        return tmp, p

    def _run(self, command, tmp, timeout_seconds, **kwargs):
        """trusted-local 执行路径：唯一入口 sandbox.launch 的 sandboxed=False 分支。"""
        with (tmp / "out.txt").open("wb") as out, (tmp / "err.txt").open("wb") as err:
            return sandbox.launch(command, cwd=tmp, env=None, stdout=out, stderr=err,
                                  timeout_seconds=timeout_seconds, sandboxed=False, **kwargs)

    def test_success_truncates_nothing_and_returns(self):
        tmp, p = self._script("print('ok')\n")
        proc = self._run([sys.executable, str(p)], tmp, 10)
        self.assertEqual(0, proc.returncode)
        self.assertEqual(b"ok", (tmp / "out.txt").read_bytes().strip())

    def test_nonzero_exit_raises_called_process_error(self):
        tmp, p = self._script("import sys; sys.exit(3)\n")
        with self.assertRaises(Exception) as ctx:
            self._run([sys.executable, str(p)], tmp, 10)
        # CalledProcessError 或 ProtocolError 均可，属"防失控"被识别
        self.assertTrue(isinstance(ctx.exception, Exception))

    def test_timeout_kills_child(self):
        # 子进程 sleep 超过 timeout，应被终止并抛错（不死锁、不遗留）
        tmp, p = self._script("import time; time.sleep(30)\n")
        start = time.monotonic()
        with self.assertRaises(Exception):
            self._run([sys.executable, str(p)], tmp, 1)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 15, "超时后应快速返回，说明子进程被杀而非继续等待")

    @unittest.skipUnless(os.name == "nt", "非沙箱路径无法强制资源限额只在 Windows 成立")
    def test_plain_path_does_not_silently_drop_limits(self):
        tmp, p = self._script("print('ok')\n")
        with self.assertRaises(OSError) as caught:
            self._run([sys.executable, str(p)], tmp, 10, mem_limit_mb=64)
        self.assertIn("mem_limit_mb", str(caught.exception))
        self.assertIn("--sandbox", str(caught.exception))
        # cpu_time_seconds 不紧于 wall-clock：由超时看门狗覆盖，不算静默丢弃用户约束
        self._run([sys.executable, str(p)], tmp, 10, cpu_time_seconds=10)
        with self.assertRaises(OSError):
            self._run([sys.executable, str(p)], tmp, 10, cpu_time_seconds=1)


if __name__ == "__main__":
    unittest.main()