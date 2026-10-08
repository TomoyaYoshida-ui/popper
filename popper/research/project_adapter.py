"""Public project declarations shared by planning and controlled execution.

This adapter does not score predictions or infer environment semantics.  Scope
rules come from the frozen experiment, while judgments remain evaluator facts.
"""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

from ..core import ProtocolError, digest, file_hash, read_json


def _public(value):
    """Reject private declarations instead of silently exposing them in prompts."""
    if isinstance(value, dict):
        forbidden = {'api_key', 'password', 'secret', 'oracle', 'oracle_private',
                     'test_labels', 'hidden_truth', 'ground_truth', 'generator'}
        if forbidden.intersection(str(k).lower() for k in value):
            raise ProtocolError('Public project declaration contains a private field')
        return {k: _public(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(v) for v in value]
    if isinstance(value, str) and (re.search(r'[A-Za-z]:[\\/]', value)
                                   or value.startswith(('\\\\', '/'))):
        raise ProtocolError('Public project declaration contains a private absolute path')
    return copy.deepcopy(value)


class RegisteredProjectAdapter:
    """Read an existing controller's frozen, public project contract.

    ``scope_schema`` is optional for historical projects.  When present it must
    describe every available scope with scope_id, label, role, split and
    applicability. Historical labels expose registered selectors without
    claiming to know the scientific meaning of those selectors.
    """

    def __init__(self, controller):
        self.controller = controller
        self.project = Path(controller.project)
        self.spec = read_json(self.project / 'experiment.json')
        registered = 'judgment-protocol.json' in self.spec.get('provenance_files', [])
        self.protocol = read_json(self.project / 'judgment-protocol.json') if registered else None
        self._scopes = None

    def comparison_schema(self):
        return self.protocol.get('schema') if self.protocol else None

    def _scope_declarations(self):
        spec, protocol = self.spec, self.protocol or {}
        paired = 'primary_dev_prefix' in protocol
        slices = spec.get('analysis_slices', [])
        if not isinstance(slices, list):
            raise ProtocolError('Project analysis_slices must be a list')
        available = {}
        for item in slices:
            sid, rule = item.get('slice_id'), item.get('rule')
            if not isinstance(sid, str) or not sid or sid in available or not isinstance(rule, dict):
                raise ProtocolError('Duplicate or missing registered scope mapping')
            available[sid] = rule
        prefixes = protocol.get('analysis_scopes', {}) if paired else {}
        if not isinstance(prefixes, dict):
            raise ProtocolError('Comparison analysis_scopes must be a mapping')
        primary = 'dev'
        dev_slices = available if 'dev' in spec.get('analysis_slice_splits', ['dev', 'test']) else {}
        expected = {'dev:slice:' + sid for sid in dev_slices}
        if paired:
            if set(prefixes) != expected:
                raise ProtocolError('Comparison scope mapping differs from registered development slices')
            for sid, rule in dev_slices.items():
                prefix = prefixes['dev:slice:' + sid]
                if (rule.get('kind') != 'id_prefix' or not isinstance(prefix, str)
                        or not prefix or prefix != rule.get('value')):
                    raise ProtocolError('Comparison scope selector contradicts experiment selector')
            if len(set(prefixes.values())) != len(prefixes):
                raise ProtocolError('Duplicate comparison scope selector')
            primary_prefix = protocol.get('primary_dev_prefix')
            if not isinstance(primary_prefix, str):
                raise ProtocolError('Comparison primary_dev_prefix must be explicitly declared')
            if primary_prefix:
                matches = [scope for scope, prefix in prefixes.items() if prefix == primary_prefix]
                if len(matches) != 1:
                    raise ProtocolError('Primary comparison scope does not have a unique registered mapping')
                primary = matches[0]
        self._primary = primary
        self._comparison_scopes = ([primary] if primary == 'dev' else []) + sorted(expected)
        scopes = {
            'dev': {'scope_id': 'dev', 'label': 'registered development split',
                    'role': 'primary' if primary == 'dev' else 'diagnostic', 'split': 'dev',
                    'applicability': 'Frozen development split; no mechanism claim', 'legacy_derived': True},
            'confirmation': {'scope_id': 'confirmation', 'label': 'registered confirmation split',
                    'role': 'primary', 'split': 'confirmation',
                    'applicability': 'Frozen confirmation split; no mechanism claim', 'legacy_derived': True}}
        for sid, rule in dev_slices.items():
            scope = 'dev:slice:' + sid
            label = rule.get('value') if rule.get('kind') == 'id_prefix' else str(rule)
            scopes[scope] = {'scope_id': scope, 'label': label,
                'role': 'primary' if scope == primary else 'diagnostic', 'split': 'dev',
                'applicability': 'Registered selector: ' + str(rule), 'legacy_derived': True}
        declared = protocol.get('scope_schema', spec.get('scope_schema'))
        if declared is not None:
            if not isinstance(declared, list):
                raise ProtocolError('scope_schema must be a list of public scope declarations')
            seen = set()
            for item in declared:
                required = {'scope_id', 'label', 'role', 'split', 'applicability'}
                if (not isinstance(item, dict) or not required <= item.keys()
                        or item['scope_id'] not in scopes or item['scope_id'] in seen
                        or any(not isinstance(item[k], str) or not item[k].strip() for k in required)):
                    raise ProtocolError('Missing, duplicate or unknown scope_schema declaration')
                scope = item['scope_id']; expected_scope = scopes[scope]
                if item['role'] != expected_scope['role'] or item['split'] != expected_scope['split']:
                    raise ProtocolError('Declared primary role or split contradicts frozen scope selector')
                scopes[scope] = {**{k: _public(item[k]) for k in required}, 'legacy_derived': False}
                seen.add(scope)
            if seen != set(scopes):
                raise ProtocolError('scope_schema must describe every registered public scope')
        return scopes

    def preflight(self):
        """Validate binding and declared semantics before any model request."""
        self.controller._verify_inputs()
        schema = self.comparison_schema()
        if schema is not None:
            from .comparison_registry import supported_schemas
            if schema not in supported_schemas():
                raise ProtocolError('Unregistered project comparison schema')
            if schema == 'paired-unit-ci/v2':
                from .comparison_registry import engine
                engine(schema).validate_protocol(self.protocol)
        metric = self.spec.get('metric')
        if (not isinstance(metric, dict) or not isinstance(metric.get('name'), str)
                or metric.get('direction') not in {'min', 'max'}
                or metric != self.controller.manifest.get('metric')):
            raise ProtocolError('Project metric differs from frozen research metric')
        if self.protocol and self.protocol.get('metric', metric) != metric:
            raise ProtocolError('Comparison metric contradicts registered project metric')
        if schema is not None:
            from .comparison_registry import supported_metrics
            if metric not in supported_metrics(schema):
                raise ProtocolError('Registered comparison does not support this project metric')
        seeds = self.spec.get('seeds', [])
        if self.protocol and 'model_seed' in self.protocol and seeds != [self.protocol['model_seed']]:
            raise ProtocolError('Comparison training seed differs from registered project seed')
        scopes = self._scope_declarations()
        features = self._feature_declarations()
        if not isinstance(self._selection_metric(), str) or not self._selection_metric().strip():
            raise ProtocolError('Independent selection metric must be a public evidence field name')
        if self.protocol and 'analysis_unit' in self.protocol:
            unit = self.protocol['analysis_unit']
            if (not isinstance(unit, dict) or not isinstance(unit.get('kind'), str)
                    or not isinstance(unit.get('definition'), str) or not unit['definition'].strip()
                    or unit.get('kind') == 'train_seed'):
                raise ProtocolError('Comparison requires a declared independent analysis unit, not training seed')
            _public(unit)
        policy = (self.protocol or {}).get('confirmation_policy')
        if policy is not None:
            if (not isinstance(policy, dict) or policy.get('schema') != 'selected_by_registered_dev_metric/v1'
                    or policy.get('direction') != metric['direction']
                    or policy.get('tie_break') != 'hypothesis_id'
                    or not isinstance(policy.get('selection_metric'), str)):
                raise ProtocolError('Confirmation selection policy contradicts project declarations')
            if policy['selection_metric'] != self._selection_metric():
                raise ProtocolError('Confirmation selection metric contradicts independent evidence field')
        _public(self.spec['objective']); _public(self.spec['baseline'])
        for candidate in self.candidate_space():
            _public(candidate)
        self._scopes, self._features = scopes, features
        return {'ok': True, 'comparison_schema': schema,
                'legacy_derived': any(s['legacy_derived'] for s in self._scopes.values())}

    def require_execution_support(self):
        """Check execution dependencies before spending an experiment or request."""
        self.preflight()
        schema = self.comparison_schema()
        if schema:
            from ..capabilities import extra_available, install_hint
            from .comparison_registry import engine
            extra = 'analysis' if schema == 'paired-unit-ci/v2' else 'figs'
            if not extra_available(extra):
                raise ProtocolError('Registered comparison requires ' + extra + ': ' + install_hint(extra))
            validate = getattr(engine(schema), 'validate_protocol', None)
            if validate:
                validate(self.protocol)

    def _feature_declarations(self):
        declared = (self.protocol or {}).get('feature_schema', self.spec.get('feature_schema'))
        if declared is None:
            return []
        if not isinstance(declared, list):
            raise ProtocolError('feature_schema must be a list of public feature names')
        seen = set(); result = []
        for feature in declared:
            if (not isinstance(feature, dict) or type(feature.get('index')) is not int
                    or feature['index'] < 0 or feature['index'] in seen
                    or not isinstance(feature.get('name'), str) or not feature['name'].strip()):
                raise ProtocolError('Duplicate or invalid public feature declaration')
            seen.add(feature['index'])
            result.append({'index': feature['index'], 'name': _public(feature['name'])})
        used = set()
        for config in [self.spec['baseline'], *self.spec['candidates']]:
            features = config.get('features', [])
            if (not isinstance(features, list) or any(type(i) is not int or i < 0 for i in features)
                    or len(features) != len(set(features))):
                raise ProtocolError('Candidate feature indices must be unique nonnegative integers')
            used.update(features)
        if not used <= seen:
            raise ProtocolError('Public feature_schema does not cover registered candidate features')
        return sorted(result, key=lambda f: f['index'])

    def _ensure(self):
        if self._scopes is None:
            self.preflight()

    def primary_scope_id(self, split='dev'):
        self._ensure()
        primary = [s['scope_id'] for s in self._scopes.values() if s['split'] == split and s['role'] == 'primary']
        if len(primary) != 1:
            raise ProtocolError('Split does not have exactly one declared primary scope')
        return primary[0]

    def comparison_scope_ids(self, split='dev'):
        self._ensure()
        return list(self._comparison_scopes) if split == 'dev' else [self.primary_scope_id(split)]

    def scope_for_observation(self, observation):
        self._ensure()
        scope = observation.get('scope') if isinstance(observation, dict) else observation
        if scope not in self._scopes:
            raise ProtocolError('Observation scope is not registered by this project')
        return copy.deepcopy(self._scopes[scope])

    def candidate_space(self):
        candidates = []
        for hid in self.controller.manifest['candidate_hypothesis_ids']:
            hypothesis = self.controller.store.get('hypothesis', hid)
            candidates.append({'hypothesis_id': hid, 'version': hypothesis['version'],
                               'config': _public(self.controller.manifest['configs'][hid])})
        return candidates

    def describe(self):
        self._ensure(); protocol = self.protocol or {}; metric = self.spec['metric']
        analysis_unit = protocol.get('analysis_unit')
        if analysis_unit is None:
            analysis_unit = ({'kind': 'group', 'definition': 'Frozen grouping map',
                              'training_seeds_are_analysis_units': False, 'legacy_derived': True}
                             if self.protocol else {'kind': self.controller.manifest.get('repeat_unit', 'train_seed'),
                                'definition': 'Registered evaluator repeat unit', 'legacy_derived': True})
        effect = protocol.get('effect', 'relative_mae_improvement' if self.comparison_schema() == 'figs-grouped-ci/v1'
                              else 'direction_normalized_absolute_metric_difference')
        result = {'schema': 'registered-project-adapter/v1', 'objective': self.spec['objective'],
            'comparison_schema': self.comparison_schema(), 'artifact_validator': protocol.get('artifact_validator'),
            'metric': metric, 'effect_definition': effect, 'analysis_unit': analysis_unit,
            'training_seeds': self.spec.get('seeds', []), 'baseline': self.spec['baseline'],
            'scopes': list(self._scopes.values()), 'primary_scope_id': self.primary_scope_id(),
            'selection_metric': self._selection_metric(),
            'statistical_rule': {k: protocol[k] for k in ('confidence', 'bootstrap_samples', 'minimum_units', 'minimum_groups') if k in protocol},
            'candidate_space': self.candidate_space(), 'feature_schema': self._features,
            'prediction_regions': protocol.get('hypothesis_predictions', {}),
            'stopping_conditions': {'max_candidate_executions': self.spec['budget'],
                'research_budget_cap': self.controller.manifest.get('budget_cap'),
                'timeout_seconds': self.spec.get('timeout_seconds'),
                'confirmation_policy': 'Freeze selection before one protected confirmation',
                'meaningful_effect_threshold': self.spec['min_improvement']}}
        return _public(result)

    def _selection_metric(self):
        metric = self.spec['metric']; protocol = self.protocol or {}
        return protocol.get('selection_metric',
            'candidate_group_mae' if self.comparison_schema() == 'figs-grouped-ci/v1'
            else 'candidate_' + metric['name'] if self.protocol else metric['name'])

    def _registered_executions(self):
        scope = self.primary_scope_id()
        return [{'hypothesis_id': observation['hypothesis_id'], 'observation_id': observation['observation_id']}
                for observation in self.controller.store.list('observation')
                if observation['hypothesis_id'] in self.controller.manifest['candidate_hypothesis_ids']
                and observation['scope'] == scope]

    def confirmation_selection_receipt(self):
        """Recompute the selection from all independently validated registered facts."""
        selected = self.select_finalist()
        return selected['selection_receipt'] if selected else None

    def select_finalist(self, executions=None):
        """Select using replay-verified primary evidence, never model annotations."""
        self.preflight()
        scope = self.primary_scope_id(); candidates = []
        metric_key = self._selection_metric()
        annotations = {e.get('observation_id'): e for e in (executions or [])}
        # Driver reports can be missing after a crash.  Only successful stored
        # Runs decide eligibility; reporting fields cannot remove valid facts.
        executions = [{**annotations.get(e['observation_id'], {}), **e}
                      for e in self._registered_executions()]
        for execution in executions:
            hid = execution.get('hypothesis_id')
            if hid not in self.controller.manifest['candidate_hypothesis_ids']:
                continue
            hypothesis = self.controller.store.get('hypothesis', hid)
            observation = self.controller.store.get('observation', execution.get('observation_id', ''))
            if (not observation or observation['hypothesis_id'] != hid or observation['scope'] != scope
                    or observation['hypothesis_version'] != hypothesis['version']):
                continue
            run = self.controller.store.get('run', observation['run_id'])
            if not run or run['status'] != 'succeeded':
                continue
            value = observation['value']; judgment_ids = []
            if self.protocol:
                judgments = [j for j in self.controller.store.list('judgment')
                    if j['hypothesis_id'] == hid and j['hypothesis_version'] == hypothesis['version']
                    and j['scope'] == scope and observation['observation_id'] in j['observation_refs']
                    and j.get('comparison_artifact') and j.get('evidence')]
                if not judgments:
                    continue
                values = [j['evidence'].get(metric_key) for j in judgments]
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    raise ProtocolError('Independent primary selection metric is not finite')
                if len(set(values)) != 1:
                    raise ProtocolError('Independent primary evidence has contradictory selection metrics')
                value = values[0]; judgment_ids = [j['judgment_id'] for j in judgments]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ProtocolError('Independent primary selection metric is not finite')
            candidates.append({**execution, 'dev_value': value, 'scope': scope,
                'selection_metric': metric_key, 'judgment_ids': judgment_ids,
                'hypothesis_version': hypothesis['version'],
                'config': copy.deepcopy(self.controller.manifest['configs'][hid])})
        if not candidates:
            return None
        direction = 1 if self.spec['metric']['direction'] == 'min' else -1
        candidates.sort(key=lambda c: (direction*c['dev_value'], c['hypothesis_id'], c['observation_id']))
        best = candidates[0]
        study_id = self.controller.manifest['study_id']
        receipt = {'schema': 'registered-dev-selection/v1', 'study_id': study_id,
            'protocol_sha256': file_hash(self.project/'judgment-protocol.json') if self.protocol else None,
            'state_version': self.controller.store.get('study', study_id)['state_version'],
            'hypothesis_id': best['hypothesis_id'], 'hypothesis_version': best['hypothesis_version'],
            'observation_id': best['observation_id'], 'scope': scope,
            'selection_metric': metric_key, 'value': best['dev_value'],
            'candidate_evidence': [{'hypothesis_id': candidate['hypothesis_id'],
                'hypothesis_version': candidate['hypothesis_version'], 'observation_id': candidate['observation_id'],
                'value': candidate['dev_value']} for candidate in candidates]}
        receipt['receipt_sha256'] = digest(receipt)
        return {**best, 'selection_receipt': receipt}
