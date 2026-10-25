import json
from functools import lru_cache
from pathlib import Path

import pandas as pd
import xgboost as xgb


BASE_DIR = Path(__file__).resolve().parents[2]
MODEL_DIR = BASE_DIR / "models"


@lru_cache(maxsize=1)
def load_model():
    model_path = MODEL_DIR / "ward_twin_mock_xgb.json"
    metadata_path = MODEL_DIR / "ward_twin_mock_xgb_metadata.json"

    with metadata_path.open(encoding="utf-8") as f:
        metadata = json.load(f)

    model = xgb.XGBClassifier()
    model.load_model(str(model_path))

    features = metadata["feature_columns"]

    if model.get_booster().feature_names != features:
        raise ValueError("Model features do not match metadata")

    return model, metadata


def predict_risk(feature_values: dict):
    model, metadata = load_model()

    columns = metadata["feature_columns"]
    medians = metadata["imputation_medians"]

    row = {}

    for feature in columns:
        value = feature_values.get(feature)

        if value is None:
            value = medians[feature]

        row[feature] = float(value)

    X = pd.DataFrame([row], columns=columns)

    probability = float(model.predict_proba(X)[0, 1])

    return {
        "probability": probability,
        "threshold": float(metadata["threshold_demo"]),
        "above_threshold": probability >= float(metadata["threshold_demo"]),
        "features": row,
    }
