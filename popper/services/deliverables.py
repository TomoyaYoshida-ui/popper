"""Deliverables responsibility service."""
from __future__ import annotations
from .base import HostService

class DeliverablesService(HostService):

    def research_evidence_preview(self, study_id):
        read_json = self.dependencies.read_json
        render_research_evidence = self.dependencies.render_research_evidence
        run_dir = self.research_run_for_study(study_id)
        manifest = read_json(run_dir / 'research.json')
        return render_research_evidence(manifest['project'], run_dir)

    def create_bound_draft_from_workbench(self, study_id, spec):
        ProtocolError = self.dependencies.ProtocolError
        create_workbench_bound_draft = self.dependencies.create_workbench_bound_draft
        read_json = self.dependencies.read_json
        if not self.trusted_local:
            raise ProtocolError('证据绑定草稿创建需要以 --trusted-local 启动工作台')
        run_dir = self.research_run_for_study(study_id)
        manifest = read_json(run_dir / 'research.json')
        with self.bound_draft_lock:
            return create_workbench_bound_draft(manifest['project'], run_dir, spec)

    def agent_bound_draft_preview(self, study_id, expected_state_version, title, author_note):
        preview_agent_bound_draft = self.dependencies.preview_agent_bound_draft
        read_json = self.dependencies.read_json
        run_dir = self.research_run_for_study(study_id)
        manifest = read_json(run_dir / 'research.json')
        return preview_agent_bound_draft(manifest['project'], run_dir, title, author_note, expected_state_version)

    def agent_create_bound_draft(self, study_id, expected_state_version, title, author_note, approval_sha256):
        ProtocolError = self.dependencies.ProtocolError
        create_workbench_bound_draft = self.dependencies.create_workbench_bound_draft
        read_json = self.dependencies.read_json
        if not self.trusted_local:
            raise ProtocolError('Agent 稿件生成需要可信本机模式')
        with self.research_execution_lock, self.bound_draft_lock:
            preview = self.agent_bound_draft_preview(study_id, expected_state_version, title, author_note)
            if preview['approval_sha256'] != approval_sha256:
                raise ProtocolError('证据或稿件方案已变化，请重新预览并审批')
            run_dir = self.research_run_for_study(study_id)
            manifest = read_json(run_dir / 'research.json')
            result = create_workbench_bound_draft(manifest['project'], run_dir, preview['spec'])
            return {key: result[key] for key in ('status', 'replayed', 'draft_id', 'study_id', 'claim_count', 'manuscript_path', 'publication_attested')}

    def bound_drafts_payload(self, study_id):
        list_workbench_bound_drafts = self.dependencies.list_workbench_bound_drafts
        read_json = self.dependencies.read_json
        run_dir = self.research_run_for_study(study_id)
        project = read_json(run_dir / 'research.json')['project']
        return {'drafts': list_workbench_bound_drafts(project, run_dir, study_id)}

    def bound_draft_detail(self, study_id, draft_id):
        Experiment = self.dependencies.Experiment
        ManuscriptFlow = self.dependencies.ManuscriptFlow
        read_json = self.dependencies.read_json
        read_workbench_bound_draft = self.dependencies.read_workbench_bound_draft
        run_dir = self.research_run_for_study(study_id)
        project = read_json(run_dir / 'research.json')['project']
        result = read_workbench_bound_draft(project, run_dir, draft_id)
        experiment = Experiment(project)
        try:
            flow = ManuscriptFlow(experiment)
            flow._verify_projection(result['manuscript_path'])
            result['flow_state'] = flow.current(result['manuscript_path'])
        finally:
            experiment.close()
        return result

    def review_bound_draft_from_workbench(self, study_id, draft_id, reason):
        Experiment = self.dependencies.Experiment
        ManuscriptFlow = self.dependencies.ManuscriptFlow
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        bound_draft_paths = self.dependencies.bound_draft_paths
        read_json = self.dependencies.read_json
        if not self.trusted_local:
            raise ProtocolError('人工审校登记需要以 --trusted-local 启动工作台')
        run_dir = self.research_run_for_study(study_id)
        project = Path(read_json(run_dir / 'research.json')['project']).resolve()
        with self.bound_draft_lock:
            (_, evidence, draft, spec_path) = bound_draft_paths(project, draft_id)
            experiment = Experiment(project)
            try:
                flow = ManuscriptFlow(experiment)
                return flow.review_bound(draft / 'manuscript.md', run_dir, evidence, spec_path, reason)
            finally:
                experiment.close()
