"""Http Routes responsibility service."""
from __future__ import annotations
from .base import HostService

class HttpRoutesService(HostService):

    def do_GET(self):
        ARBOR_NODE_STATUS_LABELS = self.dependencies.ARBOR_NODE_STATUS_LABELS
        ErrorCode = self.dependencies.ErrorCode
        ProjectFileConflict = self.dependencies.ProjectFileConflict
        ProtocolError = self.dependencies.ProtocolError
        SessionNotFound = self.dependencies.SessionNotFound
        TurnNotFound = self.dependencies.TurnNotFound
        api_failure = self.dependencies.api_failure
        api_success = self.dependencies.api_success
        hashlib = self.dependencies.hashlib
        list_vendor_runs = self.dependencies.list_vendor_runs
        load_all_arbor = self.dependencies.load_all_arbor
        load_all_literature = self.dependencies.load_all_literature
        new_trace_id = self.dependencies.new_trace_id
        parse_qs = self.dependencies.parse_qs
        urlparse = self.dependencies.urlparse
        if not self.host_ok():
            return self.send(403, {'error': 'Invalid Host'})
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        parts = route.strip('/').split('/')
        if self._handle_provider_config('GET', route):
            return
        if self._handle_task_context('GET', route, parts, query):
            return
        if len(parts) == 5 and parts[:3] == ['api', 'v1', 'research'] and (parts[4] == 'lifecycle'):
            trace_id = new_trace_id()
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id))
            try:
                return self.send(200, api_success(self.server.study_jobs_payload(parts[3]), trace_id=trace_id))
            except (ProtocolError, LookupError, FileNotFoundError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id))
        is_benchmark_list = parts == ['api', 'v1', 'benchmarks']
        is_benchmark_detail = len(parts) == 4 and parts[:3] == ['api', 'v1', 'benchmarks']
        if is_benchmark_list or is_benchmark_detail:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                result = self.server.benchmark_jobs_payload() if is_benchmark_list else self.server.benchmark_job_detail(parts[3])
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except (ProtocolError, ValueError, OSError) as error:
                return self.send(409, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
        is_followup_preview = len(parts) == 5 and parts[:3] == ['api', 'v1', 'research'] and (parts[4] == 'followup-preview')
        is_evidence_preview = len(parts) == 5 and parts[:3] == ['api', 'v1', 'research'] and (parts[4] == 'evidence-preview')
        is_bound_drafts_list = len(parts) == 5 and parts[:3] == ['api', 'v1', 'research'] and (parts[4] == 'bound-drafts')
        is_bound_draft_detail = len(parts) == 6 and parts[:3] == ['api', 'v1', 'research'] and (parts[4] == 'bound-drafts')
        if is_bound_drafts_list or is_bound_draft_detail:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                result = self.server.bound_drafts_payload(parts[3]) if is_bound_drafts_list else self.server.bound_draft_detail(parts[3], parts[5])
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except (FileNotFoundError, LookupError) as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except (ProtocolError, ValueError, KeyError, OSError) as error:
                return self.send(409, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
        if is_evidence_preview:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                return self.send(200, api_success(self.server.research_evidence_preview(parts[3]), trace_id=trace_id), extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except (FileNotFoundError, OSError, ProtocolError, ValueError, KeyError) as error:
                return self.send(409, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
        if is_followup_preview:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                return self.send(200, api_success(self.server.followup_preview(parts[3]), trace_id=trace_id), extra_headers=headers)
            except (FileNotFoundError, LookupError, ProtocolError) as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read follow-up preview', trace_id=trace_id, retryable=True), extra_headers=headers)
        if parts == ['api', 'v1', 'design-jobs']:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                return self.send(200, api_success(self.server.design_jobs_payload(), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read design jobs', trace_id=trace_id, retryable=True), extra_headers=headers)
        is_design_detail = len(parts) == 5 and parts[:3] == ['api', 'v1', 'designs']
        if is_design_detail:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                return self.send(200, api_success(self.server.research_design_for_agent(parts[3], parts[4]), trace_id=trace_id), extra_headers=headers)
            except (FileNotFoundError, LookupError) as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read design', trace_id=trace_id, retryable=True), extra_headers=headers)
        is_design_job = len(parts) == 4 and parts[:3] == ['api', 'v1', 'design-jobs']
        if is_design_job:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                return self.send(200, api_success(self.server.design_job_payload(parts[3]), trace_id=trace_id), extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read design job', trace_id=trace_id, retryable=True), extra_headers=headers)
        is_revision_job = len(parts) == 4 and parts[:3] == ['api', 'v1', 'revision-jobs']
        if is_revision_job:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                return self.send(200, api_success(self.server.revision_job_payload(parts[3]), trace_id=trace_id), extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read revision job', trace_id=trace_id, retryable=True), extra_headers=headers)
        is_worktree_events = len(parts) == 5 and parts[:3] == ['api', 'v1', 'worktrees'] and (parts[4] == 'events')
        if is_worktree_events:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                return self.send(200, api_success(self.server.revision_worktree_events_payload(parts[3]), trace_id=trace_id), extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read worktree events', trace_id=trace_id, retryable=True), extra_headers=headers)
        if route == '/api/v1/worktrees':
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                return self.send(200, api_success(self.server.revision_worktrees_payload(), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read worktrees', trace_id=trace_id, retryable=True), extra_headers=headers)
        is_session_events = len(parts) == 5 and parts[:3] == ['api', 'v1', 'sessions'] and (parts[4] == 'events')
        is_session_tool_events = len(parts) == 5 and parts[:3] == ['api', 'v1', 'sessions'] and (parts[4] == 'tool-events')
        is_project_files = parts == ['api', 'v1', 'project-files']
        is_revision = len(parts) == 4 and parts[:3] == ['api', 'v1', 'revisions']
        if is_revision:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                data = self.server.revision_payload(parts[3])
                return self.send(200, api_success(data, trace_id=trace_id), extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except ProtocolError as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read revision', trace_id=trace_id, retryable=True), extra_headers=headers)
        if is_session_events or is_session_tool_events:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id), extra_headers=headers)
            try:
                after = int(query.get('after', ['0'])[0])
                limit = int(query.get('limit', ['200'])[0])
                data = self.server.session_tool_events_payload(parts[3], after=after, limit=limit) if is_session_tool_events else self.server.session_events_payload(parts[3], after=after, limit=limit)
                return self.send(200, api_success(data, trace_id=trace_id), extra_headers=headers)
            except (SessionNotFound, TurnNotFound) as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read session events', trace_id=trace_id, retryable=True), extra_headers=headers)
        if is_project_files:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized() or not self.server.trusted_local:
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Trusted local session required', trace_id=trace_id), extra_headers=headers)
            try:
                if any((len(values) != 1 for values in query.values())):
                    raise ProtocolError('项目文件查询参数重复')
                if set(query) - {'path', 'workspace_id'}:
                    raise ProtocolError('项目文件查询参数不合法')
                result = self.server.project_files_payload(relative=query.get('path', [None])[0], workspace_id=query.get('workspace_id', [None])[0])
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except ProjectFileConflict as error:
                return self.send(409, api_failure(ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except ProtocolError as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to read project files', trace_id=trace_id, retryable=True), extra_headers=headers)
        if route in ('/api/v1/workspace', '/api/v1/research', '/api/v1/sessions'):
            trace_id = new_trace_id()
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Missing session token', trace_id=trace_id))
            try:
                if route.endswith('/workspace'):
                    data = self.server.workspace_payload()
                elif route.endswith('/research'):
                    data = self.server.research_payload()
                else:
                    data = self.server.sessions_payload(parse_qs(urlparse(self.path).query).get('scope', [None])[0])
                return self.send(200, api_success(data, trace_id=trace_id), extra_headers=[('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)])
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=[('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)])
            except Exception:
                return self.send(500, api_failure(ErrorCode.DEPENDENCY_UNAVAILABLE, 'Unable to build the requested projection', trace_id=trace_id, retryable=True), extra_headers=[('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)])
        if route == '/api/session':
            return self.send(200, {'token': self.server.token})
        if route == '/api/events':
            return self._serve_events(query.get('token', [''])[0])
        if route == '/api/state':
            if not self.authorized():
                return self.send(403, {'error': 'Missing session token'})
            try:
                self.send(200, self.server.state_payload())
            except Exception as error:
                self.send(400, {'error': str(error)})
            return
        if route in ('/api/literature', '/api/arbor', '/api/campaign', '/api/research', '/api/chat'):
            if not self.authorized():
                return self.send(403, {'error': 'Missing session token'})
            try:
                if route == '/api/literature':
                    root = self.server.vendor_workspace_root()
                    self.send(200, {'papers': load_all_literature(root), 'runs': list_vendor_runs(root), 'workspace_root': str(root)})
                elif route == '/api/arbor':
                    root = self.server.vendor_workspace_root()
                    self.send(200, {'trees': load_all_arbor(root), 'runs': list_vendor_runs(root), 'workspace_root': str(root), 'node_status_labels': ARBOR_NODE_STATUS_LABELS})
                elif route == '/api/research':
                    self.send(200, self.server.research_payload())
                elif route == '/api/chat':
                    self.send(200, self.server.chat_payload())
                else:
                    self.send(200, self.server.campaign_payload())
            except Exception as error:
                self.send(400, {'error': str(error)})
            return
        if route == '/api/workspace':
            if not self.authorized():
                return self.send(403, {'error': 'Missing session token'})
            try:
                self.send(200, self.server.workspace_payload())
            except Exception as error:
                self.send(400, {'error': str(error)})
            return
        if route == '/api/file':
            if not self.authorized():
                return self.send(403, {'error': 'Missing session token'})
            try:
                item = query.get('item', [None])[0]
                if not item:
                    raise ProtocolError('缺失 item 参数')
                with self.server.ws_lock:
                    target = self.server.workspace.deliverable_path(item)
                mime = self._MIME.get(target.suffix.lower(), 'application/octet-stream')
                content = target.read_bytes()
                self.send(200, content, mime, extra_headers=[('Content-Disposition', f'attachment; filename="{target.name}"'), ('X-Popper-Content-SHA256', hashlib.sha256(content).hexdigest())])
            except ProtocolError as error:
                self.send(400, {'error': str(error)})
            return
        return self._serve_static()

    def _serve_events(self, token):
        """SSE 长连接：先推一帧全量基线，之后只推变化资源的完整补丁。"""
        canonical = self.dependencies.canonical
        queue = self.dependencies.queue
        secrets = self.dependencies.secrets
        if not secrets.compare_digest(token or '', self.server.token):
            return self.send(403, {'error': 'Missing session token'})
        try:
            context = {'workspace_id': self.server.workspaces.current_wid(), 'navigation_version': self.server.navigation_version}
            full = {'state': self.server.state_payload(), 'workspace': self.server.workspace_payload(), 'campaign': self.server.campaign_payload(), 'research': self.server.research_payload(), 'chat': self.server.chat_payload(), 'context': context}
            if context != {'workspace_id': self.server.workspaces.current_wid(), 'navigation_version': self.server.navigation_version}:
                return self.send(409, {'error': '导航已变化，请重新连接'})
        except Exception as error:
            return self.send(400, {'error': str(error)})
        q = self.server.subscribe()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(('data: ' + canonical(full) + '\n\n').encode('utf-8'))
            self.wfile.flush()
            while True:
                try:
                    frame = q.get(timeout=25)
                except queue.Empty:
                    frame = b': keepalive\n\n'
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.server.unsubscribe(q)

    def do_POST(self):
        ErrorCode = self.dependencies.ErrorCode
        ProjectFileConflict = self.dependencies.ProjectFileConflict
        ProtocolError = self.dependencies.ProtocolError
        ResearchIdempotencyConflict = self.dependencies.ResearchIdempotencyConflict
        ResearchStore = self.dependencies.ResearchStore
        ResearchVersionConflict = self.dependencies.ResearchVersionConflict
        SessionBudgetExceeded = self.dependencies.SessionBudgetExceeded
        SessionIdempotencyConflict = self.dependencies.SessionIdempotencyConflict
        SessionMetadataConflict = self.dependencies.SessionMetadataConflict
        SessionNotFound = self.dependencies.SessionNotFound
        ToolCallConflict = self.dependencies.ToolCallConflict
        ToolCallNotFound = self.dependencies.ToolCallNotFound
        TransitionError = self.dependencies.TransitionError
        TurnNotFound = self.dependencies.TurnNotFound
        WorkspaceIdempotencyConflict = self.dependencies.WorkspaceIdempotencyConflict
        WorkspaceVersionConflict = self.dependencies.WorkspaceVersionConflict
        api_failure = self.dependencies.api_failure
        api_success = self.dependencies.api_success
        json = self.dependencies.json
        new_trace_id = self.dependencies.new_trace_id
        preview_study_action = self.dependencies.preview_study_action
        urlparse = self.dependencies.urlparse
        route = urlparse(self.path).path
        tool_parts = route.strip('/').split('/')
        if self._handle_provider_config('POST', route):
            return
        if self._handle_task_context('POST', route, tool_parts, {}):
            return
        if route == '/api/research/resume':
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            authorized = self.authorized()
            if authorized and (not self.server.trusted_local):
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Trusted local session required', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ProtocolError('请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                if not isinstance(envelope, dict):
                    raise ProtocolError('续作意图请求体必须是对象')
                allowed = {'text', 'study_id', 'run_dir', 'refs'}
                if set(envelope) - allowed:
                    raise ProtocolError('续作意图请求字段不合法')
                if 'text' not in envelope:
                    raise ProtocolError('续作意图缺少 text')
                refs = envelope.get('refs')
                if refs is not None and not isinstance(refs, list):
                    raise ProtocolError('refs 必须是字符串数组')
                result = self.server.record_resume_checkpoint_from_workbench(
                    envelope['text'],
                    study_id=envelope.get('study_id'),
                    run_dir=envelope.get('run_dir'),
                    refs=refs)
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Unable to record resume checkpoint', trace_id=trace_id, retryable=True), extra_headers=headers)
        if route in {'/api/v1/research/projects/preview', '/api/v1/research/projects'}:
            trace_id = new_trace_id()
            authorized = self.authorized()
            if authorized and (not self.server.trusted_local):
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local or (not self.origin_ok()):
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Trusted same-origin local session required', trace_id=trace_id))
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 20 * 1024 * 1024:
                    raise ProtocolError('项目准备请求长度不合法')
                body = json.loads(self.rfile.read(size))
                required = {'schema_version', 'project_path', 'protocol', 'files'}
                if route.endswith('/projects'):
                    required.add('approval_sha256')
                if not isinstance(body, dict) or set(body) != required or body['schema_version'] != '1.0':
                    raise ProtocolError('项目准备信封不合法')
                if route.endswith('/preview'):
                    result = self.server.preview_project_setup(body['project_path'], body['protocol'], body['files'])
                    return self.send(200, api_success(result, trace_id=trace_id))
                result = self.server.prepare_project(body['project_path'], body['protocol'], body['files'], body['approval_sha256'])
                return self.send(201, api_success(result, trace_id=trace_id))
            except (ValueError, ProtocolError, LookupError, OSError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id))
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Unable to prepare project', trace_id=trace_id))
        if len(tool_parts) >= 6 and tool_parts[:3] == ['api', 'v1', 'research'] and (tool_parts[4] == 'lifecycle'):
            trace_id = new_trace_id()
            authorized = self.authorized()
            if authorized and (not self.server.trusted_local):
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local or (not self.origin_ok()):
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Trusted same-origin local session required', trace_id=trace_id))
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ProtocolError('Study 请求长度不合法')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or body.get('schema_version') != '1.0':
                    raise ProtocolError('Study 请求信封不合法')
                study_id = tool_parts[3]
                if len(tool_parts) == 6 and tool_parts[5] == 'preview':
                    allowed = {'schema_version', 'action', 'max_steps'}
                    if body.get('action') == 'autopilot':
                        allowed.add('envelope')
                    if set(body) != allowed:
                        raise ProtocolError('Study 预览字段不合法')
                    result = preview_study_action(self.server.research_run_for_study(study_id), body['action'], body['max_steps'], body.get('envelope'))
                elif len(tool_parts) == 6 and tool_parts[5] == 'jobs':
                    allowed = {'schema_version', 'action', 'max_steps', 'approval_sha256', 'idempotency_key'}
                    if body.get('action') == 'autopilot':
                        allowed.add('envelope')
                    if set(body) != allowed:
                        raise ProtocolError('Study 执行字段不合法')
                    result = self.server.enqueue_study_action(study_id, body['action'], body['max_steps'], body['approval_sha256'], body['idempotency_key'], body.get('envelope'))
                    return self.send(202, api_success(result, trace_id=trace_id))
                elif len(tool_parts) == 8 and tool_parts[5] == 'jobs' and (tool_parts[7] == 'cancel') and (set(body) == {'schema_version'}):
                    result = self.server.cancel_study_job(study_id, tool_parts[6])
                else:
                    raise ProtocolError('Study 操作路径不合法')
                return self.send(200, api_success(result, trace_id=trace_id))
            except (ValueError, ProtocolError, LookupError, FileNotFoundError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id))
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Unable to process Study action', trace_id=trace_id))
        if route in {'/api/v1/research/studies/preview', '/api/v1/research/studies'}:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            authorized = self.authorized()
            if authorized and (not self.server.trusted_local):
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Trusted local session required', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ProtocolError('Study 初始化请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                required = {'schema_version', 'run_name', 'budget_cap', 'proposal_sha256'} if route.endswith('/studies') else {'schema_version', 'run_name', 'budget_cap'}
                if not isinstance(envelope, dict) or set(envelope) != required or envelope['schema_version'] != '1.0':
                    raise ProtocolError('Study 初始化信封不合法')
                if route.endswith('/preview'):
                    result = self.server.preview_study_initialization(envelope['run_name'], envelope['budget_cap'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                result = self.server.initialize_study_from_workbench(envelope['run_name'], envelope['budget_cap'], envelope['proposal_sha256'])
                return self.send(201, api_success(result, trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Unable to initialize Study', trace_id=trace_id, retryable=True), extra_headers=headers)
        if route in {'/api/v1/research/revision-drafts/preview', '/api/v1/research/revision-drafts/submit'}:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            authorized = self.authorized()
            if authorized and (not self.server.trusted_local):
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Trusted local session required', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 400000:
                    raise ProtocolError('修订提案请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                required = {'schema_version', 'spec', 'proposal_sha256'} if route.endswith('/submit') else {'schema_version', 'spec'}
                if not isinstance(envelope, dict) or set(envelope) != required or envelope['schema_version'] != '1.0':
                    raise ProtocolError('修订提案信封不合法')
                if route.endswith('/submit'):
                    result = self.server.submit_initial_revision_from_workbench(envelope['spec'], envelope['proposal_sha256'])
                    return self.send(201, api_success(result, trace_id=trace_id), extra_headers=headers)
                result = self.server.preview_initial_revision_from_workbench(envelope['spec'])
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Unable to create CodeRevision', trace_id=trace_id, retryable=True), extra_headers=headers)
        if route in {'/api/v1/project-files', '/api/v1/project-files/preview'}:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            authorized = self.authorized()
            if authorized and (not self.server.trusted_local):
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Trusted local session required', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1200000:
                    raise ProtocolError('项目文件请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'workspace_id', 'path', 'content', 'expected_sha256'} or envelope['schema_version'] != '1.0':
                    raise ProtocolError('项目文件保存信封不合法')
                operation = self.server.preview_project_file if route.endswith('/preview') else self.server.save_project_file
                result = operation(workspace_id=envelope['workspace_id'], relative=envelope['path'], content=envelope['content'], expected_sha256=envelope['expected_sha256'])
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except ProjectFileConflict as error:
                return self.send(409, api_failure(ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Unable to save project file', trace_id=trace_id, retryable=True), extra_headers=headers)
        is_worktree_create = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'revisions'] and (tool_parts[4] == 'worktrees')
        is_revision_diff_migrate = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'revisions'] and (tool_parts[4] == 'migrate-diff')
        is_initial_revision_execute = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'revisions'] and (tool_parts[4] == 'execute')
        is_worktree_remove = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'worktrees'] and (tool_parts[4] == 'remove')
        is_worktree_proposal = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'worktrees'] and (tool_parts[4] == 'proposal')
        is_worktree_submit = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'worktrees'] and (tool_parts[4] == 'submit')
        is_worktree_execute = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'worktrees'] and (tool_parts[4] == 'execute')
        is_revision_job_cancel = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'revision-jobs'] and (tool_parts[4] == 'cancel')
        is_design_execute = tool_parts == ['api', 'v1', 'design-jobs']
        is_benchmark_create = tool_parts == ['api', 'v1', 'benchmarks']
        is_followup_create = tool_parts == ['api', 'v1', 'research', 'followups']
        is_bound_draft_create = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'research'] and (tool_parts[4] == 'bound-drafts')
        is_bound_draft_review = len(tool_parts) == 7 and tool_parts[:3] == ['api', 'v1', 'research'] and (tool_parts[4] == 'bound-drafts') and (tool_parts[6] == 'review')
        is_design_job_cancel = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'design-jobs'] and (tool_parts[4] == 'cancel')
        is_design_job_retry = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'design-jobs'] and (tool_parts[4] == 'retry')
        if is_worktree_create or is_revision_diff_migrate or is_initial_revision_execute or is_worktree_remove or is_worktree_proposal or is_worktree_submit or is_worktree_execute or is_revision_job_cancel or is_design_execute or is_design_job_cancel or is_design_job_retry or is_benchmark_create or is_followup_create or is_bound_draft_create or is_bound_draft_review:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            authorized = self.authorized()
            if authorized and (not self.server.trusted_local):
                self._drain_rejected_post()
            if not authorized or not self.server.trusted_local:
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Trusted local session required', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= (65536 if is_followup_create or is_bound_draft_create else 4096):
                    raise ProtocolError('请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                if is_benchmark_create:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'run_name', 'idempotency_key'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 基准创建信封字段不合法')
                    result = self.server.enqueue_benchmark(envelope['run_name'], envelope['idempotency_key'])
                    return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_bound_draft_review:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'reason'} or envelope['schema_version'] != '1.0' or (not isinstance(envelope['reason'], str)) or (len(envelope['reason']) > 1000):
                        raise ProtocolError('API v1 证据绑定草稿审校信封字段不合法')
                    result = self.server.review_bound_draft_from_workbench(tool_parts[3], tool_parts[5], envelope['reason'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_bound_draft_create:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'spec'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 证据绑定草稿信封字段不合法')
                    result = self.server.create_bound_draft_from_workbench(tool_parts[3], envelope['spec'])
                    return self.send(201, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_followup_create:
                    required = {'schema_version', 'parent_study_id', 'expected_parent_manifest_sha256', 'new_project_name', 'new_run_name', 'new_test_path', 'candidates', 'budget', 'objective'}
                    if not isinstance(envelope, dict) or set(envelope) != required or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 后继研究创建信封字段不合法')
                    result = self.server.create_followup_from_workbench(envelope['parent_study_id'], envelope['new_project_name'], envelope['new_run_name'], envelope['new_test_path'], envelope['candidates'], envelope['budget'], envelope['objective'], envelope['expected_parent_manifest_sha256'])
                    return self.send(201, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_design_execute:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'study_id', 'design_id', 'expected_version', 'design_sha256'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 设计执行信封字段不合法')
                    result = self.server.enqueue_design_execution(envelope['study_id'], envelope['design_id'], envelope['expected_version'], envelope['design_sha256'])
                    return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_design_job_cancel:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'reason'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 设计 job 取消信封字段不合法')
                    result = self.server.cancel_design_job(tool_parts[3], envelope['reason'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_design_job_retry:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'expected_attempt', 'design_sha256'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 设计 job 重试信封字段不合法')
                    result = self.server.retry_design_job(tool_parts[3], envelope['expected_attempt'], envelope['design_sha256'])
                    return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_revision_diff_migrate:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 revision Diff 迁移信封字段不合法')
                    result = self.server.migrate_revision_diff(tool_parts[3])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_initial_revision_execute:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'manifest_sha256'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 初始 revision 执行信封字段不合法')
                    result = self.server.execute_initial_revision(tool_parts[3], envelope['manifest_sha256'])
                    return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_revision_job_cancel:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'reason'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 revision job 取消信封字段不合法')
                    result = self.server.cancel_revision_job(tool_parts[3], envelope['reason'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_worktree_create:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'idempotency_key'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 worktree 创建信封字段不合法')
                    result = self.server.create_revision_worktree(tool_parts[3], envelope['idempotency_key'])
                    return self.send(201, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_worktree_submit:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'proposal_sha256'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 worktree 审批信封字段不合法')
                    result = self.server.submit_worktree_revision(tool_parts[3], envelope['proposal_sha256'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_worktree_execute:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 worktree 执行信封字段不合法')
                    result = self.server.execute_worktree_revision(tool_parts[3])
                    return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
                if not isinstance(envelope, dict) or set(envelope) != {'schema_version'} or envelope['schema_version'] != '1.0':
                    raise ProtocolError('API v1 worktree 操作信封字段不合法')
                result = self.server.propose_worktree_revision(tool_parts[3]) if is_worktree_proposal else self.server.remove_revision_worktree(tool_parts[3])
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except FileNotFoundError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Worktree operation failed', trace_id=trace_id, retryable=True), extra_headers=headers)
        is_tool_proposal = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'sessions'] and (tool_parts[4] == 'tool-calls')
        is_tool_action = len(tool_parts) == 5 and tool_parts[:3] == ['api', 'v1', 'tool-calls'] and (tool_parts[4] in {'decision', 'execute', 'cancel'})
        if is_tool_proposal or is_tool_action:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Invalid session', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ProtocolError('请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                if not isinstance(envelope, dict) or envelope.get('schema_version') != '1.0':
                    raise ProtocolError('API v1 tool call 信封不合法')
                if is_tool_proposal:
                    required = {'schema_version', 'turn_id', 'tool_name', 'arguments', 'idempotency_key'}
                    if set(envelope) != required:
                        raise ProtocolError('tool call proposal 字段不合法')
                    result = self.server.propose_agent_tool(tool_parts[3], envelope['turn_id'], envelope['tool_name'], envelope['arguments'], envelope['idempotency_key'], trace_id)
                    return self.send(201, api_success(result, trace_id=trace_id), extra_headers=headers)
                (action, call_id) = (tool_parts[4], tool_parts[3])
                if action == 'decision':
                    if set(envelope) != {'schema_version', 'decision', 'reason'}:
                        raise ProtocolError('tool call decision 字段不合法')
                    result = self.server.decide_agent_tool(call_id, envelope['decision'], envelope['reason'])
                elif action == 'cancel':
                    if set(envelope) != {'schema_version', 'reason'}:
                        raise ProtocolError('tool call cancel 字段不合法')
                    result = self.server.cancel_agent_tool(call_id, envelope['reason'])
                else:
                    if set(envelope) != {'schema_version'}:
                        raise ProtocolError('tool call execute 字段不合法')
                    result = self.server.execute_agent_tool(call_id)
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except (SessionNotFound, TurnNotFound, ToolCallNotFound) as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except SessionIdempotencyConflict as error:
                return self.send(409, api_failure(ErrorCode.IDEMPOTENCY_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except ToolCallConflict as error:
                return self.send(409, api_failure(ErrorCode.INVALID_TRANSITION, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Tool call operation failed', trace_id=trace_id, retryable=True), extra_headers=headers)
        session_turn = route.strip('/').split('/')
        is_turn_cancel = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'turns'] and (session_turn[4] == 'cancel')
        is_turn_retry = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'turns'] and (session_turn[4] == 'retry')
        is_turn_steer = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'turns'] and (session_turn[4] == 'steer')
        is_turn_continue = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'turns'] and (session_turn[4] == 'continue')
        is_session_turn = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'sessions'] and (session_turn[4] == 'turns')
        is_session_select = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'sessions'] and (session_turn[4] == 'select')
        is_session_metadata = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'sessions'] and (session_turn[4] == 'metadata')
        is_session_budget = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'sessions'] and (session_turn[4] == 'budget')
        is_session_pricing = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'sessions'] and (session_turn[4] == 'pricing')
        is_session_cost_budget = len(session_turn) == 5 and session_turn[:3] == ['api', 'v1', 'sessions'] and (session_turn[4] == 'cost-budget')
        if route in {'/api/v1/sessions', '/api/v1/pricing-catalogs'} or is_session_turn or is_session_select or is_session_metadata or is_session_budget or is_turn_cancel or is_turn_retry or is_turn_steer or is_turn_continue or is_session_pricing or is_session_cost_budget:
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Invalid session', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ProtocolError('请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                if is_session_cost_budget:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'cost_budget_micros'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 session cost budget 信封字段不合法')
                    result = self.server.set_session_cost_budget(session_turn[3], envelope['cost_budget_micros'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if route == '/api/v1/pricing-catalogs':
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'name', 'currency', 'prices'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 pricing catalog 信封字段不合法')
                    result = self.server.register_pricing_catalog(envelope['name'], envelope['currency'], envelope['prices'])
                    return self.send(201, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_session_pricing:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'catalog_id'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 session pricing 信封字段不合法')
                    result = self.server.set_session_price_catalog(session_turn[3], envelope['catalog_id'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_session_budget:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'token_budget'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 session budget 信封字段不合法')
                    result = self.server.set_session_token_budget(session_turn[3], envelope['token_budget'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_turn_steer:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'idempotency_key', 'text'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 turn steer 信封字段不合法')
                    result = self.server.steer_session_turn(session_turn[3], envelope['text'], envelope['idempotency_key'], trace_id)
                    return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_turn_retry:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'idempotency_key'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 turn retry 信封字段不合法')
                    result = self.server.retry_session_turn(session_turn[3], envelope['idempotency_key'], trace_id)
                    return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_turn_continue:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 turn continue 信封字段不合法')
                    result = self.server.continue_session_after_tools(session_turn[3])
                    return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_turn_cancel:
                    if not isinstance(envelope, dict) or set(envelope) != {'schema_version', 'reason'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 turn cancel 信封字段不合法')
                    result = self.server.cancel_session_turn(session_turn[3], envelope['reason'])
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_session_metadata:
                    required = {'schema_version', 'workspace_id', 'expected_updated'}
                    if not isinstance(envelope, dict) or not required <= set(envelope) or set(envelope) - required not in ({'title'}, {'status'}) or (envelope['schema_version'] != '1.0'):
                        raise ProtocolError('会话修改信封字段不合法')
                    changes = {key: envelope[key] for key in ('title', 'status') if key in envelope}
                    result = self.server.update_agent_session(envelope['workspace_id'], session_turn[3], envelope['expected_updated'], **changes)
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if route == '/api/v1/sessions':
                    if not isinstance(envelope, dict) or set(envelope) - {'workspace_id'} != {'schema_version', 'title', 'harness'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 session 创建信封字段不合法')
                    result = self.server.create_agent_session(envelope['title'], envelope['harness'], envelope.get('workspace_id'))
                    return self.send(201, api_success(result, trace_id=trace_id), extra_headers=headers)
                if is_session_select:
                    if not isinstance(envelope, dict) or set(envelope) - {'workspace_id'} != {'schema_version'} or envelope['schema_version'] != '1.0':
                        raise ProtocolError('API v1 session 选择信封字段不合法')
                    result = self.server.select_agent_session(session_turn[3], envelope.get('workspace_id'))
                    return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
                if not isinstance(envelope, dict) or set(envelope) - {'workspace_id'} != {'schema_version', 'idempotency_key', 'text'} or envelope['schema_version'] != '1.0':
                    raise ProtocolError('API v1 turn 信封字段不合法')
                result = self.server.launch_session_turn(session_turn[3], envelope['text'], envelope['idempotency_key'], trace_id, envelope.get('workspace_id'))
                return self.send(202, api_success(result, trace_id=trace_id), extra_headers=headers)
            except (SessionNotFound, TurnNotFound) as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except SessionMetadataConflict as error:
                return self.send(409, api_failure(ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except SessionIdempotencyConflict as error:
                return self.send(409, api_failure(ErrorCode.IDEMPOTENCY_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except SessionBudgetExceeded as error:
                return self.send(409, api_failure(ErrorCode.BUDGET_EXHAUSTED, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Agent session operation failed', trace_id=trace_id, retryable=True), extra_headers=headers)
        if route == '/api/v1/commands/research':
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Invalid session', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ProtocolError('请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                required = {'schema_version', 'idempotency_key', 'expected_version', 'study_id', 'action', 'payload'}
                if not isinstance(envelope, dict) or set(envelope) != required:
                    raise ProtocolError('API v1 research 命令信封字段不合法')
                if envelope['schema_version'] != '1.0':
                    raise ProtocolError('不支持的命令 schema_version')
                run_dir = self.server.research_run_for_study(envelope['study_id'])
                store = ResearchStore(run_dir / 'research.sqlite')
                try:
                    result = store.execute_command(envelope['study_id'], envelope['action'], envelope['payload'], idempotency_key=envelope['idempotency_key'], expected_version=envelope['expected_version'], actor='local_user', trace_id=trace_id)
                    cursor = store.event_cursor()
                finally:
                    store.close()
                return self.send(200, api_success(result, trace_id=trace_id, event_cursor=cursor), extra_headers=headers)
            except LookupError as error:
                return self.send(404, api_failure(ErrorCode.NOT_FOUND, str(error), trace_id=trace_id), extra_headers=headers)
            except ResearchVersionConflict as error:
                return self.send(409, api_failure(ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except ResearchIdempotencyConflict as error:
                return self.send(409, api_failure(ErrorCode.IDEMPOTENCY_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except TransitionError as error:
                return self.send(409, api_failure(ErrorCode.INVALID_TRANSITION, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Research command failed', trace_id=trace_id, retryable=True), extra_headers=headers)
        if route == '/api/v1/commands/workspace':
            trace_id = new_trace_id()
            headers = [('X-Popper-API-Version', '1.0'), ('X-Trace-Id', trace_id)]
            if not self.authorized():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Invalid session', trace_id=trace_id), extra_headers=headers)
            if not self.origin_ok():
                return self.send(403, api_failure(ErrorCode.PERMISSION_DENIED, 'Cross-origin request rejected', trace_id=trace_id), extra_headers=headers)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ProtocolError('请求长度不合法')
                envelope = json.loads(self.rfile.read(size))
                required = {'schema_version', 'idempotency_key', 'expected_version', 'action', 'payload'}
                if not isinstance(envelope, dict) or set(envelope) != required:
                    raise ProtocolError('API v1 workspace 命令信封字段不合法')
                if envelope['schema_version'] != '1.0':
                    raise ProtocolError('不支持的命令 schema_version')
                if not isinstance(envelope['action'], str) or not isinstance(envelope['payload'], dict):
                    raise ProtocolError('action 必须是字符串且 payload 必须是对象')
                command = {'op': envelope['action'], **envelope['payload']}
                with self.server.ws_lock:
                    ws = self.server.workspace
                    if ws.is_empty():
                        ws.seed_if_empty(ws.project_spec(), ws.current_phase())
                    result = ws.apply_command(command, idempotency_key=envelope['idempotency_key'], expected_version=envelope['expected_version'])
                return self.send(200, api_success(result, trace_id=trace_id), extra_headers=headers)
            except WorkspaceVersionConflict as error:
                return self.send(409, api_failure(ErrorCode.VERSION_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except WorkspaceIdempotencyConflict as error:
                return self.send(409, api_failure(ErrorCode.IDEMPOTENCY_CONFLICT, str(error), trace_id=trace_id), extra_headers=headers)
            except (ValueError, ProtocolError) as error:
                return self.send(400, api_failure(ErrorCode.INVALID_REQUEST, str(error), trace_id=trace_id), extra_headers=headers)
            except Exception:
                return self.send(500, api_failure(ErrorCode.EXECUTION_FAILED, 'Workspace command failed', trace_id=trace_id, retryable=True), extra_headers=headers)
        routes = {'/api/action', '/api/adjudicate', '/api/workspace', '/api/materialize', '/api/file', '/api/campaign', '/api/chat', '/api/terminal'}
        if self.path not in routes:
            return self.send(404, {'error': 'Not found'})
        if not self.authorized():
            return self.send(403, {'error': 'Invalid session'})
        if not self.origin_ok():
            return self.send(403, {'error': 'Cross-origin request rejected'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            limit = 1024 if self.path in ('/api/action', '/api/adjudicate', '/api/campaign') else 1048576
            if not 0 < size <= limit:
                raise ProtocolError('请求长度不合法')
            payload = json.loads(self.rfile.read(size))
            if self.path == '/api/action':
                if not isinstance(payload, dict) or set(payload) != {'action'} or (not isinstance(payload['action'], str)):
                    raise ProtocolError('请求必须仅包含 action 字符串')
                self.server.launch(payload['action'])
                self.send(202, {'accepted': True})
            elif self.path == '/api/chat':
                if not isinstance(payload, dict) or set(payload) != {'text'}:
                    raise ProtocolError('请求必须仅包含 text 字符串')
                self.send(200, self.server.send_chat(payload['text']))
            elif self.path == '/api/terminal':
                self.send(200, self.server.terminal_command(payload))
            elif self.path == '/api/campaign':
                self._post_campaign(payload)
            elif self.path == '/api/adjudicate':
                self._post_adjudicate(payload)
            elif self.path == '/api/file':
                self._post_file(payload)
            elif self.path == '/api/workspace':
                self._post_workspace(payload)
            else:
                self._post_materialize(payload)
        except (ValueError, ProtocolError) as error:
            self.send(409, {'error': str(error)})
