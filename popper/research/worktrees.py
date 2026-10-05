"""Persistent isolated Git worktrees derived from immutable CodeRevisions."""
from __future__ import annotations

import secrets
import re
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from ..core import ProtocolError, canonical, digest, file_hash
from ..eventlog import GENESIS_HASH, chained_body, verify_hash_chain
from ..storage import SQLiteStorageAdapter
from .revisions import CodeEdit, RevisionStore


class RevisionWorktreeManager:
    """Create mutable review worktrees without weakening revision immutability."""

    def __init__(self, registry_path, worktree_root, project, revisions):
        self.registry_path = Path(registry_path).resolve()
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(worktree_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.project = Path(project).resolve()
        self.revisions = (revisions if isinstance(revisions, RevisionStore)
                          else RevisionStore(revisions))
        self._storage = SQLiteStorageAdapter(self.registry_path)
        self._conn = self._storage.connect()
        with self._conn.transaction(write=True):
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS revision_worktrees(
                    worktree_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    revision_id TEXT NOT NULL,
                    git_commit TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created TEXT NOT NULL,
                    removed TEXT,
                    error TEXT,
                    proposal TEXT,
                    proposal_sha256 TEXT,
                    successor_revision_id TEXT,
                    submitted TEXT,
                    execution_run_id TEXT,
                    execution_error TEXT,
                    executed TEXT)
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS worktree_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    worktree_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    created TEXT NOT NULL)
            """)
            columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(revision_worktrees)")}
            for name in ("proposal", "proposal_sha256", "successor_revision_id", "submitted",
                         "execution_run_id", "execution_error", "executed"):
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE revision_worktrees ADD COLUMN {name} TEXT")
        self._migrate_legacy_events()

    def _append_event(self, worktree_id, event_type, payload):
        row = self._conn.execute(
            "SELECT hash FROM worktree_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row[0] if row else GENESIS_HASH
        created = datetime.now().astimezone().isoformat(timespec="seconds")
        body, event_hash = chained_body(
            {"worktree_id": worktree_id, "event_type": event_type,
             "payload": payload, "created": created}, previous)
        self._conn.execute(
            "INSERT INTO worktree_events(worktree_id,event_type,payload,previous_hash,hash,created) "
            "VALUES(?,?,?,?,?,?)",
            (worktree_id, event_type, canonical(payload), previous, event_hash, created))
        return event_hash

    def _migrate_legacy_events(self):
        rows = self._conn.execute(
            "SELECT w.worktree_id,w.revision_id,w.git_commit,w.idempotency_key,w.status,"
            "w.proposal_sha256,w.successor_revision_id FROM revision_worktrees w "
            "WHERE NOT EXISTS(SELECT 1 FROM worktree_events e "
            "WHERE e.worktree_id=w.worktree_id) ORDER BY w.created,w.worktree_id").fetchall()
        if not rows:
            return
        with self._conn.transaction(write=True):
            for worktree_id, revision_id, commit, idem, status, proposal_sha, successor in rows:
                self._append_event(worktree_id, "provisioning", {
                    "revision_id": revision_id, "git_commit": commit,
                    "idempotency_key": idem, "migrated": True})
                if status == "provisioning":
                    continue
                if status == "failed":
                    self._append_event(worktree_id, "failed", {
                        "error_type": "legacy_unknown", "migrated": True})
                    continue
                self._append_event(worktree_id, "activated", {
                    "revision_id": revision_id, "migrated": True})
                if status in {"review_required", "submitted"} or proposal_sha:
                    self._append_event(worktree_id, "proposal_frozen", {
                        "proposal_sha256": proposal_sha,
                        "actor": "legacy_unknown", "edit_paths": [],
                        "migrated": True})
                if status == "submitted" or successor:
                    self._append_event(worktree_id, "submitted", {
                        "proposal_sha256": proposal_sha,
                        "successor_revision_id": successor,
                        "actor": "legacy_unknown", "migrated": True})
                if status == "removed":
                    self._append_event(worktree_id, "removed", {"migrated": True})

    def close(self):
        self._conn.close()

    def _require_integrity(self):
        integrity = self.verify()
        if not integrity["ok"]:
            raise ProtocolError(f"worktree 事件存储完整性失败: {integrity['reason']}")

    def recover_provisioning(self):
        """Reconcile worktrees left between Git creation and activation.

        The caller must own the project workstation lease, so no live creator
        can still be overlaying revision files. Incomplete paths are retained
        for inspection rather than deleted during startup.
        """
        self._require_integrity()
        ids = [row[0] for row in self._conn.execute(
            "SELECT worktree_id FROM revision_worktrees WHERE status='provisioning' "
            "ORDER BY created,worktree_id")]
        recovered = []
        for worktree_id in ids:
            record = self.get(worktree_id)
            target = Path(record["path"]).resolve()
            if target.parent != self.root or target.name != worktree_id:
                raise ProtocolError("provisioning worktree 登记路径不安全")
            complete = False
            try:
                revision = self.revisions.verify(record["revision_id"])
                source_control = revision["identity"].get("source_control") or {}
                project_path = source_control.get("project_path")
                checkout = target if project_path == "." else target / project_path
                complete = (source_control.get("kind") == "git"
                            and source_control.get("head_commit") == record["git_commit"]
                            and target.is_dir()
                            and (target / ".git").is_file()
                            and self._git(target, "rev-parse", "HEAD") == record["git_commit"]
                            and Path(self._git(target, "rev-parse", "--show-toplevel")).resolve()
                            == target
                            and checkout.is_dir()
                            and checkout.resolve().is_relative_to(target))
                if complete:
                    checkout_root = checkout.resolve()
                    for name, expected in revision["files"].items():
                        candidate = (checkout / name).resolve()
                        if (not candidate.is_relative_to(checkout_root)
                                or not candidate.is_file()
                                or file_hash(candidate) != expected):
                            complete = False
                            break
            except (OSError, ProtocolError, ValueError, TypeError):
                complete = False
            status = "active" if complete else "failed"
            event = "activated" if complete else "failed"
            error = None if complete else "InterruptedProvisioning: incomplete Git worktree"
            with self._conn.transaction(write=True):
                changed = self._conn.execute(
                    "UPDATE revision_worktrees SET status=?,error=? "
                    "WHERE worktree_id=? AND status='provisioning'",
                    (status, error, worktree_id)).rowcount
                if changed != 1:
                    continue
                payload = ({"revision_id": record["revision_id"], "recovered": True}
                           if complete else {"error_type": "InterruptedProvisioning",
                                             "recovered": True, "path_exists": target.exists()})
                self._append_event(worktree_id, event, payload)
            recovered.append({"worktree_id": worktree_id, "status": status})
        return recovered

    @staticmethod
    def _git(project, *args):
        try:
            return subprocess.run(
                ["git", "-C", str(project), *args], capture_output=True,
                check=True, timeout=30, text=True).stdout.rstrip("\r\n")
        except (OSError, subprocess.SubprocessError) as error:
            raise ProtocolError(f"Git worktree 操作失败: {type(error).__name__}") from None

    def _repo(self, source_control):
        root = Path(self._git(self.project, "rev-parse", "--show-toplevel")).resolve()
        if not self.project.is_relative_to(root):
            raise ProtocolError("项目路径不属于 Git 仓库")
        relative = self.project.relative_to(root).as_posix() or "."
        if relative != source_control.get("project_path"):
            raise ProtocolError("当前项目路径与 revision 的 Git 锚点不一致")
        if self.root.is_relative_to(root):
            raise ProtocolError("隔离 worktree 根目录不能位于源 Git worktree 内")
        commit = source_control.get("head_commit")
        self._git(root, "cat-file", "-e", f"{commit}^{{commit}}")
        return root, commit, relative

    def create(self, revision_id, idempotency_key=None):
        self._require_integrity()
        idempotency_key = idempotency_key or ("internal-" + secrets.token_hex(12))
        if (not isinstance(idempotency_key, str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key)):
            raise ProtocolError("worktree idempotency_key 格式不合法")
        prior = self._conn.execute(
            "SELECT worktree_id,revision_id FROM revision_worktrees WHERE idempotency_key=?",
            (idempotency_key,)).fetchone()
        if prior is not None:
            if prior[1] != revision_id:
                raise ProtocolError("worktree idempotency_key 已用于不同 revision")
            return self.get(prior[0])
        revision = self.revisions.verify(revision_id)
        source_control = revision["identity"].get("source_control") or {}
        if source_control.get("kind") != "git":
            raise ProtocolError("只有带 Git commit 锚点的 revision 可以创建隔离 worktree")
        repo, commit, project_path = self._repo(source_control)
        worktree_id = "wkt_" + secrets.token_hex(12)
        target = (self.root / worktree_id).resolve()
        if not target.is_relative_to(self.root) or target.exists():
            raise ProtocolError("worktree 目标路径不安全或已存在")
        created = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._conn.transaction(write=True):
            prior = self._conn.execute(
                "SELECT worktree_id,revision_id FROM revision_worktrees WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if prior is not None:
                if prior[1] != revision_id:
                    raise ProtocolError("worktree idempotency_key 已用于不同 revision")
                return self.get(prior[0])
            self._conn.execute(
                "INSERT INTO revision_worktrees(worktree_id,idempotency_key,revision_id,git_commit,path,status,created) "
                "VALUES(?,?,?,?,?,?,?)",
                (worktree_id, idempotency_key, revision_id, commit, str(target),
                 "provisioning", created))
            self._append_event(worktree_id, "provisioning", {
                "revision_id": revision_id, "git_commit": commit,
                "idempotency_key": idempotency_key})
        try:
            self._git(repo, "worktree", "add", "--detach", str(target), commit)
            checkout = target if project_path == "." else target / project_path
            revision_code = self.revisions.path(revision_id) / "code"
            for name, expected in revision["files"].items():
                source = revision_code / name
                destination = (checkout / name).resolve()
                if not destination.is_relative_to(checkout.resolve()):
                    raise ProtocolError("revision 文件路径越出 worktree 项目")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                if file_hash(destination) != expected:
                    raise ProtocolError("worktree 中的 revision 文件摘要不匹配")
            with self._conn.transaction(write=True):
                self._conn.execute(
                    "UPDATE revision_worktrees SET status='active' WHERE worktree_id=?",
                    (worktree_id,))
                self._append_event(worktree_id, "activated", {
                    "revision_id": revision_id})
            return self.get(worktree_id)
        except Exception as error:
            try:
                if target.exists():
                    self._git(repo, "worktree", "remove", "--force", str(target))
            except Exception:
                pass
            with self._conn.transaction(write=True):
                self._conn.execute(
                    "UPDATE revision_worktrees SET status='failed', error=? WHERE worktree_id=?",
                    (f"{type(error).__name__}: {error}", worktree_id))
                self._append_event(worktree_id, "failed", {
                    "error_type": type(error).__name__})
            raise

    def remove(self, worktree_id):
        self._require_integrity()
        record = self.get(worktree_id)
        if record["status"] == "removed":
            return record
        if record["status"] not in {"failed", "active", "review_required", "submitted",
                                    "executed", "execution_failed", "execution_cancelled"}:
            raise ProtocolError("只有失败、可编辑、待审批或已提交 worktree 可以移除")
        target = Path(record["path"]).resolve()
        if target.parent != self.root or target.name != worktree_id:
            raise ProtocolError("worktree 登记路径不安全")
        source_control = self.revisions.verify(
            record["revision_id"])["identity"]["source_control"]
        repo, _, _ = self._repo(source_control)
        if record["status"] == "failed":
            # A crash before Git creation leaves no directory. If one exists,
            # never recursively remove an arbitrary replacement at this path.
            if target.exists():
                if (not (target / ".git").is_file()
                        or Path(self._git(target, "rev-parse", "--show-toplevel")).resolve()
                        != target
                        or self._git(target, "rev-parse", "HEAD") != record["git_commit"]):
                    raise ProtocolError("失败 worktree 路径不是原 Git 副本，拒绝移除")
                self._git(repo, "worktree", "remove", "--force", str(target))
        else:
            if target.exists():
                self._git(repo, "worktree", "remove", "--force", str(target))
        # A process can die after Git removes the directory but before SQLite
        # records 'removed'. Only complete the retry if Git has no stale
        # worktree registration for this path; otherwise manual repair is needed.
        registered = any(
            Path(line[len("worktree "):]).resolve() == target
            for line in self._git(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree "))
        if registered:
            raise ProtocolError("Git 仍登记该 worktree，拒绝完成移除")
        removed = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_worktrees SET status='removed', removed=? "
                "WHERE worktree_id=? AND status=?",
                (removed, worktree_id, record["status"])).rowcount
            if changed != 1:
                latest = self.get(worktree_id)
                if latest["status"] == "removed":
                    return latest
                raise ProtocolError("worktree 移除状态已并发变化")
            self._append_event(worktree_id, "removed", {"removed": removed})
        return self.get(worktree_id)

    def begin_execution(self, worktree_id):
        self._require_integrity()
        record = self.get(worktree_id)
        if record["status"] == "executing":
            return record
        if record["status"] != "submitted":
            raise ProtocolError("只有已提交后继 revision 的 worktree 可以执行")
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_worktrees SET status='executing', execution_error=NULL "
                "WHERE worktree_id=? AND status='submitted'", (worktree_id,))
            if changed.rowcount != 1:
                raise ProtocolError("worktree 执行状态已并发变化")
            self._append_event(worktree_id, "execution_started", {
                "revision_id": record["successor_revision_id"]})
        return self.get(worktree_id)

    def finish_execution(self, worktree_id, *, run_id=None, error=None, cancelled=False):
        self._require_integrity()
        record = self.get(worktree_id)
        if record["status"] in {"executed", "execution_failed", "execution_cancelled"}:
            return record
        if record["status"] != "executing":
            raise ProtocolError("worktree 后继 revision 尚未开始执行")
        if (run_id is None) == (error is None):
            raise ProtocolError("worktree 执行结果必须且只能包含 run_id 或 error")
        if cancelled and error is None:
            raise ProtocolError("worktree 取消终态必须包含原因")
        executed = datetime.now().astimezone().isoformat(timespec="seconds")
        status = ("executed" if run_id is not None else
                  "execution_cancelled" if cancelled else "execution_failed")
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_worktrees SET status=?,execution_run_id=?,execution_error=?,"
                "executed=? WHERE worktree_id=? AND status='executing'",
                (status, run_id, error, executed, worktree_id))
            if changed.rowcount != 1:
                raise ProtocolError("worktree 执行完成状态已并发变化")
            self._append_event(worktree_id, status, {
                "revision_id": record["successor_revision_id"],
                "run_id": run_id, "error": error})
        return self.get(worktree_id)

    def _proposal(self, record, actor):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise ProtocolError("worktree proposal actor 不合法")
        revision = self.revisions.verify(record["revision_id"])
        source_control = revision["identity"].get("source_control") or {}
        repo, _, project_path = self._repo(source_control)
        checkout_root = Path(record["path"]).resolve()
        checkout = checkout_root if project_path == "." else checkout_root / project_path
        output = self._git(
            checkout_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries = [entry for entry in output.split("\0") if entry]
        allowed = set(revision["files"])
        candidates = set()
        prefix = "" if project_path == "." else project_path.rstrip("/") + "/"
        for entry in entries:
            if len(entry) < 4:
                raise ProtocolError("Git status 输出不合法")
            status, repo_path = entry[:2], entry[3:].replace("\\", "/")
            if "R" in status or "C" in status:
                raise ProtocolError("worktree proposal 不允许 rename/copy")
            if prefix and not repo_path.startswith(prefix):
                raise ProtocolError("worktree 包含项目目录之外的修改")
            relative = repo_path[len(prefix):] if prefix else repo_path
            if not relative or relative.startswith("../"):
                raise ProtocolError("worktree 修改路径不合法")
            if "D" in status:
                raise ProtocolError("worktree proposal 不允许删除代码文件")
            if relative in allowed:
                candidates.add(relative)
            elif status == "??" and Path(relative).suffix.lower() == ".py":
                candidates.add(relative)
            else:
                raise ProtocolError("worktree 只能修改 revision 文件或新增 Python 文件")
        edits = []
        revision_code = self.revisions.path(record["revision_id"]) / "code"
        for name in sorted(candidates):
            target = (checkout / name).resolve()
            if not target.is_relative_to(checkout.resolve()) or not target.is_file():
                raise ProtocolError("worktree proposal 文件不存在或越界")
            replacement = target.read_text(encoding="utf-8")
            original_hash = revision["files"].get(name)
            if original_hash is not None and file_hash(target) == original_hash:
                continue
            # Reuse CodeEdit validation now, before a human is asked to approve.
            edit = CodeEdit(name, replacement, original_hash)
            edits.append({"path": edit.path, "replacement": edit.replacement,
                          "original_sha256": edit.original_sha256})
        if not edits:
            raise ProtocolError("worktree 没有相对父 revision 的实际代码修改")
        identity = revision["identity"]
        proposal = {"schema_version": "1.0", "kind": "worktree_revision_proposal",
                    "worktree_id": record["worktree_id"],
                    "parent_revision_id": record["revision_id"],
                    "hypothesis_id": identity.get("hypothesis_id"),
                    "design_id": identity.get("design_id"),
                    "actor": actor.strip(), "edits": edits}
        return proposal, digest(proposal)

    def propose(self, worktree_id, actor):
        self._require_integrity()
        record = self.get(worktree_id)
        if record["status"] == "review_required":
            raise ProtocolError("worktree 已有冻结提案，必须审批或移除")
        if record["status"] != "active":
            raise ProtocolError("只有 active worktree 可以创建后继 revision 提案")
        proposal, proposal_sha256 = self._proposal(record, actor)
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_worktrees SET status='review_required', proposal=?, "
                "proposal_sha256=? WHERE worktree_id=? AND status='active'",
                (canonical(proposal), proposal_sha256, worktree_id))
            if changed.rowcount != 1:
                raise ProtocolError("worktree 状态已并发变化")
            self._append_event(worktree_id, "proposal_frozen", {
                "proposal_sha256": proposal_sha256,
                "actor": proposal["actor"],
                "edit_paths": [item["path"] for item in proposal["edits"]]})
        return self.get(worktree_id)

    def submit(self, worktree_id, proposal_sha256, actor):
        self._require_integrity()
        record = self.get(worktree_id)
        if record["status"] == "submitted":
            if record["proposal_sha256"] != proposal_sha256:
                raise ProtocolError("审批摘要与已提交提案不一致")
            return record
        if record["status"] != "review_required":
            raise ProtocolError("worktree 当前没有待审批提案")
        if record["proposal_sha256"] != proposal_sha256:
            raise ProtocolError("审批摘要与冻结提案不一致")
        proposal = json.loads(record["proposal"])
        if proposal["actor"] != actor:
            raise ProtocolError("审批 actor 与提案 actor 不一致")
        edits = [CodeEdit(item["path"], item["replacement"], item["original_sha256"])
                 for item in proposal["edits"]]
        revision = self.revisions.create(
            self.project, proposal["hypothesis_id"], proposal["design_id"], edits,
            actor=actor, parent_revision_id=proposal["parent_revision_id"])
        submitted = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_worktrees SET status='submitted', successor_revision_id=?, "
                "submitted=? WHERE worktree_id=? AND status='review_required' "
                "AND proposal_sha256=?",
                (revision["revision_id"], submitted, worktree_id, proposal_sha256))
            if changed.rowcount != 1:
                raise ProtocolError("worktree 提案状态已并发变化")
            self._append_event(worktree_id, "submitted", {
                "proposal_sha256": proposal_sha256,
                "successor_revision_id": revision["revision_id"],
                "actor": actor})
        return self.get(worktree_id)

    def get(self, worktree_id):
        if (not isinstance(worktree_id, str) or not worktree_id.startswith("wkt_")
                or len(worktree_id) != 28
                or any(ch not in "0123456789abcdef" for ch in worktree_id[4:])):
            raise ProtocolError("worktree_id 格式不合法")
        row = self._conn.execute(
            "SELECT worktree_id,idempotency_key,revision_id,git_commit,path,status,created,removed,error,"
            "proposal,proposal_sha256,successor_revision_id,submitted,"
            "execution_run_id,execution_error,executed "
            "FROM revision_worktrees WHERE worktree_id=?", (worktree_id,)).fetchone()
        if row is None:
            raise ProtocolError("worktree 不存在")
        keys = ("worktree_id", "idempotency_key", "revision_id", "git_commit", "path", "status",
                "created", "removed", "error", "proposal", "proposal_sha256",
                "successor_revision_id", "submitted", "execution_run_id",
                "execution_error", "executed")
        return dict(zip(keys, row))

    def list(self):
        rows = self._conn.execute(
            "SELECT worktree_id FROM revision_worktrees ORDER BY created, worktree_id").fetchall()
        return [self.get(row[0]) for row in rows]

    def history(self, worktree_id):
        self.get(worktree_id)
        rows = self._conn.execute(
            "SELECT seq,event_type,payload,previous_hash,hash,created FROM worktree_events "
            "WHERE worktree_id=? ORDER BY seq", (worktree_id,)).fetchall()
        return [{"seq": row[0], "event_type": row[1],
                 "payload": json.loads(row[2]), "previous_hash": row[3],
                 "hash": row[4], "created": row[5]} for row in rows]

    def verify(self):
        projected = {}
        attested = {}
        transitions = {
            "provisioning": (None, "provisioning"),
            "activated": ("provisioning", "active"),
            "failed": ("provisioning", "failed"),
            "proposal_frozen": ("active", "review_required"),
            "submitted": ("review_required", "submitted"),
            "execution_started": ("submitted", "executing"),
            "executed": ("executing", "executed"),
            "execution_failed": ("executing", "execution_failed"),
            "execution_cancelled": ("executing", "execution_cancelled"),
            "removed": ({"failed", "active", "review_required", "submitted", "executed",
                         "execution_failed", "execution_cancelled"}, "removed"),
        }
        rows = list(self._conn.execute(
            "SELECT seq,worktree_id,event_type,payload,previous_hash,hash,created "
            "FROM worktree_events ORDER BY seq"))
        integrity = verify_hash_chain(
            rows,
            lambda row: {"worktree_id": row[1], "event_type": row[2],
                         "payload": json.loads(row[3]), "created": row[6]},
            lambda row: row[4], lambda row: row[5],
            label="worktree 事件链", seq_for=lambda row: row[0])
        if not integrity["ok"]:
            return integrity
        for seq, worktree_id, event_type, payload_text, prior, event_hash, created in rows:
            payload = json.loads(payload_text)
            if event_type not in transitions:
                return {"ok": False, "reason": f"未知 worktree 事件: {event_type}"}
            expected, target = transitions[event_type]
            current = projected.get(worktree_id)
            if ((isinstance(expected, set) and current not in expected)
                    or (not isinstance(expected, set) and current != expected)):
                return {"ok": False,
                        "reason": f"worktree 状态重放失败: {worktree_id}/{event_type}"}
            projected[worktree_id] = target
            if event_type == "provisioning":
                attested[worktree_id] = {
                    "revision_id": payload.get("revision_id"),
                    "git_commit": payload.get("git_commit"),
                    "idempotency_key": payload.get("idempotency_key"),
                    "migrated": payload.get("migrated") is True,
                    "proposal_sha256": None,
                    "successor_revision_id": None,
                    "execution_run_id": None,
                    "execution_error": None,
                }
            elif event_type == "proposal_frozen":
                attested[worktree_id]["proposal_sha256"] = payload.get("proposal_sha256")
            elif event_type == "submitted":
                attested[worktree_id]["successor_revision_id"] = payload.get(
                    "successor_revision_id")
            elif event_type == "executed":
                attested[worktree_id]["execution_run_id"] = payload.get("run_id")
            elif event_type in {"execution_failed", "execution_cancelled"}:
                attested[worktree_id]["execution_error"] = payload.get("error")
            elif event_type == "removed" and "removed" in payload:
                attested[worktree_id]["removed"] = payload["removed"]
        actual = dict(self._conn.execute(
            "SELECT worktree_id,status FROM revision_worktrees"))
        if actual != projected:
            return {"ok": False, "reason": "worktree 快照与事件重放不一致"}
        for record in self.list():
            expected = attested[record["worktree_id"]]
            for field in ("revision_id", "git_commit", "idempotency_key",
                          "proposal_sha256", "successor_revision_id",
                          "execution_run_id", "execution_error"):
                if record[field] != expected[field]:
                    return {"ok": False,
                            "reason": f"worktree 快照与事件身份不一致: {record['worktree_id']}/{field}"}
            if "removed" in expected and record["removed"] != expected["removed"]:
                return {"ok": False, "reason": "worktree 移除时间与事件不一致"}
            if not expected["migrated"] and record["path"] != str(
                    self.root / record["worktree_id"]):
                return {"ok": False, "reason": "worktree 快照路径与专用根不一致"}
            if record["proposal"] is not None:
                try:
                    proposal_hash = digest(json.loads(record["proposal"]))
                except (ValueError, TypeError):
                    return {"ok": False, "reason": "worktree 冻结提案正文不合法"}
                if proposal_hash != record["proposal_sha256"]:
                    return {"ok": False, "reason": "worktree 冻结提案正文摘要不一致"}
        return {"ok": True, "events": integrity["events"], "head": integrity["head"]}
