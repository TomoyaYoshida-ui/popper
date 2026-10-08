"""Workspace responsibility service."""
from __future__ import annotations
from .base import HostService

class WorkspaceService(HostService):

    def _release_leases_after_jobs(self):
        threading = self.dependencies.threading
        if self.wait_for_jobs(timeout=0):

            def release_after_jobs():
                while self.wait_for_jobs(timeout=1):
                    pass
                self._release_workspace_leases()
            self._lease_reaper = threading.Thread(target=release_after_jobs, daemon=True)
            self._lease_reaper.start()
        else:
            self._release_workspace_leases()

    def _release_workspace_leases(self):
        for lease in reversed(self._workspace_leases):
            lease.close()

    @property
    def workspace(self):
        """当前工作区根的 Workspace：多根切换后所有 Quest/产物读写都落在当前根。"""
        return self.workspaces.current()

    def _activate_workspace_project(self):
        """Use an initialized selected root as the active experiment project."""
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        project_mode = self.dependencies.project_mode
        read_json = self.dependencies.read_json
        self.navigation_version = getattr(self, 'navigation_version', 0) + 1
        root = self.workspace.root
        try:
            mode = project_mode(root)
        except ProtocolError:
            root = self.primary_project
            mode = project_mode(root)
        self.project = root
        self.mode = mode
        registry_path = root / '.popper' / 'research-runs.json'
        self._research_run_registry_path = registry_path
        if registry_path.is_file():
            registry = read_json(registry_path)
            if not isinstance(registry, dict) or registry.get('schema_version') != '1.0' or (not isinstance(registry.get('runs'), list)):
                raise ProtocolError('研究运行目录注册表结构不合法')
            with self.research_runs_lock:
                for item in registry['runs']:
                    if not isinstance(item, str) or not item:
                        raise ProtocolError('研究运行目录注册表条目不合法')
                    run = Path(item).resolve()
                    if run not in self.research_runs:
                        self.research_runs.append(run)

    def preview_project_setup(self, project_path, protocol=None, files=None):
        ProtocolError = self.dependencies.ProtocolError
        preview_project_preparation = self.dependencies.preview_project_preparation
        if not self.trusted_local:
            raise ProtocolError('项目准备需要可信本机模式')
        return preview_project_preparation(project_path, protocol, files)

    def prepare_project(self, project_path, protocol, files, approval_sha256):
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        WorkstationLease = self.dependencies.WorkstationLease
        commit_project_preparation = self.dependencies.commit_project_preparation
        if not self.trusted_local:
            raise ProtocolError('项目准备需要可信本机模式')
        with self.study_init_lock:
            result = commit_project_preparation(project_path, protocol, files, approval_sha256)
            root = Path(result['project_path'])
            with self.ws_lock:
                if root not in {Path(row['root']).resolve() for row in self.workspaces.roots_payload()}:
                    lease = WorkstationLease(root)
                    try:
                        self.workspaces.register_cli_roots([root])
                    except Exception:
                        lease.close()
                        raise
                    self._workspace_leases.append(lease)
                wid = next((row['wid'] for row in self.workspaces.roots_payload() if Path(row['root']).resolve() == root))
                self.workspaces.switch(wid)
                self._activate_workspace_project()
                with self.terminals.lock:
                    self.terminals.allowed_roots.add(root)
            return {**result, 'workspace_id': wid}

    def vendor_workspace_root(self):
        """Capture one authorized workspace root for a read-only vendor projection."""
        with self.ws_lock:
            return self.workspace.root

    def workspace_payload(self):
        """构造 GET /api/workspace 的响应（当前根的 Quest 状态 + 根注册表）。"""
        DISCLOSURE_LABELS = self.dependencies.DISCLOSURE_LABELS
        EntityKind = self.dependencies.EntityKind
        TEMPLATE_LABELS = self.dependencies.TEMPLATE_LABELS
        VALID_DISCLOSURES = self.dependencies.VALID_DISCLOSURES
        VALID_TEMPLATES = self.dependencies.VALID_TEMPLATES
        parse_id = self.dependencies.parse_id
        with self.ws_lock:
            ws = self.workspaces.current()
            if ws.is_empty():
                ws.seed_if_empty(ws.project_spec(), ws.current_phase())
            data = ws.read()
            roots = self.workspaces.roots_payload()
            current_root = self.workspaces.current_wid()
            for root in roots:
                root['entity_ref'] = parse_id(root['wid'], EntityKind.WORKSPACE).as_dict()
            for folder in data['folders']:
                for task in folder['tasks']:
                    task['entity_ref'] = parse_id(task['quid'], EntityKind.QUEST).as_dict()
        public_meta = {key: value for (key, value) in data['meta'].items() if not key.startswith('_')}
        return {'schema_version': data['schema_version'], 'meta': public_meta, 'folders': data['folders'], 'deliverables': ws.list_deliverables(data), 'roots': roots, 'current_root': current_root, 'environment_label': getattr(self, 'environment_label', None), 'template_labels': TEMPLATE_LABELS, 'disclosure_labels': DISCLOSURE_LABELS, 'valid_templates': sorted(VALID_TEMPLATES), 'valid_disclosures': sorted((d for d in VALID_DISCLOSURES if d))}

    def project_files_payload(self, *, relative=None, workspace_id=None):
        ProjectFileConflict = self.dependencies.ProjectFileConflict
        ProjectFiles = self.dependencies.ProjectFiles
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('项目代码编辑仅在 --trusted-local 模式下可用')
        with self.ws_lock:
            current_id = self.workspaces.current_wid()
            if workspace_id is not None and workspace_id != current_id:
                raise ProjectFileConflict('工作区已切换，请重新载入项目文件')
            files = ProjectFiles(self.workspace.root)
            result = files.list_files() if relative is None else files.read(relative)
            return {'workspace_id': current_id, **result}

    def save_project_file(self, *, workspace_id, relative, content, expected_sha256):
        ProjectFileConflict = self.dependencies.ProjectFileConflict
        ProjectFiles = self.dependencies.ProjectFiles
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('项目代码编辑仅在 --trusted-local 模式下可用')
        with self.ws_lock:
            if workspace_id != self.workspaces.current_wid():
                raise ProjectFileConflict('工作区已切换，请重新载入项目文件')
            result = ProjectFiles(self.workspace.root).write(relative, content, expected_sha256)
            return {'workspace_id': workspace_id, **result}

    def preview_project_file(self, *, workspace_id, relative, content, expected_sha256):
        ProjectFileConflict = self.dependencies.ProjectFileConflict
        ProjectFiles = self.dependencies.ProjectFiles
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('项目代码编辑仅在 --trusted-local 模式下可用')
        with self.ws_lock:
            if workspace_id != self.workspaces.current_wid():
                raise ProjectFileConflict('工作区已切换，请重新载入项目文件')
            result = ProjectFiles(self.workspace.root).preview_write(relative, content, expected_sha256)
            return {'workspace_id': workspace_id, **result}

    def terminal_command(self, payload):
        """执行经过结构校验的 ConPTY 生命周期操作。"""
        ProtocolError = self.dependencies.ProtocolError
        if not isinstance(payload, dict) or not isinstance(payload.get('op'), str):
            raise ProtocolError('终端请求必须包含 op')
        op = payload['op']
        if op == 'create' and set(payload) == {'op', 'cols', 'rows'}:
            with self.ws_lock:
                root = self.workspaces.current().root
            return self.terminals.create(payload['cols'], payload['rows'], root=root)
        if op == 'read' and set(payload) == {'op', 'id', 'cursor'}:
            if type(payload['cursor']) is not int or payload['cursor'] < 0:
                raise ProtocolError('终端 cursor 不合法')
            return self.terminals.get(payload['id']).read_after(payload['cursor'])
        if op == 'input' and set(payload) == {'op', 'id', 'data'}:
            session = self.terminals.get(payload['id'])
            session.write(payload['data'])
            return {'id': session.id, 'alive': True}
        if op == 'resize' and set(payload) == {'op', 'id', 'cols', 'rows'}:
            session = self.terminals.get(payload['id'])
            session.resize(payload['cols'], payload['rows'])
            return {'id': session.id, 'alive': not session.closed}
        if op == 'close' and set(payload) == {'op', 'id'}:
            return self.terminals.close(payload['id'])
        raise ProtocolError('终端操作或字段不合法')
