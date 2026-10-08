"""Sessions responsibility service."""
from __future__ import annotations
from .base import HostService

class SessionsService(HostService):

    def _chat_system_prompt(self, workspace_root=None):
        """从真实项目状态构造对话 system prompt：只注入事实，不编造上下文。"""
        EntityKind = self.dependencies.EntityKind
        Experiment = self.dependencies.Experiment
        PHASES = self.dependencies.PHASES
        Workspace = self.dependencies.Workspace
        json = self.dependencies.json
        parse_id = self.dependencies.parse_id
        read_json = self.dependencies.read_json
        if self.mode == 'experiment':
            exp = Experiment(self.project, readonly=True)
            try:
                exp.replay()
                state = exp.state()
                results = exp.results(split='dev')
            finally:
                exp.close()
            spec = state.get('spec', {})
            metric = spec.get('metric', {})
            lines = ['你是 Popper 科研工作台的对话助手。', f"当前实验：{spec.get('name')}", f"研究目标：{spec.get('objective')}", f"指标：{metric.get('name')}（方向 {metric.get('direction')}）", f"阶段：{PHASES.get(state.get('phase'), state.get('phase'))}", f"预注册改善阈值：{spec.get('min_improvement')}"]
            claim = state.get('claim')
            if claim:
                lines.append(f"结论：oriented delta={claim.get('delta')}（{claim.get('status')}）")
                lines.append('结论证据 run_id：' + json.dumps(claim.get('evidence_ids', []), ensure_ascii=False))
            lines.append('已完成运行（每条都是控制器保存的真实结果；run_id 是证据定位符）：')
            if results:
                for result in results:
                    summary = {'run_id': result.get('run_id'), 'split': result.get('split'), 'config': result.get('config'), 'metric': result.get('metric'), 'mean': result.get('mean'), 'std': result.get('std'), 'n_seeds': result.get('n_seeds'), 'per_seed': result.get('per_seed'), 'evaluator_id': result.get('evaluator_id'), 'trust': result.get('trust')}
                    lines.append('- ' + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
            else:
                lines.append('- 无已完成运行')
            registered_studies = []
            for run_dir in self._visible_research_runs(workspace_root):
                try:
                    study_id = read_json(run_dir / 'research.json').get('study_id')
                    parse_id(study_id, EntityKind.STUDY)
                    registered_studies.append(study_id)
                except Exception:
                    continue
            lines.append('已显式登记的 Research Study ID（research.status、research.study_preview、research.study_job 与证据读取需用户批准；不得用不存在的 ID 或文件路径替代）：')
            lines.append(json.dumps(sorted(set(registered_studies)), ensure_ascii=False))
            if workspace_root is None:
                workspace = self.workspace_payload()
            else:
                workspace = Workspace(workspace_root).read() or {'meta': {'state_version': 0}, 'folders': []}
            workspace_context = {'state_version': workspace['meta']['state_version'], 'folders': [{'fid': folder['fid'], 'name': folder['name'], 'tasks': [{'quid': task['quid'], 'title': task['title'], 'done': task['done']} for task in folder['tasks']]} for folder in workspace['folders']]}
            lines.append('当前 Workspace（工具参数必须使用这里的真实 ID 与 state_version）：')
            lines.append(json.dumps(workspace_context, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
            lines.append('回答数值问题时优先直接引用上述 split/config/mean，并同时给出 run_id。只基于以上真实状态回答；不得编造未发生的实验结果、证据位置或评测数字。无法回答时如实说明。')
            lines.append('推进 Study 时先调用 research_study_preview 获取当前审批摘要；再提交 research_study_action，经用户批准后只会返回 Job。用 research_study_job 查询终态；仅在作业成功且真实 Claim 可核验后表述实验结论。独立确认资源一次性消费，不要求自动重试。')
            lines.append('需要生成研究稿件时，先读取真实 Claim 和 Study 版本，调用 research_draft_preview 获取包含全部 Claim 的稿件摘要，再调用 research_draft_create 并等待用户批准。作者说明未验证；生成结果仅是待审草稿，不得称为已投稿或已发表。')
            if self.trusted_local:
                lines.append('需要改代码时先用 workspace_file_list 查路径，再用 workspace_file_read 取得源码与 SHA-256，最后用 workspace_file_replace 提交唯一文本替换。每步须用户审批；冻结实验输入不能原地改写，须走 Revision。')
            return '\n'.join(lines)
        return '你是 Popper 科研工作台（论文复现模式）的对话助手。基于用户提供的上下文回答，不得编造实验结果。' + ('代码修改须先列文件、审批读取，再提交带摘要的唯一文本替换并等待用户审查差异；冻结输入不可原地修改。' if self.trusted_local else '')

    def chat_payload(self):
        """构造 GET /api/chat 的响应：当前工作区根的对话 + 配置状态 + 界面文案。"""
        AgentSessionStore = self.dependencies.AgentSessionStore
        CHAT_REL = self.dependencies.CHAT_REL
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                store.import_legacy_chat(self.workspace.root / CHAT_REL, self.chat_model)
                return self._chat_payload(store)
            finally:
                store.close()

    def _chat_payload(self, store):
        self._service("providers").dispatch("refresh")
        CHAT_LABELS = self.dependencies.CHAT_LABELS
        Path = self.dependencies.Path
        session = store.default_session(create=False)
        continuation = None
        if session:
            for item in store.turns(session['session_id'])[-1:]:
                state = store.get_continuation_state(item['turn_id'])
                if state:
                    reason = state.get('reason')
                    continuation = {'state': state['status'], 'reason': reason.get('message') if isinstance(reason, dict) else reason, 'source_turn_id': state['source_turn_id'], 'root_turn_id': state['root_turn_id'], 'correction_attempts': state.get('correction_count', 0), 'max_corrections': 2}
                    break
        descriptor = self.chat_harness.descriptor if self.chat_harness else None
        workspace_id = next((item['wid'] for item in self.workspaces.roots_payload() if Path(item['root']).resolve() == store.root), None)
        return {'workspace_id': workspace_id, 'navigation_version': self.navigation_version, 'configured': self.chat_harness is not None, 'model': self.chat_model, 'continuation': continuation, 'harnesses': [descriptor.as_dict()] if descriptor else [], 'session_id': session['session_id'] if session else None, 'sessions': store.list_sessions(), 'turns': store.turns(session['session_id']) if session else [], 'jobs': store.agent_jobs(session['session_id']) if session else [], 'usage': store.usage_summary(session['session_id']) if session else {'jobs': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'reserved_tokens': 0, 'token_budget': None, 'remaining_tokens': None, 'cost_micros': 0, 'reserved_cost_micros': 0, 'cost_budget_micros': None, 'remaining_cost_micros': None, 'currency': None}, 'messages': store.chat_messages(session['session_id']) if session else [], 'tool_calls': [self._agent_tool_public(call) for call in store.tool_calls(session['session_id'])] if session else [], 'pricing_catalogs': store.list_pricing_catalogs(), 'event_integrity': store.verify_turn_events(), 'tool_event_integrity': store.verify_tool_events(), 'labels': CHAT_LABELS}

    def sessions_payload(self, scope=None):
        AgentSessionStore = self.dependencies.AgentSessionStore
        ProtocolError = self.dependencies.ProtocolError
        read_session_summaries = self.dependencies.read_session_summaries
        if scope == 'all':
            with self.ws_lock:
                roots = self.workspaces.roots_payload()
            projects = []
            for root in roots:
                try:
                    sessions = [{**session, 'workspace_id': root['wid']} for session in read_session_summaries(root['root'])]
                    projects.append({'workspace_id': root['wid'], 'name': root['name'], 'sessions': sessions, 'error': None})
                except Exception:
                    projects.append({'workspace_id': root['wid'], 'name': root['name'], 'sessions': [], 'error': '会话读取失败，请重试'})
            return {'projects': projects}
        if scope not in (None, 'current'):
            raise ProtocolError('不支持的会话查询范围')
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                return {'sessions': store.list_sessions()}
            finally:
                store.close()

    def _context_root(self, workspace_id):
        Path = self.dependencies.Path
        SessionNotFound = self.dependencies.SessionNotFound
        entry = next((item for item in self.workspaces.roots_payload() if item['wid'] == workspace_id), None)
        if not entry:
            raise SessionNotFound('项目不存在')
        return Path(entry['root']).resolve()

    def _context_facts(self, root):
        ProtocolError = self.dependencies.ProtocolError
        StudyJobStore = self.dependencies.StudyJobStore
        list_workbench_bound_drafts = self.dependencies.list_workbench_bound_drafts
        read_research_state = self.dependencies.read_research_state
        task_memory = self.dependencies.task_memory
        facts = []
        for run_dir in self._visible_research_runs(root):
            state = read_research_state(run_dir)
            if not state.get('ok'):
                continue
            if not state.get('experiment_graph', {}).get('integrity', {}).get('ok', True):
                raise ProtocolError('研究证据完整性校验失败，不能将此研究作为交接事实；请在研究面板核验')
            study = state.get('study') or {}
            jobs = []
            if (run_dir / 'study-jobs.sqlite').is_file():
                job_store = StudyJobStore.readonly(run_dir)
                try:
                    job_store.verify()
                    jobs = [{'job_id': job['job_id'], 'status': job['status'], 'error': job.get('error'), 'updated': job.get('updated')} for job in job_store.list() if job.get('request', {}).get('study_id') == state['manifest']['study_id']]
                finally:
                    job_store.close()
            facts.append({'study_id': state['manifest']['study_id'], 'name': study.get('question') or state['manifest'].get('objective') or run_dir.name, 'state_version': state['state_version'], 'status': study.get('status'), 'budget': state.get('budget'), 'jobs': jobs, 'observations': len(state.get('observations', [])), 'drafts': [{'draft_id': draft['draft_id'], 'verified': draft['ok']} for draft in list_workbench_bound_drafts(root, run_dir, state['manifest']['study_id'])], 'claims': [{'claim_id': item.get('claim_id'), 'status': item.get('status')} for item in state.get('claims', [])]})
        return task_memory.redact(facts)

    def task_context_payload(self, workspace_id, session_id=None):
        AgentSessionStore = self.dependencies.AgentSessionStore
        SessionNotFound = self.dependencies.SessionNotFound
        digest = self.dependencies.digest
        task_memory = self.dependencies.task_memory
        root = self._context_root(workspace_id)
        try:
            store = AgentSessionStore.readonly(root)
        except FileNotFoundError:
            if session_id:
                raise SessionNotFound('会话不存在')
            return {'workspace_id': workspace_id, 'tasks': [], 'sessions': [], 'binding': None, 'selected': None, 'facts': self._context_facts(root)}
        try:
            result = task_memory.list_context(store, session_id)
            selected_body = result['selected']['body'] if result['selected'] else {}
            source_id = selected_body.get('source_session_id')
            source_stale = bool(source_id and store.get_session(source_id)['updated'] != selected_body.get('source_version'))
        finally:
            store.close()
        result.update(workspace_id=workspace_id, facts=self._context_facts(root))
        if result['selected']:
            current = {item['study_id']: digest(item) for item in result['facts']}
            old = result['selected']['body'].get('fact_fingerprints', {})
            result['stale'] = source_stale or result['selected']['revision'] != result['selected']['version'] or any((current.get(key) != value for (key, value) in old.items()))
        return result

    def task_context_preview(self, workspace_id, session_id, task_id=None, source_session_id=None, study_ids=None):
        AgentSessionStore = self.dependencies.AgentSessionStore
        ProtocolError = self.dependencies.ProtocolError
        digest = self.dependencies.digest
        task_memory = self.dependencies.task_memory
        root = self._context_root(workspace_id)
        store = AgentSessionStore.readonly(root)
        try:
            candidate = task_memory.preview(store, session_id, task_id, source_session_id)
        finally:
            store.close()
        facts = self._context_facts(root)
        selected = study_ids if study_ids is not None else candidate.get('study_ids', [])
        if not isinstance(selected, list) or any((not isinstance(item, str) for item in selected)) or set(selected) - {item['study_id'] for item in facts}:
            raise ProtocolError('研究对象不属于当前项目')
        candidate.update(workspace_id=workspace_id, session_id=session_id, study_ids=selected, facts=[item for item in facts if item['study_id'] in selected], fact_fingerprints={item['study_id']: digest(item) for item in facts if item['study_id'] in selected})
        candidate.pop('preview_sha256', None)
        candidate['preview_sha256'] = digest(candidate)
        return candidate

    def commit_task_context(self, workspace_id, session_id, request):
        AgentSessionStore = self.dependencies.AgentSessionStore
        ProtocolError = self.dependencies.ProtocolError
        task_memory = self.dependencies.task_memory
        with self.chat_lock, self.ws_lock:
            self._check_session_workspace(workspace_id)
            root = self._context_root(workspace_id)
            store = AgentSessionStore(root)
            try:
                prior = task_memory.replay_receipt(store, session_id, request)
                if prior is not None:
                    return prior
                candidate = request.get('preview')
                refreshed = None
                if request.get('op') != 'detach':
                    if not isinstance(candidate, dict):
                        raise ProtocolError('缺少交接预览')
                    refreshed = self.task_context_preview(workspace_id, session_id, candidate.get('task_id'), candidate.get('source_session_id'), candidate.get('study_ids', []))
                result = task_memory.commit(store, session_id, request, refreshed)
                self.navigation_version += 1
                return result
            finally:
                store.close()

    def _assemble_session_context(self, store, session_id, system, user_text=None, feedback_source_turn_id=None):
        CHAT_TOOLS = self.dependencies.CHAT_TOOLS
        ProtocolError = self.dependencies.ProtocolError
        canonical = self.dependencies.canonical
        os = self.dependencies.os
        task_memory = self.dependencies.task_memory
        tools = getattr(self.chat_harness, 'context_tools', CHAT_TOOLS)
        link = task_memory.binding(store, session_id)
        facts = []
        if link['task_id']:
            handoff = task_memory.revision(store, link['task_id'], link['revision'])['body']
            facts = [item for item in self._context_facts(store.root) if item['study_id'] in handoff.get('study_ids', [])]
        from ..research.agent_contracts import execution_contracts
        study_ids = handoff.get('study_ids', []) if link['task_id'] else None
        required = execution_contracts(store.root, self._visible_research_runs(store.root), study_ids)
        from ..agent_execution_feedback import turn_feedback
        required.extend(turn_feedback(self, store, session_id, feedback_source_turn_id))
        assembled = task_memory.assemble(store, session_id, system, tools, user_text=user_text, optional_facts=facts, required_facts=required)
        limits = getattr(self.chat_harness, 'context_request_limits', None)
        if limits is not None:
            assembled['request_limits'] = dict(limits)
            size = len(canonical({key: assembled[key] for key in ('system', 'messages', 'tools', 'request_limits')}).encode('utf-8'))
            if size > int(os.environ.get('POPPER_CONTEXT_MAX_BYTES', '65536')):
                raise ProtocolError('包含模型限制的输入超过上下文字节上限，未调用模型')
            assembled['input_bytes'] = size
        return assembled

    def _task_source_for_call(self, root, call):
        AgentSessionStore = self.dependencies.AgentSessionStore
        ProtocolError = self.dependencies.ProtocolError
        task_memory = self.dependencies.task_memory
        validate_agent_tool_arguments = self.dependencies.validate_agent_tool_arguments
        args = validate_agent_tool_arguments('context.source', call['arguments'])
        store = AgentSessionStore.readonly(root)
        try:
            link = task_memory.binding(store, call['session_id'])
            if not link['task_id']:
                raise ProtocolError('当前会话尚未确认交接来源')
            current = task_memory.revision(store, link['task_id'], link['revision'])
            if current['revision'] != current['version']:
                raise task_memory.ContextConflict('任务修订已变化，请先重新核对交接')
            handoff = current['body']
            allowed = {item['session_id'] for item in handoff.get('sources', [])}
            if args['source_session_id'] not in allowed:
                raise ProtocolError('来源不在当前已确认交接范围内')
            source = task_memory.source_preview(store, args['source_session_id'])
            if source['source_version'] != args['expected_version']:
                raise task_memory.ContextConflict('来源会话已变化，请重新核对版本')
            if args.get('turn_id'):
                permitted = {item['turn_id']: item for item in handoff.get('sources', []) if item['session_id'] == args['source_session_id']}
                source_turn = permitted.get(args['turn_id'])
                if source_turn is None:
                    raise ProtocolError('Turn 不在已确认交接来源内')
                turn = next((item for item in store.turns(args['source_session_id']) if item['turn_id'] == args['turn_id']))
                if turn['updated'] != source_turn['version']:
                    raise task_memory.ContextConflict('来源 Turn 已变化，请重新核对交接')
                source['detail'] = task_memory.redact({'session_id': args['source_session_id'], 'turn_id': turn['turn_id'], 'version': turn['updated'], 'status': turn['status'], 'user_text': task_memory.redact(turn['user_message'])[:16000], 'agent_suggestion': task_memory.redact(turn['assistant_message'] or '')[:16000], 'truncated': len(turn['user_message']) > 16000 or len(turn['assistant_message'] or '') > 16000, 'tools': [{'call_id': call['call_id'], 'tool_name': call['tool_name'], 'status': call['status'], 'updated': call['updated'], 'result': call['result'] if call['status'] == 'completed' else None, 'error': call['error']} for call in store.tool_calls(args['source_session_id']) if call['turn_id'] == turn['turn_id']]})
            return source
        finally:
            store.close()

    def session_events_payload(self, session_id, *, after=0, limit=200):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                return {**store.turn_events(session_id, after=after, limit=limit), 'integrity': store.verify_turn_events()}
            finally:
                store.close()

    def session_tool_events_payload(self, session_id, *, after=0, limit=200):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                return {**store.tool_events(session_id, after=after, limit=limit), 'integrity': store.verify_tool_events()}
            finally:
                store.close()

    def preview_initial_revision_from_workbench(self, spec):
        ProtocolError = self.dependencies.ProtocolError
        preview_initial_revision = self.dependencies.preview_initial_revision
        if not self.trusted_local:
            raise ProtocolError('创建 CodeRevision 需要可信本机模式')
        if not isinstance(spec, dict) or not isinstance(spec.get('study_id'), str):
            raise ProtocolError('CodeRevision 提案缺少 study_id')
        run_dir = self.research_run_for_study(spec['study_id'])
        with self.ws_lock:
            return preview_initial_revision(run_dir, self.workspace.root, spec)

    def submit_initial_revision_from_workbench(self, spec, proposal_sha256):
        ProtocolError = self.dependencies.ProtocolError
        submit_initial_revision = self.dependencies.submit_initial_revision
        if not self.trusted_local:
            raise ProtocolError('创建 CodeRevision 需要可信本机模式')
        if not isinstance(spec, dict) or not isinstance(spec.get('study_id'), str):
            raise ProtocolError('CodeRevision 提案缺少 study_id')
        run_dir = self.research_run_for_study(spec['study_id'])
        with self.ws_lock:
            return submit_initial_revision(run_dir, self.workspace.root, spec, proposal_sha256)

    def create_agent_session(self, title, harness='chat', workspace_id=None):
        AgentSessionStore = self.dependencies.AgentSessionStore
        ProtocolError = self.dependencies.ProtocolError
        self._service('providers').dispatch('refresh')
        (resolved_harness, harness_version) = (harness, None)
        if self.chat_harness is not None:
            descriptor = self.chat_harness.descriptor
            if harness not in {'chat', descriptor.name}:
                raise ProtocolError(f'未配置 conversation harness: {harness}')
            resolved_harness = descriptor.name
            harness_version = descriptor.protocol_version
        with self.chat_lock, self.ws_lock:
            self._check_session_workspace(workspace_id)
            store = AgentSessionStore(self.workspace.root)
            try:
                defaults = self._service('providers').dispatch('snapshot')
                session = store.create_session(title, resolved_harness, self.chat_model, harness_version=harness_version)
                if defaults.get('token_budget') is not None:
                    store.set_token_budget(session['session_id'], defaults['token_budget'])
                if store.default_session(create=False) is None:
                    store.set_default_session(session['session_id'])
                return session
            finally:
                store.close()

    def _check_session_workspace(self, workspace_id):
        SessionMetadataConflict = self.dependencies.SessionMetadataConflict
        if workspace_id is not None and workspace_id != self.workspaces.current_wid():
            raise SessionMetadataConflict('项目已切换，请重新打开所属项目')

    def update_agent_session(self, workspace_id, session_id, expected_updated, **changes):
        AgentSessionStore = self.dependencies.AgentSessionStore
        SessionNotFound = self.dependencies.SessionNotFound
        with self.chat_lock, self.ws_lock:
            root = next((item for item in self.workspaces.roots_payload() if item['wid'] == workspace_id), None)
            if root is None:
                raise SessionNotFound('项目不存在')
            store = AgentSessionStore(root['root'])
            try:
                session = store.update_session_metadata(session_id, expected_updated, **changes)
                if workspace_id == self.workspaces.current_wid():
                    self.navigation_version += 1
                return session
            finally:
                store.close()

    def select_agent_session(self, session_id, workspace_id=None):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            with self.ws_lock:
                self._check_session_workspace(workspace_id)
                store = AgentSessionStore(self.workspace.root)
                try:
                    store.set_default_session(session_id)
                    self.navigation_version += 1
                except Exception:
                    store.close()
                    raise
            try:
                return self._chat_payload(store)
            finally:
                store.close()

    def set_session_token_budget(self, session_id, token_budget):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                session = store.set_token_budget(session_id, token_budget)
                return {'session': session, 'usage': store.usage_summary(session_id)}
            finally:
                store.close()

    def register_pricing_catalog(self, name, currency, prices):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                return store.register_pricing_catalog(name, currency, prices, actor='local_user')
            finally:
                store.close()

    def set_session_price_catalog(self, session_id, catalog_id):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                session = store.set_price_catalog(session_id, catalog_id)
                return {'session': session, 'usage': store.usage_summary(session_id)}
            finally:
                store.close()

    def set_session_cost_budget(self, session_id, cost_budget_micros):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                session = store.set_cost_budget(session_id, cost_budget_micros)
                return {'session': session, 'usage': store.usage_summary(session_id)}
            finally:
                store.close()

    def _require_session_harness(self, store, session_id):
        ProtocolError = self.dependencies.ProtocolError
        descriptor = self.chat_harness.descriptor
        session = store.get_session(session_id)
        if session['harness'] == 'chat' and session['harness_version'] is None:
            session = store.bind_legacy_chat_harness(session_id, descriptor.name, descriptor.protocol_version, descriptor.model or self.chat_model)
        if session['harness'] != descriptor.name or session['harness_version'] != descriptor.protocol_version:
            raise ProtocolError(f"Session Harness 不兼容: {session['harness']}@{session['harness_version'] or 'unversioned'}")
        return session

    def _run_session_turn(self, root, session_id, turn_id, trace_id, system_prompt, cancel_event=None):
        """Run one already-persisted turn; terminal persistence is race-safe."""
        AGENT_TOOLS = self.dependencies.AGENT_TOOLS
        AgentSessionStore = self.dependencies.AgentSessionStore
        MODEL_MAX_TOKENS = self.dependencies.MODEL_MAX_TOKENS
        MODEL_TOOL_ALIASES = self.dependencies.MODEL_TOOL_ALIASES
        ProtocolError = self.dependencies.ProtocolError
        canonical = self.dependencies.canonical
        digest = self.dependencies.digest
        task_memory = self.dependencies.task_memory
        validate_agent_tool_arguments = self.dependencies.validate_agent_tool_arguments
        harness = getattr(self, '_turn_harnesses', {}).get(turn_id, self.chat_harness)
        store = AgentSessionStore(root)
        usage = {}
        reply = None
        try:
            try:
                frozen = task_memory.load_input(store, turn_id)
                if frozen:
                    (system_prompt, history) = (frozen['system'], frozen['messages'])
                    configured_tools = getattr(harness, 'context_tools', frozen['tools'])
                    if digest(configured_tools) != digest(frozen['tools']):
                        raise ProtocolError('模型工具配置已变化，拒绝用旧预算快照执行；请人工重试')
                    configured_limits = getattr(harness, 'context_request_limits', None)
                    legacy_limits = {'max_tokens': MODEL_MAX_TOKENS} if configured_limits is not None else None
                    if frozen.get('request_limits', legacy_limits) != configured_limits and ('request_limits' in frozen or configured_limits is not None):
                        raise ProtocolError('模型输出或思考限制已变化，拒绝发送旧输入快照；请重新核对')
                else:
                    history = store.model_messages(session_id, turn_id)
                reply = self._invoke_budgeted_harness(frozen, turn_id, system_prompt, history, cancel_event,
                    lambda delta: store.append_turn_delta(turn_id, delta),
                    on_progress=lambda progress: store.update_request_progress(turn_id, progress))
                usage = getattr(reply, 'usage', {})
                if isinstance(reply, str):
                    return (store.complete_turn(turn_id, reply), getattr(reply, 'usage', {}))
                if not isinstance(reply, dict) or set(reply) != {'text', 'tool_calls'} or (not isinstance(reply['text'], str)) or (not isinstance(reply['tool_calls'], list)) or (not reply['tool_calls']):
                    raise ProtocolError('模型回复必须是文本或结构化工具提议')
                proposals = []
                for (index, requested) in enumerate(reply['tool_calls']):
                    if not isinstance(requested, dict) or not {'name', 'arguments'}.issubset(requested) or set(requested) - {'name', 'arguments', 'validation_error'} or (not isinstance(requested['name'], str)) or (not isinstance(requested['arguments'], dict)):
                        raise ProtocolError('模型工具提议结构不合法')
                    tool_name = MODEL_TOOL_ALIASES.get(requested['name'], requested['name'])
                    risk = AGENT_TOOLS.get(tool_name)
                    validation_error = requested.get('validation_error')
                    if validation_error is None:
                        try:
                            if risk is None:
                                raise ProtocolError(f"未登记工具: {requested['name']}")
                            validate_agent_tool_arguments(tool_name, requested['arguments'])
                        except ProtocolError as error:
                            validation_error = {'type': 'unknown_tool' if risk is None else 'invalid_arguments', 'message': str(error)}
                    if validation_error is not None:
                        if type(usage.get('total_tokens')) is not int or usage['total_tokens'] < 0:
                            raise ProtocolError(str(validation_error.get('message', '参数无效')) + '；用量无法核实，禁止自动纠错')
                        validation_error = {**validation_error, 'original_tool_name': requested['name'], 'executed': False}
                        if 'raw_arguments' in validation_error:
                            raw = task_memory.redact(validation_error['raw_arguments'])
                            encoded = canonical(raw).encode('utf-8')
                            validation_error['raw_arguments'] = raw if len(encoded) <= 32768 else {'retained': False, 'bytes': len(encoded), 'sha256': digest(raw), 'reason': '过大无效参数仅保留摘要校验值，非语义总结'}
                    proposal = {'tool_name': tool_name, 'arguments': requested['arguments'], 'risk': risk or 'write', 'idempotency_key': f'model-{turn_id}-{index}', 'actor': 'agent', 'trace_id': trace_id}
                    if validation_error is not None:
                        proposal['validation_error'] = validation_error
                    proposals.append(proposal)
                (turn, _) = store.complete_turn_with_tool_calls(turn_id, reply['text'], proposals)
                return (turn, getattr(reply, 'usage', {}))
            except Exception as error:
                try:
                    from ..chat import ChatReplyError, ChatCancelled
                    details = {'type': type(error).__name__, 'message': str(error)}
                    if isinstance(reply, dict):
                        try:
                            safe_reply = task_memory.redact_input(reply)
                            body = canonical(safe_reply)
                            size = len(body.encode('utf-8'))
                            evidence = {'source': 'rejected model reply; no tool execution', 'bytes': size, 'sha256': digest(safe_reply), 'retained': size <= 65536}
                            if evidence['retained']:
                                evidence['reply'] = safe_reply
                            else:
                                evidence['reason'] = 'audit reply exceeds 64 KiB; digest only, not a summary'
                            details['rejected_reply'] = evidence
                        except (TypeError, ValueError):
                            details['rejected_reply'] = {'retained': False, 'reason': 'reply is not serializable audit data'}
                    if isinstance(error, ChatReplyError):
                        usage = error.usage
                        details['provider'] = task_memory.redact_input({'response_id': error.response_id, 'finish_reason': error.finish_reason, 'stream_completed': error.stream_completed, 'usage': error.usage})
                    elif isinstance(error, ChatCancelled) and error.stream_completed:
                        usage = error.usage
                    else:
                        from ..request_lifecycle import RequestFailure
                        if isinstance(error,(RequestFailure,ChatCancelled)):
                            partial = getattr(error,'partial_usage',None) or error.usage
                            details['transport'] = task_memory.redact_input({
                                'transport_phase':error.transport_phase,'sent':error.sent,
                                'output_started':error.output_started,'stream_completed':error.stream_completed,
                                'usage_status':'not_sent' if error.sent is False else ('partial' if partial else 'unknown')})
                            if partial:
                                details['partial_provider_usage'] = task_memory.redact_input(partial)
                            if (error.sent is False and error.output_started is False
                                    and error.stream_completed is False
                                    and error.transport_phase in {'not_started','connecting','connected'}):
                                # This zero is local transport proof, not fabricated provider usage.
                                # Its source is retained in turn.error.transport.
                                usage = {'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}
                    return (store.fail_turn(turn_id, details), usage)
                except ProtocolError:
                    return (store.get_turn(turn_id), usage)
        finally:
            store.close()

    def _invoke_budgeted_harness(self, frozen, turn_id, system_prompt, history, cancel_event, on_delta, on_progress=None):
        """One dispatch boundary for send, tool continuation, steer and retry.

        The durable reservation precedes provider I/O. A crash/transport error
        leaves an unresolved entry, so restart cannot resend an ambiguous call.
        """
        ProtocolError = self.dependencies.ProtocolError
        digest = self.dependencies.digest
        harness = getattr(self, '_turn_harnesses', {}).get(turn_id, self.chat_harness)
        ledger = None
        reserved = settled = False
        try:
            if self.evaluation_budget_path is not None:
                from ..research.agent_evaluation_budget import AgentEvaluationBudget
                if frozen is None:
                    raise ProtocolError('评测调用缺少持久输入快照，拒绝发送')
                ledger = AgentEvaluationBudget(self.evaluation_budget_path)
                receipt = ledger.reserve(turn_id, frozen, self._reserve_context(frozen))
                reserved = True
                if receipt['snapshot_sha256'] != digest(frozen):
                    raise ProtocolError('评测预留与执行快照不一致，拒绝发送')
            capabilities = harness.descriptor.capabilities
            if 'cooperative_cancel' in capabilities:
                extra = {'on_progress': on_progress} if getattr(harness, 'supports_progress', False) else {}
                reply = harness.invoke(system_prompt, history, cancel_event=cancel_event, on_delta=on_delta, **extra)
            else:
                reply = harness.invoke(system_prompt, history)
            if ledger is not None:
                usage = getattr(reply, 'usage', {})
                from ..request_lifecycle import usage_total
                total = usage_total(usage)
                ledger.settle(turn_id, total)
                settled = True
                if type(total) is not int or total < 0:
                    raise ProtocolError('模型实际用量无法核实，评测已停止；不得自动重发')
                if ledger.status()['unresolved_requests']:
                    raise ProtocolError('模型用量超过预留，评测已停止；需人工核对')
            return reply
        except Exception as error:
            from ..chat import ChatReplyError, ChatCancelled
            from ..request_lifecycle import RequestFailure
            if (ledger is not None and reserved and not settled
                    and isinstance(error,(RequestFailure,ChatCancelled)) and error.sent is False
                    and error.output_started is False and error.stream_completed is False
                    and error.transport_phase in {'not_started','connecting','connected'}):
                ledger.settle_not_sent(turn_id,error)
                settled = True
            known_terminal = isinstance(error, ChatReplyError) or (isinstance(error, ChatCancelled) and error.stream_completed)
            if ledger is not None and reserved and (not settled) and known_terminal:
                from ..request_lifecycle import usage_total
                total = usage_total(error.usage)
                ledger.settle(turn_id, total if type(total) is int and total >= 0 else None)
                settled = True
            raise
        finally:
            if ledger is not None:
                try:
                    if reserved and (not settled):
                        ledger.settle(turn_id, None)
                finally:
                    ledger.close()

    def _validate_turn_input(self, text):
        CHAT_LABELS = self.dependencies.CHAT_LABELS
        ProtocolError = self.dependencies.ProtocolError
        TEXT_LIMIT = self.dependencies.TEXT_LIMIT
        if self.chat_harness is None:
            raise ProtocolError(CHAT_LABELS['unconfigured_notice'])
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError('消息不能为空')
        if len(text) > TEXT_LIMIT:
            raise ProtocolError(f'消息超过长度上限 {TEXT_LIMIT} 字符')

    def send_session_turn(self, session_id, text, idempotency_key, trace_id):
        """Compatibility path: execute synchronously for the legacy chat API."""
        AgentSessionStore = self.dependencies.AgentSessionStore
        self._validate_turn_input(text)
        with self.chat_lock:
            root = self.workspace.root
            store = AgentSessionStore(self.workspace.root)
            try:
                self._require_session_harness(store, session_id)
                system_prompt = self._chat_system_prompt()
                model_input = self._assemble_session_context(store, session_id, system_prompt, text.strip())
                (turn, replayed) = store.start_turn(session_id, text.strip(), idempotency_key, actor='local_user', trace_id=trace_id, token_reservation=self._reserve_context(model_input), model_input=model_input)
                turn_id = turn['turn_id']
            finally:
                store.close()
        if not replayed:
            (turn, _) = self._run_session_turn(root, session_id, turn_id, trace_id, system_prompt)
        with self.chat_lock:
            store = AgentSessionStore(root)
            try:
                return {'session': store.get_session(session_id), 'turn': turn, 'replayed': replayed, 'messages': store.chat_messages(session_id)}
            finally:
                store.close()

    def launch_session_turn(self, session_id, text, idempotency_key, trace_id, workspace_id=None):
        """Persist a running turn and return immediately while a worker executes it."""
        AgentSessionStore = self.dependencies.AgentSessionStore
        self._validate_turn_input(text)
        with self.chat_lock, self.ws_lock:
            self._check_session_workspace(workspace_id)
            root = self.workspace.root
            store = AgentSessionStore(root)
            try:
                self._require_session_harness(store, session_id)
                system_prompt = self._chat_system_prompt(root)
                model_input = self._assemble_session_context(store, session_id, system_prompt, text.strip())
                reservation = self._reserve_context(model_input)
                (turn, replayed) = store.start_turn(session_id, text.strip(), idempotency_key, actor='local_user', trace_id=trace_id, enqueue=True, token_reservation=reservation, model_name=self.chat_model, model_input=model_input)
                job = store.agent_job_for_turn(turn['turn_id'])
                payload = {'session': store.get_session(session_id), 'turn': turn, 'job': job, 'replayed': replayed, 'messages': store.chat_messages(session_id)}
            finally:
                store.close()
        if not replayed:
            self._schedule_agent_job(root, job, turn, system_prompt)
        return payload

    def _schedule_agent_job(self, root, job, turn, system_prompt):
        self._turn_harnesses[turn['turn_id']] = self.chat_harness
        threading = self.dependencies.threading
        cancel_event = threading.Event()
        with self.turn_jobs_lock:
            if turn['turn_id'] in self._turn_jobs:
                return False
            self._turn_jobs[turn['turn_id']] = (root, cancel_event)

        def work():
            try:
                self._execute_agent_job(root, job, turn, system_prompt, cancel_event)
            finally:
                with self.turn_jobs_lock:
                    self._turn_jobs.pop(turn['turn_id'], None)
        self._start_job(work)
        return True

    def retry_session_turn(self, turn_id, idempotency_key, trace_id):
        AgentSessionStore = self.dependencies.AgentSessionStore
        CHAT_LABELS = self.dependencies.CHAT_LABELS
        ProtocolError = self.dependencies.ProtocolError
        if self.chat_harness is None:
            raise ProtocolError(CHAT_LABELS['unconfigured_notice'])
        with self.chat_lock:
            root = self.workspace.root
            store = AgentSessionStore(root)
            try:
                source = store.get_turn(turn_id)
                self._require_session_harness(store, source['session_id'])
                system_prompt = self._chat_system_prompt(root)
                model_input = self._assemble_session_context(store, source['session_id'], system_prompt, source['user_message'] if source['actor'] != 'agent_continuation' else None)
                (turn, job, replayed) = store.retry_turn(turn_id, idempotency_key, actor='agent_continuation' if source['actor'] == 'agent_continuation' else 'local_user', trace_id=trace_id, token_reservation=self._reserve_context(model_input), model_name=self.chat_model, model_input=model_input)
                payload = {'session': store.get_session(turn['session_id']), 'turn': turn, 'job': job, 'replayed': replayed, 'messages': store.chat_messages(turn['session_id'])}
            finally:
                store.close()
        if not replayed:
            self._schedule_agent_job(root, job, turn, system_prompt)
        return payload

    def steer_session_turn(self, turn_id, text, idempotency_key, trace_id):
        AgentSessionStore = self.dependencies.AgentSessionStore
        self._validate_turn_input(text)
        with self.turn_jobs_lock:
            active = self._turn_jobs.get(turn_id)
        root = active[0] if active else self.workspace.root
        with self.chat_lock:
            store = AgentSessionStore(root)
            try:
                source = store.get_turn(turn_id)
                self._require_session_harness(store, source['session_id'])
                system_prompt = self._chat_system_prompt(root)
                model_input = self._assemble_session_context(store, source['session_id'], system_prompt, text.strip())
                (turn, job, replayed) = store.steer_turn(turn_id, text, idempotency_key, actor='local_user', trace_id=trace_id, token_reservation=self._reserve_context(model_input), model_name=self.chat_model, model_input=model_input)
                payload = {'session': store.get_session(turn['session_id']), 'turn': turn, 'job': job, 'replayed': replayed, 'messages': store.chat_messages(turn['session_id'])}
            finally:
                store.close()
        if active:
            active[1].set()
        if not replayed:
            self._schedule_agent_job(root, job, turn, system_prompt)
        return payload

    def _execute_agent_job(self, root, job, turn, system_prompt, cancel_event):
        """Claim, heartbeat and terminally reconcile one persistent Agent Job."""
        AgentSessionStore = self.dependencies.AgentSessionStore
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        threading = self.dependencies.threading
        navigation_version = self.navigation_version
        with self.agent_slots:
            claimed = AgentSessionStore(root)
            try:
                claimed.claim_agent_job(job['job_id'], worker_id=self.agent_worker_id)
            except ProtocolError:
                return
            finally:
                claimed.close()
            heartbeat_stop = threading.Event()

            def heartbeat():
                while not heartbeat_stop.wait(30):
                    lease_store = AgentSessionStore(root)
                    try:
                        lease_store.renew_agent_job(job['job_id'], worker_id=self.agent_worker_id)
                    except ProtocolError:
                        return
                    finally:
                        lease_store.close()
            heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
            heartbeat_thread.start()
            usage = {}
            try:
                (_, usage) = self._run_session_turn(root, turn['session_id'], turn['turn_id'], turn['trace_id'], system_prompt, cancel_event)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)
            finished = AgentSessionStore(root)
            try:
                finished.finish_agent_job(job['job_id'], usage=usage)
                current = finished.get_turn(turn['turn_id'])
                calls = [call for call in finished.tool_calls(turn['session_id']) if call['turn_id'] == turn['turn_id']]
                invalid = next((call for call in calls if call['status'] == 'invalid'), None)
                if invalid and current['status'] == 'completed':
                    if self.workspace.root.resolve() != Path(root).resolve() or self.navigation_version != navigation_version:
                        finished.set_continuation_state(turn['turn_id'], 'blocked', '项目已切换，请核对上下文后继续')
                        invalid = None
            finally:
                finished.close()
            if invalid:
                self._maybe_continue_after_tool(invalid, root)

    def _resume_queued_agent_jobs(self):
        """Resume only durable queued work; running leases are interrupted on recovery."""
        AgentSessionStore = self.dependencies.AgentSessionStore
        Path = self.dependencies.Path
        if self.chat_harness is None:
            return
        for item in self.workspaces.roots_payload():
            root = Path(item['root'])
            store = AgentSessionStore(root)
            try:
                jobs = store.queued_agent_jobs()
            finally:
                store.close()
            for job in jobs:
                turn_store = AgentSessionStore(root)
                try:
                    turn = turn_store.get_turn(job['turn_id'])
                    self._require_session_harness(turn_store, turn['session_id'])
                finally:
                    turn_store.close()
                self._schedule_agent_job(root, job, turn, self._chat_system_prompt(root))

    def cancel_session_turn(self, turn_id, reason):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.turn_jobs_lock:
            job = self._turn_jobs.get(turn_id)
        root = job[0] if job else self.workspace.root
        store = AgentSessionStore(root)
        try:
            turn = store.cancel_turn(turn_id, actor='local_user', reason=reason)
            if job:
                job[1].set()
            return {'turn': turn, 'messages': store.chat_messages(turn['session_id'])}
        finally:
            store.close()

    def continue_session_after_tools(self, source_turn_id, workspace_root=None):
        """Queue the next model step with native settled tool messages, no fake user input."""
        AgentSessionStore = self.dependencies.AgentSessionStore
        CHAT_LABELS = self.dependencies.CHAT_LABELS
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        new_trace_id = self.dependencies.new_trace_id
        if self.chat_harness is None:
            raise ProtocolError(CHAT_LABELS['unconfigured_notice'])
        with self.chat_lock, self.ws_lock:
            if self._monitor_stop.is_set():
                raise ProtocolError('工作台正在关闭；真实结果已保存，重启后核对并手动继续')
            root = Path(workspace_root or self.workspace.root).resolve()
            if root != self.workspace.root.resolve():
                raise ProtocolError('项目已切换；真实作业结果已保留，返回原项目核对后继续')
            store = AgentSessionStore(root)
            try:
                source = store.get_turn(source_turn_id)
                self._require_session_harness(store, source['session_id'])
                from ..agent_execution_feedback import ExecutionPending, turn_feedback
                feedback = turn_feedback(self, store, source['session_id'], source_turn_id)
                if any((not item['terminal'] for item in feedback)):
                    reason = '研究作业仍在执行；等待真实终态后再根据结果继续，不重复执行实验'
                    store.set_continuation_state(source_turn_id, 'waiting_job', reason)
                    raise ExecutionPending(reason)
                system_prompt = self._chat_system_prompt(root)
                model_input = self._assemble_session_context(store, source['session_id'], system_prompt, feedback_source_turn_id=source_turn_id)
                reservation = self._reserve_context(model_input)
                if self.evaluation_budget_path is not None:
                    from ..research.agent_evaluation_budget import AgentEvaluationBudget
                    ledger = AgentEvaluationBudget(self.evaluation_budget_path)
                    try:
                        (status, policy) = (ledger.status(), ledger.policy())
                        if status['unresolved_requests'] or status['remaining_calls'] <= 0 or status['used_tokens'] + reservation > policy['max_tokens']:
                            raise ProtocolError('评测总账本用量未知或剩余额度不足，禁止续推理')
                    finally:
                        ledger.close()
                (turn, job, replayed) = store.continue_after_tools(source_turn_id, trace_id=new_trace_id(), token_reservation=reservation, model_name=self.chat_model, model_input=model_input)
                payload = {'session': store.get_session(source['session_id']), 'turn': turn, 'job': job, 'replayed': replayed, 'messages': store.chat_messages(source['session_id'])}
            finally:
                store.close()
        if not replayed:
            self._schedule_agent_job(root, job, turn, system_prompt)
        return payload

    def _maybe_continue_after_tool(self, call, workspace_root=None):
        """Best-effort scheduling; a budget failure never hides the tool outcome."""
        AgentSessionStore = self.dependencies.AgentSessionStore
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        if call['status'] not in {'completed', 'failed', 'rejected', 'cancelled', 'interrupted', 'invalid'}:
            return
        root = Path(workspace_root or self.workspace.root).resolve()
        try:
            self.continue_session_after_tools(call['turn_id'], root)
        except Exception as error:
            from ..agent_execution_feedback import ExecutionPending
            if isinstance(error, ExecutionPending):
                return
            store = AgentSessionStore(root)
            try:
                calls = [item for item in store.tool_calls(call['session_id']) if item['turn_id'] == call['turn_id']]
                pending = any((item['status'] in {'approved', 'pending_approval', 'running'} for item in calls))
                store.set_continuation_state(call['turn_id'], 'waiting_approval' if pending else 'blocked', str(error))
            except ProtocolError:
                pass
            finally:
                store.close()

    def send_chat(self, text):
        """发送一条用户消息：真实调用 BYOK 模型并原子落盘，返回最新对话载荷。

        模型调用失败时保留用户消息，并写入一条带 error 标记的助手条目如实记录
        失败（绝不伪造模型回复）。不写事件链、不推进阶段、不改评测数字。
        """
        AgentSessionStore = self.dependencies.AgentSessionStore
        CHAT_LABELS = self.dependencies.CHAT_LABELS
        CHAT_REL = self.dependencies.CHAT_REL
        ProtocolError = self.dependencies.ProtocolError
        TEXT_LIMIT = self.dependencies.TEXT_LIMIT
        new_trace_id = self.dependencies.new_trace_id
        secrets = self.dependencies.secrets
        if self.chat_harness is None:
            raise ProtocolError(CHAT_LABELS['unconfigured_notice'])
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError('消息不能为空')
        if len(text) > TEXT_LIMIT:
            raise ProtocolError(f'消息超过长度上限 {TEXT_LIMIT} 字符')
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                store.import_legacy_chat(self.workspace.root / CHAT_REL, self.chat_model)
                session = store.default_session(create=True, model=self.chat_model)
            finally:
                store.close()
        self.send_session_turn(session['session_id'], text.strip(), 'chat-' + secrets.token_hex(16), new_trace_id())
        return self.chat_payload()
