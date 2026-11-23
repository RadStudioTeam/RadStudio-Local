import json
import math

from radstudio.runner import evaluate


def test_roc_sentinel_is_json_null_and_youden_selects_finite_threshold():
    predictions = {
        "evaluation": "out_of_fold",
        "records": [
            {"label": label, "probability": probability}
            for label, probability in [(0, 0.9), (1, 0.1), (0, 0.8), (1, 0.2)]
        ],
    }
    for mode in ("fixed", "youden"):
        result = evaluate(predictions, threshold_mode=mode, bootstrap=0)
        json.dumps(result, allow_nan=False)
        assert result["roc"]["thresholds"][0] is None
        assert len(result["roc"]["thresholds"]) == len(result["roc"]["fpr"])
        assert result["roc"]["auc"] == 0.0
        assert math.isfinite(result["metrics"]["threshold"])
