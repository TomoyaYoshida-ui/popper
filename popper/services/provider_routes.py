from __future__ import annotations

import json
from ..core import ProtocolError
from ..api_contracts import success as api_success, failure as api_failure, ErrorCode, new_trace_id
from .base import HostService


class ProviderRoutesService(HostService):
    def handle(self, method, route):
        if route not in {"/api/v1/provider-config", "/api/v1/provider-config/test"}:
            return False
        trace_id = new_trace_id()
        if not self.authorized() or (method != "GET" and not self.origin_ok()):
            if method != "GET":
                self._drain_rejected_post()
            self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, "Session token and same origin required", trace_id=trace_id))
            return True
        try:
            service = self.server._service("providers")
            if method == "GET" and route == "/api/v1/provider-config":
                result = service.dispatch("snapshot")
            elif method in {"POST", "PUT"}:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 20000:
                    self.close_connection = True
                    raise ProtocolError("模型配置请求长度不合法")
                body = json.loads(self.rfile.read(size))
                result = service.dispatch("test") if route.endswith("/test") else service.dispatch("save", body)
            else:
                self.send(405, api_failure(ErrorCode.INVALID_REQUEST, "Method not supported", trace_id=trace_id))
                return True
            self.send(200, api_success(result, trace_id=trace_id))
        except (ValueError, ProtocolError, OSError):
            self.send(400, api_failure(ErrorCode.INVALID_REQUEST, "模型配置或连接测试失败，请检查端点、模型和凭据", trace_id=trace_id))
        return True
