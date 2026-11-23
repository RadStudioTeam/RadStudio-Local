"""Binary Brier score, mean((p - y)**2); no fitting or threshold selection."""
import math
from radstudio.operator import Operator, operator


@operator('ResearchBrierScore', version='1.0.0')
class BrierScore(Operator):
    def run(self, ctx):
        if ctx.parameters:
            raise ValueError('ResearchBrierScore does not accept parameters')
        table = ctx.input_json('predictions')
        rows = table.get('records')
        if not isinstance(rows, list) or not rows:
            raise ValueError('predictions.records must be a nonempty list')
        seen, errors = set(), []
        for row in rows:
            identity = row.get('imageId')
            if not isinstance(identity, str) or not identity or identity in seen:
                raise ValueError('imageId must be nonempty and unique')
            seen.add(identity)
            y, p = row.get('label'), row.get('probability')
            if type(y) not in (int, float) or y not in (0, 1):
                raise ValueError('label must be numeric 0 or 1')
            if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError('probability must be finite and in [0, 1]')
            errors.append((p - y) ** 2)
        return {'metrics': ctx.output_json('brier.json', {
            'brier': math.fsum(errors) / len(errors), 'sampleCount': len(rows),
            'weighting': 'per_image', 'positiveLabel': 1,
            'evaluation': table.get('evaluation', 'unspecified'),
            'independenceVerified': False,
        }, 'metrics')}
