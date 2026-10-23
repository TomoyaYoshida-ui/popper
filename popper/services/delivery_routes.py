"""Delivery Routes responsibility service."""
from __future__ import annotations
from .base import HostService

class DeliveryRoutesService(HostService):

    def _post_materialize(self, payload):
        Materializer = self.dependencies.Materializer
        Path = self.dependencies.Path
        ProtocolError = self.dependencies.ProtocolError
        VALID_DISCLOSURES = self.dependencies.VALID_DISCLOSURES
        VALID_TEMPLATES = self.dependencies.VALID_TEMPLATES
        if not isinstance(payload, dict):
            raise ProtocolError('请求必须为 JSON 对象')
        manuscript = payload.get('manuscript')
        if not isinstance(manuscript, dict):
            raise ProtocolError('manuscript 必须是对象')
        template = payload.get('template', 'md')
        disclosure = payload.get('disclosure', 'nature')
        if template not in VALID_TEMPLATES:
            raise ProtocolError('未知稿件模板')
        if disclosure not in VALID_DISCLOSURES:
            raise ProtocolError('未知披露声明口径')
        with self.server.materialize_lock:
            out = self.server.workspace.ensure_deliverables_dir()
            from ..research.manuscript_gate import load_known_manuscript_ids
            run_dirs = tuple(getattr(self.server, 'research_runs', ()) or ())
            roots = (self.server.project, *run_dirs)
            if getattr(self.server, 'campaign_dir', None):
                roots = (*roots, self.server.campaign_dir)
            known_ids = load_known_manuscript_ids(*roots, run_dirs=run_dirs)
            try:
                res = Materializer(self.server.project).materialize(
                    manuscript, out, template, disclosure, known_ids=known_ids)
            except ProtocolError as error:
                status = getattr(error, 'gate_status', None) or 'gate_failed'
                self.send(409, {
                    'status': status,
                    'error': str(error),
                    'writing_gate': status,
                    'problems': getattr(error, 'details', None) or [],
                })
                return
            fp = Path(res['manuscript'])
            with self.server.ws_lock:
                if self.server.workspace.is_empty():
                    self.server.workspace.seed_if_empty(self.server.workspace.project_spec(), self.server.workspace.current_phase())
                data = self.server.workspace.read()
                item = self.server.workspace.register_deliverable(data, fp.name, template, disclosure, fp.stat().st_size)
                self.server.workspace.save(data)
        self.send(200, {
            'status': 'materialized',
            'writing_gate': res.get('writing_gate', 'passed'),
            'item': {
                'id': item['id'], 'filename': item['filename'],
                'template': item['template'], 'disclosure': item['disclosure'],
                'created': item['created'], 'bytes': item['bytes'],
            },
        })
