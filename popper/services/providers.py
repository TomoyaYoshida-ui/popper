"""Workspace defaults and process-only BYOK credentials."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from ..core import ProtocolError, read_json, write_json
from ..chat import make_chat_client
from ..harness.conversation import adapt_conversation_harness
from ..scoop import make_json_client
from .base import HostService


class ProvidersService(HostService):
    def _saved(self):
        path = Path(self.workspace.root) / ".popper/provider-config.json"
        saved = read_json(path) if path.is_file() else {}
        allowed = {"schema_version", "base_url", "model", "token_budget", "cost_budget_micros"}
        if not isinstance(saved, dict) or set(saved) - allowed:
            raise ProtocolError("工作区模型配置不合法")
        return saved

    def _effective(self):
        saved = self._saved()
        cli_base = getattr(self, "_provider_cli_base", None)
        cli_model = getattr(self, "_provider_cli_model", None)
        base = cli_base or saved.get("base_url")
        model = cli_model or saved.get("model")
        key = getattr(self, "_runtime_provider_keys", {}).get(str(self.workspace.root)) or os.environ.get("POPPER_API_KEY")
        source = "cli" if cli_base else "workspace" if base else "unconfigured"
        if getattr(self, "_provider_injected", False):
            source = "injected"
        return saved, base, model, key, source

    def snapshot(self):
        saved, base, model, key, source = self._effective()
        return {"schema_version": "1.0", "base_url": base or "", "model": model or "",
                "configured": bool(base and model and key) or source == "injected",
                "credential_available": bool(key), "credential_storage": "process_or_environment_only",
                "source": source, "workspace_id": self.workspaces.current_wid(),
                "token_budget": saved.get("token_budget"), "cost_budget_micros": saved.get("cost_budget_micros"),
                "saved_defaults": {"base_url": saved.get("base_url", ""), "model": saved.get("model", "")}}

    def refresh(self):
        if getattr(self, "_provider_injected", False):
            return
        saved, base, model, key, source = self._effective()
        identity = (str(self.workspace.root), base, model, key)
        if identity == getattr(self, "_provider_active_identity", None):
            return
        client = make_chat_client(base, model, api_key=key, enable_file_tools=self.trusted_local) if base and model and key else None
        self.chat_base_url, self.chat_model, self.chat_llm = base, model, client
        self.chat_harness = adapt_conversation_harness(client)
        self._provider_active_identity = identity

    def save(self, value):
        required = {"schema_version", "base_url", "model"}
        allowed = required | {"api_key", "token_budget", "cost_budget_micros", "workspace_id"}
        if (not isinstance(value, dict) or not required <= set(value) or set(value) - allowed
                or value["schema_version"] != "1.0"):
            raise ProtocolError("模型配置字段不合法")
        base, model = value["base_url"], value["model"]
        if not isinstance(base, str) or not isinstance(model, str) or len(base) > 2000 or len(model) > 200:
            raise ProtocolError("模型端点或名称不合法")
        parsed = urlparse(base)
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not model.strip()):
            raise ProtocolError("模型端点必须为 HTTPS 或本机 HTTP，且不能包含凭据")
        for field in ("token_budget", "cost_budget_micros"):
            if value.get(field) is not None and (type(value[field]) is not int or not 1 <= value[field] <= 9007199254740991):
                raise ProtocolError("预算必须为正整数或空值")
        key = value.get("api_key")
        if key is not None and (not isinstance(key, str) or len(key) > 16000):
            raise ProtocolError("凭据格式不合法")
        with self.chat_lock, self.ws_lock:
            self._check_session_workspace(value.get("workspace_id"))
            saved = {"schema_version": "1.0", "base_url": base.rstrip("/"), "model": model.strip(),
                     "token_budget": value.get("token_budget"), "cost_budget_micros": value.get("cost_budget_micros")}
            write_json(Path(self.workspace.root) / ".popper/provider-config.json", saved)
            keys = getattr(self, "_runtime_provider_keys", {})
            if key is not None:
                if key:
                    keys[str(self.workspace.root)] = key
                else:
                    keys.pop(str(self.workspace.root), None)
            self._runtime_provider_keys = keys
            self.refresh()
            return self.snapshot()

    def test(self):
        _, base, model, key, _ = self._effective()
        if not base or not model or not key:
            raise ProtocolError("模型端点、模型名或凭据尚未配置")
        # A short explicit probe, not an unsolicited background request.
        reply = make_json_client(base, model, api_key=key)(
            'Return a JSON object with only {"ok": true}.', {"purpose": "user_requested_connection_test"})
        if reply.get("ok") is not True:
            raise ProtocolError("模型连接测试未返回预期结果")
        return {"ok": True, "model": model, "credential_available": True}
