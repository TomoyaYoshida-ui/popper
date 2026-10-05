import importlib.util
import json
import os
import runpy
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError, initialize
from popper.server import Workstation
from popper.terminal import TerminalManager, TerminalSession


class TerminalLifecycleTests(unittest.TestCase):
    def test_capability_reports_authorization_and_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            disabled = TerminalManager(tmp, enabled=False)
            self.assertEqual({"available": False,
                              "reason": "请以 --trusted-local 启动工作台"},
                             disabled.capability())
            disabled.close_all()
            enabled = TerminalManager(tmp, enabled=True)
            try:
                capability = enabled.capability()
                self.assertIs(type(capability["available"]), bool)
                if os.name != "nt":
                    self.assertEqual("当前仅支持 Windows ConPTY", capability["reason"])
            finally:
                enabled.close_all()

    def test_state_exposes_terminal_capability_without_opening_session(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(example / name, project / name)
            runpy.run_path(str(example / "generate_data.py"))["generate"](project)
            initialize(project)
            server = Workstation(("127.0.0.1", 0), project, trusted_local=False)
            try:
                self.assertEqual(server.terminals.capability(),
                                 server.state_payload()["terminal_capability"])
                self.assertFalse(server.state_payload()["terminal_capability"]["available"])
                self.assertEqual({}, server.terminals.sessions)
            finally:
                server.server_close()

    def test_output_ring_is_bounded_and_reports_lost_cursor(self):
        class FakePty:
            def isalive(self):
                return True

        session = TerminalSession.__new__(TerminalSession)
        session.id = "term_test"
        session.root = Path(".").resolve()
        session.lock = threading.Lock()
        session.chunks = deque()
        session.buffered_chars = 0
        session.seq = 0
        session.closed = False
        session.last_activity = time.monotonic()
        session.pty = FakePty()
        session.MAX_CHUNKS = 3
        session.MAX_OUTPUT_CHARS = 10
        session._append("123456")
        session._append("abcdef")
        session.last_activity -= 60
        stale_activity = session.last_activity
        frame = session.read_after(0)
        self.assertGreater(session.last_activity, stale_activity)
        self.assertTrue(frame["reset"])
        self.assertEqual("abcdef", frame["output"])
        self.assertLessEqual(session.buffered_chars, session.MAX_OUTPUT_CHARS)

        cursor = frame["cursor"]
        session._append("0123456789abcdef")
        frame = session.read_after(cursor)
        self.assertTrue(frame["reset"])
        self.assertEqual("6789abcdef", frame["output"])
        self.assertLessEqual(session.buffered_chars, session.MAX_OUTPUT_CHARS)

    def test_create_validates_size_before_allocating_pty(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TerminalManager(tmp, enabled=True)
            for cols, rows in ((0, 24), (80, 0), (True, 24), (80, "24"),
                               (501, 24), (80, 201)):
                with self.subTest(cols=cols, rows=rows):
                    with self.assertRaisesRegex(ProtocolError, "终端尺寸"):
                        manager.create(cols, rows)
            self.assertEqual({}, manager.sessions)
            other = Path(tmp) / "not-registered"
            other.mkdir()
            with self.assertRaisesRegex(ProtocolError, "未登记"):
                manager.create(80, 24, root=other)
            manager.close_all()

    def test_session_cap_and_close_release_slot(self):
        class FakeSession:
            counter = 0
            validate_size = staticmethod(TerminalSession.validate_size)

            def __init__(self, root, cols, rows):
                FakeSession.counter += 1
                self.id = f"term_fake_{FakeSession.counter}"
                self.root = Path(root).resolve()
                self.closed = False
                self.last_activity = time.monotonic()

            def read_after(self, cursor):
                return {"id": self.id, "cursor": 0, "output": "", "alive": True}

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp, patch(
                "popper.terminal.TerminalSession", FakeSession):
            manager = TerminalManager(tmp, enabled=True)
            created = [manager.create(80, 24)["id"] for _ in range(manager.MAX_SESSIONS)]
            self.assertTrue(manager.has_sessions_in_root(tmp))
            with self.assertRaisesRegex(ProtocolError, "会话已达上限"):
                manager.create(80, 24)
            self.assertEqual(manager.MAX_SESSIONS, FakeSession.counter)
            self.assertEqual({"id": created[0], "alive": False}, manager.close(created[0]))
            self.assertEqual({"id": created[0], "alive": False}, manager.close(created[0]))
            with self.assertRaisesRegex(ProtocolError, "不存在"):
                manager.get(created[0])
            manager.create(80, 24)
            self.assertEqual(manager.MAX_SESSIONS, len(manager.sessions))
            stale_id = created[1]
            manager.sessions[stale_id].last_activity -= manager.MAX_IDLE_SECONDS + 1
            self.assertEqual([stale_id], manager.prune_idle())
            self.assertNotIn(stale_id, manager.sessions)
            manager.create(80, 24)
            self.assertEqual(manager.MAX_SESSIONS, len(manager.sessions))
            manager.close_all()
            self.assertEqual({}, manager.sessions)
            self.assertFalse(manager.has_sessions_in_root(tmp))
            self.assertFalse(manager._janitor.is_alive())

    def test_reader_failure_does_not_skip_child_cleanup(self):
        class FakePty:
            def __init__(self):
                self.alive = True
                self.cancelled = False

            def isalive(self):
                return self.alive

            def write(self, data):
                self.alive = False

            def cancel_io(self):
                self.cancelled = True

        class FakeReader:
            def is_alive(self):
                return False

        session = TerminalSession.__new__(TerminalSession)
        session.closed = True  # read loop reported an error while child stayed alive
        session._finalized = False
        session._close_lock = threading.Lock()
        session.pty = FakePty()
        session.reader = FakeReader()
        session.close()
        self.assertFalse(session.pty.alive)
        self.assertTrue(session.pty.cancelled)
        session.close()

    def test_close_failure_keeps_child_retryable(self):
        class FakePty:
            pid = 123456789

            def __init__(self):
                self.alive = True

            def isalive(self):
                return self.alive

            def write(self, data):
                pass

            def cancel_io(self):
                pass

        class FakeReader:
            def is_alive(self):
                return False

        session = TerminalSession.__new__(TerminalSession)
        session.closed = False
        session._finalized = False
        session._close_lock = threading.Lock()
        session.CLOSE_GRACE_SECONDS = 0
        session.pty = FakePty()
        session.reader = FakeReader()
        with patch("popper.terminal.subprocess.run"):
            with self.assertRaisesRegex(ProtocolError, "可重试关闭"):
                session.close()
        self.assertFalse(session._finalized)
        session.pty.alive = False
        session.close()
        self.assertTrue(session._finalized)

    def test_manager_keeps_failed_close_for_retry(self):
        class FakeSession:
            validate_size = staticmethod(TerminalSession.validate_size)

            def __init__(self, root, cols, rows):
                self.id = "term_fake"
                self.closed = False
                self.last_activity = time.monotonic()
                self.fail = True

            def read_after(self, cursor):
                return {"id": self.id, "cursor": 0, "output": "", "alive": True}

            def close(self):
                if self.fail:
                    raise ProtocolError("child still alive")
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp, patch(
                "popper.terminal.TerminalSession", FakeSession):
            manager = TerminalManager(tmp, enabled=True)
            session_id = manager.create(80, 24)["id"]
            with self.assertRaisesRegex(ProtocolError, "child still alive"):
                manager.close(session_id)
            self.assertIn(session_id, manager.sessions)
            manager.sessions[session_id].last_activity -= manager.MAX_IDLE_SECONDS + 1
            self.assertEqual([], manager.prune_idle())
            self.assertIn(session_id, manager.sessions)
            manager.sessions[session_id].fail = False
            self.assertEqual([session_id], manager.prune_idle())
            self.assertNotIn(session_id, manager.sessions)
            manager.close_all()


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("winpty"),
                     "Windows ConPTY + pywinpty required")
class TerminalManagerTests(unittest.TestCase):
    def test_requires_trusted_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TerminalManager(tmp, enabled=False)
            with self.assertRaises(ProtocolError):
                manager.create(80, 24)

    def test_persistent_conpty_executes_and_streams_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "terminal-marker.txt"
            manager = TerminalManager(tmp, enabled=True)
            created = manager.create(90, 28)
            session = manager.get(created["id"])
            try:
                session.write("Write-Output POPPER_TERMINAL_OK\r\n")
                session.write("Set-Content -LiteralPath terminal-marker.txt -Value persisted\r\n")
                deadline = time.monotonic() + 8
                cursor = 0
                output = ""
                while time.monotonic() < deadline:
                    frame = session.read_after(cursor)
                    cursor = frame["cursor"]
                    output += frame["output"]
                    try:
                        complete = marker.read_text(encoding="utf-8-sig").strip() == "persisted"
                    except OSError:
                        complete = False
                    if "POPPER_TERMINAL_OK" in output and complete:
                        break
                    time.sleep(0.05)
                self.assertIn("POPPER_TERMINAL_OK", output)
                self.assertEqual("persisted", marker.read_text(encoding="utf-8-sig").strip())
                session.resize(100, 32)
            finally:
                manager.close_all()

    def test_workstation_http_runs_real_shell_and_releases_closed_session(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "quadratic"
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            extra = Path(tmp) / "extra-workspace"
            extra.mkdir()
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(example / name, project / name)
            runpy.run_path(str(example / "generate_data.py"))["generate"](project)
            initialize(project)
            server = Workstation(("127.0.0.1", 0), project, trusted_local=True,
                                 workspace_roots=(extra,))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/api/session", timeout=3) as response:
                token = json.loads(response.read())["token"]

            def terminal_request(payload):
                request = urllib.request.Request(
                    base + "/api/terminal", data=json.dumps(payload).encode("utf-8"),
                    headers={"X-Popper-Token": token, "Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.loads(response.read())

            try:
                create_body = json.dumps({"op": "create", "cols": 90, "rows": 28}).encode()
                for headers in (
                    {"Content-Type": "application/json"},
                    {"X-Popper-Token": token, "Content-Type": "application/json",
                     "Origin": "https://untrusted.example"},
                    {"X-Popper-Token": token, "Content-Type": "application/json",
                     "Host": "untrusted.example"},
                ):
                    with self.subTest(headers=headers):
                        rejected = urllib.request.Request(
                            base + "/api/terminal", data=create_body,
                            headers=headers, method="POST")
                        with self.assertRaises(urllib.error.HTTPError) as failure:
                            urllib.request.urlopen(rejected, timeout=5)
                        self.assertEqual(403, failure.exception.code)
                        self.assertEqual({}, server.terminals.sessions)
                created = terminal_request({"op": "create", "cols": 90, "rows": 28})
                session_id = created["id"]
                self.assertEqual(str(project.resolve()), created["root"])
                terminal_request({"op": "input", "id": session_id,
                                  "data": "Write-Output POPPER_HTTP_TERMINAL_OK\r\n"})
                cursor = created["cursor"]
                output = created["output"]
                deadline = time.monotonic() + 8
                while "POPPER_HTTP_TERMINAL_OK" not in output and time.monotonic() < deadline:
                    frame = terminal_request({"op": "read", "id": session_id,
                                              "cursor": cursor})
                    cursor = frame["cursor"]
                    output += frame["output"]
                    time.sleep(0.05)
                self.assertIn("POPPER_HTTP_TERMINAL_OK", output)
                self.assertEqual({"id": session_id, "alive": False},
                                 terminal_request({"op": "close", "id": session_id}))
                extra_wid = next(row["wid"] for row in server.workspaces.roots_payload()
                                 if row["root"] == str(extra.resolve()))
                with server.ws_lock:
                    server.workspaces.switch(extra_wid)
                second = terminal_request({"op": "create", "cols": 90, "rows": 28})
                self.assertEqual(str(extra.resolve()), second["root"])
                marker = extra / "terminal-root.txt"
                terminal_request({"op": "input", "id": second["id"],
                                  "data": "Set-Content -LiteralPath terminal-root.txt -Value current-root\r\n"})
                deadline = time.monotonic() + 8
                marker_text = ""
                while time.monotonic() < deadline:
                    try:
                        marker_text = marker.read_text(encoding="utf-8-sig").strip()
                    except OSError:
                        marker_text = ""
                    if marker_text == "current-root":
                        break
                    time.sleep(0.05)
                self.assertEqual("current-root", marker_text)
                self.assertFalse((project / "terminal-root.txt").exists())
                remove_request = urllib.request.Request(
                    base + "/api/workspace",
                    data=json.dumps({"op": "workspace_remove", "wid": extra_wid}).encode(),
                    headers={"X-Popper-Token": token, "Content-Type": "application/json"},
                    method="POST")
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(remove_request, timeout=5)
                self.assertEqual(409, rejected.exception.code)
                self.assertIn("请先关闭终端", rejected.exception.read().decode("utf-8"))
                terminal_request({"op": "close", "id": second["id"]})
                with urllib.request.urlopen(remove_request, timeout=5) as response:
                    removed_workspace = json.loads(response.read())
                self.assertEqual("primary", removed_workspace["current_root"])
                self.assertEqual({}, server.terminals.sessions)
                self.assertEqual({"id": session_id, "alive": False},
                                 terminal_request({"op": "close", "id": session_id}))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
