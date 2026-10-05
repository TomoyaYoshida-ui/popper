"""Local trusted-workspace terminal backed by Windows ConPTY.

This is intentionally not available unless the workstation was started with
``--trusted-local``. HTTP authentication/origin checks remain the caller's
responsibility; this module owns PTY lifecycle, bounded output and resize/input.
"""
from __future__ import annotations

from collections import deque
import importlib.util
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time

from .core import ProtocolError


class TerminalSession:
    MAX_CHUNKS = 2048
    MAX_OUTPUT_CHARS = 262_144
    CLOSE_GRACE_SECONDS = 2

    @staticmethod
    def validate_size(cols, rows):
        if (type(cols) is not int or type(rows) is not int
                or not 20 <= cols <= 500 or not 5 <= rows <= 200):
            raise ProtocolError("终端尺寸不合法")

    def __init__(self, root, cols=100, rows=30):
        self.validate_size(cols, rows)
        if os.name != "nt":
            raise ProtocolError("当前终端后端只实现了 Windows ConPTY")
        try:
            import winpty
        except ImportError:
            raise ProtocolError("Windows 真终端需要安装 pywinpty（pip install -e '.[workstation]'）") from None
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            raise ProtocolError("找不到 PowerShell/pwsh")
        self.id = "term_" + secrets.token_hex(12)
        self.root = Path(root).resolve()
        self.lock = threading.Lock()
        self.chunks = deque()
        self.buffered_chars = 0
        self.seq = 0
        self.closed = False
        self.last_activity = time.monotonic()
        self._finalized = False
        self._close_lock = threading.Lock()
        self.pty = winpty.PTY(cols, rows)
        self.pty.spawn(shell, "-NoLogo -NoProfile", cwd=str(self.root))
        self.reader = threading.Thread(target=self._read_loop, daemon=True,
                                       name=f"popper-{self.id}")
        self.reader.start()

    def _append(self, text):
        if not text:
            return
        truncated = len(text) > self.MAX_OUTPUT_CHARS
        if truncated:
            text = text[-self.MAX_OUTPUT_CHARS:]
        with self.lock:
            while self.chunks and (len(self.chunks) >= self.MAX_CHUNKS
                                   or self.buffered_chars + len(text) > self.MAX_OUTPUT_CHARS):
                _, old_text = self.chunks.popleft()
                self.buffered_chars -= len(old_text)
            if truncated:
                self.seq += 1  # omitted prefix creates a detectable cursor gap
            self.seq += 1
            self.chunks.append((self.seq, text))
            self.buffered_chars += len(text)

    def _read_loop(self):
        while not self.closed:
            try:
                text = self.pty.read(False)
                if text:
                    self._append(text)
                    continue
                if not self.pty.isalive():
                    break
            except Exception as error:
                if not self.closed:
                    self._append(f"\r\n[terminal read error: {type(error).__name__}]\r\n")
                break
            time.sleep(0.02)
        self.closed = True

    def read_after(self, cursor):
        with self.lock:
            self.last_activity = time.monotonic()
            first = self.chunks[0][0] if self.chunks else self.seq + 1
            reset = cursor < first - 1
            if reset:
                cursor = first - 1
            output = "".join(text for seq, text in self.chunks if seq > cursor)
            return {"id": self.id, "root": str(self.root), "cursor": self.seq, "output": output,
                    "alive": bool(not self.closed and self.pty.isalive()), "reset": reset}

    def write(self, data):
        if self.closed or not self.pty.isalive():
            raise ProtocolError("终端会话已结束")
        if not isinstance(data, str) or not data or len(data) > 65_536:
            raise ProtocolError("终端输入长度不合法")
        self.pty.write(data)
        self.last_activity = time.monotonic()

    def resize(self, cols, rows):
        self.validate_size(cols, rows)
        if not self.closed:
            self.pty.set_size(cols, rows)
            self.last_activity = time.monotonic()

    def close(self):
        with self._close_lock:
            if self._finalized:
                return
            self.closed = True

            def alive():
                try:
                    return bool(self.pty.isalive())
                except Exception:
                    return None  # unknown is not proof that the child exited

            try:
                if alive() is True:
                    try:
                        self.pty.write("exit\r\n")
                    except Exception:
                        pass
                deadline = time.monotonic() + self.CLOSE_GRACE_SECONDS
                while alive() is True and time.monotonic() < deadline:
                    time.sleep(0.05)
                if alive() is not False and os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/PID", str(self.pty.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=5, check=False)
                    except (OSError, subprocess.SubprocessError):
                        pass
            finally:
                try:
                    self.pty.cancel_io()
                except Exception:
                    pass
                if self.reader.is_alive() and self.reader is not threading.current_thread():
                    self.reader.join(timeout=2)
            if alive() is not False:
                raise ProtocolError("终端子进程未能结束，可重试关闭")
            self._finalized = True


class TerminalManager:
    MAX_SESSIONS = 4
    MAX_IDLE_SECONDS = 15 * 60
    REAP_INTERVAL_SECONDS = 30

    def __init__(self, root, enabled=False, allowed_roots=()):
        self.root = Path(root).resolve()
        self.allowed_roots = {self.root, *(Path(path).resolve() for path in allowed_roots)}
        self.enabled = bool(enabled)
        self.lock = threading.Lock()
        self.sessions = {}
        self._stop = threading.Event()
        self._janitor = None
        if self.enabled:
            self._janitor = threading.Thread(
                target=self._janitor_loop, daemon=True, name="popper-terminal-janitor")
            self._janitor.start()

    def capability(self):
        """Describe the actual local terminal boundary for the workbench UI."""
        if not self.enabled:
            return {"available": False, "reason": "请以 --trusted-local 启动工作台"}
        if os.name != "nt":
            return {"available": False, "reason": "当前仅支持 Windows ConPTY"}
        if importlib.util.find_spec("winpty") is None:
            return {"available": False, "reason": "请安装 workstation 可选依赖 pywinpty"}
        if not (shutil.which("pwsh") or shutil.which("powershell")):
            return {"available": False, "reason": "找不到 PowerShell/pwsh"}
        return {"available": True, "reason": None}

    def _janitor_loop(self):
        while not self._stop.wait(self.REAP_INTERVAL_SECONDS):
            self.prune_idle()

    def prune_idle(self):
        now = time.monotonic()
        with self.lock:
            expired = [(session_id, session) for session_id, session in self.sessions.items()
                       if session.closed or now - session.last_activity >= self.MAX_IDLE_SECONDS]
        reaped = []
        for session_id, session in expired:
            try:
                session.close()
            except Exception:
                continue  # retain the handle and retry on the next sweep
            with self.lock:
                if self.sessions.get(session_id) is session:
                    del self.sessions[session_id]
                    reaped.append(session_id)
        return reaped

    def _require_enabled(self):
        if not self.enabled:
            raise ProtocolError("真终端仅在 --trusted-local 模式下可用")

    def create(self, cols, rows, root=None):
        self._require_enabled()
        TerminalSession.validate_size(cols, rows)
        requested_root = self.root if root is None else Path(root).resolve()
        if requested_root not in self.allowed_roots or not requested_root.is_dir():
            raise ProtocolError("终端工作区根未登记或不存在")
        self.prune_idle()
        with self.lock:
            if len(self.sessions) >= self.MAX_SESSIONS:
                raise ProtocolError("终端会话已达上限，请先关闭旧终端")
            session = TerminalSession(requested_root, cols, rows)
            self.sessions[session.id] = session
        return session.read_after(0)

    def close(self, session_id):
        self._require_enabled()
        if not isinstance(session_id, str):
            raise ProtocolError("缺失终端会话 id")
        with self.lock:
            session = self.sessions.get(session_id)
        if session is not None:
            session.close()
            with self.lock:
                if self.sessions.get(session_id) is session:
                    del self.sessions[session_id]
        return {"id": session_id, "alive": False}

    def get(self, session_id):
        self._require_enabled()
        if not isinstance(session_id, str):
            raise ProtocolError("缺失终端会话 id")
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise ProtocolError("终端会话不存在")
        return session

    def has_sessions_in_root(self, root):
        resolved = Path(root).resolve()
        with self.lock:
            return any(session.root == resolved for session in self.sessions.values())

    def close_all(self):
        self._stop.set()
        if self._janitor is not None and self._janitor is not threading.current_thread():
            self._janitor.join(timeout=2)
        with self.lock:
            sessions = list(self.sessions.items())
        failures = []
        for session_id, session in sessions:
            try:
                session.close()
            except Exception:
                failures.append(session_id)
                continue
            with self.lock:
                if self.sessions.get(session_id) is session:
                    del self.sessions[session_id]
        if failures:
            raise ProtocolError(f"终端子进程未能结束: {len(failures)} 个会话")
