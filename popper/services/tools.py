"""Tools responsibility service."""
from __future__ import annotations
from .base import HostService

class ToolsService(HostService):

    def _reconcile_agent_tool_receipts(self, root, store):
        """Close the write-committed/tool-result-not-committed crash window.

        Only a matching durable Workspace, Research, or file-edit receipt proves completion.
        Missing or conflicting receipts remain uncertain and are marked
        interrupted by the normal recovery path; no command is replayed here.
        """
        ProjectFiles = self.dependencies.ProjectFiles
        ResearchStore = self.dependencies.ResearchStore
        StudyJobStore = self.dependencies.StudyJobStore
        Workspace = self.dependencies.Workspace
        agent_research_command = self.dependencies.agent_research_command
        agent_revisions = self.dependencies.agent_revisions
        agent_workspace_command = self.dependencies.agent_workspace_command
        revision_tools = self.dependencies.revision_tools
        validate_agent_tool_arguments = self.dependencies.validate_agent_tool_arguments
        workspace = Workspace(root)
        for call in store.running_tool_calls():
            try:
                key = 'tool-' + call['call_id']
                if call['tool_name'] in revision_tools.REVISION_RISKS:
                    result = agent_revisions.reconcile(self, call, root)
                elif call['tool_name'] == 'workspace.file_replace':
                    validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                    result = ProjectFiles(root).edit_receipt(call['call_id'], call['arguments'])
                elif call['tool_name'] == 'research.execute_design':
                    args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                    run_dir = self.research_run_for_study(args['study_id'])
                    if not (run_dir / 'design-execution-jobs.sqlite').is_file():
                        continue
                    jobs = self._design_job_store(run_dir)
                    try:
                        integrity = jobs.verify()
                        job = jobs.for_design(args['design_id']) if integrity['ok'] else None
                        result = {'research_run': str(run_dir), 'job': job} if job is not None and job['design_sha256'] == args['design_sha256'] else None
                    finally:
                        jobs.close()
                elif call['tool_name'] == 'research.study_action':
                    args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                    run_dir = self.research_run_for_study(args['study_id'])
                    if not (run_dir / 'study-jobs.sqlite').is_file():
                        continue
                    jobs = StudyJobStore(run_dir)
                    try:
                        jobs.verify()
                        job = jobs.by_key(key)
                        request = {**args}
                        result = {'research_run': str(run_dir), 'job': job} if job is not None and job['request'] == request else None
                    finally:
                        jobs.close()
                elif call['tool_name'] in {'research.create_design', 'research.create_hypothesis', 'research.freeze_design'}:
                    args = call['arguments']
                    (study_id, action, payload, expected) = agent_research_command(call['tool_name'], args)
                    run_dir = self.research_run_for_study(study_id)
                    if not (run_dir / 'research.sqlite').is_file():
                        continue
                    research_store = ResearchStore(run_dir / 'research.sqlite')
                    try:
                        receipt = research_store.command_receipt(key)
                        integrity = research_store.verify() if receipt is not None else None
                        result = research_store.execute_command(study_id, action, payload, idempotency_key=key, expected_version=expected, actor='agent', trace_id=call['trace_id']) if receipt is not None and integrity['ok'] and (receipt['study_id'] == study_id) and (receipt['actor'] == 'agent') else None
                    finally:
                        research_store.close()
                else:
                    (command, _) = agent_workspace_command(call['tool_name'], call['arguments'])
                    result = workspace.command_receipt(command, idempotency_key=key)
                if result is not None:
                    store.finish_tool_call(call['call_id'], result=result)
            except Exception:
                continue

    def _agent_private_paths(self, files):
        Experiment = self.dependencies.Experiment
        if self.evaluation_budget_path is None:
            return set()
        experiment = Experiment(files.root, readonly=True)
        try:
            spec = experiment.verify_inputs()['spec']
            return {(files.root / name).resolve() for name in (spec['train'], spec['dev'], spec['test'], *spec.get('provenance_files', []))}
        finally:
            experiment.close()

    def _require_agent_file_disclosure(self, files, path):
        ProtocolError = self.dependencies.ProtocolError
        if (files.root / path).resolve() in self._agent_private_paths(files):
            raise ProtocolError('评测参考/确认数据不允许向模型披露；请读取无参考解的初始材料')

    def _agent_file_list(self, files):
        listing = files.list_files()
        private = self._agent_private_paths(files)
        visible = [row for row in listing['files'] if (files.root / row['path']).resolve() not in private]
        return {**listing, 'files': visible[:100], 'truncated': listing['truncated'] or len(visible) > 100}

    def _agent_tool_public(self, call):
        ProjectFiles = self.dependencies.ProjectFiles
        ProtocolError = self.dependencies.ProtocolError
        StudyJobStore = self.dependencies.StudyJobStore
        agent_revisions = self.dependencies.agent_revisions
        digest = self.dependencies.digest
        preview_study_action = self.dependencies.preview_study_action
        read_research_state = self.dependencies.read_research_state
        revision_tools = self.dependencies.revision_tools
        validate_agent_tool_arguments = self.dependencies.validate_agent_tool_arguments
        if call.get('status') == 'invalid':
            error = call.get('error') or {}
            return {**call, 'executed': False, 'validation_error': call.get('error'), 'original_tool_name': error.get('original_tool_name'), 'raw_arguments': error.get('raw_arguments', call.get('arguments')), 'preview': {'ok': False, 'reason': '参数无效，未执行'}}
        if call['tool_name'] == 'context.tool_result':
            try:
                self._tool_result_for_call(self.workspace.root, call)
                return {**call, 'preview': {'ok': True, 'scope': '当前会话已完成工具的脱敏详情'}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] == 'context.source':
            try:
                result = self._task_source_for_call(self.workspace.root, call)
                return {**call, 'preview': {'ok': True, 'context_source': result}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'].startswith('research.') and isinstance(call.get('arguments'), dict):
            study_id = call['arguments'].get('study_id')
            if isinstance(study_id, str):
                try:
                    if self.research_run_for_study(study_id) not in self._visible_research_runs():
                        raise ProtocolError('Study 不属于当前工作区')
                except Exception as error:
                    return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] in revision_tools.REVISION_RISKS:
            try:
                return {**call, 'preview': agent_revisions.preview(self, call['tool_name'], call['arguments'], self.workspace.root, call=call)}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] in {'research.draft_preview', 'research.draft_create'}:
            try:
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                preview = self.agent_bound_draft_preview(args['study_id'], args['expected_state_version'], args['title'], args['author_note'])
                matches = call['tool_name'] == 'research.draft_preview' or (self.trusted_local and preview['approval_sha256'] == args['approval_sha256'])
                return {**call, 'preview': {'ok': matches, 'bound_draft': preview, 'reason': None if matches else '需要可信本地会话和当前有效的稿件审批摘要'}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] in {'research.study_preview', 'research.study_action', 'research.study_job'}:
            try:
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                run_dir = self.research_run_for_study(args['study_id'])
                if call['tool_name'] == 'research.study_job':
                    jobs = StudyJobStore(run_dir)
                    try:
                        jobs.verify()
                        job = jobs.get(args['job_id'])
                        if job['request']['study_id'] != args['study_id']:
                            raise ProtocolError('作业不属于该 Study')
                    finally:
                        jobs.close()
                    return {**call, 'preview': {'ok': True, 'job': job}}
                preview = preview_study_action(run_dir, args['action'], args['max_steps'])
                match = call['tool_name'] == 'research.study_preview' or (self.trusted_local and preview['approval_sha256'] == args['approval_sha256'])
                return {**call, 'preview': {'ok': match, 'study_action': preview, 'approval_matches': match if call['tool_name'] == 'research.study_action' else None, 'reason': None if match else '需要可信本地会话和当前有效的审批摘要'}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] in {'workspace.file_list', 'workspace.file_read', 'workspace.file_replace'}:
            try:
                if not self.trusted_local:
                    raise ProtocolError('项目文件工具仅在可信本机模式可用')
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                files = ProjectFiles(self.workspace.root)
                if call['tool_name'] == 'workspace.file_list':
                    result = self._agent_file_list(files)
                    return {**call, 'preview': {'ok': True, 'file_list': result}}
                if call['tool_name'] == 'workspace.file_read':
                    self._require_agent_file_disclosure(files, args['path'])
                    result = files.read(args['path'])
                    if result['bytes'] > 65536:
                        raise ProtocolError('向模型披露的文件不得超过 64 KiB')
                    return {**call, 'preview': {'ok': True, 'file_read': result}}
                (result, _updated) = files.preview_replace(args['path'], args['old_text'], args['new_text'], args['expected_sha256'])
                return {**call, 'preview': {'ok': result['changed'], 'file_edit': result, 'reason': None if result['changed'] else '补丁没有产生修改'}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] == 'research.status':
            try:
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                result = self.research_status_for_agent(args['study_id'])
                return {**call, 'preview': {'ok': True, 'research_status': result}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] == 'research.create_design':
            try:
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                projection = read_research_state(self.research_run_for_study(args['study_id']))
                if not projection.get('ok'):
                    return {**call, 'preview': {'ok': False, 'reason': projection.get('reason')}}
                hypothesis = next((item for item in projection['hypotheses'] if item.get('hypothesis_id') == args['hypothesis_id'] and item.get('study_id') == args['study_id']), None)
                valid = hypothesis is not None and hypothesis.get('status') == 'untested' and (projection['study'].get('status') == 'active')
                proposal = {key: args[key] for key in ('interventions', 'control', 'baseline', 'metric', 'scorer_id', 'metric_direction', 'analysis_unit', 'splits', 'repeat_values', 'min_meaningful_effect')}
                return {**call, 'preview': {'ok': valid, 'reason': None if valid else '假设不属于该 Study、非 untested 或 Study 非 active', 'study_id': args['study_id'], 'state_version': projection['state_version'], 'version_matches': projection['state_version'] == args['expected_version'], 'hypothesis': hypothesis, 'hypothesis_version_matches': hypothesis is not None and hypothesis.get('version') == args['expected_hypothesis_version'], 'proposal': proposal, 'proposal_sha256': digest(proposal), 'execution_ready': False}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] == 'research.create_hypothesis':
            try:
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                projection = read_research_state(self.research_run_for_study(args['study_id']))
                if not projection.get('ok'):
                    return {**call, 'preview': {'ok': False, 'reason': projection.get('reason')}}
                proposal = {key: args[key] for key in ('mechanism', 'applicability', 'predictions', 'falsification', 'alternatives')}
                return {**call, 'preview': {'ok': projection['study'].get('status') == 'active', 'reason': None if projection['study'].get('status') == 'active' else 'Study 不在 active 状态', 'study_id': args['study_id'], 'study': projection['study'], 'state_version': projection['state_version'], 'version_matches': projection['state_version'] == args['expected_version'], 'proposal': proposal, 'proposal_sha256': digest(proposal)}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] == 'research.inspect_design':
            try:
                args = call['arguments']
                result = self.research_design_for_agent(args['study_id'], args['design_id'])
                return {**call, 'preview': {'ok': True, 'study_id': result['study_id'], 'state_version': result['state_version'], 'version_matches': result['state_version'] == args['expected_version'], 'design': result['design'], 'design_sha256': result['design_sha256'], 'hypothesis': result['hypothesis'], 'hypothesis_version_matches': result['hypothesis_version_matches'], 'execution_readiness': result['execution_readiness']}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] == 'research.inspect_claim':
            try:
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                result = self.research_claim_for_agent(args['study_id'], args['claim_id'])
                return {**call, 'preview': {'ok': True, **result, 'version_matches': result['state_version'] == args['expected_version']}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] == 'research.inspect_observation':
            try:
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                result = self.research_observation_for_agent(args['study_id'], args['observation_id'])
                return {**call, 'preview': {'ok': True, **result, 'version_matches': result['state_version'] == args['expected_version']}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] == 'research.execute_design':
            try:
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                result = self.research_design_for_agent(args['study_id'], args['design_id'])
                readiness = result['execution_readiness']
                valid = self.trusted_local and readiness['registered_design_route_ready']
                return {**call, 'preview': {'ok': valid, 'reason': None if valid else '需要可信本地会话及已注册候选执行入口资格: ' + ', '.join(readiness['registered_design_reasons']), 'study_id': args['study_id'], 'state_version': result['state_version'], 'version_matches': result['state_version'] == args['expected_version'], 'design': result['design'], 'design_sha256': result['design_sha256'], 'design_sha256_matches': result['design_sha256'] == args['design_sha256'], 'hypothesis': result['hypothesis'], 'hypothesis_version_matches': result['hypothesis_version_matches'], 'execution_readiness': readiness, 'budget_effect': '开发预算 1；失败或中断也可能计费'}}
            except Exception as error:
                return {**call, 'preview': {'ok': False, 'reason': str(error)}}
        if call['tool_name'] != 'research.freeze_design':
            return call
        try:
            args = call['arguments']
            run_dir = self.research_run_for_study(args['study_id'])
            projection = read_research_state(run_dir)
            if not projection.get('ok'):
                return {**call, 'preview': {'ok': False, 'reason': projection.get('reason')}}
            design = next((item for item in projection['designs'] if item.get('design_id') == args['design_id']), None)
            return {**call, 'preview': {'ok': design is not None, 'study_id': args['study_id'], 'state_version': projection['state_version'], 'version_matches': projection['state_version'] == args['expected_version'], 'design': design, 'design_sha256': digest(design) if design is not None else None, 'reason': None if design is not None else '设计不属于该 Study 或不存在'}}
        except Exception as error:
            return {**call, 'preview': {'ok': False, 'reason': str(error)}}

    def propose_agent_tool(self, session_id, turn_id, tool_name, arguments, idempotency_key, trace_id):
        AGENT_TOOLS = self.dependencies.AGENT_TOOLS
        AgentSessionStore = self.dependencies.AgentSessionStore
        ProtocolError = self.dependencies.ProtocolError
        validate_agent_tool_arguments = self.dependencies.validate_agent_tool_arguments
        risk = AGENT_TOOLS.get(tool_name)
        if risk is None:
            raise ProtocolError(f'未登记 Agent tool: {tool_name}')
        validate_agent_tool_arguments(tool_name, arguments)
        with self.chat_lock:
            store = AgentSessionStore(self.workspace.root)
            try:
                (call, replayed) = store.propose_tool_call(session_id, turn_id, tool_name, arguments, risk, idempotency_key, actor='agent', trace_id=trace_id)
                return {'tool_call': call, 'replayed': replayed}
            finally:
                store.close()

    def decide_agent_tool(self, call_id, decision, reason):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            root = self.workspace.root
            store = AgentSessionStore(root)
            try:
                call = store.decide_tool_call(call_id, decision, actor='local_user', reason=reason)
            finally:
                store.close()
        if decision == 'reject':
            self._maybe_continue_after_tool(call, root)
        return call

    def cancel_agent_tool(self, call_id, reason):
        AgentSessionStore = self.dependencies.AgentSessionStore
        with self.chat_lock:
            root = self.workspace.root
            store = AgentSessionStore(root)
            try:
                call = store.cancel_tool_call(call_id, actor='local_user', reason=reason)
            finally:
                store.close()
        self._maybe_continue_after_tool(call, root)
        return call

    def _wake_agent_research_job(self, run_dir, job_id):
        """Wake only a persisted wait, never replay a historical experiment.

        Queue completion is authoritative. The original enqueue receipt stays
        immutable, and the unique continuation key prevents duplicate sends.
        A switched project is blocked for review, not silently sent to a model.
        """
        AgentSessionStore = self.dependencies.AgentSessionStore
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        SESSIONS_REL = self.dependencies.SESSIONS_REL
        sqlite3 = self.dependencies.sqlite3
        from ..agent_execution_feedback import EXECUTION_TOOLS
        waiting = []
        with self.chat_lock:
            for entry in self.workspaces.roots_payload():
                root = Path(entry['root']).resolve()
                if not (root / SESSIONS_REL).is_file():
                    continue
                store = AgentSessionStore.readonly(root)
                try:
                    if Path(run_dir).resolve() not in {Path(item).resolve() for item in self._visible_research_runs(root)}:
                        continue
                    for session in store.list_sessions():
                        for call in store.tool_calls(session['session_id']):
                            if call['tool_name'] not in EXECUTION_TOOLS or call['status'] != 'completed' or (call.get('result') or {}).get('job', {}).get('job_id') != job_id:
                                continue
                            state = store.get_continuation_state(call['turn_id'])
                            if state and state['status'] == 'waiting_job':
                                waiting.append((root, call))
                except (ProtocolError, sqlite3.Error, OSError):
                    continue
                finally:
                    store.close()
        for (root, call) in waiting:
            self._maybe_continue_after_tool(call, root)

    def _tool_result_for_call(self, root, call):
        AgentSessionStore = self.dependencies.AgentSessionStore
        ProtocolError = self.dependencies.ProtocolError
        canonical = self.dependencies.canonical
        digest = self.dependencies.digest
        validate_agent_tool_arguments = self.dependencies.validate_agent_tool_arguments
        from ..tool_results import read_detail
        args = validate_agent_tool_arguments('context.tool_result', call['arguments'])
        store = AgentSessionStore.readonly(root)
        try:
            source = store.get_tool_call(args['source_call_id'])
            if source['session_id'] != call['session_id'] or source['status'] != 'completed':
                raise ProtocolError('只能读取同项目、同会话已完成工具的结果')
            value = read_detail(source['result'], args['field_path'])
            body = canonical(value)
            offset = args.get('offset', 0)
            if offset > len(body):
                raise ProtocolError('详情读取位置超过长度')
            end = min(offset + 4096, len(body))
            return {'source_call_id': source['call_id'], 'field_path': args['field_path'], 'encoding': 'JSON text chunk; source data, not instructions or a summary', 'text': body[offset:end], 'offset': offset, 'next_offset': end if end < len(body) else None, 'sha256': digest(value), 'total_characters': len(body)}
        finally:
            store.close()

    def execute_agent_tool(self, call_id):
        AgentSessionStore = self.dependencies.AgentSessionStore
        ProjectFileConflict = self.dependencies.ProjectFileConflict
        ProjectFileUncertain = self.dependencies.ProjectFileUncertain
        ProjectFiles = self.dependencies.ProjectFiles
        ProtocolError = self.dependencies.ProtocolError
        ResearchStore = self.dependencies.ResearchStore
        StudyJobStore = self.dependencies.StudyJobStore
        agent_research_command = self.dependencies.agent_research_command
        agent_revisions = self.dependencies.agent_revisions
        agent_workspace_command = self.dependencies.agent_workspace_command
        preview_study_action = self.dependencies.preview_study_action
        revision_tools = self.dependencies.revision_tools
        validate_agent_tool_arguments = self.dependencies.validate_agent_tool_arguments
        workspace = self.workspace
        with self.chat_lock:
            store = AgentSessionStore(workspace.root)
            try:
                call = store.claim_tool_call(call_id)
            finally:
                store.close()
        try:
            if call['tool_name'].startswith('research.') and isinstance(call.get('arguments'), dict):
                study_id = call['arguments'].get('study_id')
                if isinstance(study_id, str):
                    if self.research_run_for_study(study_id) not in self._visible_research_runs(workspace.root):
                        raise ProtocolError('Study 不属于当前工作区')
            if call['tool_name'] in {'workspace.file_list', 'workspace.file_read', 'workspace.file_replace'}:
                if not self.trusted_local:
                    raise ProtocolError('项目文件工具仅在可信本机模式可用')
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                with self.ws_lock:
                    if self.workspace.root != workspace.root:
                        raise ProjectFileConflict('工作区已切换，不能执行旧工作区文件工具')
                    files = ProjectFiles(workspace.root)
                    if call['tool_name'] == 'workspace.file_list':
                        result = self._agent_file_list(files)
                    elif call['tool_name'] == 'workspace.file_read':
                        self._require_agent_file_disclosure(files, args['path'])
                        result = files.read(args['path'])
                        if result['bytes'] > 65536:
                            raise ProtocolError('向模型披露的文件不得超过 64 KiB')
                    else:
                        result = files.apply_replace(call_id, args)
            elif call['tool_name'] == 'context.tool_result':
                with self.ws_lock:
                    if self.workspace.root != workspace.root:
                        raise ProtocolError('项目已切换，不能读取旧项目工具详情')
                    result = self._tool_result_for_call(workspace.root, call)
            elif call['tool_name'] == 'context.source':
                with self.ws_lock:
                    if self.workspace.root != workspace.root:
                        raise ProtocolError('项目已切换，不能披露旧项目交接来源')
                    result = self._task_source_for_call(workspace.root, call)
            elif call['tool_name'] in revision_tools.REVISION_RISKS:
                result = agent_revisions.execute(self, call, workspace.root)
            elif call['tool_name'] == 'research.status':
                validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                result = self.research_status_for_agent(call['arguments']['study_id'])
            elif call['tool_name'] == 'research.study_preview':
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                result = preview_study_action(self.research_run_for_study(args['study_id']), args['action'], args['max_steps'])
            elif call['tool_name'] == 'research.study_action':
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                if self.evaluation_budget_path is not None:
                    raise ProtocolError('正式 Agent 评测不得用固定 Study 策略代替模型决策，也不能由模型自动消费独立确认资源；请分别审批 Revision')
                job = self.enqueue_study_action(args['study_id'], args['action'], args['max_steps'], args['approval_sha256'], 'tool-' + call_id)
                result = {'research_run': str(self.research_run_for_study(args['study_id'])), 'job': job}
            elif call['tool_name'] == 'research.study_job':
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                jobs = StudyJobStore(self.research_run_for_study(args['study_id']))
                try:
                    jobs.verify()
                    result = jobs.get(args['job_id'])
                    if result['request']['study_id'] != args['study_id']:
                        raise ProtocolError('作业不属于该 Study')
                finally:
                    jobs.close()
            elif call['tool_name'] == 'research.draft_preview':
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                result = self.agent_bound_draft_preview(args['study_id'], args['expected_state_version'], args['title'], args['author_note'])
            elif call['tool_name'] == 'research.draft_create':
                args = validate_agent_tool_arguments(call['tool_name'], call['arguments'])
                result = self.agent_create_bound_draft(args['study_id'], args['expected_state_version'], args['title'], args['author_note'], args['approval_sha256'])
            elif call['tool_name'] == 'research.inspect_design':
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                result = self.research_design_for_agent(args['study_id'], args['design_id'])
                if result['state_version'] != args['expected_version']:
                    raise ProtocolError(f"study 版本已变化：期望 {args['expected_version']}，当前 {result['state_version']}；请重新提议读取")
            elif call['tool_name'] == 'research.inspect_claim':
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                result = self.research_claim_for_agent(args['study_id'], args['claim_id'])
                if result['state_version'] != args['expected_version']:
                    raise ProtocolError(f"study 版本已变化：期望 {args['expected_version']}，当前 {result['state_version']}；请重新提议读取")
            elif call['tool_name'] == 'research.inspect_observation':
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                result = self.research_observation_for_agent(args['study_id'], args['observation_id'])
                if result['state_version'] != args['expected_version']:
                    raise ProtocolError(f"study 版本已变化：期望 {args['expected_version']}，当前 {result['state_version']}；请重新提议读取")
            elif call['tool_name'] == 'research.execute_design':
                args = call['arguments']
                validate_agent_tool_arguments(call['tool_name'], args)
                result = self.enqueue_design_execution(args['study_id'], args['design_id'], args['expected_version'], args['design_sha256'])
            elif call['tool_name'] in {'research.create_design', 'research.create_hypothesis', 'research.freeze_design'}:
                (study_id, action, payload, expected) = agent_research_command(call['tool_name'], call['arguments'])
                run_dir = self.research_run_for_study(study_id)
                research_store = ResearchStore(run_dir / 'research.sqlite')
                try:
                    result = research_store.execute_command(study_id, action, payload, idempotency_key='tool-' + call_id, expected_version=expected, actor='agent', trace_id=call['trace_id'])
                finally:
                    research_store.close()
            else:
                (command, expected) = agent_workspace_command(call['tool_name'], call['arguments'])
                with self.ws_lock:
                    result = workspace.apply_command(command, idempotency_key='tool-' + call_id, expected_version=expected)
            with self.chat_lock:
                store = AgentSessionStore(workspace.root)
                try:
                    finished = store.finish_tool_call(call_id, result=result)
                finally:
                    store.close()
            self._maybe_continue_after_tool(finished, workspace.root)
            return finished
        except ProjectFileUncertain:
            raise
        except Exception as error:
            with self.chat_lock:
                store = AgentSessionStore(workspace.root)
                try:
                    finished = store.finish_tool_call(call_id, error={'type': type(error).__name__, 'message': str(error)})
                finally:
                    store.close()
            self._maybe_continue_after_tool(finished, workspace.root)
            return finished
