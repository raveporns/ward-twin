from datetime import datetime, timezone

from app.ml.readiness import check_readiness
from app.ml.feature_engineering import build_vital_features
from app.ml.patient_features import build_patient_features
from app.ml.model_loader import predict_risk


def process_prediction(db, encounter_id: str, as_of=None):
    as_of = as_of or datetime.now(timezone.utc)

    readiness = check_readiness(db, encounter_id, as_of)

    if not readiness["ready"]:
        return {
            "encounter_id": encounter_id,
            "status": "unavailable",
            "probability": None,
            "unavailable_reason": ",".join(readiness["reasons"]),
            "features": None,
            "as_of": as_of,
            "policy_version": readiness["policy_version"],
        }

    features = {
        **build_vital_features(db, encounter_id, as_of),
        **build_patient_features(db, encounter_id, as_of),
    }

    result = predict_risk(features)

    return {
        "encounter_id": encounter_id,
        "status": "ready",
        "probability": result["probability"],
        "unavailable_reason": None,
        "features": result["features"],
        "as_of": as_of,
        "policy_version": readiness["policy_version"],
    }
