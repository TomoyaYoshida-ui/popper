"""Process lifetime lock for a local workstation workspace.

Crash recovery may only run when no other local workstation still owns the
workspace. The operating system releases this lock if its process exits.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from .core import ProtocolError


_registry_lock = threading.Lock()
_held_paths: set[Path] = set()


class WorkstationLease:
    def __init__(self, root):
        resolved_root = Path(root).resolve()
        if not resolved_root.is_dir():
            raise ProtocolError("工作区根不存在或不是目录")
        self.path = resolved_root / ".popper" / "workstation.lock"
        self._file = None
        self._closed = False
        with _registry_lock:
            if self.path in _held_paths:
                raise ProtocolError(f"工作区已有运行中的 Popper 工作台: {root}")
            _held_paths.add(self.path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a+b")
            self._file.seek(0, os.SEEK_END)
            if self._file.tell() == 0:
                self._file.write(b"\0")
                self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as error:
            if self._file is not None:
                self._file.close()
            with _registry_lock:
                _held_paths.discard(self.path)
            raise ProtocolError(f"工作区已有运行中的 Popper 工作台: {root}") from error

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._file is not None:
                try:
                    self._file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                finally:
                    self._file.close()
        finally:
            with _registry_lock:
                _held_paths.discard(self.path)
