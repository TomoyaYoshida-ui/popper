"""Events responsibility service."""
from __future__ import annotations
from .base import HostService

class EventsService(HostService):

    def subscribe(self):
        queue = self.dependencies.queue
        q: queue.Queue = queue.Queue(maxsize=16)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)

    def _broadcast(self, patch):
        canonical = self.dependencies.canonical
        queue = self.dependencies.queue
        json = self.dependencies.json
        frame = ("data: " + canonical(patch) + "\n\n").encode("utf-8")
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.put_nowait(frame)
            except queue.Full:
                merged = {}
                while True:
                    try:
                        pending = subscriber.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        value = json.loads(pending.decode("utf-8")[6:])
                        if value.get("context") == patch.get("context"):
                            merged.update(value)
                    except (ValueError, UnicodeError):
                        continue
                merged.update(patch)
                subscriber.put_nowait(("data: " + canonical(merged) + "\n\n").encode("utf-8"))

    def _fingerprints(self):
        SESSIONS_REL = self.dependencies.SESSIONS_REL
        STATE_DIR = self.dependencies.STATE_DIR
        if self.mode == 'experiment':
            state_src = self.project / '.popper' / 'state.db'
        else:
            state_src = self.project / STATE_DIR
        return {'state': (self._path_sig(state_src), self._job_sig(self.job)), 'workspace': (self._path_sig(self.workspaces.registry_path), self._path_sig(self.workspaces.current().path)), 'campaign': (self._path_sig(self.campaign_dir / 'campaign.json') if self.campaign_dir else None, self._job_sig(self.campaign_job), self._vendor_sig()), 'research': tuple(((str(run_dir), self._path_sig(run_dir / 'research.json'), self._path_sig(run_dir / 'research.sqlite')) for run_dir in self.research_runs)), 'chat': (str(self.workspace.root), self._path_sig(self.workspace.root / SESSIONS_REL), self._path_sig(self.workspace.root / (str(SESSIONS_REL) + '-wal')))}

    def _start_monitor(self):
        threading = self.dependencies.threading
        baseline = self._fingerprints()
        builders = {"state": "state_payload", "workspace": "workspace_payload",
                    "campaign": "campaign_payload", "research": "research_payload", "chat": "chat_payload"}

        def loop():
            consumed = dict(baseline)
            while not self._monitor_stop.wait(1.0):
                try:
                    context = {"workspace_id": self.workspaces.current_wid(),
                               "navigation_version": self.navigation_version}
                    current = self._fingerprints()
                    changed = {name for name in current if current[name] != consumed.get(name)}
                    patch, successful = {}, {}
                    for name in changed:
                        acquired = name != "chat" or self.chat_lock.acquire(blocking=False)
                        if not acquired:
                            continue
                        try:
                            patch[name] = getattr(self, builders[name])()
                            successful[name] = current[name]
                        except Exception as error:
                            errors = getattr(self, "monitor_errors", {})
                            errors[name] = {"error_type": type(error).__name__, "retry_pending": True}
                            self.monitor_errors = errors
                        finally:
                            if name == "chat":
                                self.chat_lock.release()
                    latest = {"workspace_id": self.workspaces.current_wid(),
                              "navigation_version": self.navigation_version}
                    if context != latest:
                        consumed = {}
                        continue
                    if patch:
                        patch["context"] = context
                        self._broadcast(patch)
                        consumed.update(successful)
                        errors = getattr(self, "monitor_errors", {})
                        for name in successful:
                            errors.pop(name, None)
                        self.monitor_errors = errors
                except Exception:
                    # No fingerprint is consumed until a complete patch has been delivered.
                    continue
        self._monitor_thread = threading.Thread(target=loop, daemon=True)
        self._monitor_thread.start()
