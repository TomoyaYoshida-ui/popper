"""New evaluator identity for genuine vector-valued regression inputs."""
import statistics
from .aligned import AlignedPredictionPack
from .protocol import MetricSpec, register
from ..core import ProtocolError, number


class VectorMaeRegression(AlignedPredictionPack):
    pack_id = 'vector-regression-mae'
    schema_version = '1.0'
    evaluator_id = 'vector-mae-v1'
    input_fields = ('features',)
    vector_field = 'features'
    target_field = 'y'
    _metrics = (MetricSpec(name='mae', direction='min', unit='absolute_error',
                          description='id-aligned vector regression MAE', value_domain=(0, None)),)
    _entry = {'id': evaluator_id, 'metric': {'name': 'mae', 'direction': 'min'},
              'definition': 'id-aligned absolute finite prediction losses; registered independent comparator defines group aggregation',
              'dataset': 'rows[id,features,y]; fixed finite feature vectors'}

    def validate_input_fields(self, row):
        if not isinstance(row['features'], list) or not row['features'] or not all(map(number, row['features'])):
            raise ProtocolError('Regression features must be a nonempty finite vector')

    def validate_target(self, value):
        if not number(value):
            raise ProtocolError('Regression target must be finite')

    validate_prediction = validate_target

    def score_values(self, rows, values, metric_id):
        return statistics.mean(abs(values[row['id']] - row['y']) for row in rows)


PACK = register(VectorMaeRegression())
