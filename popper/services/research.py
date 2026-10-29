"""Research responsibility service."""
from __future__ import annotations
from .base import HostService

class ResearchService(HostService):

    def research_run_for_study(self, study_id):
        """Resolve a study only within explicitly registered research runs."""
        ProtocolError = self.dependencies.ProtocolError
        read_json = self.dependencies.read_json
        if not isinstance(study_id, str) or not study_id:
            raise ProtocolError('study_id 必须是非空字符串')
        matches = []
        for run_dir in self.research_runs:
            manifest_path = run_dir / 'research.json'
            if not manifest_path.is_file():
                continue
            try:
                manifest = read_json(manifest_path)
            except Exception:
                continue
            if manifest.get('study_id') == study_id:
                matches.append(run_dir)
        if not matches:
            raise LookupError(f'未登记 study: {study_id}')
        if len(matches) != 1:
            raise ProtocolError(f'study 登记冲突: {study_id}')
        return matches[0]

    def state_payload(self):
        """构造 GET /api/state 的响应（含后端单一定义的阶段标签 phases）。"""
        EVENT_NAMES = self.dependencies.EVENT_NAMES
        Experiment = self.dependencies.Experiment
        GATE_STATUS_LABELS = self.dependencies.GATE_STATUS_LABELS
        MODE7_NAMES = self.dependencies.MODE7_NAMES
        PHASES = self.dependencies.PHASES
        RISK_NAMES = self.dependencies.RISK_NAMES
        ReproductionTask = self.dependencies.ReproductionTask
        STAGE_STATUS_LABELS = self.dependencies.STAGE_STATUS_LABELS
        build_review_payload = self.dependencies.build_review_payload
        json = self.dependencies.json
        read_json = self.dependencies.read_json
        reproduction_research_projection = self.dependencies.reproduction_research_projection
        if self.mode == 'experiment':
            exp = Experiment(self.project)
            try:
                events = [{'seq': r['seq'], 'kind': r['kind'], 'payload': json.loads(r['payload'])} for r in exp.db.execute('SELECT seq,kind,payload FROM events ORDER BY seq DESC LIMIT 50')]
                runs = [dict(row) for row in exp.db.execute('SELECT id,split,status FROM runs ORDER BY rowid')]
                payload = {'mode': 'experiment', 'state': exp.state(), 'results': exp.results(), 'runs': runs, 'events': events, 'review': build_review_payload(exp.state())}
            finally:
                exp.close()
        else:
            task = ReproductionTask(self.project)
            state = task.audit()
            history = task.event_history()
            manifest = state['manifest']
            outputs = state.get('run', {}).get('output_hashes', {}) if state.get('run') else {}
            artifacts = [{'path': name, 'sha256': value, 'bytes': (self.project / name).stat().st_size} for (name, value) in outputs.items()]
            result_path = next((name for name in outputs if name.endswith('results.json')), None)
            report_path = next((name for name in outputs if name.lower().endswith('report.md')), None)
            payload = {'mode': 'reproduction', 'state': state, 'paper': read_json(self.project / manifest['paper_record']), 'protocol': read_json(self.project / manifest['protocol']), 'result': read_json(self.project / result_path) if result_path else None, 'report': (self.project / report_path).read_text(encoding='utf-8')[:100000] if report_path else None, 'artifacts': artifacts, 'events': [{'seq': event['seq'], 'kind': event['event_type'], 'payload': {key: value for (key, value) in event['payload'].items() if key != 'state'}} for event in reversed(history['events'][-50:])], 'reproduction_integrity': history['integrity'], 'research_projection': reproduction_research_projection(self.project, state)}
        payload.update({'job': self.job, 'trusted_local': self.trusted_local, 'terminal_capability': self.terminals.capability(), 'phases': PHASES, 'event_names': EVENT_NAMES, 'stage_status_labels': STAGE_STATUS_LABELS, 'risk_names': RISK_NAMES, 'gate_status_labels': GATE_STATUS_LABELS, 'mode7_names': MODE7_NAMES})
        return payload

    def research_payload(self):
        """构造 GET /api/research 的响应：已登记研究 run 的只读投影。

        每个 run 目录独立投影，失败以 ok:false + reason 原样呈现，不吞错误。
        """
        RESEARCH_STATUS_LABELS = self.dependencies.RESEARCH_STATUS_LABELS
        read_research_state = self.dependencies.read_research_state
        runs = [{'run_dir': str(run_dir), **read_research_state(run_dir)} for run_dir in self._visible_research_runs()]
        return {'runs': runs, 'status_labels': RESEARCH_STATUS_LABELS, 'trusted_local': self.trusted_local}

    def record_resume_checkpoint_from_workbench(self, text, *, study_id=None, run_dir=None, refs=None):
        """覆盖写入同 run_dir 的 resume_checkpoint；与 CLI memory note 同源。

        空/超长在落盘前拒绝，避免无意义创建 memory 目录。不升格为 evidence。
        """
        from ..research.memory_context import MEMORY_TEXT_CHARS, record_resume_checkpoint
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        read_json = self.dependencies.read_json
        if not self.trusted_local:
            raise ProtocolError('续作意图写入需要可信本机模式')
        has_study = isinstance(study_id, str) and bool(study_id.strip())
        has_run = isinstance(run_dir, str) and bool(run_dir.strip())
        if has_study == has_run:
            raise ProtocolError('必须且只能提供 study_id 或 run_dir 之一')
        if has_study:
            resolved = self.research_run_for_study(study_id.strip())
            visible = set(self._visible_research_runs())
            if resolved not in visible:
                raise LookupError(f'未登记 study: {study_id.strip()}')
            study = study_id.strip()
        else:
            resolved = Path(run_dir.strip()).resolve()
            visible = set(self._visible_research_runs())
            if resolved not in visible:
                raise LookupError(f'未登记 run_dir: {resolved}')
            study = None
            manifest_path = resolved / 'research.json'
            if manifest_path.is_file():
                try:
                    manifest = read_json(manifest_path)
                except Exception:
                    manifest = None
                if isinstance(manifest, dict) and isinstance(manifest.get('study_id'), str):
                    study = manifest['study_id']
        if not isinstance(text, str):
            raise ProtocolError('续作意图 text 必须是字符串')
        normalized = ' '.join(text.split())
        if not normalized:
            raise ProtocolError('续作意图不能为空')
        if len(normalized) > MEMORY_TEXT_CHARS:
            raise ProtocolError(f'续作意图不能超过 {MEMORY_TEXT_CHARS} 字')
        entry_id = record_resume_checkpoint(
            resolved, normalized, refs=refs, study_id=study)
        if not entry_id:
            raise ProtocolError('续作意图写入失败')
        return {
            'id': entry_id,
            'text': normalized,
            'run_dir': str(resolved),
            'study_id': study,
            'trust': 'unverified_session_notes',
        }

    def _visible_research_runs(self, workspace_root=None):
        """Show only runs registered to the selected workspace, plus CLI roots on the primary."""
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        read_json = self.dependencies.read_json
        if workspace_root is None:
            with self.ws_lock:
                root = self.workspace.root.resolve()
        else:
            root = Path(workspace_root).resolve()
        registry_path = root / '.popper' / 'research-runs.json'
        registered = []
        if registry_path.is_file():
            registry = read_json(registry_path)
            if not isinstance(registry, dict) or registry.get('schema_version') != '1.0' or (not isinstance(registry.get('runs'), list)):
                raise ProtocolError('研究运行目录注册表结构不合法')
            registered = [Path(item).resolve() for item in registry['runs'] if isinstance(item, str) and item]
        if root == self.primary_project:
            registered = [*self._explicit_research_runs, *registered]
        allowed = set(registered)
        with self.research_runs_lock:
            return [run_dir for run_dir in self.research_runs if run_dir in allowed]

    def study_jobs_payload(self, study_id):
        StudyJobStore = self.dependencies.StudyJobStore
        read_json = self.dependencies.read_json
        run_dir = self.research_run_for_study(study_id)
        if not (run_dir / 'study-jobs.sqlite').exists():
            return {'jobs': []}
        store = StudyJobStore(run_dir)
        try:
            store.verify()
            progress_path = run_dir / 'search-progress.json'
            progress = read_json(progress_path) if progress_path.is_file() else None
            return {'jobs': store.list(), 'search_progress': progress}
        finally:
            store.close()

    def enqueue_study_action(self, study_id, action, max_steps, approval_sha256, idempotency_key, envelope=None):
        ProtocolError = self.dependencies.ProtocolError
        StudyJobStore = self.dependencies.StudyJobStore
        preview_study_action = self.dependencies.preview_study_action
        if not self.trusted_local:
            raise ProtocolError('Study 执行需要可信本机模式')
        run_dir = self.research_run_for_study(study_id)
        request = {'study_id': study_id, 'action': action, 'max_steps': max_steps, 'approval_sha256': approval_sha256}
        if action == 'autopilot':
            request['envelope'] = envelope
        store = StudyJobStore(run_dir)
        try:
            store.verify()
            prior = store.by_key(idempotency_key)
            if prior:
                if prior['request'] != request:
                    raise ProtocolError('幂等键已绑定不同的 Study 请求')
                return prior
            preview = preview_study_action(run_dir, action, max_steps, envelope=envelope)
            if action == 'autopilot':
                request['envelope'] = preview['envelope']
            if preview['approval_sha256'] != approval_sha256:
                raise ProtocolError('研究状态已变化，请重新预览')
            job = store.enqueue(idempotency_key, request)
        finally:
            store.close()
        self._start_job(lambda : self._run_study_job(run_dir, job['job_id']))
        return job

    def _run_study_job(self, run_dir, job_id):
        ProtocolError = self.dependencies.ProtocolError
        StudyJobStore = self.dependencies.StudyJobStore
        controller_for_run = self.dependencies.controller_for_run
        preview_study_action = self.dependencies.preview_study_action
        with self.research_execution_lock:
            store = StudyJobStore(run_dir)
            controller = None
            try:
                if store.get(job_id)['status'] != 'queued':
                    return
                job = store.claim(job_id)
                request = job['request']
                try:
                    preview = preview_study_action(run_dir, request['action'], request['max_steps'], envelope=request.get('envelope'))
                    if preview['approval_sha256'] != request['approval_sha256']:
                        raise ProtocolError('排队期间研究状态已变化，请重新预览')
                    from ..research.controller import ResearchController
                    controller = controller_for_run(run_dir)
                    cancelled = lambda : store.get(job_id)['cancel_requested']
                    if request['action'] == 'autopilot':
                        from ..harness.json_agent import JSONAgentHarness
                        from ..research.envelope import save_envelope
                        if not self.chat_base_url or not self.chat_model:
                            raise ProtocolError('自治实验需要已配置的模型端点')
                        save_envelope(run_dir, request['envelope'])
                        controller.harness = JSONAgentHarness.from_endpoint(self.chat_base_url, self.chat_model, run_dir / 'model-diagnostics')
                        result = controller.run(sandboxed=True, max_steps=request['max_steps'], autonomous_code=True, search=True, should_cancel=cancelled)
                        from ..research.search_session import lock_claim_card
                        lock_claim_card(controller.project)
                    elif request['action'] == 'run':
                        result = controller.run(sandboxed=True, max_steps=request['max_steps'], auto_confirm=False, should_cancel=cancelled)
                    else:
                        from ..research.search_session import lock_claim_card, record_split_confirmation
                        lock_claim_card(controller.project)
                        result = controller.confirm(sandboxed=True, should_cancel=cancelled)
                        record_split_confirmation(controller, sandboxed=True, trusted_local=False)
                    summary = {'phase': result['phase'], 'observation_ids': [row['observation_id'] for row in result['observations']], 'claim_ids': [row['claim_id'] for row in result['claims']]}
                    planning = result.get('planning_runtime')
                    if planning is not None:
                        summary['planning_runtime'] = planning
                    planner = (planning or {}).get('planner', {})
                    if planner.get('status') in {'failed', 'exhausted', 'cancelled', 'recovery_blocked'}:
                        store.finish(job_id, 'cancelled' if planner['status'] == 'cancelled' else 'failed',
                                     result=summary, error='研究规划未完成: ' + str(planner.get('reason')))
                    else:
                        store.finish(job_id, 'succeeded', result=summary)
                except Exception as error:
                    store.finish(job_id, 'cancelled' if store.get(job_id)['cancel_requested'] else 'failed', error=f'{type(error).__name__}: {error}')
            finally:
                if controller is not None:
                    controller.close()
                store.close()
                self._wake_agent_research_job(run_dir, job_id)

    def _recover_study_jobs(self):
        StudyJobStore = self.dependencies.StudyJobStore
        if not self.trusted_local:
            return
        for run_dir in self.research_runs:
            if not (run_dir / 'study-jobs.sqlite').exists():
                continue
            store = StudyJobStore(run_dir)
            try:
                store.verify()
                for job in store.list():
                    if job['status'] == 'running':
                        store.finish(job['job_id'], 'interrupted', error='服务重启，执行中断；请审查研究记录，确认资源不会自动重试')
                    elif job['status'] == 'queued':
                        self._start_job(lambda root=run_dir, key=job['job_id']: self._run_study_job(root, key))
            finally:
                store.close()

    def cancel_study_job(self, study_id, job_id):
        ProtocolError = self.dependencies.ProtocolError
        StudyJobStore = self.dependencies.StudyJobStore
        if not self.trusted_local:
            raise ProtocolError('取消 Study 作业需要可信本机模式')
        store = StudyJobStore(self.research_run_for_study(study_id))
        try:
            return store.cancel(job_id)
        finally:
            store.close()

    def preview_study_initialization(self, run_name, budget_cap):
        Experiment = self.dependencies.Experiment
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        digest = self.dependencies.digest
        math = self.dependencies.math
        re = self.dependencies.re
        read_json = self.dependencies.read_json
        read_research_state = self.dependencies.read_research_state
        if not self.trusted_local:
            raise ProtocolError('创建 Study 需要以 --trusted-local 启动工作台')
        if not isinstance(run_name, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,63}', run_name):
            raise ProtocolError('研究运行名称只能使用字母、数字、点、横线或下划线')
        if isinstance(budget_cap, bool) or not isinstance(budget_cap, (int, float)) or (not math.isfinite(budget_cap)) or (not 4 <= budget_cap <= 10000):
            raise ProtocolError('Study 预算上限必须是 4 到 10000 的有限数')
        with self.ws_lock:
            root = self.workspace.root.resolve()
        parent = (root / '.popper' / 'research-runs').resolve()
        run_dir = (parent / run_name).resolve()
        if not parent.is_relative_to(root) or run_dir.parent != parent:
            raise ProtocolError('研究运行目录越出当前工作区')
        exp = Experiment(root, readonly=True)
        try:
            state = exp.verify_inputs()
        finally:
            exp.close()
        if state['phase'] != 'searching':
            raise ProtocolError('只有 searching 阶段的预注册实验可以创建 Study')
        if read_json(root / 'experiment.json') != state['spec']:
            raise ProtocolError('预注册协议文件与实验状态不一致')
        existing = None
        if run_dir.exists():
            if not (run_dir / 'research.json').is_file():
                raise ProtocolError('研究运行目录已存在但未完成初始化，需先审计')
            existing = read_research_state(run_dir)
            if not existing.get('ok') or not existing.get('experiment_graph', {}).get('integrity', {}).get('ok') or Path(existing['manifest']['project']).resolve() != root:
                raise ProtocolError('已有研究运行与当前项目不匹配或完整性失败')
            if existing['budget']['cap'] != float(budget_cap):
                raise ProtocolError('已有 Study 的预算上限与本次请求不同')
        proposal = {'project': str(root), 'run_dir': str(run_dir), 'budget_cap': float(budget_cap), 'input_hashes': state['input_hashes'], 'spec_sha256': digest(state['spec']), 'evaluator_id': state['evaluator_id'], 'evaluator_hash': state['evaluator_hash'], 'objective': state['spec']['objective']}
        return {'project': str(root), 'run_dir': str(run_dir), 'run_name': run_name, 'budget_cap': float(budget_cap), 'objective': state['spec']['objective'], 'metric': state['spec']['metric'], 'candidate_count': len(state['spec']['candidates']), 'already_initialized': existing is not None, 'study_id': existing['manifest']['study_id'] if existing else None, 'proposal_sha256': digest(proposal)}

    def initialize_study_from_workbench(self, run_name, budget_cap, proposal_sha256):
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        re = self.dependencies.re
        read_research_state = self.dependencies.read_research_state
        write_json = self.dependencies.write_json
        if not isinstance(proposal_sha256, str) or re.fullmatch('[0-9a-f]{64}', proposal_sha256) is None:
            raise ProtocolError('Study 初始化确认摘要不合法')
        with self.study_init_lock:
            preview = self.preview_study_initialization(run_name, budget_cap)
            if preview['proposal_sha256'] != proposal_sha256:
                raise ProtocolError('预注册实验或初始化参数已变化，请重新预览')
            run_dir = Path(preview['run_dir'])
            if not preview['already_initialized']:
                from ..research.controller import ResearchController
                ResearchController.initialize(Path(preview['project']), run_dir, budget_cap=budget_cap)
            state = read_research_state(run_dir)
            if not state.get('ok') or not state.get('experiment_graph', {}).get('integrity', {}).get('ok'):
                raise ProtocolError('Study 已初始化但研究状态完整性失败，未登记')
            with self.research_runs_lock:
                if run_dir not in self.research_runs:
                    write_json(self._research_run_registry_path, {'schema_version': '1.0', 'runs': [*[str(path) for path in self.research_runs], str(run_dir)]})
                    self.research_runs.append(run_dir)
            return {'study_id': state['manifest']['study_id'], 'run_dir': str(run_dir), 'already_initialized': preview['already_initialized'], 'state_version': state['state_version']}

    def _recover_benchmark_jobs(self):
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        verify_cross_domain_benchmark = self.dependencies.verify_cross_domain_benchmark
        for root_info in self.workspaces.roots_payload():
            root = Path(root_info['root'])
            db = root / '.popper' / 'benchmark-jobs.sqlite'
            if not db.is_file():
                continue
            jobs = self._benchmark_job_store(root)
            try:
                integrity = jobs.verify()
                if not integrity['ok']:
                    raise ProtocolError(f"基准 Job 队列完整性失败: {integrity['reason']}")
                for job in jobs.list():
                    if job['status'] != 'running':
                        continue
                    try:
                        verify_cross_domain_benchmark(job['output_dir'])
                    except Exception:
                        continue
                    jobs.finish(job['job_id'])
                jobs.interrupt_running()
                queued = [row['job_id'] for row in jobs.list() if row['status'] == 'queued']
            finally:
                jobs.close()
            if self.trusted_local:
                for job_id in queued:
                    self._start_job(lambda root=root, job_id=job_id: self._run_benchmark_job(root, job_id))

    def _run_benchmark_job(self, root, job_id):
        ProtocolError = self.dependencies.ProtocolError
        run_cross_domain_benchmark = self.dependencies.run_cross_domain_benchmark
        verify_cross_domain_benchmark = self.dependencies.verify_cross_domain_benchmark
        jobs = self._benchmark_job_store(root)
        try:
            integrity = jobs.verify()
            if not integrity['ok']:
                raise ProtocolError(f"基准 Job 队列完整性失败: {integrity['reason']}")
            job = jobs.claim(job_id)
        except ProtocolError:
            jobs.close()
            return
        jobs.close()
        error = None
        try:
            result = run_cross_domain_benchmark(job['output_dir'], trusted_local=True)
            if result['status'] != 'passed':
                error = '跨领域基准含失败案例；详情见 benchmark.json'
            else:
                verify_cross_domain_benchmark(job['output_dir'])
        except Exception as exc:
            error = str(exc)
        jobs = self._benchmark_job_store(root)
        try:
            jobs.finish(job_id, error=error)
        finally:
            jobs.close()

    def enqueue_benchmark(self, run_name, idempotency_key):
        ProtocolError = self.dependencies.ProtocolError
        re = self.dependencies.re
        if not self.trusted_local:
            raise ProtocolError('基准执行需要以 --trusted-local 启动工作台')
        if not isinstance(run_name, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,63}', run_name):
            raise ProtocolError('基准名称只允许字母、数字、点、横线或下划线')
        with self.ws_lock:
            root = self.workspace.root
        parent = root / '.popper' / 'benchmarks'
        if not parent.resolve().is_relative_to(root):
            raise ProtocolError('基准输出目录越出工作区')
        output = parent / run_name
        with self.benchmark_enqueue_lock:
            jobs = self._benchmark_job_store(root)
            try:
                integrity = jobs.verify()
                if not integrity['ok']:
                    raise ProtocolError(f"基准 Job 队列完整性失败: {integrity['reason']}")
                (job, created) = jobs.enqueue(idempotency_key, output)
            finally:
                jobs.close()
            if created:
                self._start_job(lambda : self._run_benchmark_job(root, job['job_id']))
        return job

    def benchmark_jobs_payload(self):
        with self.ws_lock:
            root = self.workspace.root
        db = root / '.popper' / 'benchmark-jobs.sqlite'
        if not db.is_file():
            return {'jobs': [], 'integrity': {'ok': True}}
        jobs = self._benchmark_job_store(root)
        try:
            integrity = jobs.verify()
            rows = jobs.list() if integrity['ok'] else []
        finally:
            jobs.close()
        return {'jobs': rows, 'integrity': integrity}

    def benchmark_job_detail(self, job_id):
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        read_json = self.dependencies.read_json
        verify_cross_domain_benchmark = self.dependencies.verify_cross_domain_benchmark
        with self.ws_lock:
            root = self.workspace.root
        if not (root / '.popper' / 'benchmark-jobs.sqlite').is_file():
            raise LookupError('基准 Job 不存在')
        jobs = self._benchmark_job_store(root)
        try:
            integrity = jobs.verify()
            if not integrity['ok']:
                raise ProtocolError(f"基准 Job 队列完整性失败: {integrity['reason']}")
            job = jobs.get(job_id)
        finally:
            jobs.close()
        report_path = Path(job['output_dir']) / 'benchmark.json'
        report = read_json(report_path) if report_path.is_file() else None
        verification = None
        if job['status'] == 'succeeded':
            verification = verify_cross_domain_benchmark(job['output_dir'])
        return {'job': job, 'report': report, 'verification': verification}

    def followup_preview(self, parent_study_id):
        Path = self.dependencies.Path
        file_hash = self.dependencies.file_hash
        read_json = self.dependencies.read_json
        parent_run = self.research_run_for_study(parent_study_id)
        manifest_path = parent_run / 'research.json'
        manifest = read_json(manifest_path)
        parent_project = Path(manifest['project']).resolve()
        return {'parent_study_id': parent_study_id, 'parent_manifest_sha256': file_hash(manifest_path), 'allowed_test_root': str(parent_project.parent), 'project_parent': str(parent_project.parent), 'run_parent': str(parent_run.parent), 'existing_candidates': [manifest['configs'][key] for key in manifest['candidate_hypothesis_ids']]}

    def create_followup_from_workbench(self, parent_study_id, new_project_name, new_run_name, new_test_path, candidates, budget, objective, expected_parent_manifest_sha256):
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        WorkstationLease = self.dependencies.WorkstationLease
        create_followup_study = self.dependencies.create_followup_study
        file_hash = self.dependencies.file_hash
        re = self.dependencies.re
        read_json = self.dependencies.read_json
        resume_followup_study = self.dependencies.resume_followup_study
        write_json = self.dependencies.write_json
        if not self.trusted_local:
            raise ProtocolError('后继研究创建需要以 --trusted-local 启动工作台')
        name_pattern = '[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
        if not isinstance(new_project_name, str) or not re.fullmatch(name_pattern, new_project_name) or (not isinstance(new_run_name, str)) or (not re.fullmatch(name_pattern, new_run_name)):
            raise ProtocolError('新项目和研究运行目录名称只能使用字母、数字、点、横线或下划线')
        if type(budget) is not int or budget < 1:
            raise ProtocolError('后继研究预算必须是正整数')
        parent_run = self.research_run_for_study(parent_study_id)
        parent_manifest_path = parent_run / 'research.json'
        if not isinstance(expected_parent_manifest_sha256, str) or expected_parent_manifest_sha256 != file_hash(parent_manifest_path):
            raise ProtocolError('父研究清单已变化，请重新核对')
        parent_project = Path(read_json(parent_manifest_path)['project']).resolve()
        new_project = (parent_project.parent / new_project_name).resolve()
        new_run = (parent_run.parent / new_run_name).resolve()
        if new_project.parent != parent_project.parent or new_run.parent != parent_run.parent:
            raise ProtocolError('后继项目或研究运行目录越界')
        test_path = Path(new_test_path).resolve() if isinstance(new_test_path, str) else None
        if test_path is None or not test_path.is_file() or (not test_path.is_relative_to(parent_project.parent)):
            raise ProtocolError('新确认集必须是父项目同级目录树内已有的文件')
        with self.followup_create_lock:
            if new_project.exists() or new_run.exists():
                result = resume_followup_study(parent_run, new_project, new_run, candidates, test_path, budget=budget, objective=objective)
            else:
                result = create_followup_study(parent_run, new_project, new_run, candidates, test_path, budget=budget, objective=objective)
            already_leased = any((lease.path.parent.parent == new_project for lease in self._workspace_leases))
            lease = None if already_leased else WorkstationLease(new_project)
            try:
                with self.research_runs_lock:
                    persisted = [str(path) for path in self.research_runs]
                    if new_run not in self.research_runs:
                        write_json(self._research_run_registry_path, {'schema_version': '1.0', 'runs': [*persisted, str(new_run)]})
                        self.research_runs.append(new_run)
                with self.ws_lock:
                    self.workspaces.register_cli_roots([new_project])
                with self.terminals.lock:
                    self.terminals.allowed_roots.add(new_project)
                if lease is not None:
                    self._workspace_leases.append(lease)
            except Exception:
                if lease is not None:
                    lease.close()
                raise
            return result

    def _verified_research_state_for_agent(self, study_id, development_observation_id=None):
        """Replay-check a registered Study before any Agent research disclosure."""
        ProtocolError = self.dependencies.ProtocolError
        ResearchStore = self.dependencies.ResearchStore
        _agent_development_observation = self.dependencies._agent_development_observation
        file_hash = self.dependencies.file_hash
        json = self.dependencies.json
        read_research_state = self.dependencies.read_research_state
        run_dir = self.research_run_for_study(study_id)
        before = read_research_state(run_dir)
        if not before.get('ok') or not before['experiment_graph']['integrity']['ok']:
            raise ProtocolError(f"research 状态或来源图不可读取: {before.get('reason')}")
        if development_observation_id is not None:
            candidate = next((item for item in before['observations'] if item['entity_id'] == development_observation_id and item.get('study_id') == study_id), None)
            if candidate is None:
                raise LookupError('Observation 不属于已登记 Study 或不存在')
            if not _agent_development_observation(candidate.get('scope')):
                raise ProtocolError('Agent 仅允许读取开发评估 Observation；确认集结果须通过终结 Claim 查看')
        manifest_path = run_dir / 'research.json'
        manifest_sha = file_hash(manifest_path)
        try:
            frozen_scope = json.loads(before['study']['scope'])
        except (KeyError, TypeError, ValueError):
            raise ProtocolError('Study 缺少冻结研究范围') from None
        if not isinstance(frozen_scope, dict) or frozen_scope.get('manifest_sha256') != manifest_sha:
            raise ProtocolError('Research manifest 与 Study 冻结登记不一致')
        store = ResearchStore(run_dir / 'research.sqlite', readonly=True)
        try:
            audit = store.verify()
        finally:
            store.close()
        if not audit['ok']:
            raise ProtocolError(f"research 证据完整性校验失败: {audit['reason']}")
        after = read_research_state(run_dir)
        if not after.get('ok') or not after['experiment_graph']['integrity']['ok'] or after['state_version'] != before['state_version'] or (after['events_total'] != before['events_total']) or (file_hash(manifest_path) != manifest_sha):
            raise ProtocolError('证据读取期间 Study 状态变化或来源图失效，请重新提议读取')
        return (run_dir, after)

    def research_status_for_agent(self, study_id):
        """Bounded, replay-checked Study summary from a registered run."""
        _agent_development_observation = self.dependencies._agent_development_observation
        (_, state) = self._verified_research_state_for_agent(study_id)
        graph = state.get('experiment_graph') or {}
        draft_designs = [{'design_id': design.get('design_id'), 'hypothesis_id': design.get('hypothesis_id'), 'hypothesis_version': design.get('hypothesis_version')} for design in state.get('designs') or [] if design.get('status') == 'draft']
        hypotheses = [{'hypothesis_id': item.get('hypothesis_id'), 'version': item.get('version'), 'status': item.get('status'), 'mechanism_excerpt': str(item.get('mechanism') or '')[:120]} for item in state.get('hypotheses') or []]
        designs = [{'design_id': item.get('design_id'), 'hypothesis_id': item.get('hypothesis_id'), 'hypothesis_version': item.get('hypothesis_version'), 'status': item.get('status')} for item in state.get('designs') or []]
        observations = [{'observation_id': item.get('entity_id'), 'run_id': item.get('run_id'), 'scope': item.get('scope'), 'status': item.get('status')} for item in state.get('observations') or [] if _agent_development_observation(item.get('scope'))]
        return {'ok': True, 'study_id': study_id, 'state_version': state.get('state_version'), 'study': state.get('study'), 'manifest': state.get('manifest'), 'budget': state.get('budget'), 'entity_counts': {name: len(state.get(name) or []) for name in ('hypotheses', 'designs', 'runs', 'observations', 'decisions', 'reflections')}, 'draft_designs': draft_designs[:20], 'draft_designs_total': len(draft_designs), 'hypotheses': hypotheses[:8], 'hypotheses_total': len(hypotheses), 'designs': designs[:8], 'designs_total': len(designs), 'observations': observations[:20], 'observations_total': len(observations), 'experiment_graph_integrity': graph.get('integrity'), 'events_total': state.get('events_total')}

    def research_design_for_agent(self, study_id, design_id):
        """Disclose one registered Study's design; never accept filesystem paths."""
        EntityKind = self.dependencies.EntityKind
        ProtocolError = self.dependencies.ProtocolError
        controller_routing_readiness = self.dependencies.controller_routing_readiness
        digest = self.dependencies.digest
        parse_id = self.dependencies.parse_id
        parse_id(study_id, EntityKind.STUDY)
        parse_id(design_id, EntityKind.DESIGN)
        (run_dir, state) = self._verified_research_state_for_agent(study_id)
        design = next((item for item in state.get('designs') or [] if item.get('design_id') == design_id and item.get('study_id') == study_id), None)
        if design is None:
            raise LookupError('design 不属于已登记 study 或不存在')
        hypothesis = next((item for item in state.get('hypotheses') or [] if item.get('hypothesis_id') == design.get('hypothesis_id') and item.get('study_id') == study_id), None)
        if hypothesis is None:
            raise ProtocolError('design 引用的 hypothesis 不存在于该 Study')
        readiness = controller_routing_readiness(run_dir, state, design)
        return {'ok': True, 'study_id': study_id, 'design_id': design_id, 'state_version': state['state_version'], 'design': design, 'design_sha256': digest(design), 'hypothesis': hypothesis, 'hypothesis_version_matches': hypothesis.get('version') == design.get('hypothesis_version'), 'execution_readiness': readiness}

    def research_claim_for_agent(self, study_id, claim_id):
        """Disclose one checked Claim, never raw artifacts or arbitrary paths."""
        EntityKind = self.dependencies.EntityKind
        ProtocolError = self.dependencies.ProtocolError
        parse_id = self.dependencies.parse_id
        read_json = self.dependencies.read_json
        read_research_state = self.dependencies.read_research_state
        render_research_evidence = self.dependencies.render_research_evidence
        parse_id(study_id, EntityKind.STUDY)
        parse_id(claim_id, EntityKind.CLAIM)
        run_dir = self.research_run_for_study(study_id)
        state = read_research_state(run_dir)
        if not state.get('ok') or not state['experiment_graph']['integrity']['ok']:
            raise ProtocolError(f"research 状态或来源图不可读取: {state.get('reason')}")
        manifest = read_json(run_dir / 'research.json')
        evidence = render_research_evidence(manifest['project'], run_dir)['bundle']
        after = read_research_state(run_dir)
        if not after.get('ok') or not after['experiment_graph']['integrity']['ok'] or after['state_version'] != state['state_version'] or (evidence['study_state_version'] != state['state_version']):
            raise ProtocolError('证据读取期间 Study 状态变化或来源图失效，请重新提议读取')
        if evidence['study_id'] != study_id:
            raise ProtocolError('证据附录与请求 Study 不一致')
        binding = next((item for item in evidence['bindings'] if item['claim_id'] == claim_id), None)
        if binding is None:
            raise LookupError('Claim 不属于已登记 Study 或不存在')
        return {'ok': True, 'study_id': study_id, 'claim_id': claim_id, 'state_version': state['state_version'], 'binding': binding, 'verification_level': evidence['verification_level'], 'limitations': evidence['limitations']}

    def research_observation_for_agent(self, study_id, observation_id):
        """Recheck one active or concluded Observation without exposing local paths."""
        EntityKind = self.dependencies.EntityKind
        ProtocolError = self.dependencies.ProtocolError
        _agent_development_observation = self.dependencies._agent_development_observation
        parse_id = self.dependencies.parse_id
        parse_id(study_id, EntityKind.STUDY)
        parse_id(observation_id, EntityKind.OBSERVATION)
        (_, after) = self._verified_research_state_for_agent(study_id, development_observation_id=observation_id)
        observation = next((item for item in after['observations'] if item['entity_id'] == observation_id and item.get('study_id') == study_id), None)
        if observation is None:
            raise LookupError('Observation 不属于已登记 Study 或不存在')
        if not _agent_development_observation(observation.get('scope')):
            raise ProtocolError('Agent 仅允许读取开发评估 Observation；确认集结果须通过终结 Claim 查看')
        if not all((observation.get(key) for key in ('artifact_id', 'artifact_sha256', 'selector'))):
            raise ProtocolError('Observation 缺少可重验的评分制品绑定')
        run = next((item for item in after['runs'] if item['entity_id'] == observation['run_id']), None)
        if run is None:
            raise ProtocolError('Observation 来源 Run 不存在')
        fields = ('entity_id', 'status', 'run_id', 'design_id', 'hypothesis_id', 'hypothesis_version', 'scorer_id', 'value', 'unit', 'uncertainty', 'scope', 'artifact_sha256', 'selector', 'evaluator_service_hash', 'trust')
        public_observation = {key: observation.get(key) for key in fields}
        return {'ok': True, 'study_id': study_id, 'observation_id': observation_id, 'state_version': after['state_version'], 'observation': public_observation, 'run': {'run_id': run['entity_id'], 'status': run['status'], 'design_id': run['design_id'], 'revision_id': run.get('revision_id')}, 'verification_level': 'local_research_store_and_scoring_artifact', 'limitations': ['development_observation_not_final_claim', 'not_an_independent_reproduction']}

    def revision_payload(self, revision_id):
        """Return one verified immutable revision and its identity-bound diff."""
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        RevisionDiffArchive = self.dependencies.RevisionDiffArchive
        RevisionStore = self.dependencies.RevisionStore
        file_hash = self.dependencies.file_hash
        if not isinstance(revision_id, str) or len(revision_id) != 28 or (not revision_id.startswith('REV-')) or any((ch not in '0123456789abcdef' for ch in revision_id[4:])):
            raise ProtocolError('revision_id 格式不合法')
        matches = []
        for run_dir in self.research_runs:
            manifest_path = run_dir / 'revisions' / revision_id / 'revision.json'
            if not manifest_path.is_file():
                continue
            store = RevisionStore(run_dir / 'revisions')
            manifest = store.verify(revision_id)
            matches.append((str(run_dir), manifest, file_hash(manifest_path)))
        if not matches:
            raise FileNotFoundError('CodeRevision 不存在')
        manifest_hashes = {item[2] for item in matches}
        if len(manifest_hashes) != 1:
            raise ProtocolError('相同 revision_id 在多个研究目录中内容冲突')
        manifest = matches[0][1]
        identity = manifest['identity']
        diff_record = RevisionDiffArchive(Path(matches[0][0]) / 'revisions').resolve(revision_id)
        return {'revision_id': revision_id, 'schema_version': manifest['schema_version'], 'status': manifest['status'], 'manifest_sha256': matches[0][2], 'research_runs': [item[0] for item in matches], 'hypothesis_id': identity.get('hypothesis_id'), 'design_id': identity.get('design_id'), 'parent_revision_id': identity.get('parent_revision_id'), 'actor': identity.get('actor'), 'source_control': identity.get('source_control', {'kind': 'legacy_unbound'}), 'files': [{'path': name, 'sha256': sha} for (name, sha) in sorted(manifest['files'].items())], 'diff': diff_record['diff'], 'diff_sha256': diff_record['diff_sha256'], 'diff_source': diff_record['source'], 'diff_attestation_id': diff_record['attestation_id']}

    def migrate_revision_diff(self, revision_id):
        ProtocolError = self.dependencies.ProtocolError
        RevisionDiffArchive = self.dependencies.RevisionDiffArchive
        if not self.trusted_local:
            raise ProtocolError('迁移旧 revision Diff 需要以 --trusted-local 启动')
        matches = [run_dir for run_dir in self.research_runs if (run_dir / 'revisions' / revision_id / 'revision.json').is_file()]
        if not matches:
            raise FileNotFoundError('CodeRevision 不存在')
        if len(matches) != 1:
            raise ProtocolError('revision 位于多个研究目录；请用 CLI 指定 run_dir 迁移')
        RevisionDiffArchive(matches[0] / 'revisions').migrate(revision_id)
        return self.revision_payload(revision_id)

    def _recover_design_execution_jobs(self):
        ProtocolError = self.dependencies.ProtocolError
        ResearchStore = self.dependencies.ResearchStore
        digest = self.dependencies.digest
        read_research_state = self.dependencies.read_research_state
        queued = []
        for run_dir in self.research_runs:
            if not (run_dir / 'design-execution-jobs.sqlite').is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                integrity = jobs.verify()
                if not integrity['ok']:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                running = [row for row in jobs.list() if row['status'] == 'running']
                if running:
                    try:
                        projection = read_research_state(run_dir)
                    except Exception:
                        projection = {'ok': False}
                    verified = False
                    if projection.get('ok') and projection.get('experiment_graph', {}).get('integrity', {}).get('ok'):
                        research_store = ResearchStore(run_dir / 'research.sqlite')
                        try:
                            verified = research_store.verify()['ok']
                        finally:
                            research_store.close()
                    if verified:
                        designs = {row['design_id']: row for row in projection['designs']}
                        runs = {row['run_id']: row for row in projection['runs']}
                        for job in running:
                            design = designs.get(job['design_id'])
                            if design is None or digest(design) != job['design_sha256']:
                                continue
                            matches = [obs for obs in projection['observations'] if obs.get('scope') == 'dev' and runs.get(obs.get('run_id'), {}).get('design_id') == job['design_id'] and (runs[obs['run_id']]['status'] == 'succeeded')]
                            if len(matches) == 1:
                                jobs.finish(job['job_id'], observation_id=matches[0]['observation_id'])
                jobs.interrupt_running()
                queued.extend(((run_dir, row['job_id']) for row in jobs.list() if row['status'] == 'queued'))
            finally:
                jobs.close()
        if self.trusted_local:
            for (run_dir, job_id) in queued:
                self._start_design_job(run_dir, job_id)

    def _run_design_job(self, run_dir, job_id):
        with self.research_execution_lock:
            return self._run_design_job_locked(run_dir, job_id)

    def _run_design_job_locked(self, run_dir, job_id):
        ProtocolError = self.dependencies.ProtocolError
        ResearchStore = self.dependencies.ResearchStore
        controller_for_run = self.dependencies.controller_for_run
        read_research_state = self.dependencies.read_research_state
        jobs = self._design_job_store(run_dir)
        controller = None
        try:
            try:
                job = jobs.claim(job_id)
            except ProtocolError:
                return
            try:
                from ..research.controller import ResearchController
                controller = controller_for_run(run_dir)
                current = self.research_design_for_agent(controller.manifest['study_id'], job['design_id'])
                if current['design_sha256'] != job['design_sha256']:
                    raise ProtocolError('冻结设计快照已变化，拒绝执行')
                observation = controller.execute_registered_design(job['design_id'], should_cancel=lambda : bool(jobs.get(job_id)['cancel_requested']))
                jobs.finish(job_id, observation_id=observation['observation_id'])
            except Exception as error:
                current = jobs.get(job_id)
                observation_id = None
                try:
                    projection = read_research_state(run_dir)
                    if projection.get('ok') and projection.get('experiment_graph', {}).get('integrity', {}).get('ok'):
                        store = ResearchStore(run_dir / 'research.sqlite')
                        try:
                            verified = store.verify()['ok']
                        finally:
                            store.close()
                        if verified:
                            runs = {row['run_id']: row for row in projection['runs']}
                            matches = [obs for obs in projection['observations'] if obs.get('scope') == 'dev' and runs.get(obs.get('run_id'), {}).get('design_id') == current['design_id'] and (runs[obs['run_id']]['status'] == 'succeeded')]
                            if len(matches) == 1:
                                observation_id = matches[0]['observation_id']
                except Exception:
                    pass
                if observation_id is not None:
                    jobs.finish(job_id, observation_id=observation_id)
                elif current['cancel_requested']:
                    jobs.finish_cancelled(job_id)
                else:
                    jobs.finish(job_id, error=f'{type(error).__name__}: {error}')
        finally:
            if controller is not None:
                controller.close()
            jobs.close()
            self._wake_agent_research_job(run_dir, job_id)

    def _start_design_job(self, run_dir, job_id):
        Path = self.dependencies.Path
        self._start_job(lambda : self._run_design_job(Path(run_dir), job_id))

    def enqueue_design_execution(self, study_id, design_id, expected_version, design_sha256):
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('设计执行需要以 --trusted-local 启动工作台')
        if type(expected_version) is not int or expected_version < 1:
            raise ProtocolError('study 版本不合法')
        current = self.research_design_for_agent(study_id, design_id)
        if current['design_sha256'] != design_sha256:
            raise ProtocolError('冻结设计摘要不一致，请重新审查设计')
        run_dir = self.research_run_for_study(study_id)
        jobs = self._design_job_store(run_dir)
        try:
            integrity = jobs.verify()
            if not integrity['ok']:
                raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
            prior = jobs.for_design(design_id)
            if prior is not None:
                if prior['design_sha256'] != design_sha256:
                    raise ProtocolError('同一设计已有不同冻结快照的执行 Job')
                return {'research_run': str(run_dir), 'job': prior}
        finally:
            jobs.close()
        if current['state_version'] != expected_version:
            raise ProtocolError('study 版本已变化，请重新审查设计')
        if current['design']['status'] != 'frozen' or not current['hypothesis_version_matches'] or (not current['execution_readiness']['registered_design_route_ready']):
            raise ProtocolError('设计不满足已注册候选执行入口: ' + ', '.join(current['execution_readiness']['registered_design_reasons']))
        jobs = self._design_job_store(run_dir)
        try:
            integrity = jobs.verify()
            if not integrity['ok']:
                raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
            job = jobs.enqueue(design_id, design_sha256)
        finally:
            jobs.close()
        if job['status'] == 'queued':
            self._start_design_job(run_dir, job['job_id'])
        return {'research_run': str(run_dir), 'job': job}

    def design_job_payload(self, job_id):
        ProtocolError = self.dependencies.ProtocolError
        for run_dir in self.research_runs:
            if not (run_dir / 'design-execution-jobs.sqlite').is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                try:
                    job = jobs.get(job_id)
                except ProtocolError:
                    continue
                integrity = jobs.verify()
                if not integrity['ok']:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                return {'research_run': str(run_dir), 'job': job}
            finally:
                jobs.close()
        raise FileNotFoundError('设计执行 Job 不存在')

    def cancel_design_job(self, job_id, reason):
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('设计执行取消需要以 --trusted-local 启动工作台')
        for run_dir in self.research_runs:
            if not (run_dir / 'design-execution-jobs.sqlite').is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                try:
                    jobs.get(job_id)
                except ProtocolError:
                    continue
                integrity = jobs.verify()
                if not integrity['ok']:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                return {'research_run': str(run_dir), 'job': jobs.cancel(job_id, reason)}
            finally:
                jobs.close()
        raise FileNotFoundError('设计执行 Job 不存在')

    def retry_design_job(self, job_id, expected_attempt, design_sha256):
        ProtocolError = self.dependencies.ProtocolError
        read_research_state = self.dependencies.read_research_state
        if not self.trusted_local:
            raise ProtocolError('设计执行重试需要以 --trusted-local 启动工作台')
        if type(expected_attempt) is not int or expected_attempt < 1:
            raise ProtocolError('设计执行重试次数不合法')
        for run_dir in self.research_runs:
            if not (run_dir / 'design-execution-jobs.sqlite').is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                try:
                    job = jobs.get(job_id)
                except ProtocolError:
                    continue
                integrity = jobs.verify()
                if not integrity['ok']:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                if job['design_sha256'] != design_sha256:
                    raise ProtocolError('冻结设计摘要不一致，请重新审查设计')
                if job['attempt'] == expected_attempt + 1:
                    return {'research_run': str(run_dir), 'job': job}
                projection = read_research_state(run_dir)
                if not projection.get('ok'):
                    raise ProtocolError('研究状态不可验证，拒绝重试设计执行')
                study_id = projection['manifest']['study_id']
                current = self.research_design_for_agent(study_id, job['design_id'])
                if current['design_sha256'] != design_sha256 or not current['execution_readiness']['registered_design_route_ready']:
                    raise ProtocolError('设计或执行条件已变化，请重新审查')
                (retried, applied) = jobs.retry_cancelled(job_id, expected_attempt)
            finally:
                jobs.close()
            if applied and retried['status'] == 'queued':
                self._start_design_job(run_dir, job_id)
            return {'research_run': str(run_dir), 'job': retried}
        raise FileNotFoundError('设计执行 Job 不存在')

    def design_jobs_payload(self):
        ProtocolError = self.dependencies.ProtocolError
        records = []
        for run_dir in self.research_runs:
            if not (run_dir / 'design-execution-jobs.sqlite').is_file():
                continue
            jobs = self._design_job_store(run_dir)
            try:
                integrity = jobs.verify()
                if not integrity['ok']:
                    raise ProtocolError(f"设计执行队列完整性失败: {integrity['reason']}")
                records.extend(({'research_run': str(run_dir), **job} for job in jobs.list()))
            finally:
                jobs.close()
        return {'jobs': records}

    def _recover_revision_worktrees(self):
        if not self.trusted_local:
            return
        for run_dir in self.research_runs:
            if not (run_dir / 'revision-worktrees.sqlite').is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                manager.recover_provisioning()
            finally:
                manager.close()

    def _recover_revision_execution_jobs(self):
        """Expose interrupted work honestly and resume durable queued work."""
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            return
        queued = []
        for run_dir in self.research_runs:
            path = run_dir / 'revision-execution-jobs.sqlite'
            if not path.is_file():
                continue
            jobs = self._revision_job_store(run_dir)
            try:
                integrity = jobs.verify()
                if not integrity['ok']:
                    raise ProtocolError(f"Revision 执行队列完整性失败: {integrity['reason']}")
                running = [row for row in jobs.list() if row['status'] == 'running']
                jobs.interrupt_running()
                if running and (run_dir / 'revision-worktrees.sqlite').is_file():
                    manager = self._revision_worktree_manager(run_dir)
                    try:
                        for job in running:
                            if job['worktree_id'] == f"initial:{job['revision_id']}":
                                continue
                            try:
                                manager.finish_execution(job['worktree_id'], error='Interrupted: workstation restarted during execution')
                            except ProtocolError:
                                pass
                    finally:
                        manager.close()
                queued.extend(((run_dir, row['job_id']) for row in jobs.list() if row['status'] == 'queued'))
            finally:
                jobs.close()
        for (run_dir, job_id) in queued:
            self._start_revision_job(run_dir, job_id)

    def revision_worktrees_payload(self):
        records = []
        registries = []
        jobs_payload = []
        job_registries = []
        for run_dir in self.research_runs:
            registry = run_dir / 'revision-worktrees.sqlite'
            if registry.is_file():
                manager = self._revision_worktree_manager(run_dir)
                try:
                    records.extend(({'research_run': str(run_dir), **self._public_worktree(row)} for row in manager.list()))
                    registries.append({'research_run': str(run_dir), 'integrity': manager.verify()})
                finally:
                    manager.close()
            job_path = run_dir / 'revision-execution-jobs.sqlite'
            if job_path.is_file():
                jobs = self._revision_job_store(run_dir)
                try:
                    jobs_payload.extend(({'research_run': str(run_dir), **row} for row in jobs.list()))
                    job_registries.append({'research_run': str(run_dir), 'integrity': jobs.verify()})
                finally:
                    jobs.close()
        records.sort(key=lambda row: (row['created'], row['worktree_id']))
        jobs_payload.sort(key=lambda row: (row['created'], row['job_id']))
        return {'worktrees': records, 'jobs': jobs_payload, 'registries': registries, 'job_registries': job_registries, 'trusted_local': self.trusted_local}

    def revision_worktree_events_payload(self, worktree_id):
        ProtocolError = self.dependencies.ProtocolError
        for run_dir in self.research_runs:
            if not (run_dir / 'revision-worktrees.sqlite').is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                try:
                    record = manager.get(worktree_id)
                except ProtocolError:
                    continue
                return {'research_run': str(run_dir), 'worktree': self._public_worktree(record), 'events': manager.history(worktree_id), 'integrity': manager.verify()}
            finally:
                manager.close()
        raise FileNotFoundError('worktree 不存在')

    def create_revision_worktree(self, revision_id, idempotency_key):
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('创建 Git worktree 需要以 --trusted-local 启动')
        detail = self.revision_payload(revision_id)
        run_dir = Path(detail['research_runs'][0])
        manager = self._revision_worktree_manager(run_dir)
        try:
            return {'research_run': str(run_dir), **self._public_worktree(manager.create(revision_id, idempotency_key))}
        finally:
            manager.close()

    def remove_revision_worktree(self, worktree_id):
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('移除 Git worktree 需要以 --trusted-local 启动')
        for run_dir in self.research_runs:
            if not (run_dir / 'revision-worktrees.sqlite').is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                try:
                    manager.get(worktree_id)
                except ProtocolError:
                    continue
                return {'research_run': str(run_dir), **self._public_worktree(manager.remove(worktree_id))}
            finally:
                manager.close()
        raise FileNotFoundError('worktree 不存在')

    def _worktree_operation(self, worktree_id, operation):
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('worktree 操作需要以 --trusted-local 启动')
        for run_dir in self.research_runs:
            if not (run_dir / 'revision-worktrees.sqlite').is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                try:
                    manager.get(worktree_id)
                except ProtocolError:
                    continue
                record = operation(manager)
                return {'research_run': str(run_dir), **self._public_worktree(record)}
            finally:
                manager.close()
        raise FileNotFoundError('worktree 不存在')

    def propose_worktree_revision(self, worktree_id):
        return self._worktree_operation(worktree_id, lambda manager: manager.propose(worktree_id, 'local_user'))

    def submit_worktree_revision(self, worktree_id, proposal_sha256):
        return self._worktree_operation(worktree_id, lambda manager: manager.submit(worktree_id, proposal_sha256, 'local_user'))

    def _run_revision_job(self, run_dir, job_id):
        with self.research_execution_lock:
            return self._run_revision_job_locked(run_dir, job_id)

    def _run_revision_job_locked(self, run_dir, job_id):
        ProtocolError = self.dependencies.ProtocolError
        controller_for_run = self.dependencies.controller_for_run
        digest = self.dependencies.digest
        threading = self.dependencies.threading
        jobs = self._revision_job_store(run_dir)
        manager = None
        controller = None
        try:
            try:
                job = jobs.claim(job_id)
            except ProtocolError:
                return
            if job['cancel_requested']:
                return
            initial = job['worktree_id'] in {f"initial:{job['revision_id']}", f"approved:{job['revision_id']}"}
            if initial:
                revision_id = job['revision_id']
            else:
                manager = self._revision_worktree_manager(run_dir)
                record = manager.begin_execution(job['worktree_id'])
                revision_id = record['successor_revision_id']
            jobs.progress(job_id, 'executing_revision', 35)
            try:
                from ..research.controller import ResearchController
                controller = controller_for_run(run_dir)
                with self.revision_job_events_lock:
                    cancel_event = self._revision_job_cancel_events.setdefault(job_id, threading.Event())
                result = controller.execute_revision(revision_id, should_cancel=cancel_event.is_set)
                jobs.progress(job_id, 'recording_result', 90)
                run_id = (result.get('observation') or {}).get('run_id')
                if run_id is None:
                    run_id = 'RUN-' + digest({'revision_id': revision_id, 'split': 'dev'})[:24]
                if manager is not None:
                    manager.finish_execution(job['worktree_id'], run_id=run_id)
                jobs.finish(job_id, run_id=run_id)
            except Exception as error:
                message = f'{type(error).__name__}: {error}'
                current = jobs.get(job_id)
                if current['cancel_requested']:
                    if manager is not None:
                        manager.finish_execution(job['worktree_id'], error=message, cancelled=True)
                    jobs.finish_cancelled(job_id, current['error'] or 'user requested cancellation')
                else:
                    if manager is not None:
                        manager.finish_execution(job['worktree_id'], error=message)
                    jobs.finish(job_id, error=message)
        finally:
            if controller is not None:
                controller.close()
            if manager is not None:
                manager.close()
            jobs.close()
            with self.revision_job_events_lock:
                self._revision_job_cancel_events.pop(job_id, None)
            self._wake_agent_research_job(run_dir, job_id)

    def _start_revision_job(self, run_dir, job_id):
        Path = self.dependencies.Path
        self._start_job(lambda : self._run_revision_job(Path(run_dir), job_id))

    def execute_worktree_revision(self, worktree_id):
        ProtocolError = self.dependencies.ProtocolError
        if not self.trusted_local:
            raise ProtocolError('执行后继 revision 需要以 --trusted-local 启动')
        for run_dir in self.research_runs:
            if not (run_dir / 'revision-worktrees.sqlite').is_file():
                continue
            manager = self._revision_worktree_manager(run_dir)
            try:
                try:
                    record = manager.get(worktree_id)
                except ProtocolError:
                    continue
                jobs = self._revision_job_store(run_dir)
                try:
                    job = jobs.for_worktree(worktree_id)
                    if job is None:
                        if record['status'] != 'submitted':
                            raise ProtocolError('只有已提交的 worktree 可以创建执行 Job')
                        job = jobs.enqueue(worktree_id, record['successor_revision_id'])
                finally:
                    jobs.close()
                if job['status'] == 'queued':
                    self._start_revision_job(run_dir, job['job_id'])
                return {'research_run': str(run_dir), 'job': job, 'worktree': self._public_worktree(record)}
            finally:
                manager.close()
        raise FileNotFoundError('worktree 不存在')

    def execute_initial_revision(self, revision_id, manifest_sha256):
        return self._execute_approved_revision(revision_id, manifest_sha256, allow_repair=False)

    def execute_agent_revision(self, revision_id, manifest_sha256):
        return self._execute_approved_revision(revision_id, manifest_sha256, allow_repair=True)

    def _execute_approved_revision(self, revision_id, manifest_sha256, *, allow_repair):
        Experiment = self.dependencies.Experiment
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        RevisionStore = self.dependencies.RevisionStore
        read_json = self.dependencies.read_json
        read_research_state = self.dependencies.read_research_state
        if not self.trusted_local:
            raise ProtocolError('执行初始 revision 需要以 --trusted-local 启动')
        detail = self.revision_payload(revision_id)
        if detail['parent_revision_id'] is not None and (not allow_repair):
            raise ProtocolError('此入口只执行没有父版本的初始 revision')
        if detail['manifest_sha256'] != manifest_sha256:
            raise ProtocolError('Revision manifest 已变化，请重新审查')
        if len(detail['research_runs']) != 1:
            raise ProtocolError('Revision 研究目录不唯一')
        run_dir = Path(detail['research_runs'][0])
        manifest = read_json(run_dir / 'research.json')
        if Path(manifest['project']).resolve() != self.workspace.root.resolve():
            raise ProtocolError('Revision 不属于当前登记工作区')
        state = read_research_state(run_dir)
        if not state.get('ok') or not state.get('experiment_graph', {}).get('integrity', {}).get('ok'):
            raise ProtocolError('研究状态或来源图未通过完整性检查')
        if detail['parent_revision_id'] is not None:
            from ..research.revision_drafts import failed_repair_parent
            revision = RevisionStore(run_dir / 'revisions').verify(revision_id)
            failed_repair_parent(run_dir, state, detail['parent_revision_id'], revision['identity']['hypothesis_id'], revision['identity']['design_id'])
        jobs = self._revision_job_store(run_dir)
        try:
            integrity = jobs.verify()
            if not integrity['ok']:
                raise ProtocolError(f"Revision 执行队列完整性失败: {integrity['reason']}")
            policy_path = self.workspace.root / 'evaluation-policy.json'
            if self.evaluation_budget_path is not None and policy_path.is_file():
                exp = Experiment(self.workspace.root, readonly=True)
                try:
                    core = exp.verify_inputs()
                finally:
                    exp.close()
                if 'evaluation-policy.json' not in core['spec'].get('provenance_files', []):
                    raise ProtocolError('评测开发限制必须是冻结的预注册输入')
                policy = read_json(policy_path)
                limit = policy.get('candidate_development_limit')
                if type(limit) is not int or not 1 <= limit <= 6:
                    raise ProtocolError('评测开发执行限制不合法')
                candidate_revisions = set()
                for previous_job in jobs.list():
                    previous = RevisionStore(run_dir / 'revisions').verify(previous_job['revision_id'])
                    if previous['identity']['hypothesis_id'] != manifest['control_hypothesis_id']:
                        candidate_revisions.add(previous_job['revision_id'])
                if revision_id not in candidate_revisions and len(candidate_revisions) >= limit:
                    raise ProtocolError('预注册候选开发次数已耗尽；失败和取消均计入，不自动追加')
            key = 'approved' if detail['parent_revision_id'] is not None else 'initial'
            job = jobs.enqueue(f'{key}:{revision_id}', revision_id)
        finally:
            jobs.close()
        if job['status'] == 'queued':
            self._start_revision_job(run_dir, job['job_id'])
        return {'research_run': str(run_dir), 'job': job}

    def revision_job_payload(self, job_id):
        ProtocolError = self.dependencies.ProtocolError
        RevisionExecutionJobStore = self.dependencies.RevisionExecutionJobStore
        read_json = self.dependencies.read_json
        task_memory = self.dependencies.task_memory
        for run_dir in self._visible_research_runs(self.workspace.root):
            path = run_dir / 'revision-execution-jobs.sqlite'
            if not path.is_file():
                continue
            jobs = RevisionExecutionJobStore.readonly(path)
            try:
                try:
                    job = jobs.get(job_id)
                except ProtocolError:
                    continue
                result = {'research_run': str(run_dir), 'job': job, 'events': jobs.history(job_id), 'integrity': jobs.verify()}
            finally:
                jobs.close()
            manifest = read_json(run_dir / 'research.json')
            if result['integrity']['ok'] and job['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'} and (manifest.get('scorer_id') == 'oscillator-trajectory-v1') and (run_dir / 'oscillator-worker-binding.json').is_file():
                from ..research.development_evidence import build_development_evidence
                try:
                    result['development_diagnostics'] = build_development_evidence(manifest['project'], run_dir, job_id)
                except (ProtocolError, OSError, ValueError) as error:
                    result['development_diagnostics_error'] = task_memory.redact({'reason': str(error), 'verified': False, 'scope': 'dev'})
            return result
        raise FileNotFoundError('revision execution job 不存在')

    def cancel_revision_job(self, job_id, reason):
        ProtocolError = self.dependencies.ProtocolError
        threading = self.dependencies.threading
        if not self.trusted_local:
            raise ProtocolError('取消 revision job 需要以 --trusted-local 启动')
        for run_dir in self.research_runs:
            path = run_dir / 'revision-execution-jobs.sqlite'
            if not path.is_file():
                continue
            jobs = self._revision_job_store(run_dir)
            try:
                try:
                    job = jobs.cancel(job_id, reason)
                except ProtocolError as error:
                    if '不存在' in str(error):
                        continue
                    raise
                if job['status'] == 'running':
                    with self.revision_job_events_lock:
                        self._revision_job_cancel_events.setdefault(job_id, threading.Event()).set()
                return {'research_run': str(run_dir), 'job': job}
            finally:
                jobs.close()
        raise FileNotFoundError('revision execution job 不存在')

    def campaign_payload(self):
        """构造 GET /api/campaign 的响应（9 阶段统一切磋进度）。"""
        Experiment = self.dependencies.Experiment
        build_campaign_payload = self.dependencies.build_campaign_payload
        campaign_pending_payload = self.dependencies.campaign_pending_payload
        load_all_arbor = self.dependencies.load_all_arbor
        load_all_literature = self.dependencies.load_all_literature
        if self.mode != 'experiment':
            return {'objective': None, 'phase': 'reproduction', 'phase_label': '论文复现', 'stages': [], 'campaign': None}
        exp = Experiment(self.project)
        try:
            state = exp.state()
            results = exp.results()
        finally:
            exp.close()
        papers = load_all_literature(self.project)
        arbor = load_all_arbor(self.project)
        with self.ws_lock:
            ws = self.workspace
            if ws.is_empty():
                ws.seed_if_empty(ws.project_spec(), ws.current_phase())
            data = ws.read()
            deliverables = ws.list_deliverables(data)
        history = []
        if self.campaign_dir:
            read_json = self.dependencies.read_json
            from pathlib import Path
            manifest_path = Path(self.campaign_dir) / 'campaign.json'
            if manifest_path.is_file():
                try:
                    history = read_json(manifest_path).get('history') or []
                except Exception:
                    history = []
        payload = build_campaign_payload(state, results or [], papers, arbor, deliverables, history)
        payload['campaign'] = campaign_pending_payload(self.campaign_dir, self.campaign_job) if self.campaign_dir else None
        return payload

    def launch(self, action):
        Experiment = self.dependencies.Experiment
        ProtocolError = self.dependencies.ProtocolError
        ReproductionTask = self.dependencies.ReproductionTask
        allowed = {'search', 'freeze', 'confirm', 'replay', 'report'} if self.mode == 'experiment' else {'run', 'verify'}
        if action not in allowed:
            raise ProtocolError('未知操作')
        if action in {'search', 'confirm', 'run', 'verify'} and (not self.trusted_local):
            raise ProtocolError('请用 --trusted-local 启动以执行已信任代码')
        if not self.job_lock.acquire(blocking=False):
            raise ProtocolError('已有操作正在执行')
        self.job = {'status': 'running', 'action': action}

        def work():
            try:
                if self.mode == 'experiment':
                    spec = self.dependencies.read_json(self.project / 'experiment.json')
                    protocol = spec.get('metadata', {}).get('planning_protocol')
                    if protocol not in {None, 'research-runtime/v1'}:
                        raise ProtocolError('未支持的研究规划协议，不能退回旧执行通道')
                    if protocol and action in {'search', 'freeze', 'confirm'}:
                        from ..research.controller import ResearchController
                        run_dir = self.project / '.popper' / 'research-runtime'
                        if action == 'search' and not (run_dir / 'research.json').is_file():
                            ResearchController.initialize(self.project, run_dir)
                        target = ResearchController(run_dir)
                    else:
                        target = Experiment(self.project)
                    try:
                        if protocol and action in {'search', 'freeze', 'confirm'}:
                            if action == 'freeze':
                                result = {'selection_receipt': target.prepare_registered_selection(), **target.status()}
                            else:
                                method = target.run if action == 'search' else target.confirm
                                result = method(trusted_local=True)
                        else:
                            result = getattr(target, action)(True) if action in {'search', 'confirm'} else getattr(target, action)()
                    finally:
                        target.close()
                else:
                    target = ReproductionTask(self.project)
                    result = getattr(target, action)(trusted_local=True)
                planner = (result.get('planning_runtime') or {}).get('planner', {}) if isinstance(result, dict) else {}
                if planner.get('status') in {'failed', 'exhausted', 'cancelled', 'recovery_blocked'}:
                    self.job = {'status': 'cancelled' if planner['status'] == 'cancelled' else 'failed',
                                'action': action, 'result': result,
                                'error': '研究规划未完成: ' + str(planner.get('reason'))}
                else:
                    self.job = {'status': 'completed', 'action': action, 'result': result}
            except Exception as error:
                self.job = {'status': 'failed', 'action': action, 'error': str(error)}
            finally:
                self.job_lock.release()
        self._start_job(work)

    def _start_job(self, target):
        """启动一个可被 `wait_for_jobs()` 等到的后台作业线程。"""
        threading = self.dependencies.threading
        thread = threading.Thread(target=target, daemon=True)
        with self.job_threads_lock:
            self._job_threads.append(thread)
            thread.start()
        return thread

    def wait_for_jobs(self, timeout=120):
        """等所有已登记的后台作业线程结束，返回仍存活的个数。

        超时不抛异常：调用方（测试与将来的优雅停机）需要知道「还没收尾」这个事实
        本身，而不是在一个自己造的错误里丢掉现场。
        """
        time = self.dependencies.time
        deadline = time.monotonic() + timeout
        while True:
            with self.job_threads_lock:
                pending = list(self._job_threads)
                self._job_threads.clear()
            for thread in pending:
                thread.join(max(0.0, deadline - time.monotonic()))
            with self.job_threads_lock:
                self._job_threads.extend(thread for thread in pending if thread.is_alive())
                self._job_threads[:] = list(dict.fromkeys(self._job_threads))
                alive = [thread for thread in self._job_threads if thread.is_alive()]
                self._job_threads[:] = alive
                if not alive or time.monotonic() >= deadline:
                    return len(alive)

    def approve_campaign(self, decision, reason='', materialize=None):
        """工作台交互审批：记录决定，批准时在后台线程真正恢复执行 campaign。

        - 批准：写入 `<run_dir>/approvals.json` 之后，用 Orchestrator 重跑该 run_dir
          并注入审批标记（proposal_approved）与 trusted_local 边界，后台线程更新
          campaign_job。恢复所需 LLM 配置缺省不入 —— 无 --base-url/--model 时 LLM
          依赖步骤如实跳过，已产出的候选/检索/提案证据文件保持幂等保留。
        - 驳回：只落盘决定，不执行任何副作用；campaign 保持挂起（可用 CLI 另行处理）。
        """
        Orchestrator = self.dependencies.Orchestrator
        ProtocolError = self.dependencies.ProtocolError
        datetime = self.dependencies.datetime
        read_json = self.dependencies.read_json
        write_json = self.dependencies.write_json
        if self.campaign_dir is None:
            raise ProtocolError('未配置 --campaign 运行目录，无法审批')
        if decision not in {'approved', 'rejected'}:
            raise ProtocolError('审批决定必须为 approved 或 rejected')
        if not self.campaign_lock.acquire(blocking=False):
            raise ProtocolError('已有审批/恢复操作正在执行')
        manifest_path = self.campaign_dir / 'campaign.json'
        if not manifest_path.is_file():
            self.campaign_lock.release()
            raise ProtocolError('该 campaign 缺少 manifest（campaign.json），无法审批')
        manifest = read_json(manifest_path)
        pending = manifest.get('waiting_approval')
        if not pending:
            self.campaign_lock.release()
            raise ProtocolError('该 campaign 当前不在挂起审批状态')
        approvals_path = self.campaign_dir / 'approvals.json'
        records = read_json(approvals_path) if approvals_path.is_file() else {'records': []}
        if not isinstance(records, dict) or 'records' not in records:
            records = {'records': []}
        records['records'].append({'step': pending.get('step'), 'required': pending.get('required'), 'decision': decision, 'reason': reason, 'at': datetime.datetime.now().isoformat()})
        write_json(approvals_path, records)
        self.campaign_job = {'status': 'idle', 'decision': decision}
        if decision == 'rejected':
            self.campaign_lock.release()
            return {'decision': 'rejected', 'run_dir': str(self.campaign_dir)}

        def work():
            try:
                from ..campaign_nodes import BUILTIN_NODES
                nodes = self._campaign_nodes or BUILTIN_NODES
                orch = Orchestrator(self.campaign_dir)
                config = {'project': str(self.project), 'mode': 'trusted_local', 'objective': manifest.get('objective') or '', pending.get('required'): True}
                if materialize:
                    config['materialize'] = materialize
                result = orch.run(nodes=nodes, config=config)
                self.campaign_job = {'status': 'completed', 'decision': 'approved', 'result': result}
            except Exception as error:
                self.campaign_job = {'status': 'failed', 'decision': 'approved', 'error': str(error)}
            finally:
                self.campaign_lock.release()
        self._start_job(work)
        return {'decision': 'approved', 'accepted': True, 'run_dir': str(self.campaign_dir)}
