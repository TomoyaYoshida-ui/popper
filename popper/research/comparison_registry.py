"""Registered comparison engines shared by execution, adapters and replay."""
from __future__ import annotations

from ..core import ProtocolError


def supported_schemas():
    return frozenset({'figs-grouped-ci/v1', 'paired-unit-ci/v1', 'paired-unit-ci/v2', 'prediction-packet-ci/v1'})


def supported_metrics(schema):
    """A declaration gate; successful execution still verifies full artifacts."""
    engine(schema)  # reject unknown registrations
    names = ('mae', 'mse') if schema == 'paired-unit-ci/v2' else ('mae',)
    return tuple({'name': name, 'direction': 'min'} for name in names)


def engine(schema):
    if schema == 'prediction-packet-ci/v1':
        from . import prediction_comparison
        return prediction_comparison
    if schema == 'paired-unit-ci/v2':
        from . import unit_comparison
        return unit_comparison
    if schema == 'paired-unit-ci/v1':
        from . import paired_comparison
        return paired_comparison
    if schema in {'figs-grouped-ci/v1', None}:  # old FIGS descriptors had no tag
        from . import grouped_comparison
        return grouped_comparison
    raise ProtocolError('Unregistered comparison schema')


def compare(controller, baseline, observation, protocol):
    schema = protocol.get('schema')
    module = engine(schema)
    hid = observation['hypothesis_id']
    ids = ([hid, protocol['hypothesis_contrasts'][hid]['alternative_id']]
           if schema in {'paired-unit-ci/v1', 'paired-unit-ci/v2'} else list(protocol['hypothesis_predictions']))
    return ids, module.compare_observations(controller.project, controller.run_dir, baseline, observation)


def verify_artifact(descriptor, observations):
    return engine(descriptor.get('comparison_schema')).verify_artifact(descriptor, observations)
