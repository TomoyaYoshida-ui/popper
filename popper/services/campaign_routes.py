"""Campaign Routes responsibility service."""
from __future__ import annotations
from .base import HostService

class CampaignRoutesService(HostService):

    def _post_adjudicate(self, payload):
        """用户对拒稿风险的裁定（放行/驳回），留痕入事件溯源链。"""
        Experiment = self.dependencies.Experiment
        ProtocolError = self.dependencies.ProtocolError
        if not isinstance(payload, dict):
            raise ProtocolError('请求必须为 JSON 对象')
        for key in ('risk_id', 'verdict', 'reason'):
            if not isinstance(payload.get(key), str):
                raise ProtocolError(f'缺少裁定字段: {key}')
        exp = Experiment(self.server.project)
        try:
            result = exp.adjudicate(payload['risk_id'], payload['verdict'], payload['reason'])
        finally:
            exp.close()
        self.send(200, result)

    def _post_campaign(self, payload):
        """工作台交互审批（批准/驳回）。批准恢复执行需要 --trusted-local 边界。"""
        ProtocolError = self.dependencies.ProtocolError
        VALID_DISCLOSURES = self.dependencies.VALID_DISCLOSURES
        VALID_TEMPLATES = self.dependencies.VALID_TEMPLATES
        if not isinstance(payload, dict) or not isinstance(payload.get('action'), str):
            raise ProtocolError('请求必须含 action 字符串')
        action = payload['action']
        if action not in {'approve', 'reject'}:
            raise ProtocolError('campaign 动作必须为 approve 或 reject')
        if not self.server.campaign_dir:
            raise ProtocolError('未配置 --campaign 运行目录，无法审批')
        if action == 'approve' and (not self.server.trusted_local):
            raise ProtocolError('批准并恢复需以 --trusted-local 启动工作台（将执行已信任代码）')
        reason = payload.get('reason')
        if reason is not None and (not isinstance(reason, str)):
            raise ProtocolError('reason 必须为字符串')
        materialize = payload.get('materialize')
        if materialize is not None:
            if not isinstance(materialize, dict):
                raise ProtocolError('materialize 必须为对象')
            if not isinstance(materialize.get('manuscript'), dict):
                raise ProtocolError('materialize.manuscript 必须是对象')
            if materialize.get('template', 'md') not in VALID_TEMPLATES:
                raise ProtocolError('未知稿件模板')
            if materialize.get('disclosure', 'nature') not in VALID_DISCLOSURES:
                raise ProtocolError('未知披露声明口径')
        decision = 'approved' if action == 'approve' else 'rejected'
        result = self.server.approve_campaign(decision, reason or '', materialize)
        self.send(200, {'ok': True, **result})
