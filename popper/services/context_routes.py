"""Context Routes responsibility service."""
from __future__ import annotations
from .base import HostService

class ContextRoutesService(HostService):

    def _handle_task_context(self, method, route, parts, query):
        ErrorCode = self.dependencies.ErrorCode
        ProtocolError = self.dependencies.ProtocolError
        SessionMetadataConflict = self.dependencies.SessionMetadataConflict
        SessionNotFound = self.dependencies.SessionNotFound
        api_failure = self.dependencies.api_failure
        api_success = self.dependencies.api_success
        json = self.dependencies.json
        new_trace_id = self.dependencies.new_trace_id
        task_memory = self.dependencies.task_memory
        session_route = len(parts) == 5 and parts[:3] == ['api', 'v1', 'sessions'] and (parts[4] == 'context')
        if route not in {'/api/v1/context/tasks', '/api/v1/context/preview'} and (not session_route):
            return False
        trace = new_trace_id()
        try:
            if not self.authorized() or (method == 'POST' and (not self.origin_ok())):
                self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Same-origin local session required', trace_id=trace))
                return True
            if method == 'GET':
                if route == '/api/v1/context/preview' or set(query) != {'workspace_id'} or len(query['workspace_id']) != 1:
                    raise ProtocolError('任务上下文查询需指定唯一项目')
                result = self.server.task_context_payload(query['workspace_id'][0], parts[3] if session_route else None)
            else:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 131072:
                    raise ProtocolError('交接请求长度不合法')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or data.get('schema_version') != '1.0' or (not isinstance(data.get('workspace_id'), str)):
                    raise ProtocolError('交接信封不合法')
                if route == '/api/v1/context/preview':
                    if set(data) - {'schema_version', 'workspace_id', 'session_id', 'task_id', 'source_session_id', 'study_ids'} or not isinstance(data.get('session_id'), str):
                        raise ProtocolError('交接预览字段不合法')
                    result = self.server.task_context_preview(data['workspace_id'], data['session_id'], data.get('task_id'), data.get('source_session_id'), data.get('study_ids'))
                else:
                    sid = parts[3] if session_route else data.get('session_id')
                    if not isinstance(sid, str) or data.get('op') not in {'adopt', 'detach'} or set(data) - {'schema_version', 'workspace_id', 'session_id', 'op', 'preview', 'fields', 'title', 'idempotency_key', 'expected_binding_version', 'status'}:
                        raise ProtocolError('交接写请求字段不合法')
                    result = self.server.commit_task_context(data['workspace_id'], sid, data)
            self.send(200, api_success(result, trace_id=trace))
        except (task_memory.ContextConflict, SessionMetadataConflict) as error:
            self.send(409, api_failure(ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace))
        except (SessionNotFound, FileNotFoundError) as error:
            self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace))
        except (ProtocolError, ValueError, TypeError) as error:
            self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace))
        except Exception:
            self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, '无法读取任务上下文，请重试', trace_id=trace, retryable=True))
        return True
