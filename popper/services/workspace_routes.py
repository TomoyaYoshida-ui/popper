"""Workspace Routes responsibility service."""
from __future__ import annotations
from .base import HostService

class WorkspaceRoutesService(HostService):

    def _post_file(self, payload):
        """写回已登记的物化文本稿件（md/tex/json）。仅覆写，防穿越/防新建。"""
        ProtocolError = self.dependencies.ProtocolError
        file_hash = self.dependencies.file_hash
        if not isinstance(payload, dict):
            raise ProtocolError('请求必须为 JSON 对象')
        item = payload.get('item')
        content = payload.get('content')
        expected_sha256 = payload.get('expected_sha256')
        if not isinstance(item, str) or not isinstance(content, str):
            raise ProtocolError('item 与 content 必须为字符串')
        with self.server.ws_lock:
            target = self.server.workspace.write_deliverable(item, content, expected_sha256=expected_sha256)
        self.send(200, {'ok': True, 'item': item, 'filename': target.name, 'bytes': target.stat().st_size, 'sha256': file_hash(target)})

    def _post_workspace(self, payload):
        ProtocolError = self.dependencies.ProtocolError
        if not isinstance(payload, dict) or not isinstance(payload.get('op'), str):
            raise ProtocolError('请求必须含 op 字符串')
        op = payload['op']
        if op in ('workspace_switch', 'workspace_remove'):
            wid = payload.get('wid')
            if not isinstance(wid, str) or not wid:
                raise ProtocolError('缺少 wid')
            with self.server.ws_lock:
                if op == 'workspace_switch':
                    self.server.workspaces.switch(wid)
                    self.server._activate_workspace_project()
                else:
                    entry = next((row for row in self.server.workspaces.roots_payload() if row['wid'] == wid), None)
                    if entry is not None and (not entry.get('primary')) and self.server.terminals.has_sessions_in_root(entry['root']):
                        raise ProtocolError('工作区仍有终端会话，请先关闭终端')
                    self.server.workspaces.remove(wid)
                    self.server._activate_workspace_project()
            self.send(200, {'ok': True, **self.server.workspace_payload()})
            return
        with self.server.ws_lock:
            data = self.server.workspace.read()
            self.server.workspace.apply_operation(data, payload)
            self.server.workspace.save(data)
        self.send(200, {'ok': True, 'folders': data['folders']})
